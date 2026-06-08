# backend/extractor.py

import asyncio
import base64
import json

from io import BytesIO
from json import JSONDecodeError
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from PIL import Image

from backend.barcode import decode_barcode
from backend.image_aggregation import group_by_tag_similarity
from backend.schema import IMDBRecordWithConfidence
from backend.utils import VLMCallParams, VLMImageData, vlm_call

jinja_templates_folder = f"{Path(__file__).parent.parent}/core/prompts"
env = Environment(loader=FileSystemLoader(jinja_templates_folder))

system_template = env.get_template("vlm_system_prompt.j2")
extraction_template = env.get_template("vlm_extraction_prompt.j2")

SYSTEM_PROMPT = system_template.render()
EXTRACTION_PROMPT = extraction_template.render()

FILE_NAME_LOG = "[extractor]"
_MAX_ATTEMPTS = 3

# Qwen3-VL tiles images dynamically. Capping the longest side here keeps tile
# count in the 4-9 range instead of 16+, which cuts visual-token count and
# inference time without meaningfully reducing label legibility.
_MAX_IMAGE_SIDE = 768


def _encode_image(image_path: str | Path) -> str:
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    if max(w, h) > _MAX_IMAGE_SIDE:
        scale = _MAX_IMAGE_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _extract_json_payload(raw: str) -> dict:
    raw = raw.strip()

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    decoder = json.JSONDecoder()
    for start in [i for i, char in enumerate(raw) if char == "{"]:
        try:
            data, _ = decoder.raw_decode(raw[start:])
        except JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    raise ValueError(f"Extractor did not return valid JSON: {raw[:500]}")


def _extract_json_array(raw: str) -> list[dict]:
    raw = raw.strip()

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    decoder = json.JSONDecoder()
    for start in [i for i, char in enumerate(raw) if char == "{"]:
        try:
            data, _ = decoder.raw_decode(raw[start:])
        except JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [item for item in data["items"] if isinstance(item, dict)]

    for start in [i for i, char in enumerate(raw) if char == "["]:
        try:
            data, _ = decoder.raw_decode(raw[start:])
        except JSONDecodeError:
            continue
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

    raise ValueError(f"Extractor did not return a valid JSON array: {raw[:500]}")


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return "; ".join(str(item) for item in value if item)
    return str(value)


def _first_text(items: list[dict], key: str) -> str | None:
    for item in items:
        value = _as_text(item.get(key))
        if value:
            return value
    return None


def _longest_text(items: list[dict], key: str) -> str | None:
    values = [_as_text(item.get(key)) for item in items]
    values = [value for value in values if value]
    return max(values, key=len, default=None)


def _unique_join(items: list[dict], key: str) -> str | None:
    values = []
    seen = set()
    for item in items:
        value = _as_text(item.get(key))
        if value and value.lower() not in seen:
            values.append(value)
            seen.add(value.lower())
    return "; ".join(values) if values else None


def _record_from_group(
    group_items: list[dict],
    group_paths: list[str],
) -> IMDBRecordWithConfidence:
    # Barcode is handled only by pyzbar after grouping; VLM barcode OCR is unreliable.
    barcode_value, barcode_confidence = decode_barcode(group_paths)
    final_barcode = _as_text(barcode_value)
    final_barcode_confidence = barcode_confidence

    def conf(value) -> float:
        return 0.9 if value is not None else 0.0

    category_type = _first_text(group_items, "category_type")
    segment_type = _first_text(group_items, "segment_type")
    manufacturer = _longest_text(group_items, "manufacturer")
    brand = _longest_text(group_items, "brand")
    product_name = _longest_text(group_items, "product_name")
    weight = _first_text(group_items, "weight")
    unit = _first_text(group_items, "unit") or _first_text(group_items, "weight_unit")
    packaging_type = _first_text(group_items, "packaging_type")
    country_of_origin = _first_text(group_items, "country_of_origin")
    promotional_messages = _unique_join(group_items, "promotional_messages")
    variant = _first_text(group_items, "variant")
    fragrance_flavor = _first_text(group_items, "fragrance_flavor")
    addons = _unique_join(group_items, "addons")
    tagline = _longest_text(group_items, "tagline")

    record = IMDBRecordWithConfidence(
        barcode=final_barcode,
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
        barcode_confidence=final_barcode_confidence,
        category_type_confidence=conf(category_type),
        segment_type_confidence=conf(segment_type),
        manufacturer_confidence=conf(manufacturer),
        brand_confidence=conf(brand),
        product_name_confidence=conf(product_name),
        weight_confidence=conf(weight),
        unit_confidence=conf(unit),
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


async def _extract_single_image(
    image_path: Path,
    known_paths: dict[str, str],
) -> list[dict]:
    """Fire one VLM call for a single image and return its parsed items.

    Retries up to _MAX_ATTEMPTS times. On total failure returns [] so the
    caller still produces an empty IMDBRecordWithConfidence flagged for review.
    """
    encoded = _encode_image(image_path)
    last_error = ""

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await vlm_call(
                VLMCallParams(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=EXTRACTION_PROMPT,
                    image_data_list=[
                        VLMImageData(
                            img_path=str(image_path),
                            encoded_data=encoded,
                        )
                    ],
                    description=f"[IMAGE INFO EXTRACTION] {image_path.name} (attempt {attempt}/{_MAX_ATTEMPTS})",
                )
            )

            choice = response.choices[0]
            raw = (choice.message.content or "").strip()

            print("==" * 50)
            print(f"[extractor] image: {image_path.name} attempt {attempt}/{_MAX_ATTEMPTS}")
            print(f"[extractor] finish_reason: {getattr(choice, 'finish_reason', None)}")
            print(f"[extractor] raw[:300]: {raw[:300]}")
            print("==" * 50)

            if not raw:
                last_error = "empty response"
                raise ValueError("VLM returned empty content")

            items = _extract_json_array(raw)

            valid = []
            for item in items:
                item_path = item.get("image_path", "")
                if item_path not in known_paths:
                    continue
                item["image_path"] = known_paths[item_path]
                item["tag_text"] = item.get("tag_text") or ""
                valid.append(item)
            return valid

        except Exception as e:
            last_error = str(e)
            print(f"[extractor] attempt {attempt}/{_MAX_ATTEMPTS} failed for {image_path.name}: {e}")

    print(
        f"[extractor] all {_MAX_ATTEMPTS} attempts exhausted for {image_path.name} "
        f"(last error: {last_error}) — record will be empty and flagged for review"
    )
    return []


async def extract_information_from_images(
    image_paths: list[str] | list[Path],
    use_local: bool = True,
) -> list[tuple[IMDBRecordWithConfidence, list[str]]]:

    image_paths = [Path(_img) for _img in image_paths]
    known_paths = {str(path): str(path) for path in image_paths}

    # Fire all images in parallel. Ollama serialises internally when
    # OLLAMA_NUM_PARALLEL=1 (default), but with OLLAMA_NUM_PARALLEL≥2 on a GPU
    # this gives near-linear throughput. return_exceptions=True prevents one
    # bad image from aborting the whole batch.
    tasks = [_extract_single_image(p, known_paths) for p in image_paths]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_items: list[dict] = []
    for image_path, result in zip(image_paths, results):
        if isinstance(result, Exception):
            print(f"[extractor] ERROR on {image_path.name}: {result}")
            continue
        valid_items.extend(result)

    grouped_items = group_by_tag_similarity(valid_items)
    grouped_paths = {item["image_path"] for group in grouped_items for item in group}
    for image_path in known_paths.values():
        if image_path not in grouped_paths:
            grouped_items.append([{"image_path": image_path, "tag_text": ""}])

    extracted_products = []
    for group in grouped_items:
        group_paths = [item["image_path"] for item in group]
        record = _record_from_group(group, group_paths)
        extracted_products.append((record, group_paths))

    return extracted_products
