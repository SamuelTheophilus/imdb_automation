"""Field-level accuracy metrics for the IMDB extraction pipeline."""

from rapidfuzz import fuzz

# Ground truth column → pipeline field
GT_FIELD_MAP = {
    "ITEM_NAME":       "product_name",
    "BARCODE":         "barcode",
    "MANUFACTURER":    "manufacturer",
    "BRAND":           "brand",
    "WEIGHT":          "weight",
    "PACKAGING  TYPE": "packaging_type",
    "COUNTRY":         "country_of_origin",
    "VARIANT":         "variant",
    "TYPE":            "category_type",
    "FRAGRANCE_FLAVOR": "fragrance_flavor",
    "PROMOTION":       "promotional_messages",
    "ADDONS":          "addons",
    "TAGLINE":         "tagline",
}

FUZZY_THRESHOLD = 80  # WRatio score (0–100)


def _norm(v) -> str:
    """Normalise a value to uppercase string, empty string if missing."""
    if v is None:
        return ""
    s = str(v).strip().upper()
    if s in ("NAN", "NONE", ""):
        return ""
    # pandas reads integer CSV columns as floats (e.g. "6034000482027.0") — strip the .0
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


def _is_correct(pred: str, gt: str, field: str) -> bool:
    if not gt:
        return not pred   # both empty = correct
    if not pred:
        return False
    if field == "barcode":
        return pred == gt
    return fuzz.WRatio(pred, gt) >= FUZZY_THRESHOLD


def match_to_gt(pred: dict, gt_rows: list[dict]) -> dict | None:
    """Return the best-matching ground truth row for a prediction."""
    pred_barcode = _norm(pred.get("barcode"))

    # 1. exact barcode
    if pred_barcode:
        for row in gt_rows:
            if _norm(row.get("barcode")) == pred_barcode:
                return row

    # 2. brand + product_name fuzzy
    pred_brand = _norm(pred.get("brand"))
    pred_name  = _norm(pred.get("product_name"))
    if pred_brand and pred_name:
        best_score, best_row = 0.0, None
        for row in gt_rows:
            gt_brand = _norm(row.get("brand"))
            gt_name  = _norm(row.get("item_name"))
            if not gt_brand:
                continue
            score = (fuzz.WRatio(pred_brand, gt_brand) + fuzz.WRatio(pred_name, gt_name)) / 2
            if score > best_score:
                best_score, best_row = score, row
        if best_score >= FUZZY_THRESHOLD:
            return best_row

    return None


def score_pair(pred: dict, gt: dict) -> dict[str, bool]:
    """Return a per-field correct/incorrect dict for one matched pair."""
    results = {}
    for gt_col, pred_field in GT_FIELD_MAP.items():
        gt_val   = _norm(gt.get(gt_col.lower().replace("  ", "_").replace(" ", "_")))
        pred_val = _norm(pred.get(pred_field))
        results[pred_field] = _is_correct(pred_val, gt_val, pred_field)
    return results


def compute_report(pairs: list[tuple[dict, dict]]) -> dict:
    """
    pairs: list of (prediction_dict, gt_dict) matched pairs.
    Returns per-field accuracy dict + overall score.
    """
    field_names = list(GT_FIELD_MAP.values())
    counts = {f: {"correct": 0, "total": 0} for f in field_names}

    for pred, gt in pairs:
        scores = score_pair(pred, gt)
        for field, correct in scores.items():
            gt_col = next(k for k, v in GT_FIELD_MAP.items() if v == field)
            gt_val = _norm(gt.get(gt_col.lower().replace("  ", "_").replace(" ", "_")))
            # only count fields where GT has a value
            if gt_val:
                counts[field]["total"] += 1
                if correct:
                    counts[field]["correct"] += 1

    report = {}
    for field in field_names:
        total   = counts[field]["total"]
        correct = counts[field]["correct"]
        report[field] = {
            "correct": correct,
            "total":   total,
            "accuracy": round(correct / total, 3) if total else None,
        }

    all_correct = sum(v["correct"] for v in report.values())
    all_total   = sum(v["total"]   for v in report.values())
    report["__overall__"] = {
        "correct": all_correct,
        "total":   all_total,
        "accuracy": round(all_correct / all_total, 3) if all_total else None,
    }
    return report
