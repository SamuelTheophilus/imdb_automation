from pathlib import Path

from nicegui import ui

from backend.pipeline import PipelineResult

# ── Shared per-session state ─────────────────────────────────────────────────
# NiceGUI runs one Python process for all connected clients. Each client gets
# its own AG Grid instance (keyed by NiceGUI client id) but shares the same
# in-memory row list within a session. Rows are hydrated from SQLite on page
# load and kept in sync with the grid via direct mutation.

_grids_by_client: dict[str, object] = {}
_model_by_client: dict[str, str] = {}


def set_grid(grid) -> None:
    client = ui.context.client
    _grids_by_client[client.id] = grid
    client.on_delete(lambda c: _grids_by_client.pop(c.id, None))


def get_grid():
    client = ui.context.client
    return _grids_by_client.get(client.id)


row_data: list[dict] = []

# Callback registered by app.py so components.py can trigger a batch jobs refresh
# without a circular import.
_batch_jobs_refresh_fn = None
_grid_filter_by_client: dict[str, object] = {}


def set_batch_jobs_refresh(fn) -> None:
    global _batch_jobs_refresh_fn
    _batch_jobs_refresh_fn = fn


def refresh_batch_jobs() -> None:
    if _batch_jobs_refresh_fn:
        _batch_jobs_refresh_fn()


def set_grid_source_filter(fn) -> None:
    client = ui.context.client
    _grid_filter_by_client[client.id] = fn
    client.on_delete(lambda c: _grid_filter_by_client.pop(c.id, None))


def get_client_model() -> str:
    """Return the model display name selected by this client, or the env default."""
    from backend.extractor import get_default_display_name
    client = ui.context.client
    return _model_by_client.get(client.id) or get_default_display_name()


def set_client_model(display_name: str) -> None:
    client = ui.context.client
    _model_by_client[client.id] = display_name
    client.on_delete(lambda c: _model_by_client.pop(c.id, None))


def switch_to_batch_view() -> None:
    client = ui.context.client
    fn = _grid_filter_by_client.get(client.id)
    if fn:
        fn("batch")


# ── Field definitions ────────────────────────────────────────────────────────
# FIELDS drives both the AG Grid column list and the review drawer inputs.
# Each tuple is (schema_field_name, display_label).
FIELDS: list[tuple[str, str]] = [
    # Order matches the export sheet columns exactly
    ("product_name",         "Item Name"),
    ("barcode",              "Barcode"),
    ("manufacturer",         "Manufacturer"),
    ("brand",                "Brand"),
    ("weight",               "Weight"),
    ("packaging_type",       "Packaging Type"),
    ("country_of_origin",    "Country"),
    ("variant",              "Variant"),
    ("category_type",        "Type"),
    ("fragrance_flavor",     "Fragrance / Flavor"),
    ("promotional_messages", "Promotion"),
    ("addons",               "Add-ons"),
    ("tagline",              "Tagline"),
    # Not in export sheet — kept for internal reference
    ("segment_type",         "Segment"),
]

# Column order for the dataset submission file — must match the ground truth.
SUBMISSION_COLUMNS: list[str] = [
    "ITEM_NAME",
    "BARCODE",
    "MANUFACTURER",
    "BRAND",
    "WEIGHT",
    "PACKAGING  TYPE",
    "COUNTRY",
    "VARIANT",
    "TYPE",
    "FRAGRANCE_FLAVOR",
    "PROMOTION",
    "ADDONS",
    "TAGLINE",
]


# ── Image URL helper ─────────────────────────────────────────────────────────

def image_to_url(path: str) -> str:
    """Convert an uploaded image path to a browser-accessible URL.

    The app mounts data/uploads at /uploads. Rows carry only the short URL so
    AG Grid click/edit payloads remain small (no base64 embedding).
    Returns an empty string if the file doesn't exist or is outside uploads.
    """
    image_path = Path(path)
    if not image_path.exists():
        return ""
    upload_dir = Path("data/uploads").resolve()
    try:
        return f"/uploads/{image_path.resolve().relative_to(upload_dir)}"
    except ValueError:
        return ""


# ── Row mappers ──────────────────────────────────────────────────────────────

def failed_row(image_path: str, reason: str, idx: int) -> dict:
    """Build a grid row for an image that failed extraction."""
    row: dict = {
        "id":          idx,
        "thumbnail":   image_to_url(image_path),
        "image_path":  image_path,
        "image_paths": [image_path],
        "_status":     "failed",
        "_normalized": "",
        "_low":        "",
        "product_name": f"[Failed] {reason}",
    }
    for key, _ in FIELDS:
        if key not in row:
            row[key] = ""
    return row


def result_to_row(result: PipelineResult, idx: int) -> dict:
    """Map a PipelineResult to an AG Grid row dict."""
    record = result.record

    if result.has_duplicates:
        status = "duplicate"
    elif result.has_low_confidence:
        status = "warn"
    else:
        status = "ok"

    # Build duplicate summary for display
    dupe = result.duplicate_suggestions[0] if result.duplicate_suggestions else None
    dupe_label = ""
    if dupe:
        name = dupe.get("product_name") or dupe.get("brand") or "unknown"
        reason = dupe.get("match_reason", "")
        dupe_label = f"{name} · {reason}" if reason else name

    row: dict = {
        "id":          idx,
        "thumbnail":   image_to_url(result.image_path),
        "image_path":  result.image_path,
        "image_paths": result.image_paths,
        "_status":     status,
        "_normalized": ", ".join(result.normalized_fields) if result.normalized_fields else "",
        "_low":        ", ".join(result.low_confidence_fields) if result.low_confidence_fields else "",
        "_source":     getattr(result, "source", "quick"),
        "_batch_id":   getattr(result, "batch_job_id", "") or "",
        "_dupe_of":    dupe_label,
        "_cost_usd":   getattr(result, "cost_usd", 0.0) or 0.0,
        "_model_used": getattr(result, "model_used", "") or "",
    }

    for key, _ in FIELDS:
        val = getattr(record, key)
        row[key] = str(val.value if hasattr(val, "value") else val) if val is not None else ""

    return row


def db_record_to_row(record: dict, idx: int) -> dict:
    """Map a SQLite extraction row to an AG Grid row dict."""
    import json as _json

    low_fields = record.get("low_confidence_fields_json") or "[]"
    raw_paths  = record.get("image_paths_json")
    image_paths = _json.loads(raw_paths) if raw_paths else [record["image_path"]]

    # Rebuild dupe label from stored duplicate_suggestions_json
    dupe_label = ""
    raw_dupes = record.get("duplicate_suggestions_json") or "[]"
    try:
        dupes = _json.loads(raw_dupes)
        if dupes:
            d = dupes[0]
            name = d.get("product_name") or d.get("brand") or "unknown"
            reason = d.get("match_reason", "")
            dupe_label = f"{name} · {reason}" if reason else name
    except Exception:
        pass

    row: dict = {
        "id":          idx,
        "db_id":       record["id"],
        "thumbnail":   image_to_url(record["image_path"]),
        "image_path":  record["image_path"],
        "image_paths": image_paths,
        "_status":     record["status"],
        "_normalized": "",
        "_low":        low_fields,
        "_source":     record.get("source") or "quick",
        "_batch_id":   str(record.get("batch_job_id") or ""),
        "_dupe_of":    dupe_label,
        "_cost_usd":   record.get("cost_usd") or 0.0,
        "_model_used": record.get("model_used") or "",
        "video_path":  record.get("video_path") or "",
    }
    for key, _ in FIELDS:
        row[key] = record.get(key) or ""
    return row


def row_to_export_dict(row: dict) -> dict:
    """Map an AG Grid row to the dataset submission column format."""
    data = {
        "ITEM_NAME":     row.get("product_name", ""),
        "BARCODE":       row.get("barcode", ""),
        "MANUFACTURER":  row.get("manufacturer", ""),
        "BRAND":         row.get("brand", ""),
        "WEIGHT":        (row.get("weight", "") or "").upper(),
        "PACKAGING  TYPE": (row.get("packaging_type", "") or "").upper(),
        "COUNTRY":       row.get("country_of_origin", ""),
        "VARIANT":       row.get("variant", ""),
        "TYPE":          row.get("category_type", ""),
        "FRAGRANCE_FLAVOR": row.get("fragrance_flavor", ""),
        "PROMOTION":     row.get("promotional_messages", ""),
        "ADDONS":        row.get("addons", ""),
        "TAGLINE":       row.get("tagline", ""),
    }
    return {column: data[column] for column in SUBMISSION_COLUMNS}


# ── AG Grid helpers ──────────────────────────────────────────────────────────

def cell_renderer(renderer_type: str) -> str:
    """Return the JavaScript cellRenderer string for a given column type."""
    match renderer_type:
        case "review":
            return """
                function(p) {
                    if (p.data && p.data._status === 'failed') {
                        return `
                            <div style="display:flex;align-items:center;height:100%">
                                <span style="
                                    color:#64748b;font-size:11px;font-weight:500;
                                    font-family:Inter,sans-serif;cursor:pointer;
                                    padding:3px 8px;border:1px solid rgba(100,116,139,0.3);
                                    border-radius:6px;opacity:0.85;transition:opacity 0.15s"
                                    onmouseover="this.style.opacity='1'"
                                    onmouseout="this.style.opacity='0.85'"
                                >Keep / Discard</span>
                            </div>
                        `;
                    }
                    return `
                        <div style="display:flex;align-items:center;height:100%">
                            <span
                                style="
                                    color:#7480e0;
                                    font-size:12px;
                                    font-weight:500;
                                    font-family:Inter,sans-serif;
                                    cursor:pointer;
                                    letter-spacing:0.1px;
                                    padding:4px 2px;
                                    opacity:0.75;
                                    transition:opacity 0.15s;
                                "
                                onmouseover="this.style.opacity='1'"
                                onmouseout="this.style.opacity='0.75'"
                            >Review</span>
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
                    const cfg = {
                        ok:        { c:'#10b981', t:'OK' },
                        warn:      { c:'#f59e0b', t:'Needs review' },
                        duplicate: { c:'#ef4444', t:'Duplicate' },
                        failed:    { c:'#ef4444', t:'No product detected' },
                    };
                    const { c, t } = cfg[p.value] || { c:'#64748b', t: p.value };
                    if (p.value === 'failed') {
                        return `
                            <div style="display:flex;flex-direction:column;justify-content:center;height:100%;gap:2px">
                                <div style="display:flex;align-items:center;gap:6px">
                                    <span style="width:6px;height:6px;border-radius:50%;background:${c};display:inline-block;flex-shrink:0"></span>
                                    <span style="font-size:12px;color:#ef4444;font-family:Inter,sans-serif;font-weight:500">Extraction failed</span>
                                </div>
                                <span style="font-size:10px;color:#475569;font-family:Inter,sans-serif;padding-left:12px">No product detected</span>
                            </div>
                        `;
                    }
                    if (p.value === 'duplicate' && p.data && p.data._dupe_of) {
                        return `
                            <div style="display:flex;flex-direction:column;justify-content:center;height:100%;gap:2px">
                                <div style="display:flex;align-items:center;gap:6px">
                                    <span style="width:6px;height:6px;border-radius:50%;background:${c};display:inline-block;flex-shrink:0"></span>
                                    <span style="font-size:12px;color:#ef4444;font-family:Inter,sans-serif;font-weight:500">Duplicate</span>
                                </div>
                                <span title="${p.data._dupe_of}" style="font-size:10px;color:#64748b;font-family:Inter,sans-serif;
                                    padding-left:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px">
                                    of: ${p.data._dupe_of}
                                </span>
                            </div>
                        `;
                    }
                    return `
                        <div style="display:flex;align-items:center;height:100%;gap:8px">
                            <span style="
                                width:6px; height:6px; border-radius:50%;
                                background:${c}; display:inline-block; flex-shrink:0;
                            "></span>
                            <span style="
                                font-size:12px; color:#8492a6;
                                font-family:Inter,sans-serif;
                            ">${t}</span>
                        </div>
                    `;
                }
            """

        case _:
            return ""


def build_column_defs() -> list[dict]:
    """Build the AG Grid column definition list from FIELDS."""
    cols = [
        {
            "headerName": "",
            "field": "_review",
            "width": 110, "minWidth": 110, "maxWidth": 110,
            "resizable": False, "editable": False, "sortable": False, "filter": False,
            "pinned": "left",
            ":cellRenderer": cell_renderer("review"),
        },
        {
            "headerName": "Batch",
            "field": "_batch_id",
            "width": 90, "minWidth": 90, "maxWidth": 90,
            "resizable": False, "editable": False, "sortable": False, "filter": False,
            "pinned": "left",
            "headerTooltip": "Batch job ID",
            ":cellRenderer": """function(p) {
                if (!p.value) return '';
                return '<span title="Batch job #' + p.value + '" style="'
                    + 'font-size:10px;color:#475569;font-family:DM Mono,monospace;'
                    + 'background:rgba(99,102,241,0.08);border-radius:4px;padding:2px 6px">'
                    + '#' + p.value + '</span>';
            }""",
        },
        {
            "headerName": "Cost",
            "field": "_cost_usd",
            "width": 80, "minWidth": 80, "maxWidth": 80,
            "resizable": False, "editable": False, "sortable": True, "filter": False,
            "pinned": "left",
            "headerTooltip": "Estimated API cost for this extraction",
            ":cellRenderer": """function(p) {
                var v = parseFloat(p.value) || 0;
                if (v === 0) return '<span style="color:#475569;font-size:10px;font-family:DM Mono,monospace">--</span>';
                var s = v < 0.001 ? v.toFixed(6) : v.toFixed(4);
                return '<span title="Model: ' + (p.data._model_used || 'unknown') + '" style="'
                    + 'font-size:10px;color:#10b981;font-family:DM Mono,monospace">$' + s + '</span>';
            }""",
        },
        {
            "headerName": "Image",
            "field": "thumbnail",
            "width": 90, "minWidth": 90, "maxWidth": 90,
            "resizable": False, "editable": False, "sortable": False, "filter": False,
            "pinned": "left",
            ":cellRenderer": cell_renderer("image"),
        },
        {
            "headerName": "Status",
            "field": "_status",
            "width": 180, "minWidth": 180, "maxWidth": 180,
            "resizable": False, "editable": False, "sortable": False, "filter": False,
            "pinned": "left",
            ":cellRenderer": cell_renderer("status"),
        },
    ]

    for key, label in FIELDS:
        col: dict = {
            "headerName": label,
            "headerTooltip": label,
            "field": key,
            "editable": True,
            "flex": 1,
            "minWidth": 120,
            "filter": "agTextColumnFilter",
            "tooltipField": key,
            # Highlight cells whose field name appears in the _low confidence list
            "cellClassRules": {
                "cell-warn": f"data._low && data._low.includes('{key}')",
            },
        }
        if key in ("product_name", "promotional_messages", "tagline", "addons"):
            col["minWidth"] = 200
        if key in ("barcode", "weight"):
            col[":cellStyle"] = "{'fontFamily': 'DM Mono, monospace', 'fontSize': '11px'}"

        cols.append(col)

    return cols
