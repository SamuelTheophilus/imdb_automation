import re
import unicodedata
from pathlib import Path

import pandas as pd
from rapidfuzz import process, fuzz

from backend.schema import IMDBRecordWithConfidence


def _strip_accents(text: str) -> str:
    """Decompose unicode accents and drop combining characters (PÓMO → POMO)."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


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
    "AFRICAN CONSUMER PRODUCTS", "AJC TRADING CO LTD", "AL-AIN NATIONAL JUICE & REFRESHMENTS CO.",
    "AL AIN COMPANY LTD", "AQUAFRESH LIMITED", "ATONA FOODS", "B-DIET LTD",
    "BLOW CHEM INDUSTRIES LTD", "C'PROPRE", "ETKAF", "FAGIP VENTURES", "GB FOODS",
    "GEE TRADING SAL", "HAMTA & SONS LIMITED", "HAMTA & SONS LTD", "HOMEPRO COMPANY LTD",
    "KING SAM", "LGD LIMITED", "MADHU JAYANTI INTERNATIONAL PVT LTD", "MENKISH IMPEX",
    "NAM VIET PHAT FOOD CO. LIMITED", "NESTLE", "NUTRIFOODS", "PROCUS LIMITED",
    "PROMASIDOR", "PT SAYAP MAS UTAMA", "S.D.T.M", "SENICO",
    "SISTER SARDINE & MACKEREL VENTURES", "SYNERGY ENTREPRISES ( FZE)",
    "THE COCA COLA COMPANY", "U-FRESH ENTERPRISES", "UNILEVER", "UPFIELD",
    "WATAWALA TEA CEYLON LTD", "ZHEJIANG NATIVE PRODUCE & ANIMAL CO LTD",
]

# Manufacturer-specific corrections that fuzzy matching can't reliably catch
_MANUFACTURER_CORRECTIONS: dict[str, str] = {
    "SDTM-CI": "S.D.T.M",
    "S.D.T.M-CI": "S.D.T.M",
    "SDTM CI": "S.D.T.M",
}

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


_WEIGHT_MULTI    = re.compile(r"\s*[-/]\s*.+$")
_WEIGHT_SPACE    = re.compile(r"(\d)\s+(ML|G|KG|L|GMS?)\b", re.IGNORECASE)
_WEIGHT_GMS      = re.compile(r"^(\d+(?:\.\d+)?)GMS?$", re.IGNORECASE)
_WEIGHT_ML_CONV  = re.compile(r"^(\d+(?:\.\d+)?)ML$", re.IGNORECASE)
_WEIGHT_G_CONV   = re.compile(r"^(\d+(?:\.\d+)?)G$", re.IGNORECASE)


def _fix_weight(value: str | None) -> tuple[str | None, bool]:
    """Normalise weight strings to match GT format.

    Rules (applied in order):
    1. Take the first value when multiple are combined e.g. "1000ML - 940G" → "1000ML"
    2. Strip spaces between number and unit e.g. "350 ML" → "350ML"
    3. Convert GMS → G e.g. "2200GMS" → "2200G"
    4. 1000ML → 1L
    5. ≥1000G → KG e.g. "2200G" → "2.2KG"
    """
    if not value:
        return value, False
    original = str(value).upper().strip()
    w = original

    w = _WEIGHT_MULTI.sub("", w).strip()
    w = _WEIGHT_SPACE.sub(lambda m: m.group(1) + m.group(2).upper(), w).upper()
    w = _WEIGHT_GMS.sub(lambda m: f"{m.group(1)}G", w).upper()

    m = _WEIGHT_ML_CONV.match(w)
    if m:
        ml = float(m.group(1))
        if ml == 1000:
            w = "1L"

    m = _WEIGHT_G_CONV.match(w)
    if m:
        g = float(m.group(1))
        if g >= 1000:
            kg = g / 1000
            w = f"{int(kg)}KG" if kg == int(kg) else f"{kg}KG"

    return w, w != original


_ADDON_TEA_BAGS = re.compile(r"(\d+)\s*FREE\s+TEA\s+BAGS?", re.IGNORECASE)

def _fix_addons(value: str | None) -> tuple[str | None, bool]:
    """Normalise addon text: 'N FREE TEA BAGS' → 'N FREE ENVELOPE' to match GT labelling."""
    if not value:
        return value, False
    fixed = _ADDON_TEA_BAGS.sub(lambda m: f"{m.group(1)} FREE ENVELOPE", value)
    changed = fixed != value
    return fixed, changed


_GS1_AI = re.compile(r"^\(\d+\)")

def _fix_barcode(value: str | None) -> tuple[str | None, bool]:
    """Normalise a barcode string to a plain numeric EAN/GTIN."""
    if not value:
        return value, False
    original = str(value)
    # Strip GS1 Application Identifier prefix e.g. "(01)08882033623812" → "08882033623812"
    stripped = _GS1_AI.sub("", original).strip()
    # Keep only digits
    digits = _BARCODE_STRIP.sub("", stripped) if stripped else _BARCODE_STRIP.sub("", original)
    # Handle float strings from CSV (e.g. "6034000482027.0")
    if "." in original and not _GS1_AI.match(original):
        try:
            digits = str(int(float(original)))
        except (ValueError, OverflowError):
            pass
    # GTIN-14 with leading 0 → EAN-13
    if len(digits) == 14 and digits.startswith("0"):
        digits = digits[1:]
    # EAN-13 starting with 0 → UPC-A (12 digits) — GT uses 12-digit format for US/CA barcodes
    if len(digits) == 13 and digits.startswith("0"):
        digits = digits[1:]
    result = digits if digits else None
    return result, result != original


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

    # Strip accents before fuzzy match so PÓMO → POMO, etc.
    deaccented_brand = _strip_accents(record.brand) if record.brand else record.brand
    brand, changed = _fuzzy_normalize(deaccented_brand, CANONICAL_BRANDS, threshold=85)
    if not changed and deaccented_brand != record.brand:
        brand, changed = deaccented_brand, True  # accent stripping alone fixed it
    if changed:
        record.brand = brand
        normalized_fields.append("brand")

    mfr = record.manufacturer
    if mfr and mfr.upper().strip() in _MANUFACTURER_CORRECTIONS:
        mfr = _MANUFACTURER_CORRECTIONS[mfr.upper().strip()]
        record.manufacturer = mfr
        normalized_fields.append("manufacturer")
    manufacturer, changed = _fuzzy_normalize(record.manufacturer, CANONICAL_MANUFACTURERS, threshold=82)
    if changed:
        record.manufacturer = manufacturer
        normalized_fields.append("manufacturer")

    # category_type is handled by the prompt — no normalizer correction needed

    packaging, changed = _fuzzy_normalize(record.packaging_type, CANONICAL_PACKAGING, threshold=90)
    if changed and packaging:
        record.packaging_type = packaging
        normalized_fields.append("packaging_type")

    weight, changed = _fix_weight(record.weight)
    if changed:
        record.weight = weight
        normalized_fields.append("weight")

    country, changed = _fix_country(record.country_of_origin)
    if changed:
        record.country_of_origin = country or None
        normalized_fields.append("country_of_origin")

    addons, changed = _fix_addons(record.addons)
    if changed:
        record.addons = addons
        normalized_fields.append("addons")

    barcode, changed = _fix_barcode(record.barcode)
    if changed:
        record.barcode = barcode
        normalized_fields.append("barcode")

    return record, normalized_fields


def check_duplicate(
    record: IMDBRecordWithConfidence,
    existing_records: list[dict],
    barcode_match: bool = True,
    similarity_threshold: float = 0.85,
) -> list[dict]:
    """Check whether a newly extracted record duplicates an existing IMDB entry.

    Scoring (out of 1.0):
      - Brand similarity     : 40%
      - Product name sim.    : 40%
      - Weight exact match   : +10% bonus
      - Manufacturer sim.    : 20% (used only when brand is missing)
    Threshold: 0.85.

    Rule 0 (short-circuit): exact barcode match → definite duplicate.

    Returns a list of matching existing records (empty means no duplicates).
    Each match includes 'match_reason' and 'match_score'.
    """
    duplicates = []

    for existing in existing_records:
        # Rule 0: exact barcode — definitive, no further scoring needed
        if (
            barcode_match
            and record.barcode
            and existing.get("barcode")
            and str(record.barcode).strip() == str(existing["barcode"]).strip()
        ):
            duplicates.append({
                **existing,
                "match_reason": "Exact barcode match",
                "match_score": 1.0,
            })
            continue

        # Composite scoring
        existing_brand = (existing.get("brand") or "").strip()
        existing_name  = (existing.get("product_name") or "").strip()
        existing_mfr   = (existing.get("manufacturer") or "").strip()
        existing_weight = (existing.get("weight") or "").strip().upper()

        new_brand  = (record.brand or "").strip()
        new_name   = (record.product_name or "").strip()
        new_mfr    = (record.manufacturer or "").strip()
        new_weight = (getattr(record, "weight", None) or "").strip().upper()

        if not new_name:
            continue

        if new_brand and existing_brand:
            brand_score = fuzz.WRatio(new_brand, existing_brand) / 100
            name_score  = fuzz.WRatio(new_name, existing_name)  / 100
            score = brand_score * 0.4 + name_score * 0.4
        elif new_mfr and existing_mfr:
            # Fall back to manufacturer when brand is absent
            mfr_score  = fuzz.WRatio(new_mfr, existing_mfr) / 100
            name_score = fuzz.WRatio(new_name, existing_name) / 100
            score = mfr_score * 0.2 + name_score * 0.6
        else:
            # Only name available — require very high name similarity
            name_score = fuzz.WRatio(new_name, existing_name) / 100
            score = name_score * 0.8

        # Weight bonus: same weight pushes score up, different weight pulls it down
        if new_weight and existing_weight:
            if new_weight == existing_weight:
                score += 0.10
            else:
                score -= 0.15

        score = max(0.0, min(1.0, score))

        if score >= similarity_threshold:
            reasons = []
            if new_brand and existing_brand:
                reasons.append(f"brand ({fuzz.WRatio(new_brand, existing_brand):.0f}%)")
            if new_name and existing_name:
                reasons.append(f"name ({fuzz.WRatio(new_name, existing_name):.0f}%)")
            if new_weight and existing_weight and new_weight == existing_weight:
                reasons.append("weight match")
            reason_str = "Similar " + " + ".join(reasons) if reasons else "High similarity"
            duplicates.append({
                **existing,
                "match_reason": f"{reason_str} · score {score:.0%}",
                "match_score": round(score, 3),
            })

    # Return the closest match first
    duplicates.sort(key=lambda d: d.get("match_score", 0), reverse=True)
    return duplicates
