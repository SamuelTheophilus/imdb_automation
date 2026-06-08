import json
import re
from json import JSONDecodeError
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from backend.barcode import _bottom_crop
from backend.utils import VLMCallParams, VLMImageData, _encode_pil_image, vlm_call


class ImageGroup(BaseModel):
    tag_text: str = ""
    image_paths: list[str] = Field(default_factory=list)


class GroupedImages(BaseModel):
    groups: list[ImageGroup] = Field(default_factory=list)


def normalize_tag(text: str | None) -> str:
    if not text:
        return ""

    text = text.lower().strip()
    text = text.replace("_", " ")

    # Dataset tags often append the product angle. These words identify the
    # shot, not the product, so removing them makes front/back/side tags group.
    text = re.sub(r"\b(first|second|third|fourth)\s+side\b", " ", text)
    text = re.sub(
        r"\b(front|back|side|left|right|top|bottom|rear|angle|view)\b",
        " ",
        text,
    )

    # keep letters, numbers, and spaces
    text = "".join(ch if ch.isalnum() else " " for ch in text)

    # collapse repeated spaces
    return " ".join(text.split())


def tag_similarity(a: str, b: str) -> float:
    a = normalize_tag(a)
    b = normalize_tag(b)

    if not a or not b:
        return 0.0

    return fuzz.WRatio(a, b) / 100


def item_similarity(
    a: dict,
    b: dict,
    tag_threshold: float = 0.88,
) -> bool:
    tag_a = normalize_tag(a.get("tag_text"))
    tag_b = normalize_tag(b.get("tag_text"))

    if not tag_a or not tag_b:
        return False

    return tag_similarity(tag_a, tag_b) >= tag_threshold


def group_by_tag_similarity(items: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []

    for item in items:
        placed = False

        for group in groups:
            # compare against all existing group members
            is_match = any(item_similarity(item, other) for other in group)

            if is_match:
                group.append(item)
                placed = True
                break

        if not placed:
            groups.append([item])

    return groups


def post_process_tags(response_str: str) -> list[dict]:
    if not response_str:
        return []

    response_str = response_str.strip()
    if response_str.startswith("```"):
        response_str = response_str.split("```", 2)[1]
        if response_str.startswith("json"):
            response_str = response_str[4:]
        response_str = response_str.strip()

    decoder = json.JSONDecoder()
    for start in [i for i, char in enumerate(response_str) if char == "["]:
        try:
            data, _ = decoder.raw_decode(response_str[start:])
        except JSONDecodeError:
            continue
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

    raise ValueError(f"Tag OCR did not return a valid JSON array: {response_str[:500]}")


async def group_images(image_paths: list[str] | list[Path]) -> GroupedImages:
    image_data_list = []
    for image_path in image_paths:
        # cropped_image = _bottom_crop(Image.open(image_path).convert("RGB"))
        cropped_image = Image.open(image_path).convert("RGB")
        encoded_image = _encode_pil_image(cropped_image)
        image_data_list.append(
            VLMImageData(img_path=str(image_path), encoded_data=encoded_image)
        )

    system_prompt = """
You are a precise OCR assistant for product dataset image tags.
Your goal is to extract the image tag.
The image tag can be found at the bottom/top/left/right of the image.
You will notice that the image tag is not *ON* the object in the image.
Read only the dataset tag visible in the provided image and return that tag as your response.
"""
    user_prompt = """
Each image is a bottom crop from a product photo.
Read only the product image tag text visible in the image.
Return only one valid JSON array. Do not include markdown, explanations, or text outside the JSON.

Each item in the array must have exactly these keys:
- image_path: the exact Image Path provided before the image
- tag_text: the product image tag text, or "" if no clear tag is visible

Do not describe the image.
Only return the image tag of the image. This is a crucial component of a much larger pipeline.
Any deviation/error from your response will render the rest of the pipeline useless.

Your final response should look like this
[{"image_path": <the original image path provided>, "tag_text": <the tag text you extracted>}]
"""

    response = await vlm_call(
        VLMCallParams(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_data_list=image_data_list,
            description="IMAGE GROUPING",
        )
    )
    raw = response.choices[0].message.content.strip()

    try:
        reasoning_content = response.choices[0].message.reasoning
        print(
            f"[grouping images] Reasoning of the VLM during inference: {reasoning_content}"
        )
    except Exception as error:
        print(
            f"[grouping images] error occured when extracting the reasoning content: {str(error)}"
        )

    print(f"[grouping images] Response after grouping: {response}")
    print(f"[grouping images] RAW response after grouping: {raw}")

    items = post_process_tags(raw)
    known_paths = {str(path): str(path) for path in image_paths}
    valid_items = []
    for item in items:
        image_path = item.get("image_path", "")
        if image_path not in known_paths:
            continue
        item["image_path"] = known_paths[image_path]
        item["tag_text"] = item.get("tag_text") or ""
        valid_items.append(item)

    grouped_images = group_by_tag_similarity(valid_items)
    print(f"[grouping images] Number of groups found: {len(grouped_images)}")

    groups: list[ImageGroup] = []
    for group in grouped_images:
        representative_tag = max(
            (item.get("tag_text", "") for item in group),
            key=len,
            default="",
        )
        groups.append(
            ImageGroup(
                tag_text=representative_tag,
                image_paths=[item["image_path"] for item in group],
            )
        )

    grouped_paths = {path for group in groups for path in group.image_paths}
    for image_path in image_paths:
        image_path = str(image_path)
        if image_path not in grouped_paths:
            groups.append(ImageGroup(image_paths=[image_path]))

    return GroupedImages(groups=groups)
