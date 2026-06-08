# backend/normalizer.py

from pathlib import Path
from rapidfuzz import process, fuzz
import pandas as pd

from backend.schema import IMDBRecordWithConfidence


# Fallback canonical lists if no CSV is provided
CANONICAL_BRANDS: list[str] = []
CANONICAL_CATEGORIES: list[str] = [
    "Beverages", "Dairy", "Snacks", "Personal Care", "Household",
    "Bakery", "Confectionery", "Frozen Foods", "Canned Goods",
    "Condiments", "Cereals", "Baby Products", "Health & Wellness",
]
CANONICAL_SEGMENTS: list[str] = [
    "Carbonated Drinks", "Juices", "Water", "Tea", "Coffee",
    "Yoghurt", "Cheese", "Milk", "Chips", "Biscuits", "Chocolate",
    "Shampoo", "Soap", "Detergent", "Bread", "Pasta", "Rice",
]
CANONICAL_PACKAGING: list[str] = [
    "bottle", "can", "box", "bag", "pouch",
    "tube", "jar", "sachet", "carton", "other",
]


def load_canonical_brands(csv_path: str | Path) -> None:
    """
    Load canonical brand names from your existing IMDB CSV.
    Call this once at startup.
    """
    global CANONICAL_BRANDS
    df = pd.read_csv(csv_path)
    if "brand" in df.columns:
        CANONICAL_BRANDS = df["brand"].dropna().unique().tolist()


def _fuzzy_normalize(
    value: str | None,
    canonical_list: list[str],
    threshold: int = 80,
) -> tuple[str | None, bool]:
    """
    Tries to match value against a canonical list using fuzzy matching.
    Returns (normalized_value, was_normalized).
    was_normalized=True means we changed the value.
    was_normalized=False means it was already clean or no match found.
    """
    if not value or not canonical_list:
        return value, False

    match, score, _ = process.extractOne(
        value,
        canonical_list,
        scorer=fuzz.WRatio,  # handles typos, word order, abbreviations
    )

    if score >= threshold:
        was_normalized = match.lower() != value.lower()
        return match, was_normalized

    # No confident match found — return original
    return value, False


def normalize_record(
    record: IMDBRecordWithConfidence,
) -> tuple[IMDBRecordWithConfidence, list[str]]:
    """
    Runs fuzzy normalization on brand, category, segment, and packaging.
    Returns (normalized_record, list_of_normalized_field_names).
    """
    normalized_fields: list[str] = []

    # Brand normalization
    brand, brand_changed = _fuzzy_normalize(
        record.brand,
        CANONICAL_BRANDS,
        threshold=85,  # stricter for brands — avoid false matches
    )
    if brand_changed:
        record.brand = brand
        normalized_fields.append("brand")

    # Category normalization
    category, category_changed = _fuzzy_normalize(
        record.category_type,
        CANONICAL_CATEGORIES,
        threshold=80,
    )
    if category_changed:
        record.category_type = category
        normalized_fields.append("category_type")

    # Segment normalization
    segment, segment_changed = _fuzzy_normalize(
        record.segment_type,
        CANONICAL_SEGMENTS,
        threshold=80,
    )
    if segment_changed:
        record.segment_type = segment
        normalized_fields.append("segment_type")

    # Packaging normalization
    packaging, packaging_changed = _fuzzy_normalize(
        # record.packaging_type.value if record.packaging_type else None,
        record.packaging_type if record.packaging_type else None,
        CANONICAL_PACKAGING,
        threshold=90,  # packaging is an enum so should be very close
    )
    if packaging_changed and packaging:
        record.packaging_type = packaging
        normalized_fields.append("packaging_type")

    return record, normalized_fields


def check_duplicate(
    record: IMDBRecordWithConfidence,
    existing_records: list[dict],
    barcode_match: bool = True,
    similarity_threshold: float = 0.95,
) -> list[dict]:
    """
    Checks if a new record might be a duplicate of existing IMDB entries.
    Returns list of potential duplicate records.

    Matching logic:
    1. Exact barcode match → definite duplicate
    2. Brand + product_name similarity >= threshold → potential duplicate
    """
    duplicates = []

    for existing in existing_records:
        # Rule 1: exact barcode match
        if (
            barcode_match
            and record.barcode
            and existing.get("barcode")
            and record.barcode == existing["barcode"]
        ):
            duplicates.append({**existing, "match_reason": "Exact barcode match"})
            continue

        # Rule 2: brand + product name fuzzy similarity
        if record.brand and record.product_name:
            existing_brand = existing.get("brand", "") or ""
            existing_name = existing.get("product_name", "") or ""

            brand_score = fuzz.WRatio(record.brand, existing_brand) / 100
            name_score = fuzz.WRatio(record.product_name, existing_name) / 100
            combined_score = (brand_score + name_score) / 2

            if combined_score >= similarity_threshold:
                duplicates.append({
                    **existing,
                    "match_reason": f"Brand + name similarity ({combined_score:.0%})",
                })

    return duplicates
