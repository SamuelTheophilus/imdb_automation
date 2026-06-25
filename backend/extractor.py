# backend/extractor.py

import asyncio
import base64
import json
import os
import re

from io import BytesIO
from json import JSONDecodeError
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from PIL import Image, ImageEnhance

from backend.barcode import decode_barcode, ean_checksum_valid
from backend.image_aggregation import group_by_tag_similarity
from backend.schema import IMDBRecordWithConfidence
from backend.utils import (
    ANTHROPIC_MODEL,
    GEMINI_MODEL,
    MODEL as OLLAMA_MODEL,
    OPENAI_MODEL,
    VLMCallParams,
    VLMImageData,
    vlm_call_w_anthropic,
    vlm_call_w_gemini,
    vlm_call_w_ollama,
    vlm_call_w_openai,
)

# ── Prompt templates ────────────────────────────────────────────────────────
jinja_templates_folder = f"{Path(__file__).parent.parent}/core/prompts"
env = Environment(loader=FileSystemLoader(jinja_templates_folder))

SYSTEM_PROMPT    = env.get_template("vlm_system_prompt.j2").render()
EXTRACTION_PROMPT = env.get_template("vlm_extraction_prompt.j2").render()

# Prompts for the multi-view / video pipeline (one product, many frames).
# These are kept separate so the standard single-image pipeline is unaffected.
VIDEO_SYSTEM_PROMPT    = env.get_template("vlm_video_system_prompt.j2").render()
VIDEO_EXTRACTION_PROMPT = env.get_template("vlm_video_extraction_prompt.j2").render()

# ── Constants ────────────────────────────────────────────────────────────────
_MAX_ATTEMPTS = 3

# llama3.2-vision tiles images dynamically. Capping at 512px keeps the visual
# token count low without losing label legibility.
_MAX_IMAGE_SIDE = 2048

# Which backend to use. Set VLM_BACKEND to one of: ollama, openai, gemini.
# Falls back to USE_LOCAL_MODEL for backward compatibility.
def _resolve_backend() -> str:
    explicit = os.getenv("VLM_BACKEND", "").strip().lower()
    if explicit in ("ollama", "openai", "gemini", "anthropic"):
        return explicit
    return "ollama" if os.getenv("USE_LOCAL_MODEL", "YES").strip().upper() == "YES" else "openai"

_active_backend: str = _resolve_backend()
_active_model_id: str = ""  # set after MODEL_OPTIONS is defined below

# Ollama supports only one image per request. Cloud backends (OpenAI, Gemini)
# accept multiple images per call — batching reduces round-trips and lets the
# model cross-reference all faces of a product in one pass.
_BATCH_SIZE = 1 if _active_backend == "ollama" else int(os.getenv("VLM_BATCH_SIZE", "8"))

# Semaphore limits simultaneous requests. For Ollama this matches
# OLLAMA_NUM_PARALLEL; for cloud APIs it caps parallel calls.
_CONCURRENCY = int(os.getenv("OLLAMA_CONCURRENCY", "2"))
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_CONCURRENCY)
    return _semaphore


# ── Model selection & pricing ─────────────────────────────────────────────────

# Display name -> (backend, model_id). Model IDs come from env vars so the
# operator can swap models without changing code.
# Batch variants use the same model but routed through the provider's Batch API
# (async, lower cost). Marked here for display; batch routing is handled by
# the bulk batch processor, not the quick upload pipeline.
MODEL_OPTIONS: dict[str, tuple[str, str]] = {
    "Claude Sonnet 4.6 (Recommended)": ("anthropic", ANTHROPIC_MODEL),
    "GPT-5.5":                          ("openai",    OPENAI_MODEL),
    "Gemini 2.5 Flash":                 ("gemini",    GEMINI_MODEL),
}

# Cost per 1 million tokens (input_rate, output_rate) in USD.
PRICING_TABLE: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-haiku-4-5-20251001":  (0.80,   4.00),
    OPENAI_MODEL:                 (5.00,  30.00),   # GPT-5.5
    GEMINI_MODEL:                 (0.30,   2.50),   # Gemini 2.5 Flash
}


def _default_model_for(backend: str) -> str:
    if backend == "anthropic":
        return ANTHROPIC_MODEL
    if backend == "openai":
        return OPENAI_MODEL
    if backend == "gemini":
        return GEMINI_MODEL
    return OLLAMA_MODEL


def get_default_display_name() -> str:
    """Return the display name matching the env-var-configured backend."""
    for name, (backend, model_id) in MODEL_OPTIONS.items():
        if backend == _active_backend and model_id == _active_model_id:
            return name
    return next(iter(MODEL_OPTIONS))


def calculate_cost(input_tokens: int, output_tokens: int, model_id: str) -> float:
    """Return the USD cost for the given token counts and model."""
    rates = PRICING_TABLE.get(model_id)
    if not rates:
        return 0.0
    in_rate, out_rate = rates
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


# Initialise _active_model_id after MODEL_OPTIONS exists.
_active_model_id = _default_model_for(_active_backend)


async def _vlm_call(params: VLMCallParams, backend: str, model_id: str):
    """Route the VLM call to the specified backend and model."""
    params = params.model_copy(update={"model_override": model_id or None})
    if backend == "anthropic":
        return await vlm_call_w_anthropic(params)
    if backend == "gemini":
        return await vlm_call_w_gemini(params)
    if backend == "openai":
        return await vlm_call_w_openai(params)
    return await vlm_call_w_ollama(params)


# ── JSON schema for structured VLM output ───────────────────────────────────
# Ollama uses grammar-based sampling to guarantee the response matches this
# schema. image_path is intentionally omitted — long paths cause soft-hyphen
# padding corruption in the grammar sampler; we inject the path ourselves.
_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "tag_text":             {"type": "string"},
        "category_type":        {"type": "string"},
        "manufacturer":         {"type": "string"},
        "brand":                {"type": "string"},
        "product_name":         {"type": "string"},
        "weight":               {"type": "string"},
        "unit":                 {"type": "string"},
        "packaging_type":       {"type": "string"},
        "country_of_origin":    {"type": "string"},
        "promotional_messages": {"type": "string"},
        "variant":              {"type": "string"},
        "fragrance_flavor":     {"type": "string"},
        "addons":               {"type": "string"},
        "tagline":              {"type": "string"},
    },
    "required": ["tag_text"],
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": _ITEM_SCHEMA}},
    "required": ["items"],
}


# ── Image encoding ───────────────────────────────────────────────────────────

def _encode_image(image_path: str | Path) -> str:
    """Resize, enhance, and base64-encode an image for the VLM."""
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    if max(w, h) > _MAX_IMAGE_SIDE:
        scale = _MAX_IMAGE_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    # Modest sharpness + contrast boost to make label text more legible
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    img = ImageEnhance.Contrast(img).enhance(1.3)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ── JSON parsing helpers ─────────────────────────────────────────────────────

def _extract_json_payload(raw: str) -> dict:
    """Pull the first JSON object from a raw string, stripping markdown fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    decoder = json.JSONDecoder()
    for start in [i for i, ch in enumerate(raw) if ch == "{"]:
        try:
            data, _ = decoder.raw_decode(raw[start:])
        except JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    raise ValueError(f"Extractor did not return valid JSON: {raw[:500]}")


def _extract_json_array(raw: str) -> list[dict]:
    """Pull the first JSON array (or items-wrapped object) from a raw string."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    decoder = json.JSONDecoder()

    # Try { "items": [...] } wrapper first
    for start in [i for i, ch in enumerate(raw) if ch == "{"]:
        try:
            data, _ = decoder.raw_decode(raw[start:])
        except JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [item for item in data["items"] if isinstance(item, dict)]

    # Try bare array
    for start in [i for i, ch in enumerate(raw) if ch == "["]:
        try:
            data, _ = decoder.raw_decode(raw[start:])
        except JSONDecodeError:
            continue
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

    raise ValueError(f"Extractor did not return a valid JSON array: {raw[:500]}")


# ── Item normalisation ───────────────────────────────────────────────────────

_EMPTY = {"", "null", "none", "n/a", "unknown"}
# Strips soft-hyphen (\xad) padding that Ollama's grammar sampler occasionally
# injects into string fields when the token budget is tight.
_SOFT_HYPHEN = "\xad"


def _normalize_item(item: dict) -> dict:
    """Clean a single extracted item dict from the VLM response.

    - Removes soft-hyphen padding injected by the grammar sampler.
    - Converts empty/null placeholder strings to None.
    - Uppercases the weight field (ground truth format is e.g. "100G", "1.5 KG").
    """
    result = {}
    for k, v in item.items():
        if v is None:
            result[k] = None
            continue
        v = str(v).replace(_SOFT_HYPHEN, "").strip()
        if v.lower() in _EMPTY:
            result[k] = None
        elif k == "weight":
            result[k] = v.upper()
        else:
            result[k] = v
    return result


# ── Field aggregation ────────────────────────────────────────────────────────
# Each product is photographed from 3–5 angles. Aggregation merges the per-face
# extractions into one canonical record.

def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return "; ".join(str(item) for item in value if item)
    return str(value)


def _first_text(items: list[dict], key: str) -> str | None:
    """Return the first non-empty value for key across all items."""
    for item in items:
        value = _as_text(item.get(key))
        if value:
            return value
    return None


def _longest_text(items: list[dict], key: str) -> str | None:
    """Return the longest non-empty value for key (favours more complete text)."""
    values = [_as_text(item.get(key)) for item in items]
    values = [v for v in values if v]
    return max(values, key=len, default=None)


def _fuzzy_key(v: str) -> str:
    """Normalise for comparison: uppercase and strip punctuation."""
    return re.sub(r"[^A-Z0-9 ]", "", v.upper()).strip()


def _most_common_text(items: list[dict], key: str, strict: bool = False) -> str | None:
    """Return the most frequent value for key using fuzzy-normalised grouping.

    Fuzzy grouping means "Mummy's Kitchen" and "MUMMYS KITCHEN" are treated as
    the same value and both contribute to the same count.

    strict=True: return None when no value appears more than once. Use for
    optional fields (variant, promo, tagline) where a single hallucinated value
    from one face should not win.
    """
    from collections import Counter
    values = [_as_text(item.get(key)) for item in items]
    values = [v for v in values if v]
    if not values:
        return None

    # Map each value to a canonical fuzzy key, keep the longest representative
    groups: dict[str, str] = {}
    for v in values:
        k = _fuzzy_key(v)
        if k not in groups or len(v) > len(groups[k]):
            groups[k] = v

    counts = Counter(_fuzzy_key(v) for v in values)
    winner_key, count = counts.most_common(1)[0]
    if strict and count == 1:
        return None
    return groups[winner_key].upper()


def _best_category(items: list[dict], brand: str | None) -> str | None:
    """Pick the most common category_type, skipping values that match the brand.

    The model sometimes puts the brand name in the category field when the
    category isn't visible on a particular image face.
    """
    from collections import Counter
    brand_upper = brand.upper() if brand else ""
    values = [_as_text(item.get("category_type")) for item in items]
    values = [v.upper() for v in values if v]
    if not values:
        return None
    filtered = [v for v in values if v != brand_upper]
    pool = filtered if filtered else values
    winner, _ = Counter(pool).most_common(1)[0]
    return winner


def _product_name_for_brand(items: list[dict], brand: str | None) -> str | None:
    """Return the best product name, preferring names that contain the brand.

    Majority vote first (handles model output variation across faces).
    Falls back to the longest product name that includes the brand string when
    all per-face values are unique, to filter out manufacturer-face hallucinations.
    """
    common = _most_common_text(items, "product_name")
    if common:
        return common
    if brand:
        brand_upper = brand.upper()
        values = [_as_text(item.get("product_name")) for item in items]
        values = [v for v in values if v]
        branded = [v for v in values if brand_upper in v.upper()]
        if branded:
            return max(branded, key=len)
    return None


def _unique_join(items: list[dict], key: str) -> str | None:
    """Join all unique non-empty values for key with '; ' separator."""
    values = []
    seen = set()
    for item in items:
        value = _as_text(item.get(key))
        if value and value.lower() not in seen:
            values.append(value)
            seen.add(value.lower())
    return "; ".join(values) if values else None


# ── Barcode resolution ────────────────────────────────────────────────────────

def _resolve_barcode(
    pipeline_bc: str | None,
    pipeline_conf: float,
    vlm_bc: str | None,
) -> tuple[str | None, float, dict]:
    """Pick the best barcode from pipeline decoder and VLM digit read.

    Priority rules (EAN checksum is the arbiter):
      Both agree                → use either, full confidence
      Pipeline ✓, VLM ✗        → pipeline wins
      VLM ✓, Pipeline ✗        → VLM wins
      Both ✓ but different      → prefer pipeline (bar-pattern read is primary)
      Neither ✓, both exist     → prefer pipeline
      Only pipeline exists      → pipeline wins
      Only VLM ✓ checksum       → VLM wins (pipeline found nothing valid)
      Only VLM ✗ checksum       → reject (likely hallucinated)

    Returns (barcode, confidence, audit_dict). The audit dict carries the
    intermediate values for the Barcode Trust panel in the review drawer.
    """
    pip_valid = pipeline_bc is not None and ean_checksum_valid(pipeline_bc)
    vlm_valid = vlm_bc is not None and ean_checksum_valid(vlm_bc)

    def _audit(decision: str, winner: str | None) -> dict:
        return {
            "pipeline": pipeline_bc,
            "pipeline_checksum": pip_valid if pipeline_bc is not None else None,
            "vlm": vlm_bc,
            "vlm_checksum": vlm_valid if vlm_bc is not None else None,
            "decision": decision,
            "winner": winner,
        }

    if pipeline_bc and vlm_bc:
        if pipeline_bc == vlm_bc:
            return pipeline_bc, 1.0, _audit("both_agree", "pipeline")
        if pip_valid and not vlm_valid:
            return pipeline_bc, pipeline_conf, _audit("pipeline_wins", "pipeline")
        if vlm_valid and not pip_valid:
            print(f"[barcode] VLM wins over invalid pipeline read: {vlm_bc} vs {pipeline_bc}")
            return vlm_bc, 0.9, _audit("vlm_wins", "vlm")
        # Both valid but different — pipeline is primary
        return pipeline_bc, pipeline_conf, _audit("both_valid_pipeline_primary", "pipeline")

    if pipeline_bc:
        return pipeline_bc, pipeline_conf, _audit("pipeline_only", "pipeline")

    if vlm_bc and vlm_valid:
        print(f"[barcode] VLM-only read (pipeline failed): {vlm_bc}")
        return vlm_bc, 0.85, _audit("vlm_only", "vlm")

    return None, 0.0, _audit("none", None)


# ── Record builder ───────────────────────────────────────────────────────────

def _record_from_group(
    group_items: list[dict],
    group_paths: list[str],
) -> tuple[IMDBRecordWithConfidence, dict]:
    """Build one IMDBRecordWithConfidence from the aggregated per-face extractions.

    Aggregation strategy per field:
    - brand, category, segment: majority vote (most common across faces)
    - manufacturer: longest text (manufacturer face has the full legal name)
    - product_name: majority vote, falling back to brand-aware longest
    - weight: majority vote (most faces should agree on the same weight string)
    - packaging_type, country: majority vote
    - optional fields (promo, variant, etc.): strict majority vote — a value
      seen on only one face is likely a hallucination and is dropped to None
    - barcode: pipeline decoder + VLM digit read, resolved via EAN checksum
    """
    # Pipeline barcode (bar-pattern decoding)
    pipeline_bc, pipeline_conf = decode_barcode(group_paths)

    # VLM barcode (digits read as text from the label image)
    vlm_bc_raw = _most_common_text(group_items, "barcode")
    vlm_bc = "".join(c for c in (vlm_bc_raw or "") if c.isdigit()) or None

    barcode_value, barcode_confidence, barcode_audit = _resolve_barcode(
        pipeline_bc, pipeline_conf, vlm_bc
    )

    def conf(value) -> float:
        return 0.9 if value is not None else 0.0

    brand         = _most_common_text(group_items, "brand")
    manufacturer  = _longest_text(group_items, "manufacturer")
    category_type = _best_category(group_items, brand)
    segment_type  = _most_common_text(group_items, "segment_type")
    product_name  = _product_name_for_brand(group_items, brand)
    weight        = _most_common_text(group_items, "weight")
    unit          = None  # no longer extracted; weight already includes the unit
    packaging_type       = _most_common_text(group_items, "packaging_type")
    country_of_origin    = _most_common_text(group_items, "country_of_origin")
    promotional_messages = _most_common_text(group_items, "promotional_messages", strict=True)
    variant              = _most_common_text(group_items, "variant",              strict=True)
    fragrance_flavor     = _most_common_text(group_items, "fragrance_flavor")
    addons               = _most_common_text(group_items, "addons")
    tagline              = _most_common_text(group_items, "tagline")

    record = IMDBRecordWithConfidence(
        barcode=_as_text(barcode_value),
        category_type=category_type,
        segment_type=segment_type,
        manufacturer=manufacturer,
        brand=brand,
        product_name=product_name,
        weight=weight,
        unit=unit,
        packaging_type=packaging_type,
        country_of_origin=country_of_origin,
        promotional_messages=promotional_messages,
        variant=variant,
        fragrance_flavor=fragrance_flavor,
        addons=addons,
        tagline=tagline,
        barcode_confidence=barcode_confidence,
        category_type_confidence=conf(category_type),
        segment_type_confidence=conf(segment_type),
        manufacturer_confidence=conf(manufacturer),
        brand_confidence=conf(brand),
        product_name_confidence=conf(product_name),
        weight_confidence=conf(weight),
        unit_confidence=0.0,
        packaging_type_confidence=conf(packaging_type),
        country_of_origin_confidence=conf(country_of_origin),
        promotional_messages_confidence=conf(promotional_messages),
        variant_confidence=conf(variant),
        fragrance_flavor_confidence=conf(fragrance_flavor),
        addons_confidence=conf(addons),
        tagline_confidence=conf(tagline),
    )
    print("[extractor] Returning processed record")
    return record, barcode_audit


# ── Multi-view / video extraction ────────────────────────────────────────────

async def extract_from_frames(
    frames: list[Path],
    product_name: str,
    backend: str | None = None,
    model_id: str | None = None,
) -> "PipelineResult":
    """Extract one product record from multiple views of the same item.

    Unlike the standard pipeline this function:
    - Sends ALL frames in a single VLM call (no batching/grouping).
    - Uses the video prompts that instruct the model to return one flat
      JSON object rather than one object per image.
    - Skips tag_text grouping entirely because the caller guarantees all
      frames show the same product.

    Args:
        frames:       Ordered list of image paths (best-first after sharpness
                      selection by the caller).
        product_name: User-supplied product name hint.  Injected into the user
                      prompt so the model can confirm or refine it.
        backend:      One of "anthropic", "openai", "gemini".  Defaults to the
                      module-level active backend.
        model_id:     Exact model identifier string.  Defaults to the model
                      associated with the active backend.

    Returns:
        A single PipelineResult whose image_paths covers all input frames.
    """
    from backend.pipeline import PipelineResult  # local import avoids circular dep
    from backend.normalizer import check_duplicate, normalize_record
    from backend.utils import VLMCallParams, VLMImageData

    _backend  = backend  or _active_backend
    _model_id = model_id or _active_model_id

    encoded_frames = await asyncio.gather(
        *[asyncio.to_thread(_encode_image, p) for p in frames]
    )
    image_data_list: list[VLMImageData] = [
        VLMImageData(img_path=p.name, encoded_data=enc)
        for p, enc in zip(frames, encoded_frames)
    ]

    # Inject the user-supplied name as a hint so the model can confirm or
    # improve it using the label text actually visible in the images.
    hint = f'Product name hint: "{product_name}"\n\n' if product_name.strip() else ""
    user_prompt = hint + VIDEO_EXTRACTION_PROMPT

    params = VLMCallParams(
        system_prompt=VIDEO_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        image_data_list=image_data_list,
        description=f"video extraction: {product_name or 'unknown'}",
        model_override=_model_id or None,
    )

    response = await _vlm_call(params, _backend, _model_id)
    raw_text  = response.choices[0].message.content or ""

    # Parse the single JSON object the video prompt asks for.
    # Fall back gracefully if the model returns {"items": [...]} despite
    # instructions -- unwrap it and take the first item.
    try:
        payload = _extract_json_payload(raw_text)
        if "items" in payload and isinstance(payload["items"], list):
            payload = payload["items"][0] if payload["items"] else {}
    except Exception as exc:
        print(f"[video] JSON parse failed: {exc} — raw: {raw_text[:200]}")
        payload = {}

    # Normalise the dict the same way the standard pipeline does per-item.
    item = _normalize_item(payload)

    # Override product_name with the user hint only if the model left it blank.
    if product_name.strip() and not item.get("product_name"):
        item["product_name"] = product_name

    # Run barcode decoder in a thread so CPU-heavy OpenCV/pyzbar work
    # does not block the asyncio event loop.
    frame_strs = [str(f) for f in frames]
    pipeline_bc, pipeline_conf = await asyncio.get_event_loop().run_in_executor(
        None, decode_barcode, frame_strs
    )
    vlm_bc_raw = item.get("barcode", "")
    vlm_bc = "".join(c for c in vlm_bc_raw if c.isdigit()) or None
    barcode_value, barcode_confidence, barcode_audit = _resolve_barcode(pipeline_bc, pipeline_conf, vlm_bc)

    def _conf(v) -> float:
        return 0.9 if v else 0.0

    record = IMDBRecordWithConfidence(
        barcode=_as_text(barcode_value),
        category_type=item.get("category_type") or None,
        segment_type=item.get("segment_type") or None,
        manufacturer=item.get("manufacturer") or None,
        brand=item.get("brand") or None,
        product_name=item.get("product_name") or None,
        weight=item.get("weight") or None,
        unit=None,  # not extracted; weight already includes the unit
        packaging_type=item.get("packaging_type") or None,
        country_of_origin=item.get("country_of_origin") or None,
        promotional_messages=item.get("promotional_messages") or None,
        variant=item.get("variant") or None,
        fragrance_flavor=item.get("fragrance_flavor") or None,
        addons=item.get("addons") or None,
        tagline=item.get("tagline") or None,
        barcode_confidence=barcode_confidence,
        category_type_confidence=_conf(item.get("category_type")),
        segment_type_confidence=_conf(item.get("segment_type")),
        manufacturer_confidence=_conf(item.get("manufacturer")),
        brand_confidence=_conf(item.get("brand")),
        product_name_confidence=_conf(item.get("product_name")),
        weight_confidence=_conf(item.get("weight")),
        unit_confidence=0.0,
        packaging_type_confidence=_conf(item.get("packaging_type")),
        country_of_origin_confidence=_conf(item.get("country_of_origin")),
        promotional_messages_confidence=_conf(item.get("promotional_messages")),
        variant_confidence=_conf(item.get("variant")),
        fragrance_flavor_confidence=_conf(item.get("fragrance_flavor")),
        addons_confidence=_conf(item.get("addons")),
        tagline_confidence=_conf(item.get("tagline")),
    )

    record, normalized_fields = normalize_record(record)
    cost = calculate_cost(response.input_tokens, response.output_tokens, _model_id)

    print(
        f"[video] extracted — brand={record.brand!r} product={record.product_name!r} "
        f"cost=${cost:.6f}"
    )

    return PipelineResult(
        record=record,
        normalized_fields=normalized_fields,
        duplicate_suggestions=[],   # duplicate check done by caller if needed
        image_path=str(frames[0]),  # primary image is the sharpest (first after sort)
        image_paths=frame_strs,
        cost_usd=cost,
        model_used=_model_id,
        barcode_audit=barcode_audit,
    )


# ── Batch extraction ─────────────────────────────────────────────────────────

async def _extract_batch(
    batch: list[Path],
    backend: str,
    model_id: str,
) -> tuple[list[list[dict]], int, int]:
    """Send a batch of images to the VLM and return items plus token counts.

    Returns (per_image_items, input_tokens, output_tokens). Items are matched to
    images positionally. Retries up to _MAX_ATTEMPTS on failure.
    """
    encoded = await asyncio.gather(*[asyncio.to_thread(_encode_image, p) for p in batch])
    names = ", ".join(p.name for p in batch)
    last_error = ""

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with _get_semaphore():
                response = await _vlm_call(
                    VLMCallParams(
                        system_prompt=SYSTEM_PROMPT,
                        user_prompt=EXTRACTION_PROMPT,
                        image_data_list=[
                            VLMImageData(img_path=str(p), encoded_data=enc)
                            for p, enc in zip(batch, encoded)
                        ],
                        description=f"[BATCH {len(batch)}] {names} (attempt {attempt}/{_MAX_ATTEMPTS})",
                        format_schema=EXTRACTION_SCHEMA,
                    ),
                    backend=backend,
                    model_id=model_id,
                )

            choice = response.choices[0]
            raw = (choice.message.content or "").strip()

            print("==" * 50)
            print(f"[extractor] batch: {names} attempt {attempt}/{_MAX_ATTEMPTS}")
            print(f"[extractor] finish_reason: {getattr(choice, 'finish_reason', None)}")
            print(f"[extractor] raw[:300]: {raw[:300]}")
            print("==" * 50)

            if not raw:
                raise ValueError("VLM returned empty content")

            items = [_normalize_item(i) for i in _extract_json_array(raw)]

            if len(items) != len(batch):
                raise ValueError(f"expected {len(batch)} items, got {len(items)}")

            result = []
            for image_path, item in zip(batch, items):
                item["image_path"] = str(image_path)
                item["tag_text"] = item.get("tag_text") or ""
                result.append([item])
            return result, response.input_tokens, response.output_tokens

        except Exception as e:
            last_error = str(e)
            print(f"[extractor] attempt {attempt}/{_MAX_ATTEMPTS} failed for [{names}]: {e}")

    print(
        f"[extractor] all {_MAX_ATTEMPTS} attempts exhausted for [{names}] "
        f"(last error: {last_error}) — images will be empty and flagged for review"
    )
    return [[] for _ in batch], 0, 0


# ── Entry point ──────────────────────────────────────────────────────────────

async def extract_information_from_images(
    image_paths: list[str] | list[Path],
    model_display_name: str | None = None,
) -> list[tuple[IMDBRecordWithConfidence, list[str], float, str, dict | None]]:
    """Run extraction on a list of images and return one record per product group.

    Flow:
    1. Split images into batches of _BATCH_SIZE.
    2. Fire all batches concurrently (semaphore limits actual parallelism).
    3. Flatten per-batch results into a list of item dicts.
    4. Group item dicts by tag/brand similarity → one group = one product.
    5. Aggregate each group into one IMDBRecordWithConfidence.

    model_display_name selects from MODEL_OPTIONS; falls back to the env-var
    default when omitted or unrecognised.

    Returns a list of (record, image_paths, cost_usd, model_used) tuples.
    Cost is distributed proportionally across groups by image count.
    """
    # Resolve backend and model_id from the display name, falling back to defaults.
    if model_display_name and model_display_name in MODEL_OPTIONS:
        backend, model_id = MODEL_OPTIONS[model_display_name]
    else:
        backend, model_id = _active_backend, _active_model_id

    image_paths = [Path(p) for p in image_paths]
    known_paths = {str(path): str(path) for path in image_paths}

    batches = [
        image_paths[i : i + _BATCH_SIZE]
        for i in range(0, len(image_paths), _BATCH_SIZE)
    ]
    print(
        f"[extractor] {len(image_paths)} images → "
        f"{len(batches)} batches of ≤{_BATCH_SIZE} (concurrency={_CONCURRENCY}) "
        f"backend={backend} model={model_id}"
    )

    batch_tasks = [_extract_batch(b, backend, model_id) for b in batches]
    batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

    total_input_tokens = 0
    total_output_tokens = 0
    valid_items: list[dict] = []
    for batch, batch_result in zip(batches, batch_results):
        if isinstance(batch_result, Exception):
            print(f"[extractor] ERROR on batch {[p.name for p in batch]}: {batch_result}")
            continue
        items, in_tok, out_tok = batch_result
        total_input_tokens += in_tok
        total_output_tokens += out_tok
        for per_image_items in items:
            valid_items.extend(per_image_items)

    total_cost = calculate_cost(total_input_tokens, total_output_tokens, model_id)
    model_used = model_id
    total_images = len(image_paths) or 1

    print(
        f"[extractor] tokens: input={total_input_tokens} output={total_output_tokens} "
        f"cost=${total_cost:.6f} model={model_used}"
    )

    # Group extracted items by product and build one record per group
    grouped_items = group_by_tag_similarity(valid_items)

    # Any image whose path didn't appear in any group (e.g. all attempts failed)
    # gets a singleton group so it still produces an empty flagged record.
    grouped_paths = {item["image_path"] for group in grouped_items for item in group}
    for path in known_paths.values():
        if path not in grouped_paths:
            grouped_items.append([{"image_path": path, "tag_text": ""}])

    loop = asyncio.get_event_loop()
    extracted_products = []
    for group in grouped_items:
        group_paths = [item["image_path"] for item in group]
        # Run in a thread so CPU-heavy barcode decoding (OpenCV/pyzbar/zxing)
        # doesn't block the asyncio event loop and drop the websocket heartbeat.
        record, barcode_audit = await loop.run_in_executor(
            None, _record_from_group, group, group_paths
        )
        group_cost = total_cost * (len(group_paths) / total_images)
        extracted_products.append((record, group_paths, group_cost, model_used, barcode_audit))

    return extracted_products
