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

from backend.barcode import decode_barcode
from backend.image_aggregation import group_by_tag_similarity
from backend.schema import IMDBRecordWithConfidence
from backend.utils import VLMCallParams, VLMImageData, vlm_call_w_anthropic, vlm_call_w_ollama, vlm_call_w_openai, vlm_call_w_gemini

# ── Prompt templates ────────────────────────────────────────────────────────
jinja_templates_folder = f"{Path(__file__).parent.parent}/core/prompts"
env = Environment(loader=FileSystemLoader(jinja_templates_folder))

SYSTEM_PROMPT    = env.get_template("vlm_system_prompt.j2").render()
EXTRACTION_PROMPT = env.get_template("vlm_extraction_prompt.j2").render()

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

_VLM_BACKEND: str = _resolve_backend()

# Ollama supports only one image per request. Cloud backends (OpenAI, Gemini)
# accept multiple images per call — batching reduces round-trips and lets the
# model cross-reference all faces of a product in one pass.
_BATCH_SIZE = 1 if _VLM_BACKEND == "ollama" else int(os.getenv("VLM_BATCH_SIZE", "8"))

# Semaphore limits simultaneous requests. For Ollama this matches
# OLLAMA_NUM_PARALLEL; for cloud APIs it caps parallel calls.
_CONCURRENCY = int(os.getenv("OLLAMA_CONCURRENCY", "2"))
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_CONCURRENCY)
    return _semaphore


async def _vlm_call(params: VLMCallParams):
    """Route the VLM call to the configured backend."""
    if _VLM_BACKEND == "anthropic":
        return await vlm_call_w_anthropic(params)
    if _VLM_BACKEND == "gemini":
        return await vlm_call_w_gemini(params)
    if _VLM_BACKEND == "openai":
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


# ── Record builder ───────────────────────────────────────────────────────────

def _record_from_group(
    group_items: list[dict],
    group_paths: list[str],
) -> IMDBRecordWithConfidence:
    """Build one IMDBRecordWithConfidence from the aggregated per-face extractions.

    Aggregation strategy per field:
    - brand, category, segment: majority vote (most common across faces)
    - manufacturer: longest text (manufacturer face has the full legal name)
    - product_name: majority vote, falling back to brand-aware longest
    - weight: majority vote (most faces should agree on the same weight string)
    - packaging_type, country: majority vote
    - optional fields (promo, variant, etc.): strict majority vote — a value
      seen on only one face is likely a hallucination and is dropped to None
    - barcode: decoded by pyzbar, not by the VLM
    """
    barcode_value, barcode_confidence = decode_barcode(group_paths)

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
    return record


# ── Batch extraction ─────────────────────────────────────────────────────────

async def _extract_batch(batch: list[Path]) -> list[list[dict]]:
    """Send a batch of images to the VLM and return one item-list per image.

    Items are matched to images positionally (first item → first image). If the
    model returns a different number of items than images the attempt is retried.
    All attempts exhausted → returns empty lists so downstream still produces a
    record flagged for review.

    The semaphore limits concurrent Ollama calls to OLLAMA_CONCURRENCY.
    """
    encoded = [_encode_image(p) for p in batch]
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
                    )
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
            return result

        except Exception as e:
            last_error = str(e)
            print(f"[extractor] attempt {attempt}/{_MAX_ATTEMPTS} failed for [{names}]: {e}")

    print(
        f"[extractor] all {_MAX_ATTEMPTS} attempts exhausted for [{names}] "
        f"(last error: {last_error}) — images will be empty and flagged for review"
    )
    return [[] for _ in batch]


# ── Entry point ──────────────────────────────────────────────────────────────

async def extract_information_from_images(
    image_paths: list[str] | list[Path],
) -> list[tuple[IMDBRecordWithConfidence, list[str]]]:
    """Run extraction on a list of images and return one record per product group.

    Flow:
    1. Split images into batches of _BATCH_SIZE.
    2. Fire all batches concurrently (semaphore limits actual Ollama parallelism).
    3. Flatten per-batch results into a list of item dicts.
    4. Group item dicts by tag/brand similarity → one group = one product.
    5. Aggregate each group into one IMDBRecordWithConfidence.

    Returns a list of (record, image_paths_for_group) tuples, one per product.
    """
    image_paths = [Path(p) for p in image_paths]
    known_paths = {str(path): str(path) for path in image_paths}

    batches = [
        image_paths[i : i + _BATCH_SIZE]
        for i in range(0, len(image_paths), _BATCH_SIZE)
    ]
    print(
        f"[extractor] {len(image_paths)} images → "
        f"{len(batches)} batches of ≤{_BATCH_SIZE} (concurrency={_CONCURRENCY})"
    )

    batch_tasks = [_extract_batch(b) for b in batches]
    batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

    valid_items: list[dict] = []
    for batch, batch_result in zip(batches, batch_results):
        if isinstance(batch_result, Exception):
            print(f"[extractor] ERROR on batch {[p.name for p in batch]}: {batch_result}")
            continue
        for per_image_items in batch_result:
            valid_items.extend(per_image_items)

    # Group extracted items by product and build one record per group
    grouped_items = group_by_tag_similarity(valid_items)

    # Any image whose path didn't appear in any group (e.g. all attempts failed)
    # gets a singleton group so it still produces an empty flagged record.
    grouped_paths = {item["image_path"] for group in grouped_items for item in group}
    for path in known_paths.values():
        if path not in grouped_paths:
            grouped_items.append([{"image_path": path, "tag_text": ""}])

    extracted_products = []
    for group in grouped_items:
        group_paths = [item["image_path"] for item in group]
        record = _record_from_group(group, group_paths)
        extracted_products.append((record, group_paths))

    return extracted_products
