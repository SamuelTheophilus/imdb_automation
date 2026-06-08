from pathlib import Path

from nicegui import ui

from backend.pipeline import PipelineResult

# ── shared state ───────────────────────────────────────────────────────────
_grids_by_client: dict[str, object] = {}


def set_grid(grid) -> None:
    client = ui.context.client
    _grids_by_client[client.id] = grid
    client.on_delete(
        lambda deleted_client: _grids_by_client.pop(deleted_client.id, None)
    )


def get_grid():
    client = ui.context.client
    return _grids_by_client.get(client.id)


row_data: list[dict] = []

# ── constants ──────────────────────────────────────────────────────────────
FIELDS: list[tuple[str, str]] = [
    ("barcode", "Barcode"),
    ("category_type", "Type"),
    ("manufacturer", "Manufacturer"),
    ("brand", "Brand"),
    ("product_name", "Product Name"),
    ("weight", "Weight"),
    ("unit", "Unit"),
    ("packaging_type", "Packaging"),
    ("country_of_origin", "Country"),
    ("variant", "Variant"),
    ("fragrance_flavor", "Fragrance / Flavor"),
    ("promotional_messages", "Promotion"),
    ("addons", "Add-ons"),
    ("tagline", "Tagline"),
    ("segment_type", "Segment"),
]

SUBMISSION_COLUMNS: list[str] = [
    "ITEM_NAME",
    "BARCODE",
    "MANUFACTURER",
    "BRAND",
    "WEIGHT",
    "PACKAGING TYPE",
    "COUNTRY",
    "VARIANT",
    "TYPE",
    "FRAGRANCE_FLAVOR",
    "PROMOTION",
    "ADDONS",
    "TAGLINE",
]


# ── helpers ────────────────────────────────────────────────────────────────
def image_to_url(path: str) -> str:
    """Return a browser URL for an uploaded image without embedding the bytes.

    The previous implementation stored base64 image data directly in each AG
    Grid row. That made row click events enormous because AG Grid sends the
    whole row back to Python. Instead, `frontend.app` exposes `data/uploads` at
    `/uploads`, and rows carry only a short URL such as `/uploads/file.png`.
    """
    image_path = Path(path)
    if not image_path.exists():
        return ""
    try:
        return f"/uploads/{image_path.relative_to(Path('data/uploads'))}"
    except ValueError:
        return str(image_path)


def result_to_row(result: PipelineResult, idx: int) -> dict:
    record = result.record

    if result.has_duplicates:
        status = "duplicate"
    elif result.has_low_confidence:
        status = "warn"
    else:
        status = "ok"

    row: dict = {
        "id": idx,
        "thumbnail": image_to_url(result.image_path),
        "image_path": result.image_path,
        "image_paths": result.image_paths,
        "_status": status,
        "_normalized": ", ".join(result.normalized_fields)
        if result.normalized_fields
        else "",
        "_low": ", ".join(result.low_confidence_fields)
        if result.low_confidence_fields
        else "",
    }

    for key, _ in FIELDS:
        val = getattr(record, key)
        row[key] = (
            str(val.value if hasattr(val, "value") else val) if val is not None else ""
        )

    return row


def db_record_to_row(record: dict, idx: int) -> dict:
    """Build one AG Grid row from a saved SQLite extraction.

    The grid carries a few UI-only keys (`thumbnail`, `_status`, `_low`) beside
    the editable product fields. Export and database updates strip those keys.
    """
    import json as _json

    low_fields = record.get("low_confidence_fields_json") or "[]"
    raw_paths = record.get("image_paths_json")
    if raw_paths:
        image_paths = _json.loads(raw_paths)
    else:
        image_paths = [record["image_path"]]

    row: dict = {
        "id": idx,
        "db_id": record["id"],
        "thumbnail": image_to_url(record["image_path"]),
        "image_path": record["image_path"],
        "image_paths": image_paths,
        "_status": record["status"],
        "_normalized": "",
        "_low": low_fields,
    }
    for key, _ in FIELDS:
        row[key] = record.get(key) or ""
    return row


def _format_submission_weight(weight: str, unit: str) -> str:
    weight = (weight or "").strip()
    unit = (unit or "").strip().upper()
    if not weight:
        return ""
    if any(char.isalpha() for char in weight):
        return weight.upper()
    if not unit:
        return weight
    if unit == "G":
        return f"{weight}{unit}"
    return f"{weight} {unit}"


def row_to_export_dict(row: dict) -> dict:
    """Return one row in the dataset submission format."""
    data = {
        "ITEM_NAME": row.get("product_name", ""),
        "BARCODE": row.get("barcode", ""),
        "MANUFACTURER": row.get("manufacturer", ""),
        "BRAND": row.get("brand", ""),
        "WEIGHT": _format_submission_weight(row.get("weight", ""), row.get("unit", "")),
        "PACKAGING TYPE": (row.get("packaging_type", "") or "").upper(),
        "COUNTRY": row.get("country_of_origin", ""),
        "VARIANT": row.get("variant", ""),
        "TYPE": row.get("category_type", ""),
        "FRAGRANCE_FLAVOR": row.get("fragrance_flavor", ""),
        "PROMOTION": row.get("promotional_messages", ""),
        "ADDONS": row.get("addons", ""),
        "TAGLINE": row.get("tagline", ""),
    }
    return {column: data[column] for column in SUBMISSION_COLUMNS}


def cell_renderer(renderer_type: str) -> str:
    match renderer_type:
        case "review":
            return """
                function() {
                    return `
                        <div style="display:flex;align-items:center;height:100%">
                            <button
                                style="
                                    height:34px;
                                    width:100px;
                                    border:none;
                                    border-radius:10px;
                                    background:linear-gradient(135deg,#6366f1,#4f46e5);
                                    color:white;
                                    font-size:12px;
                                    font-weight:700;
                                    letter-spacing:0.2px;
                                    box-shadow:0 2px 8px rgba(79,70,229,0.35);
                                    cursor:pointer;
                                    transition:all 0.15s ease;
                                "
                                onmouseover="
                                    this.style.transform='translateY(-1px)';
                                    this.style.boxShadow='0 4px 12px rgba(79,70,229,0.45)';
                                "
                                onmouseout="
                                    this.style.transform='translateY(0)';
                                    this.style.boxShadow='0 2px 8px rgba(79,70,229,0.35)';
                                "
                            >
                                Review
                            </button>
                        </div>
                    `;
                }
            """

        case "image":
            return """
                function(p) {
                    return `
                        <div style="
                            display:flex;
                            align-items:center;
                            justify-content:center;
                            height:100%;
                        ">
                            <img
                                src="${p.value}"
                                style="
                                    height:46px;
                                    width:46px;
                                    object-fit:cover;
                                    border-radius:10px;
                                    border:1px solid rgba(255,255,255,0.08);
                                    box-shadow:0 2px 6px rgba(0,0,0,0.25);
                                "
                            />
                        </div>
                    `;
                }
            """

        case "status":
            return """
                function(p) {
                    const map = {
                        ok: ['#10b981', 'High confidence'],
                        warn: ['#f59e0b', 'Needs review'],
                        duplicate: ['#ef4444', 'Duplicate'],
                    };

                    const [color, label] = map[p.value] || ['#888', p.value];

                    return `
                        <div style="
                            display:flex;
                            align-items:center;
                            height:100%;
                        ">
                            <div style="
                                display:inline-flex;
                                align-items:center;
                                gap:8px;
                                padding:6px 10px;
                                border-radius:999px;
                                background:rgba(255,255,255,0.04);
                                border:1px solid rgba(255,255,255,0.06);
                                font-size:12px;
                                font-weight:600;
                                color:#e5e7eb;
                            ">
                                <span style="
                                    width:9px;
                                    height:9px;
                                    border-radius:999px;
                                    background:${color};
                                    box-shadow:0 0 6px ${color};
                                "></span>

                                ${label}
                            </div>
                        </div>
                    `;
                }
            """

        case _:
            return ""


def build_column_defs() -> list[dict]:
    cols = [
        {
            "headerName": "Review",
            "field": "_review",
            "width": 140,
            "minWidth": 140,
            "maxWidth": 140,
            "resizable": False,
            "editable": False,
            "sortable": False,
            "filter": False,
            "pinned": "left",
            ":cellRenderer": cell_renderer("review"),
        },
        {
            "headerName": "Image",
            "field": "thumbnail",
            "width": 90,
            "minWidth": 90,
            "maxWidth": 90,
            "resizable": False,
            "editable": False,
            "sortable": False,
            "filter": False,
            "pinned": "left",
            ":cellRenderer": cell_renderer("image"),
        },
        {
            "headerName": "Status",
            "field": "_status",
            "width": 180,
            "minWidth": 180,
            "maxWidth": 180,
            "resizable": False,
            "editable": False,
            "sortable": False,
            "filter": False,
            "pinned": "left",
            ":cellRenderer": cell_renderer("status"),
        },
    ]

    for key, label in FIELDS:
        col: dict = {
            "headerName": label,
            "field": key,
            "editable": True,
            "flex": 1,
            "minWidth": 120,
            "filter": "agTextColumnFilter",
            "cellClassRules": {
                "cell-warn": f"data._low && data._low.includes('{key}')",
            },
        }
        if key in ("product_name", "promotional_messages", "tagline", "addons"):
            col["minWidth"] = 200
        if key in ("barcode", "weight", "unit"):
            col[":cellStyle"] = (
                "{'fontFamily': 'DM Mono, monospace', 'fontSize': '11px'}"
            )

        cols.append(col)

    return cols
