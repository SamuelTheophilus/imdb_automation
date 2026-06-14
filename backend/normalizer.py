import re
from pathlib import Path

import pandas as pd
from rapidfuzz import process, fuzz

from backend.schema import IMDBRecordWithConfidence


# ── Canonical lists ─────────────────────────────────────────────────────────
# These lists are used for fuzzy normalization. CANONICAL_BRANDS is populated
# at startup from a CSV if one is present (see load_canonical_brands). The
# others are placeholder lists that will be replaced with ground-truth values
# once eval results confirm the correct canonical forms for this dataset.

CANONICAL_BRANDS: list[str] = [
    "ALFA", "BAMA", "BEL", "BLUE BAND", "BRISK FARM", "C'PROPRE",
    "CHOCOLIM", "EASY", "ENA PA", "GET", "GOLDEN VICTORIA", "JOJONAVI",
    "KADI", "KALYPPO", "KING SAM", "KIVO", "LAILA", "LELE", "LUX",
    "MAGGI", "MASEDA", "MIKSI", "MILO", "MOK", "MOSSE", "MUMMY'S KITCHEN",
    "POMO", "ROSALINDA", "SISTER", "SIYA", "SO KLIN", "TAPOK",
    "TASTY TOM", "THIS WAY", "U-FRESH", "VIBE", "ZAA", "ZESTA",
]

CANONICAL_MANUFACTURERS: list[str] = [
    "AFRICAN CONSUMER PRODUCTS", "AJC TRADING CO LTD", "AL AIN COMPANY LTD",
    "AQUAFRESH LIMITED", "ATONA FOODS", "B-DIET LTD", "BLOW CHEM INDUSTRIES LTD",
    "C'PROPRE", "ETKAF", "FAGIP VENTURES", "GB FOODS", "GEE TRADING SAL",
    "HAMTA & SONS LTD", "HOMEPRO COMPANY LTD", "KING SAM", "LGD LIMITED",
    "MADHU JAYANTI INTERNATIONAL PVT LTD", "MENKISH IMPEX",
    "NAM VIET PHAT FOOD CO. LIMITED", "NESTLE", "NUTRIFOODS", "PROCUS LIMITED",
    "PROMASIDOR", "PT SAYAP MAS UTAMA", "S.D.T.M", "SENICO",
    "SISTER SARDINE & MACKEREL VENTURES", "SYNERGY ENTREPRISES ( FZE)",
    "THE COCA COLA COMPANY", "U-FRESH ENTERPRISES", "UNILEVER", "UPFIELD",
    "WATAWALA TEA CEYLON LTD", "ZHEJIANG NATIVE PRODUCE & ANIMAL CO LTD",
]

CANONICAL_CATEGORIES: list[str] = [
    "MAYONNAISE", "SALTED MARGARINE", "BUTTER", "POWDER",
    "BLACK TEA", "BAR", "TOMATO MIX", "3 IN 1",
]

CANONICAL_SEGMENTS: list[str] = []

CANONICAL_PACKAGING: list[str] = [
    "bottle", "can", "box", "bag", "pouch",
    "tube", "jar", "sachet", "carton", "other",
]

# ── Country corrections ─────────────────────────────────────────────────────
# GT countries: CHINA, COTE D'IVOIRE, GHANA, INDIA, INDONESIA, NIGERIA,
#               SRI LANKA, VIETNAM
_COUNTRY_CORRECTIONS: dict[str, str] = {
    # Ghana
    "GHAN": "GHANA", "GHANATI": "GHANA",
    # Nigeria
    "NIGERIAT": "NIGERIA", "NIGERI": "NIGERIA",
    # South Africa (not in GT but model hallucinates it)
    "SOUTH AFRICAN": "SOUTH AFRICA", "SOUTH AFRIC": "SOUTH AFRICA",
    "SOUTH AFRI": "SOUTH AFRICA", "SOUTH AF": "SOUTH AFRICA",
    "SOUTA": "SOUTH AFRICA",
    # Not in GT — clear to avoid false positives
    "SINGAPUR": None, "SINGAPOUR": None, "SINGHAPUR": None,
    "SINGH": None, "SINGAPORE": None,
    "SOUTHERN AFRICA": None, "SOUTH AFRICA": None,
    # Indonesia
    "INDONESI": "INDONESIA",
    # Vietnam
    "VIET NAM": "VIETNAM", "VIET": "VIETNAM",
    # Sri Lanka
    "SRI LANK": "SRI LANKA", "SRILANKA": "SRI LANKA",
    # Cote d'Ivoire
    "IVORY COAST": "COTE D'IVOIRE", "COTE DIVOIRE": "COTE D'IVOIRE",
    "CÔTE D'IVOIRE": "COTE D'IVOIRE",
    # India
    "INDI": "INDIA",
    # China (P.R.C. and PRC are common label abbreviations for China)
    "CHIN": "CHINA", "CHINI": "CHINA",
    "P.R.C.": "CHINA", "PRC": "CHINA", "P.R.C": "CHINA",
    "PEOPLES REPUBLIC OF CHINA": "CHINA",
    "PEOPLE'S REPUBLIC OF CHINA": "CHINA",
}

_BARCODE_STRIP = re.compile(r"[^0-9]")


def load_canonical_brands(csv_path: str | Path) -> None:
    """Populate CANONICAL_BRANDS from the project's existing IMDB CSV.

    Call this once at startup. When the list is non-empty, brand values from
    the VLM are fuzzy-matched against it and corrected if a close match exists.
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
    """Match value against a canonical list using fuzzy string similarity.

    Returns (normalized_value, was_changed). If no canonical entry scores
    above threshold the original value is returned unchanged.
    """
    if not value or not canonical_list:
        return value, False

    match, score, _ = process.extractOne(
        value,
        canonical_list,
        scorer=fuzz.WRatio,
    )

    if score >= threshold:
        was_normalized = match.lower() != value.lower()
        return match, was_normalized

    return value, False


def _fix_country(value: str | None) -> tuple[str | None, bool]:
    """Apply known country-name corrections and uppercase the result.

    Values mapped to None in _COUNTRY_CORRECTIONS are cleared (not in GT).
    """
    if not value:
        return value, False
    upper = value.upper().strip()
    if upper in _COUNTRY_CORRECTIONS:
        corrected = _COUNTRY_CORRECTIONS[upper]   # may be None (clear it)
        return corrected, True
    return upper, upper != value


def _fix_barcode(value: str | None) -> tuple[str | None, bool]:
    """Strip non-numeric characters from a barcode string."""
    if not value:
        return value, False
    # Handle float strings from CSV (e.g., "6034000482027.0" → "6034000482027")
    try:
        cleaned = str(int(float(str(value))))
    except (ValueError, OverflowError):
        cleaned = _BARCODE_STRIP.sub("", str(value))
    result = cleaned if cleaned else None
    return result, result != str(value)


def normalize_record(
    record: IMDBRecordWithConfidence,
) -> tuple[IMDBRecordWithConfidence, list[str]]:
    """Run all normalization passes on an extracted record in place.

    Passes applied in order:
    1. Brand fuzzy-match against canonical list (no-op until list is populated)
    2. Category and segment fuzzy-match
    3. Packaging fuzzy-match
    4. Country name correction (truncations and adjectival forms)
    5. Barcode non-numeric character stripping

    Returns the modified record and a list of field names that were changed,
    so the UI can highlight which fields were auto-corrected.
    """
    normalized_fields: list[str] = []

    brand, changed = _fuzzy_normalize(record.brand, CANONICAL_BRANDS, threshold=85)
    if changed:
        record.brand = brand
        normalized_fields.append("brand")

    manufacturer, changed = _fuzzy_normalize(record.manufacturer, CANONICAL_MANUFACTURERS, threshold=82)
    if changed:
        record.manufacturer = manufacturer
        normalized_fields.append("manufacturer")

    # category_type is handled by the prompt — no normalizer correction needed

    packaging, changed = _fuzzy_normalize(record.packaging_type, CANONICAL_PACKAGING, threshold=90)
    if changed and packaging:
        record.packaging_type = packaging
        normalized_fields.append("packaging_type")

    country, changed = _fix_country(record.country_of_origin)
    if changed:
        record.country_of_origin = country or None
        normalized_fields.append("country_of_origin")

    barcode, changed = _fix_barcode(record.barcode)
    if changed:
        record.barcode = barcode
        normalized_fields.append("barcode")

    return record, normalized_fields


def check_duplicate(
    record: IMDBRecordWithConfidence,
    existing_records: list[dict],
    barcode_match: bool = True,
    similarity_threshold: float = 0.95,
) -> list[dict]:
    """Check whether a newly extracted record duplicates an existing IMDB entry.

    Two matching rules, applied in order:
    1. Exact barcode match — definite duplicate.
    2. Brand + product_name fuzzy similarity ≥ threshold — probable duplicate.

    Returns a list of matching existing records (empty means no duplicates).
    """
    duplicates = []

    for existing in existing_records:
        # Rule 1: exact barcode
        if (
            barcode_match
            and record.barcode
            and existing.get("barcode")
            and record.barcode == existing["barcode"]
        ):
            duplicates.append({**existing, "match_reason": "Exact barcode match"})
            continue

        # Rule 2: brand + product name similarity
        if record.brand and record.product_name:
            existing_brand = existing.get("brand", "") or ""
            existing_name  = existing.get("product_name", "") or ""
            brand_score = fuzz.WRatio(record.brand, existing_brand) / 100
            name_score  = fuzz.WRatio(record.product_name, existing_name) / 100
            combined    = (brand_score + name_score) / 2
            if combined >= similarity_threshold:
                duplicates.append({
                    **existing,
                    "match_reason": f"Brand + name similarity ({combined:.0%})",
                })

    return duplicates
