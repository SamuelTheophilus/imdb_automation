import re

from rapidfuzz import fuzz


def normalize_tag(text: str | None) -> str:
    """Normalise a dataset tag string for similarity comparison.

    Dataset tags follow the pattern "GH<code> <product> <side>" where <side>
    is one of Front, Back, Second_Side, etc. Stripping side indicators lets
    images of the same product from different angles produce near-identical
    normalised strings, which is what the grouping threshold checks against.
    """
    if not text:
        return ""

    text = text.lower().strip()
    text = text.replace("_", " ")

    # Remove side/angle indicators that differ across faces of the same product
    text = re.sub(r"\b(first|second|third|fourth)\s+side\b", " ", text)
    text = re.sub(
        r"\b(front|back|side|left|right|top|bottom|rear|angle|view)\b",
        " ",
        text,
    )

    # Keep only alphanumeric characters and spaces
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


def tag_similarity(a: str, b: str) -> float:
    """Fuzzy similarity score (0.0–1.0) between two normalised tag strings."""
    a = normalize_tag(a)
    b = normalize_tag(b)
    if not a or not b:
        return 0.0
    return fuzz.WRatio(a, b) / 100


def item_similarity(a: dict, b: dict, tag_threshold: float = 0.88) -> bool:
    """Return True if two extracted item dicts likely represent the same product.

    Primary signal: tag_text similarity. Tags include the GH product code which
    is the same across all faces of one product, so high similarity means same
    product. Threshold 0.88 gives enough slack for OCR variation while rejecting
    different products with partially-overlapping tag text.

    Fallback: when tag_text is absent or ambiguous, check brand + product_name
    agreement. Both must score ≥ 0.85 to avoid false merges.
    """
    tag_a = normalize_tag(a.get("tag_text"))
    tag_b = normalize_tag(b.get("tag_text"))

    if tag_a and tag_b and tag_similarity(tag_a, tag_b) >= tag_threshold:
        return True

    # Fallback for image faces where the edge tag is not visible
    brand_a = normalize_tag(a.get("brand"))
    brand_b = normalize_tag(b.get("brand"))
    name_a  = normalize_tag(a.get("product_name"))
    name_b  = normalize_tag(b.get("product_name"))

    if brand_a and brand_b and name_a and name_b:
        return (
            tag_similarity(brand_a, brand_b) >= 0.9
            and tag_similarity(name_a, name_b) >= 0.85
        )

    return False


def group_by_tag_similarity(items: list[dict]) -> list[list[dict]]:
    """Group extracted item dicts so each group is one distinct product.

    Uses a greedy single-pass algorithm: each item is placed into the first
    existing group where it matches any member. Items that match nothing start
    a new group. This is O(n²) but n is small (3–5 images per session).
    """
    groups: list[list[dict]] = []

    for item in items:
        placed = False
        for group in groups:
            if any(item_similarity(item, other) for other in group):
                group.append(item)
                placed = True
                break
        if not placed:
            groups.append([item])

    return groups
