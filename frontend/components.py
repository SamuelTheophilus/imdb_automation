import asyncio
from pathlib import Path

from nicegui import events, ui

from backend.db import (
    get_extraction_by_id,
    merge_extractions,
    update_extraction_image_paths,
    update_extraction_status,
)
from backend.extractor import MODEL_OPTIONS
from frontend.auth_pages import current_user, logout, render_change_password_dialog
from frontend.tour import TOUR_JS, TOUR_SAMPLE_ROW
from frontend.handlers import (
    QUICK_UPLOAD_LIMIT,
    do_delete_row,
    do_export_csv,
    do_export_excel,
    handle_batch_upload,
    handle_bulk_start,
    persist_row_edits,
    stage_bulk_files,
)

from frontend.state import FIELDS, build_column_defs, get_client_model, get_grid, image_to_url, row_data, set_client_model, set_grid, set_grid_source_filter

review_drawer = None
review_carousel_container = None
review_title = None
review_subtitle = None
review_inputs: dict[str, ui.input] = {}
review_row_id: int | None = None
review_status_btn = None      # toggles between Mark OK / Needs review

# Tracks which carousel slide is currently visible so the "Delete image"
# button always targets the right image regardless of navigation.
_current_slide_index: int = 0
_current_paths: list[str] = []


def _on_slide_change(e) -> None:
    global _current_slide_index
    try:
        _current_slide_index = int(e.value.split("-")[1])
    except Exception:
        pass

# ── Processing toast ─────────────────────────────────────────────────────────
# Injected as plain HTML+JS so ui.run_javascript() reliably updates it from
# any async handler without needing NiceGUI element context.

_PROC_HEAD = """
<style>
  /* ── mobile / tablet wall ───────────────────────────────────────────── */
  #_app_wall {
    display: none;
    position: fixed; inset: 0; z-index: 9999;
    background: #1a1816;
    flex-direction: column; align-items: center; justify-content: center;
    gap: 14px; padding: 40px 28px; text-align: center;
    font-family: Inter, sans-serif;
  }
  #_app_wall ._wall_icon   { opacity: 0.3; color: #f0ebe5; }
  #_app_wall ._wall_title  { margin:0; font-size:18px; font-weight:700; color:#f0ebe5; letter-spacing:-0.4px; }
  #_app_wall ._wall_sub    { margin:0; font-size:13px; color:#52504c; line-height:1.65; max-width:300px; }
  #_app_wall ._wall_cd     { margin:0; font-size:12px; color:#3d5166; font-style:italic; }
  @media (max-width: 1023px) { #_app_wall { display: flex; } }
  /* ── processing toast ──────────────────────────────────────────────── */
  @keyframes _proc_spin { to { transform: rotate(360deg); } }
  #_proc_card {
    display: none;
    position: fixed; bottom: 24px; right: 24px; z-index: 9900;
    background: #1a1a2e;
    border: 1px solid rgba(99,102,241,0.3);
    border-radius: 10px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    padding: 10px 16px;
    align-items: center; gap: 10px;
    font-family: Inter, sans-serif;
  }
  #_proc_spinner {
    width: 14px; height: 14px; flex-shrink: 0;
    border: 2px solid rgba(99,102,241,0.2);
    border-top-color: #6366f1;
    border-radius: 50%;
    animation: _proc_spin 0.7s linear infinite;
  }
  #_proc_label { color: #c4c4d4; font-size: 12px; white-space: nowrap; }
  #_proc_close {
    background: none; border: none; cursor: pointer;
    color: #4b5563; font-size: 13px; line-height: 1;
    padding: 0 2px; margin-left: 2px;
  }
  #_proc_close:hover { color: #9ca3af; }
</style>
<script>
  function _procShow(msg) {
    document.getElementById('_proc_label').textContent = msg;
    document.getElementById('_proc_card').style.display = 'flex';
  }
  function _procHide() {
    document.getElementById('_proc_card').style.display = 'none';
  }
  /* Auto-logout countdown for small screens */
  (function () {
    function _startWallCountdown() {
      var w = document.getElementById('_app_wall');
      if (!w || window.getComputedStyle(w).display === 'none') return;
      var secs = 15;
      var cd = document.getElementById('_wall_cd');
      var t = setInterval(function () {
        secs -= 1;
        if (cd) cd.textContent = 'Signing you out in ' + secs + 's…';
        if (secs <= 0) { clearInterval(t); window.location.href = '/force-logout'; }
      }, 1000);
    }
    /* Delay slightly so CSS media query has been applied before we check display */
    setTimeout(_startWallCountdown, 600);
  })();
</script>
"""

_PROC_DIVS = """
<div id="_proc_card">
  <div id="_proc_spinner"></div>
  <span id="_proc_label">Processing…</span>
  <button id="_proc_close" onclick="_procHide()" title="Dismiss">&#x2715;</button>
</div>
"""

_WALL_DIVS = """
<div id="_app_wall">
  <svg class="_wall_icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
       fill="none" stroke="currentColor" stroke-width="1.5"
       stroke-linecap="round" stroke-linejoin="round"
       width="52" height="52">
    <rect x="2" y="3" width="20" height="13" rx="2"/>
    <line x1="12" y1="16" x2="12" y2="21"/>
    <line x1="8"  y1="21" x2="16" y2="21"/>
  </svg>
  <p class="_wall_title">Best on a larger screen</p>
  <p class="_wall_sub">
    This workspace is designed for wider screens.
    Please open it on a larger display for the best experience.
  </p>
  <p class="_wall_cd" id="_wall_cd">Signing you out in 15s…</p>
</div>
"""


def render_processing_overlay() -> None:
    ui.add_head_html(_PROC_HEAD)
    # add_body_html writes to client._body_html → rendered into initial HTML
    # before #app, bypassing both DOMPurify and WebSocket requirements
    ui.add_body_html(_PROC_DIVS)
    ui.add_body_html(_WALL_DIVS)


def show_processing(message: str = "Processing…") -> None:
    import json
    ui.run_javascript(f"_procShow({json.dumps(message)})")


def hide_processing() -> None:
    ui.run_javascript("_procHide()")


# ── Tour ─────────────────────────────────────────────────────────────────────

def _launch_tour(client=None) -> None:
    """Used by the first-time tour (called from an already-async context)."""
    import asyncio
    if client is None:
        client = ui.context.client
    open_review_drawer(TOUR_SAMPLE_ROW)

    async def _run():
        await asyncio.sleep(0.7)
        client.run_javascript(TOUR_JS)

    asyncio.create_task(_run())


async def _replay_tour() -> None:
    """Replay button handler — async so NiceGUI flushes the drawer before the JS fires."""
    import asyncio
    client = ui.context.client
    open_review_drawer(TOUR_SAMPLE_ROW)
    await asyncio.sleep(0.7)
    client.run_javascript(TOUR_JS)


# ── Header ───────────────────────────────────────────────────────────────────

def render_header():
    with (
        ui.row()
        .classes("w-full items-center justify-between px-8 app-header")
        .style("height: 56px")
    ):
        with ui.row().classes("items-center gap-2"):
            ui.link("⬡", "/").style(
                "color:#6366f1; font-size:1.2rem; text-decoration:none; line-height:1"
            )
            ui.link("IMDB AutoFill", "/").classes("app-logo")
            ui.badge("beta").props("color=indigo outline").classes("text-xs").style(
                "font-size:9px; padding:2px 6px; opacity:0.6"
            )

        with ui.row().classes("items-center gap-0"):
            user = current_user()
            if user:
                ui.label(user["username"]).style(
                    "font-size:12px; color:#334155; padding:0 10px 0 4px"
                )
                ui.separator().props("vertical").style("height:16px; opacity:0.15; margin: 0 4px")
                ui.button(
                    "History", icon="history",
                    on_click=lambda: ui.navigate.to("/history"),
                ).props("flat color=white").classes("text-xs")
                ui.button(
                    "Catalog", icon="auto_awesome",
                    on_click=lambda: ui.navigate.to("/catalog"),
                ).props("flat color=white").classes("text-xs")
                ui.button(icon="help_outline", on_click=_replay_tour).props(
                    "flat round dense color=white"
                ).style("opacity:0.4").tooltip("Take the tour")
            ui.button("Export CSV", icon="download", on_click=do_export_csv).props(
                "flat color=white"
            ).classes("text-xs")
            ui.button("Export Excel", icon="table_chart", on_click=do_export_excel).props(
                "flat color=white"
            ).classes("text-xs")
            ui.separator().props("vertical").style("height:16px; opacity:0.15; margin: 0 4px")
            if user:
                pw_dialog = render_change_password_dialog()
                ui.button(icon="lock_reset", on_click=pw_dialog.open).props(
                    "flat round dense color=white"
                ).style("opacity:0.5").tooltip("Change password")
                ui.separator().props("vertical").style("height:16px; opacity:0.15; margin: 0 4px")
            ui.button("Log out", icon="logout", on_click=logout).props(
                "flat color=white"
            ).classes("text-xs")

    render_processing_overlay()


# ── Review drawer ────────────────────────────────────────────────────────────

def render_review_drawer():
    """Create the right-side row editor. Opens via `open_review_drawer`.

    Carousel at top for all product angles, then grouped field inputs, then
    action buttons. Per-slide delete lets users remove a mis-grouped image.
    """
    global review_drawer, review_carousel_container, review_title, review_subtitle, review_inputs, review_status_btn

    review_inputs = {}
    review_drawer = (
        ui.right_drawer(value=False, fixed=True, bordered=True)
        .classes("p-0")
        .style("width: 560px; background: #1e1c19;")
    )
    with review_drawer:
        # Header strip
        with (
            ui.row()
            .classes("w-full items-start justify-between px-5 pt-5 pb-4")
            .style("border-bottom: 1px solid rgba(240,225,205,0.07)")
        ):
            with ui.column().classes("gap-0.5 flex-1 min-w-0"):
                review_title = ui.label("Review extraction").classes(
                    "text-sm font-semibold"
                ).style("color:#e2e8f0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis")
                review_subtitle = ui.label("").classes("text-xs").style("color:#475569")

            ui.button(icon="close", on_click=review_drawer.hide).props(
                "flat round dense"
            ).style("color:#475569; margin-top:-4px; flex-shrink:0")

        # Carousel — rebuilt each time the drawer opens for a different row
        review_carousel_container = ui.column().classes("w-full p-0 gap-0")

        # Field groups
        with ui.column().classes("w-full gap-0 px-5 pt-3 pb-1").style("overflow-y:auto; flex:1"):

            # Identification
            ui.label("Identification").classes("drawer-section-label")
            with ui.row().classes("w-full gap-2"):
                review_inputs["barcode"] = ui.input("Barcode").classes("flex-1")
                review_inputs["brand"] = ui.input("Brand").classes("flex-1")
            review_inputs["manufacturer"] = ui.input("Manufacturer").classes("w-full")

            ui.separator().classes("my-2 opacity-0")
            ui.label("Product").classes("drawer-section-label")
            review_inputs["product_name"] = ui.input("Product Name").classes("w-full")
            with ui.row().classes("w-full gap-2"):
                review_inputs["weight"] = ui.input("Weight").classes("flex-1")
                review_inputs["category_type"] = ui.input("Category").classes("flex-1")
            review_inputs["segment_type"] = ui.input("Segment").classes("w-full")

            ui.separator().classes("my-2 opacity-0")
            ui.label("Attributes").classes("drawer-section-label")
            with ui.row().classes("w-full gap-2"):
                review_inputs["country_of_origin"] = ui.input("Country").classes("flex-1")
                review_inputs["packaging_type"] = ui.input("Packaging").classes("flex-1")
            with ui.row().classes("w-full gap-2"):
                review_inputs["variant"] = ui.input("Variant").classes("flex-1")
                review_inputs["fragrance_flavor"] = ui.input("Fragrance / Flavor").classes("flex-1")

            ui.separator().classes("my-2 opacity-0")
            ui.label("Marketing").classes("drawer-section-label")
            review_inputs["promotional_messages"] = ui.input("Promotion").classes("w-full")
            review_inputs["addons"] = ui.input("Add-ons").classes("w-full")
            review_inputs["tagline"] = ui.input("Tagline").classes("w-full")

        # Action bar — Delete is quiet left, Save is prominent right
        with (
            ui.row()
            .classes("w-full items-center justify-between px-5")
            .style("border-top:1px solid rgba(240,225,205,0.07); min-height:56px; flex-shrink:0")
        ):
            ui.button(icon="delete_outline", on_click=delete_from_review_drawer).props(
                "flat round dense color=grey-7"
            )
            with ui.row().classes("gap-2 items-center"):
                review_status_btn = ui.button(
                    "Mark OK", on_click=toggle_status_from_drawer
                ).props("flat dense color=positive").style(
                    "font-size:12px; font-weight:500; padding:0 12px"
                )
                ui.button("Save", on_click=save_review_drawer).props(
                    "unelevated dense color=indigo-5"
                ).style("font-size:12px; font-weight:600; padding:0 18px; min-width:80px; height:34px")


def _sync_status_btn(status: str) -> None:
    """Update the status toggle button to reflect the row's current status."""
    if review_status_btn is None:
        return
    if status == "ok":
        review_status_btn.text = "Needs review"
        review_status_btn.props("flat dense color=warning")
    else:
        review_status_btn.text = "Mark OK"
        review_status_btn.props("flat dense color=positive")


def toggle_status_from_drawer() -> None:
    """Toggle the current row between 'ok' and 'warn' (needs review)."""
    global review_row_id
    if review_row_id is None:
        return

    for row in row_data:
        if row["id"] != review_row_id:
            continue

        new_status = "warn" if row.get("_status") == "ok" else "ok"
        row["_status"] = new_status

        db_id = row.get("db_id")
        if db_id:
            update_extraction_status(int(db_id), new_status)

        _sync_status_btn(new_status)

        grid = get_grid()
        if grid:
            grid.options["rowData"] = list(row_data)
            grid.update()

        label = "OK" if new_status == "ok" else "Needs review"
        ui.notify(f"Status set to {label}", type="positive", position="center")
        return


async def delete_current_image() -> None:
    """Delete whichever carousel slide is currently visible, after confirmation."""
    if not _current_paths:
        return

    if len(_current_paths) <= 1:
        ui.notify("Cannot remove the only image", type="warning", position="center")
        return

    idx = min(_current_slide_index, len(_current_paths) - 1)
    path_to_remove = _current_paths[idx]
    slide_num = idx + 1
    total = len(_current_paths)

    with ui.dialog() as dialog, ui.card().classes("p-6 gap-3"):
        ui.label("Remove this image?").classes("text-sm font-medium")
        ui.label(f"Image {slide_num} of {total} will be permanently deleted.").classes(
            "text-xs"
        ).style("color:#64748b")
        with ui.row().classes("justify-end gap-2 w-full pt-2"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props(
                "flat color=white"
            ).classes("text-xs")
            ui.button("Delete", on_click=lambda: dialog.submit(True)).props(
                "unelevated color=red"
            ).classes("text-xs")

    result = await dialog
    if not result:
        return

    # Run deletion after the dialog context is fully torn down.
    # Calling ui.notify inside a destroyed dialog slot raises RuntimeError,
    # so we do the work here rather than inside _remove_image_from_group.
    _remove_image_from_group(path_to_remove)
    try:
        ui.notify("Image removed", type="info", position="center")
    except RuntimeError:
        pass


def _open_merge_dialog(current_row: dict, matched_db_id: int) -> None:
    """Open the Golden Record merge dialog for two duplicate records.

    Fetches both records fresh from the DB, shows a side-by-side field
    comparison with auto-suggested winners (highest confidence wins), lets
    the user override per field, then merges on confirmation.
    """
    import json as _json
    from frontend.state import FIELDS, get_grid, row_data

    primary_id = int(current_row["db_id"])
    primary_rec = get_extraction_by_id(primary_id)
    matched_rec = get_extraction_by_id(matched_db_id)

    if not primary_rec or not matched_rec:
        ui.notify("One of the records no longer exists.", type="warning", position="center")
        return

    primary_conf = _json.loads(primary_rec.get("confidence_json") or "{}")
    matched_conf = _json.loads(matched_rec.get("confidence_json") or "{}")

    # For each field decide which record's value is the auto-suggested winner.
    # Rule: non-empty beats empty; when both non-empty, higher confidence wins.
    selections: dict[str, str] = {}  # field_key -> "primary" | "matched"
    for key, _ in FIELDS:
        pv = (primary_rec.get(key) or "").strip()
        mv = (matched_rec.get(key) or "").strip()
        pc = primary_conf.get(key, 0.0)
        mc = matched_conf.get(key, 0.0)
        if pv and not mv:
            selections[key] = "primary"
        elif mv and not pv:
            selections[key] = "matched"
        elif pc >= mc:
            selections[key] = "primary"
        else:
            selections[key] = "matched"

    # ── UI ────────────────────────────────────────────────────────────────────
    _CARD  = "background:#0d1117; border:1px solid rgba(255,255,255,0.08); border-radius:14px; padding:0; overflow:hidden; min-width:680px; max-width:780px;"
    _HDR   = "border-bottom:1px solid rgba(255,255,255,0.06);"
    _LABEL = "font-size:10px; font-weight:600; color:#475569; font-family:Inter,sans-serif; letter-spacing:0.5px; text-transform:uppercase;"
    _CONF  = "font-size:10px; font-family:Inter,sans-serif; border-radius:4px; padding:1px 5px;"

    def _conf_badge(score: float) -> str:
        if score >= 0.8:
            color = "rgba(16,185,129,0.15)"; text = "#10b981"
        elif score >= 0.6:
            color = "rgba(245,158,11,0.15)"; text = "#f59e0b"
        else:
            color = "rgba(100,116,139,0.15)"; text = "#64748b"
        return (
            f'<span style="background:{color}; color:{text}; {_CONF}">'
            f'{score:.0%}</span>'
        )

    with ui.dialog() as dlg, ui.card().style(_CARD):
        # Header
        with ui.row().classes("w-full items-center justify-between px-5 py-4").style(_HDR):
            with ui.column().classes("gap-0"):
                ui.label("Merge Duplicate Records").style(
                    "font-size:15px; font-weight:700; color:#e2e8f0;"
                    "font-family:Inter,sans-serif;"
                )
                ui.label("Pick the best value for each field. Auto-suggestions are pre-selected.").style(
                    "font-size:12px; color:#475569; font-family:Inter,sans-serif;"
                )
            ui.button(icon="close", on_click=dlg.close).props("flat round dense").style("color:#475569;")

        # Column headers
        with ui.row().classes("w-full px-5 py-2 gap-0").style(
            "background:rgba(255,255,255,0.02); border-bottom:1px solid rgba(255,255,255,0.04);"
        ):
            ui.label("Field").style(_LABEL + " flex:0 0 140px;")
            ui.label("This record").style(_LABEL + " flex:1;")
            ui.label("Matched record").style(_LABEL + " flex:1;")

        # Scrollable field rows
        with ui.scroll_area().style("max-height:380px; width:100%;"):
            cell_states: dict[str, dict] = {}  # key -> {primary_el, matched_el}

            for key, label in FIELDS:
                pv = (primary_rec.get(key) or "").strip()
                mv = (matched_rec.get(key) or "").strip()
                if not pv and not mv:
                    continue  # skip entirely empty fields

                pc = primary_conf.get(key, 0.0)
                mc = matched_conf.get(key, 0.0)

                _SEL   = "border:1px solid rgba(99,102,241,0.5); background:rgba(99,102,241,0.08); border-radius:8px; cursor:pointer;"
                _UNSEL = "border:1px solid transparent; background:transparent; border-radius:8px; cursor:pointer; opacity:0.55;"

                p_init = _SEL if selections[key] == "primary" else _UNSEL
                m_init = _SEL if selections[key] == "matched" else _UNSEL

                with ui.row().classes("w-full items-start px-5 py-2 gap-0").style(
                    "border-bottom:1px solid rgba(255,255,255,0.03);"
                ):
                    ui.label(label).style(
                        "font-size:11px; color:#64748b; font-family:Inter,sans-serif;"
                        "font-weight:500; flex:0 0 140px; padding-top:8px;"
                    )

                    p_cell = ui.column().classes("gap-1 flex-1 p-2").style(p_init)
                    with p_cell:
                        ui.html(
                            f'<span style="font-size:13px; color:#e2e8f0; font-family:Inter,sans-serif;">'
                            f'{pv or "<em style=\'color:#334155\'>empty</em>"}</span>'
                            + (f' {_conf_badge(pc)}' if pv else '')
                        )

                    ui.element("div").style("flex:0 0 8px;")

                    m_cell = ui.column().classes("gap-1 flex-1 p-2").style(m_init)
                    with m_cell:
                        ui.html(
                            f'<span style="font-size:13px; color:#e2e8f0; font-family:Inter,sans-serif;">'
                            f'{mv or "<em style=\'color:#334155\'>empty</em>"}</span>'
                            + (f' {_conf_badge(mc)}' if mv else '')
                        )

                    cell_states[key] = {"p": p_cell, "m": m_cell}

                    # Wire click handlers after both cells exist
                    def _select_primary(k=key):
                        selections[k] = "primary"
                        cell_states[k]["p"].style(_SEL)
                        cell_states[k]["m"].style(_UNSEL)

                    def _select_matched(k=key):
                        selections[k] = "matched"
                        cell_states[k]["p"].style(_UNSEL)
                        cell_states[k]["m"].style(_SEL)

                    p_cell.on("click", _select_primary)
                    m_cell.on("click", _select_matched)

        # Footer
        with ui.row().classes("w-full items-center justify-between px-5 py-4").style(
            "border-top:1px solid rgba(255,255,255,0.06);"
        ):
            ui.label(
                f"This record · {primary_rec.get('original_filename', '')}"
            ).style("font-size:11px; color:#334155; font-family:Inter,sans-serif; flex:1;")

            with ui.row().classes("gap-3"):
                ui.button("Cancel", on_click=dlg.close).props("flat").style(
                    "color:#475569; font-family:Inter,sans-serif;"
                )

                def _do_merge():
                    merged = {}
                    for key, _ in FIELDS:
                        rec = primary_rec if selections[key] == "primary" else matched_rec
                        merged[key] = (rec.get(key) or "").strip() or None

                    merge_extractions(primary_id, matched_db_id, merged)

                    # Update in-memory grid state
                    for r in row_data:
                        if r.get("db_id") == primary_id:
                            for k, _ in FIELDS:
                                r[k] = merged.get(k) or ""
                            r["_status"] = "warn"
                            r["_dupe_of"] = ""
                            r["_dupe_id"] = None
                    row_data[:] = [r for r in row_data if r.get("db_id") != matched_db_id]

                    grid = get_grid()
                    if grid:
                        grid.options["rowData"] = list(row_data)
                        grid.update()

                    dlg.close()
                    if review_drawer:
                        review_drawer.hide()
                    ui.notify("Records merged into one verified product.", type="positive", position="center")

                ui.button("Merge into one record", icon="merge", on_click=_do_merge).props(
                    "unelevated"
                ).style(
                    "background:#6366f1; color:#fff; font-family:Inter,sans-serif;"
                    "font-size:13px; font-weight:600; border-radius:8px; padding:6px 18px;"
                )

    dlg.open()


def open_review_drawer(row: dict):
    """Populate and open the drawer for the clicked grid row."""
    global review_row_id
    if review_drawer is None:
        return

    review_row_id = row["id"]

    product = row.get("product_name") or "Review extraction"
    brand = row.get("brand", "")
    weight = row.get("weight", "")

    if review_title is not None:
        review_title.text = product
    if review_subtitle is not None:
        parts = [p for p in [brand, weight] if p]
        review_subtitle.text = " · ".join(parts) if parts else ""

    if review_carousel_container is not None:
        try:
            paths: list[str] = row.get("image_paths") or [row.get("image_path", "")]
            urls = [image_to_url(p) for p in paths if p]

            # Update module-level state so delete_current_image knows what to target
            global _current_slide_index, _current_paths
            _current_slide_index = 0
            _current_paths = paths

            review_carousel_container.clear()
            with review_carousel_container:
                # ── Main image / carousel ────────────────────────────────────
                if len(urls) > 1:
                    with (
                        ui.carousel(
                            animated=True, arrows=True, navigation=True,
                            value="slide-0",
                            on_value_change=_on_slide_change,
                        )
                        .props("infinite")
                        .classes("w-full")
                        .style("height:280px; background:#060a12;")
                    ):
                        for i, url in enumerate(urls):
                            with ui.carousel_slide(name=f"slide-{i}").classes(
                                "p-0 flex items-center justify-center"
                            ):
                                ui.image(url).style(
                                    "max-height:280px; max-width:100%; object-fit:contain;"
                                )
                else:
                    url = urls[0] if urls else ""
                    ui.image(url).classes("w-full").style(
                        "max-height:280px; object-fit:contain;"
                        " background:#060a12; display:block;"
                    )

                # ── Single delete button, only shown when removals are possible
                if len(paths) > 1:
                    with ui.row().classes("w-full items-center justify-end px-4 py-2").style(
                        "border-top:1px solid rgba(255,255,255,0.05);"
                        "background:#060a12;"
                    ):
                        ui.label(f"{len(paths)} images").style(
                            "font-size:11px; color:#334155; font-family:Inter,sans-serif; flex:1"
                        )
                        ui.button(
                            "Delete this image", icon="delete_outline",
                            on_click=delete_current_image,
                        ).props("flat dense").style(
                            "font-size:11px; color:rgba(239,68,68,0.65);"
                            "font-family:Inter,sans-serif"
                        )

        except Exception as exc:
            print(f"[components] carousel build error: {exc}")

        # ── Video button — opens a modal player for video-sourced rows ──────────
        video_path_raw = row.get("video_path") or ""
        if video_path_raw:
            video_url = (
                image_to_url(video_path_raw)
                if not video_path_raw.startswith("/")
                else video_path_raw
            )
            if video_url:
                with review_carousel_container:
                    with ui.row().classes("w-full px-4 py-3").style(
                        "border-top:1px solid rgba(255,255,255,0.05); background:#060a12;"
                    ):
                        def _open_video_modal(url=video_url):
                            with ui.dialog() as dlg, ui.card().style(
                                "background:#0d1117; border:1px solid rgba(255,255,255,0.08);"
                                "border-radius:14px; padding:0; overflow:hidden; min-width:520px;"
                            ):
                                with ui.row().classes("w-full items-center justify-between px-4 py-3").style(
                                    "border-bottom:1px solid rgba(255,255,255,0.06);"
                                ):
                                    ui.label("Source video").style(
                                        "font-size:13px; font-weight:600; color:#e2e8f0;"
                                        "font-family:Inter,sans-serif;"
                                    )
                                    ui.button(icon="close", on_click=dlg.close).props(
                                        "flat round dense"
                                    ).style("color:#64748b;")
                                ui.html(
                                    f'<div style="display:flex; justify-content:center;'
                                    f' align-items:center; background:#000; padding:12px;">'
                                    f'<video controls autoplay preload="auto"'
                                    f' style="max-width:100%; max-height:420px;">'
                                    f'<source src="{url}">'
                                    f'</video>'
                                    f'</div>'
                                )
                            dlg.open()

                        ui.button(
                            "View video", icon="play_circle_outline",
                            on_click=_open_video_modal,
                        ).props("flat dense").style(
                            "font-size:12px; font-weight:500; color:#818cf8;"
                            "font-family:Inter,sans-serif; padding:0;"
                        )

    for key, _ in FIELDS:
        review_inputs[key].value = row.get(key, "")

    # ── Barcode Trust panel ──────────────────────────────────────────────────
    if review_carousel_container is not None:
        audit = row.get("_barcode_audit")
        if audit and audit.get("decision") != "none":
            _DECISION_LABELS = {
                "both_agree":              "Both sources agree",
                "pipeline_wins":           "Pixel scan wins (VLM failed checksum)",
                "vlm_wins":                "VLM read wins (scan failed checksum)",
                "both_valid_pipeline_primary": "Both valid -- scan preferred",
                "pipeline_only":           "Scan only (no VLM digit read)",
                "vlm_only":                "VLM only (scan found nothing)",
            }
            decision_label = _DECISION_LABELS.get(audit.get("decision", ""), audit.get("decision", ""))

            def _check_icon(ok: bool | None) -> str:
                if ok is True:
                    return '<span style="color:#22c55e; font-size:11px;">checksum ok</span>'
                if ok is False:
                    return '<span style="color:#ef4444; font-size:11px;">checksum fail</span>'
                return '<span style="color:#64748b; font-size:11px;">--</span>'

            pip_val = audit.get("pipeline") or "--"
            vlm_val = audit.get("vlm") or "--"

            with review_carousel_container:
                with ui.expansion("Barcode source").classes("w-full").style(
                    "border-top:1px solid rgba(255,255,255,0.05);"
                    "background:#060a12; color:#94a3b8;"
                    "font-size:12px; font-family:Inter,sans-serif;"
                ):
                    with ui.column().classes("w-full gap-2").style("padding:8px 4px 4px"):
                        for label, val, chk in [
                            ("Pixel scan", pip_val, audit.get("pipeline_checksum")),
                            ("VLM text",   vlm_val, audit.get("vlm_checksum")),
                        ]:
                            with ui.row().classes("w-full items-center gap-2"):
                                ui.html(
                                    f'<span style="color:#64748b; font-size:11px;'
                                    f' font-family:Inter,sans-serif; width:72px;'
                                    f' flex-shrink:0;">{label}</span>'
                                    f'<span style="color:#e2e8f0; font-size:12px;'
                                    f' font-family:Inter,sans-serif; font-weight:500;'
                                    f' flex:1;">{val}</span>'
                                    f'{_check_icon(chk)}'
                                )
                        ui.html(
                            f'<div style="margin-top:4px; padding-top:6px;'
                            f' border-top:1px solid rgba(255,255,255,0.05);">'
                            f'<span style="color:#64748b; font-size:11px;'
                            f' font-family:Inter,sans-serif;">Decision: </span>'
                            f'<span style="color:#818cf8; font-size:11px;'
                            f' font-family:Inter,sans-serif; font-weight:500;">{decision_label}</span>'
                            f'</div>'
                        )

    # ── Known brand hint ──────────────────────────────────────────────────────
    if review_carousel_container is not None:
        brand_val = row.get("brand", "").strip()
        if brand_val:
            from backend.db import get_brand_profile
            profile = get_brand_profile(brand_val)
            if profile and profile.get("product_count", 0) > 1:
                parts = []
                if profile.get("manufacturer"):
                    parts.append(profile["manufacturer"])
                if profile.get("category_type"):
                    parts.append(profile["category_type"])
                if profile.get("country_of_origin"):
                    parts.append(profile["country_of_origin"])
                count = profile["product_count"]
                detail = " · ".join(parts) if parts else ""
                with review_carousel_container:
                    with ui.row().classes("w-full items-center gap-2 px-4 py-2").style(
                        "border-top:1px solid rgba(255,255,255,0.05);"
                        "background:rgba(99,102,241,0.05);"
                    ):
                        ui.icon("auto_awesome", size="0.9rem").style("color:#818cf8; flex-shrink:0")
                        ui.html(
                            f'<span style="color:#818cf8; font-size:11px; font-weight:600;'
                            f' font-family:Inter,sans-serif;">Known brand</span>'
                            f'<span style="color:#475569; font-size:11px;'
                            f' font-family:Inter,sans-serif;"> &nbsp;·&nbsp; seen {count}x</span>'
                            + (f'<span style="color:#64748b; font-size:11px;'
                               f' font-family:Inter,sans-serif;"> &nbsp;·&nbsp; {detail}</span>'
                               if detail else "")
                        )

    # ── Duplicate banner + resolve button ────────────────────────────────────
    if review_carousel_container is not None:
        dupe_of = row.get("_dupe_of", "")
        dupe_id = row.get("_dupe_id")
        if dupe_of and row.get("_status") == "duplicate":
            with review_carousel_container:
                with ui.row().classes("w-full items-center justify-between").style(
                    "background:rgba(239,68,68,0.07); border-left:3px solid #ef4444;"
                    "padding:10px 16px; margin:0; gap:8px;"
                ):
                    with ui.column().classes("gap-0 flex-1"):
                        ui.html(
                            '<p style="margin:0;font-size:11px;font-weight:600;color:#ef4444;'
                            'font-family:Inter,sans-serif;letter-spacing:0.3px;'
                            'text-transform:uppercase;margin-bottom:3px">Potential duplicate</p>'
                            f'<p style="margin:0;font-size:12px;color:#94a3b8;'
                            f'font-family:Inter,sans-serif;line-height:1.5">{dupe_of}</p>'
                        )
                    if dupe_id:
                        def _open_merge(current=row, matched_db_id=dupe_id):
                            _open_merge_dialog(current, matched_db_id)
                        ui.button("Resolve", icon="merge", on_click=_open_merge).props(
                            "dense unelevated"
                        ).style(
                            "background:rgba(239,68,68,0.15); color:#ef4444;"
                            "font-size:11px; font-weight:600; font-family:Inter,sans-serif;"
                            "border:1px solid rgba(239,68,68,0.3); border-radius:6px;"
                            "padding:4px 10px; flex-shrink:0;"
                        )

    # Update status toggle button label to reflect the row's current state
    _sync_status_btn(row.get("_status", "warn"))

    review_drawer.show()


def _remove_image_from_group(path_to_remove: str) -> None:
    """Remove one image from the current row's image group."""
    global review_row_id
    if review_row_id is None:
        return

    row = next((r for r in row_data if r["id"] == review_row_id), None)
    if not row:
        return

    paths = list(row.get("image_paths") or [row.get("image_path", "")])
    if len(paths) <= 1:
        ui.notify("Cannot remove the only image", type="warning", position="center")
        return

    if path_to_remove in paths:
        paths.remove(path_to_remove)
        p = Path(path_to_remove)
        if p.exists():
            p.unlink()

    row["image_paths"] = paths
    row["image_path"] = paths[0]
    row["thumbnail"] = image_to_url(paths[0])

    db_id = row.get("db_id")
    if db_id:
        update_extraction_image_paths(int(db_id), paths)

    grid = get_grid()
    if grid:
        grid.options["rowData"] = list(row_data)
        grid.update()

    open_review_drawer(row)


def save_review_drawer():
    """Write drawer edits back to row_data, AG Grid, and SQLite."""
    if review_row_id is None:
        return

    for row in row_data:
        if row["id"] != review_row_id:
            continue

        for key, _ in FIELDS:
            row[key] = review_inputs[key].value or ""

        persist_row_edits(row)
        grid = get_grid()
        if grid:
            grid.options["rowData"] = list(row_data)
            grid.update()
        ui.notify("Saved", type="positive", position="center")
        return



async def delete_from_review_drawer():
    """Remove the current row from state, the grid, and the database."""
    global review_row_id
    if review_row_id is None:
        return

    row_to_delete = next((r for r in row_data if r["id"] == review_row_id), None)
    if not row_to_delete:
        return

    with ui.dialog() as dialog, ui.card().classes("p-6 gap-4"):
        ui.label("Delete this record?").classes("text-sm font-medium")
        ui.label(
            row_to_delete.get("product_name") or "This product"
        ).classes("text-xs text-slate-400")
        with ui.row().classes("justify-end gap-2 w-full pt-2"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props(
                "flat color=white"
            ).classes("text-xs")
            ui.button("Delete", on_click=lambda: dialog.submit(True)).props(
                "unelevated color=red"
            ).classes("text-xs")

    result = await dialog
    if not result:
        return

    do_delete_row(row_to_delete)
    row_data[:] = [r for r in row_data if r["id"] != review_row_id]

    grid = get_grid()
    if grid:
        grid.options["rowData"] = list(row_data)
        grid.update()

    review_drawer.hide()
    ui.notify("Deleted", type="negative", position="center")


# ── Upload zone ──────────────────────────────────────────────────────────────

def render_upload_zone():
    """Two-level pill navigation upload zone.

    Outer pills: Image | Video   (type of media)
    Inner pills: Quick Upload | Batch   (processing mode)

    All four combinations are supported:
      Image / Quick Upload  -- up to 20 images, immediate.
      Image / Batch         -- unlimited images, background job.
      Video / Quick Upload  -- up to 5 videos, immediate.
      Video / Batch         -- up to 50 videos, background job.

    Uses custom pill controls instead of NiceGUI tabs so the visual hierarchy
    matches the app's existing legend/filter pill design language.
    """
    user = current_user()

    # ── Pill styles -- match the legend filter pills in render_legend() ───────
    _BASE = (
        "font-family:Inter,sans-serif; cursor:pointer; transition:all 0.15s;"
        "border:1px solid; border-radius:20px; user-select:none;"
    )
    # Outer pills are slightly larger -- they represent the primary choice.
    _O_ON  = _BASE + (
        "padding:5px 16px; font-size:12px; font-weight:600;"
        "background:rgba(99,102,241,0.15); color:#a5b4fc;"
        "border-color:rgba(99,102,241,0.35);"
    )
    _O_OFF = _BASE + (
        "padding:5px 16px; font-size:12px; font-weight:500;"
        "background:transparent; color:#475569; border-color:transparent;"
    )
    # Inner pills are smaller -- secondary choice inside the active outer.
    _I_ON  = _BASE + (
        "padding:4px 12px; font-size:11px; font-weight:500;"
        "background:rgba(99,102,241,0.15); color:#a5b4fc;"
        "border-color:rgba(99,102,241,0.35);"
    )
    _I_OFF = _BASE + (
        "padding:4px 12px; font-size:11px; font-weight:500;"
        "background:transparent; color:#334155; border-color:transparent;"
    )

    with ui.column().classes("w-full gap-0"):

        # ── Header: outer pills on the left, model selector on the right ─────
        with ui.row().classes("w-full items-center justify-between pb-2").style(
            "border-bottom:1px solid rgba(240,225,205,0.07)"
        ):
            with ui.row().classes("items-center gap-1"):
                outer_img = ui.label("Image").style(_O_ON)
                outer_vid = ui.label("Video").style(_O_OFF)

            with ui.row().classes("items-center gap-2 pr-1"):
                ui.label("Model:").style("font-size:11px; color:#64748b")
                ui.select(
                    list(MODEL_OPTIONS.keys()),
                    value=get_client_model(),
                    on_change=lambda e: set_client_model(e.value),
                ).props("dense dark outlined").style("font-size:11px; min-width:160px")
                with ui.element("div").style("position:relative"):
                    info_btn = ui.icon("info_outline", size="1rem").style(
                        "color:#475569; cursor:pointer; margin-top:2px"
                    )
                    with ui.menu().props(
                        "anchor='bottom right' self='top right' auto-close"
                    ).classes("shadow-xl").style(
                        "background:#1e1c19; border:1px solid rgba(240,225,205,0.09);"
                        "border-radius:10px; padding:14px 16px; width:340px"
                    ) as pricing_menu:
                        ui.html("""
                            <p style="font-size:12px;font-weight:700;color:#f0ebe5;
                               font-family:Inter,sans-serif;margin:0 0 10px 0">
                               Estimated cost per image
                            </p>
                            <table style="font-size:11px;color:#94a3b8;
                                          font-family:DM Mono,monospace;
                                          border-collapse:collapse;width:100%;white-space:nowrap">
                              <tr style="color:#64748b;font-size:10px">
                                <th style="text-align:left;padding-bottom:6px;padding-right:20px">Model</th>
                                <th style="text-align:right;padding-bottom:6px;padding-right:16px">Quick Upload</th>
                                <th style="text-align:right;padding-bottom:6px">Batch</th>
                              </tr>
                              <tr>
                                <td style="padding:4px 20px 4px 0;color:#c7d2fe">Claude Sonnet 4.6</td>
                                <td style="text-align:right;padding-right:16px">~$0.009</td>
                                <td style="text-align:right;color:#10b981">~$0.0045</td>
                              </tr>
                              <tr>
                                <td style="padding:4px 20px 4px 0;color:#c7d2fe">GPT-5.5</td>
                                <td style="text-align:right;padding-right:16px">~$0.017</td>
                                <td style="text-align:right;color:#10b981">~$0.0085</td>
                              </tr>
                              <tr>
                                <td style="padding:4px 20px 4px 0;color:#c7d2fe">Gemini 2.5 Flash</td>
                                <td style="text-align:right;padding-right:16px">~$0.0012</td>
                                <td style="text-align:right;color:#10b981">~$0.0006</td>
                              </tr>
                            </table>
                            <p style="font-size:10px;color:#475569;font-family:Inter,sans-serif;
                               margin:10px 0 0 0;line-height:1.6;
                               border-top:1px solid rgba(240,225,205,0.07);padding-top:8px">
                              Based on ~1,560 input tokens and ~300 output tokens per image.
                              Batch uses provider batch APIs at 50% off standard rates.
                            </p>
                        """)
                    info_btn.on("click", pricing_menu.open)

        # ── Image panel ───────────────────────────────────────────────────────
        image_panel = ui.column().classes("w-full gap-0")
        with image_panel:
            # Inner pills
            with ui.row().classes("items-center gap-1 pt-2 pb-1"):
                img_quick_pill = ui.label("Quick Upload").style(_I_ON)
                img_batch_pill = ui.label("Batch").style(_I_OFF).classes("bulk-batch-tab")

            # Content panels -- only one visible at a time
            img_quick_panel = ui.column().classes("w-full pt-2")
            with img_quick_panel:
                _render_quick_tab()

            img_batch_panel = ui.column().classes("w-full pt-2")
            img_batch_panel.set_visibility(False)
            with img_batch_panel:
                _render_bulk_tab(user)

        # ── Video panel (hidden by default) ───────────────────────────────────
        video_panel = ui.column().classes("w-full gap-0")
        video_panel.set_visibility(False)
        with video_panel:
            # Inner pills
            with ui.row().classes("items-center gap-1 pt-2 pb-1"):
                vid_quick_pill = ui.label("Quick Upload").style(_I_ON)
                vid_batch_pill = ui.label("Batch").style(_I_OFF)

            # Content panels
            vid_quick_panel = ui.column().classes("w-full pt-2")
            with vid_quick_panel:
                _render_multiview_tab(user)

            vid_batch_panel = ui.column().classes("w-full pt-2")
            vid_batch_panel.set_visibility(False)
            with vid_batch_panel:
                _render_video_batch_tab(user)

        # ── Toggle callbacks (closures -- all UI elements are defined above) ──

        def _show_image():
            outer_img.style(_O_ON);  outer_vid.style(_O_OFF)
            image_panel.set_visibility(True)
            video_panel.set_visibility(False)

        def _show_video():
            outer_img.style(_O_OFF); outer_vid.style(_O_ON)
            image_panel.set_visibility(False)
            video_panel.set_visibility(True)

        def _show_img_quick():
            img_quick_pill.style(_I_ON);  img_batch_pill.style(_I_OFF)
            img_quick_panel.set_visibility(True)
            img_batch_panel.set_visibility(False)

        def _show_img_batch():
            img_quick_pill.style(_I_OFF); img_batch_pill.style(_I_ON)
            img_quick_panel.set_visibility(False)
            img_batch_panel.set_visibility(True)

        def _show_vid_quick():
            vid_quick_pill.style(_I_ON);  vid_batch_pill.style(_I_OFF)
            vid_quick_panel.set_visibility(True)
            vid_batch_panel.set_visibility(False)

        def _show_vid_batch():
            vid_quick_pill.style(_I_OFF); vid_batch_pill.style(_I_ON)
            vid_quick_panel.set_visibility(False)
            vid_batch_panel.set_visibility(True)

        outer_img.on("click", _show_image)
        outer_vid.on("click", _show_video)
        img_quick_pill.on("click", _show_img_quick)
        img_batch_pill.on("click", _show_img_batch)
        vid_quick_pill.on("click", _show_vid_quick)
        vid_batch_pill.on("click", _show_vid_batch)


def _render_quick_tab() -> None:
    """Original instant-processing upload zone, capped at QUICK_UPLOAD_LIMIT images."""
    with ui.element("div").classes("upload-zone w-full").style(
        "position:relative; min-height:190px"
    ):
        with ui.column().classes("items-center justify-center gap-3").style(
            "position:absolute; inset:0; pointer-events:none; padding:36px 20px; z-index:5"
        ):
            ui.icon("cloud_upload", size="2rem").style("color:rgba(99,102,241,0.5)")
            with ui.column().classes("items-center gap-1"):
                ui.label("Drop product images here").style(
                    "color:#64748b; font-size:14px; font-weight:500;"
                    "font-family:Inter,sans-serif; letter-spacing:-0.1px"
                )
                ui.label(
                    f"Up to {QUICK_UPLOAD_LIMIT} images · "
                    "multiple angles are grouped automatically"
                ).style("color:#263344; font-size:12px; font-family:Inter,sans-serif")
            with ui.element("div").style(
                "margin-top:6px; padding:7px 20px;"
                "border:1px solid rgba(99,102,241,0.22); border-radius:8px;"
                "font-size:12px; font-weight:500; font-family:Inter,sans-serif;"
            ):
                ui.label("Add images").style(
                    "color:#818cf8; font-family:Inter,sans-serif; font-size:12px"
                )

        _qup = ui.upload(
            multiple=True,
            on_multi_upload=handle_batch_upload,
            auto_upload=True,
        ).props('accept=".jpg,.jpeg,.png,.webp" flat label=""').classes(
            "upload-zone-cover"
        )
        from frontend.state import set_quick_upload
        set_quick_upload(_qup)


def _render_multiview_tab(user: dict | None) -> None:
    """Video tab -- upload up to 5 video files, one product per video.

    Frames are extracted automatically (OpenCV for MP4/MOV/AVI, imageio-ffmpeg
    for WebM), the sharpest ones are selected, then all frames are passed
    together to the multi-view extraction pipeline as a single product.
    """
    from pathlib import Path as _Path

    _MAX_VIDEOS = 5
    _MAX_MB     = 100
    _MAX_BYTES  = _MAX_MB * 1024 * 1024

    # ── Pipeline helper ──────────────────────────────────────────────────────────

    async def _run_pipeline_on_video(video_path: _Path, original_name: str) -> bool:
        """Extract frames, run multi-view pipeline, append row to grid.  Returns True on success."""
        from backend.db import create_extraction
        from backend.extractor import MODEL_OPTIONS, extract_from_frames
        from backend.video_processor import extract_frames_from_video_async, select_best_frames_async
        from frontend.state import get_grid, result_to_row, row_data

        name_hint = _Path(original_name).stem.replace("_", " ")
        model_display = get_client_model()
        backend_name, model_id = MODEL_OPTIONS.get(
            model_display, next(iter(MODEL_OPTIONS.values()))
        )

        frame_dir = video_path.parent / f"frames_{video_path.stem}"
        raw_frames = await extract_frames_from_video_async(video_path, frame_dir)
        if not raw_frames:
            raise ValueError("no frames could be extracted from the video")

        best_frames = await select_best_frames_async(raw_frames, max_frames=12)
        result = await extract_from_frames(
            frames=best_frames,
            product_name=name_hint,
            backend=backend_name,
            model_id=model_id,
        )

        extraction_id = create_extraction(
            user_id=user["id"],
            original_filename=original_name,
            result=result,
            source="video",
            video_path=str(video_path),
            barcode_audit=result.barcode_audit,
        )
        row = result_to_row(result, len(row_data))
        row["db_id"] = extraction_id
        row["_source"] = "video"
        row["video_path"] = image_to_url(str(video_path))
        row_data.append(row)

        grid = get_grid()
        if grid:
            grid.options["rowData"] = list(row_data)
            grid.update()
        return True

    # ── Upload state & callbacks ─────────────────────────────────────────────────

    staged: list[tuple[_Path, str]] = []

    async def _on_videos_staged(e: events.MultiUploadEventArguments) -> None:
        from uuid import uuid4
        upload_dir = _Path("data/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        for file in e.files:
            if len(staged) >= _MAX_VIDEOS:
                ui.notify(f"Maximum {_MAX_VIDEOS} videos allowed.", type="warning", position="center")
                break
            file_size = file.size()
            if file_size > _MAX_BYTES:
                ui.notify(
                    f"{file.name} is too large ({file_size // (1024 * 1024)} MB). "
                    f"Limit is {_MAX_MB} MB.",
                    type="warning", position="center", timeout=6000,
                )
                continue
            suffix = _Path(file.name).suffix or ".mp4"
            dest = upload_dir / f"{_Path(file.name).stem}_{uuid4().hex[:8]}{suffix}"
            await file.save(dest)
            staged.append((dest, file.name))
        n = len(staged)
        upload_count_label.set_text(f"{n} video{'s' if n != 1 else ''} ready")
        upload_submit_btn.set_enabled(n > 0)
        upload_hint.set_visibility(False)
        upload_staged.set_visibility(True)

    def _upload_clear() -> None:
        staged.clear()
        upload_count_label.set_text("")
        upload_submit_btn.set_enabled(False)
        upload_hint.set_visibility(True)
        upload_staged.set_visibility(False)

    async def _on_upload_submit() -> None:
        if not staged:
            ui.notify("Upload at least one video first.", type="warning", position="center")
            return
        if not user:
            ui.notify("Please log in.", type="warning", position="center")
            return
        n = len(staged)
        show_processing(f"Processing {n} video{'s' if n != 1 else ''}…")
        errors: list[str] = []
        count = 0
        for video_path, original_name in staged:
            try:
                await _run_pipeline_on_video(video_path, original_name)
                count += 1
            except Exception as exc:
                errors.append(f"{original_name}: {exc}")
        hide_processing()
        if count:
            ui.notify(f"{count} product{'s' if count != 1 else ''} extracted", type="positive", position="center")
        for msg in errors:
            ui.notify(msg, type="negative", position="center", timeout=8000)
        _upload_clear()

    # ── UI ───────────────────────────────────────────────────────────────────────

    with ui.column().classes("w-full gap-3 pt-1"):
        with ui.element("div").classes("upload-zone w-full").style(
            "position:relative; min-height:160px"
        ):
            upload_hint = ui.column().classes("items-center justify-center gap-2").style(
                "position:absolute; inset:0; pointer-events:none; padding:28px 20px; z-index:5"
            )
            with upload_hint:
                ui.icon("videocam", size="2rem").style("color:rgba(99,102,241,0.5)")
                ui.label("Upload a video of your product").style(
                    "color:#64748b; font-size:14px; font-weight:500;"
                    "font-family:Inter,sans-serif; letter-spacing:-0.1px; text-align:center"
                )
                ui.label("Up to 5 videos · 100 MB max per video · one product per video").style(
                    "color:#263344; font-size:12px; font-family:Inter,sans-serif; text-align:center"
                )
                with ui.element("div").style(
                    "margin-top:6px; padding:7px 20px;"
                    "border:1px solid rgba(99,102,241,0.22); border-radius:8px;"
                ):
                    ui.label("Add video").style(
                        "color:#818cf8; font-family:Inter,sans-serif; font-size:12px; font-weight:500"
                    )

            upload_staged = ui.column().classes("items-center justify-center gap-2").style(
                "position:absolute; inset:0; pointer-events:none; padding:28px 20px; z-index:5"
            )
            upload_staged.set_visibility(False)
            with upload_staged:
                ui.icon("check_circle", size="2rem").style("color:rgba(16,185,129,0.6)")
                upload_count_label = ui.label("").style(
                    "color:#10b981; font-size:14px; font-weight:500; font-family:Inter,sans-serif"
                )
                ui.label("Drop more to add. Each video is one product.").style(
                    "color:#1e3a2e; font-size:12px; font-family:Inter,sans-serif; text-align:center"
                )

            ui.upload(
                multiple=True, auto_upload=True, on_multi_upload=_on_videos_staged,
            ).props('accept=".mp4,.mov,.avi,.webm,.mkv" flat label=""').classes("upload-zone-cover")

        with ui.row().classes("w-full items-center justify-between pt-1"):
            ui.button("Clear", icon="clear_all", on_click=_upload_clear).props(
                "flat dense color=grey-6"
            ).classes("text-xs")
            upload_submit_btn = ui.button(
                "Process Videos", icon="auto_awesome", on_click=_on_upload_submit,
            ).props("unelevated color=indigo-5").style(
                "font-size:13px; font-weight:600; padding:0 20px; height:38px"
            )
            upload_submit_btn.set_enabled(False)


def _render_video_batch_tab(user: dict | None) -> None:
    """Video Batch tab -- stage up to 50 videos for background extraction.

    Each video is one product.  The job runs as a background asyncio task:
    frames are extracted, the sharpest ones selected, then passed to the
    multi-view VLM pipeline.  An email is sent when all videos are done.
    Results appear in the batch jobs panel and are loaded into the grid
    automatically when the job completes.
    """
    from pathlib import Path as _Path

    _MAX_VIDEOS = 50
    _MAX_MB     = 100
    _MAX_BYTES  = _MAX_MB * 1024 * 1024

    staged: list[_Path] = []
    counters = {"skipped": 0}

    # ── Callbacks ────────────────────────────────────────────────────────────────

    async def _on_staged(e: events.MultiUploadEventArguments) -> None:
        from uuid import uuid4
        upload_dir = _Path("data/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        for file in e.files:
            if len(staged) >= _MAX_VIDEOS:
                ui.notify(
                    f"Maximum {_MAX_VIDEOS} videos per batch.", type="warning", position="center"
                )
                break
            suffix = _Path(file.name).suffix.lower()
            if suffix not in {".mp4", ".mov", ".avi", ".webm", ".mkv"}:
                counters["skipped"] += 1
                continue
            file_size = file.size()
            if file_size > _MAX_BYTES:
                ui.notify(
                    f"{file.name} is too large ({file_size // (1024 * 1024)} MB). "
                    f"Limit is {_MAX_MB} MB.",
                    type="warning", position="center", timeout=6000,
                )
                counters["skipped"] += 1
                continue
            dest = upload_dir / f"{_Path(file.name).stem}_{uuid4().hex[:8]}{suffix}"
            await file.save(dest)
            staged.append(dest)

        n  = len(staged)
        sk = counters["skipped"]
        skip_txt = f" · {sk} file{'s' if sk != 1 else ''} skipped" if sk else ""
        count_label.set_text(f"{n} video{'s' if n != 1 else ''} ready{skip_txt}")
        start_btn.set_text(f"Submit Batch · {n} video{'s' if n != 1 else ''}")

        hint_empty.set_visibility(False)
        staged_hint_label.set_text(f"{n} video{'s' if n != 1 else ''} staged. Drop more to add.")
        hint_staged.set_visibility(True)
        staging_area.set_visibility(True)

    def _clear() -> None:
        staged.clear()
        counters["skipped"] = 0
        staging_area.set_visibility(False)
        hint_staged.set_visibility(False)
        hint_empty.set_visibility(True)

    async def _on_start() -> None:
        n = len(staged)
        if n == 0:
            return
        email = (email_input.value or "").strip()
        if not email:
            ui.notify(
                "Please enter an email address so we can notify you when results are ready.",
                type="warning", position="center",
            )
            return

        # Confirmation dialog matching the image batch style.
        with ui.dialog() as dlg, ui.card().classes("p-6 gap-4").style(
            "min-width:380px; max-width:480px;"
            "background:#1e1c19; border:1px solid rgba(99,102,241,0.25);"
            "border-radius:12px;"
        ):
            ui.label("Confirm video batch").classes("text-base font-semibold").style(
                "color:#f0ebe5"
            )
            with ui.column().classes("gap-2 py-1"):
                for line in [
                    f"{n} video{'s' if n != 1 else ''} will be submitted for extraction",
                    f"Results emailed to {email}",
                    "Each video is treated as one product",
                    "You can close this page. Results are saved to your account.",
                ]:
                    with ui.row().classes("items-start gap-2"):
                        ui.icon("chevron_right", size="1rem").style(
                            "color:#6366f1; margin-top:1px; flex-shrink:0"
                        )
                        ui.label(line).style("color:#94a3b8; font-size:13px; line-height:1.5")
            with ui.row().classes("justify-end gap-2 w-full pt-2"):
                ui.button("Cancel", on_click=lambda: dlg.submit(False)).props(
                    "flat color=white"
                ).classes("text-xs")
                ui.button(
                    f"Submit {n} video{'s' if n != 1 else ''}",
                    on_click=lambda: dlg.submit(True),
                ).props("unelevated color=indigo-5").classes("text-xs")

        if not await dlg:
            return

        show_processing(f"Queuing {n} video{'s' if n != 1 else ''} for batch extraction…")
        batch_error: Exception | None = None
        try:
            from backend.batch_processor import submit_video_batch
            from frontend.state import get_client_model
            await submit_video_batch(
                paths=list(staged),
                notify_email=email,
                user_id=user["id"],
                model_display_name=get_client_model(),
            )
        except Exception as exc:
            batch_error = exc
        finally:
            hide_processing()

        if batch_error is not None:
            ui.notify(f"Failed to queue batch: {batch_error}", type="negative", position="center")
            return

        from frontend.state import refresh_batch_jobs
        refresh_batch_jobs()
        ui.notify(
            f"Video batch queued · {n} video{'s' if n != 1 else ''} · "
            f"results will be emailed to {email}",
            type="positive", position="center", timeout=8000,
        )
        _clear()

    # ── Drop zone ────────────────────────────────────────────────────────────────

    with ui.element("div").classes("upload-zone w-full").style(
        "position:relative; min-height:190px"
    ):
        with ui.column().classes("items-center justify-center gap-3").style(
            "position:absolute; inset:0; pointer-events:none; padding:36px 20px; z-index:5"
        ):
            hint_empty = ui.column().classes("items-center gap-2")
            with hint_empty:
                ui.icon("video_library", size="2rem").style("color:rgba(99,102,241,0.5)")
                with ui.column().classes("items-center gap-1"):
                    ui.label("Drop product videos here").style(
                        "color:#64748b; font-size:14px; font-weight:500;"
                        "font-family:Inter,sans-serif; letter-spacing:-0.1px"
                    )
                    ui.label(
                        f"Up to {_MAX_VIDEOS} videos · {_MAX_MB} MB max each · "
                        "one product per video · results in your inbox"
                    ).style(
                        "color:#263344; font-size:12px; font-family:Inter,sans-serif;"
                        "text-align:center"
                    )
                with ui.element("div").style(
                    "margin-top:6px; padding:7px 20px;"
                    "border:1px solid rgba(99,102,241,0.22); border-radius:8px;"
                ):
                    ui.label("Choose videos").style(
                        "color:#818cf8; font-family:Inter,sans-serif; font-size:12px; font-weight:500"
                    )

            hint_staged = ui.column().classes("items-center gap-2")
            hint_staged.set_visibility(False)
            with hint_staged:
                ui.icon("check_circle", size="2rem").style("color:rgba(16,185,129,0.6)")
                staged_hint_label = ui.label("").style(
                    "color:#10b981; font-size:14px; font-weight:500;"
                    "font-family:Inter,sans-serif"
                )
                ui.label("Drop more videos to add them to the batch").style(
                    "color:#1e3a2e; font-size:12px; font-family:Inter,sans-serif"
                )

        ui.upload(
            multiple=True,
            auto_upload=True,
            on_multi_upload=_on_staged,
        ).props('accept=".mp4,.mov,.avi,.webm,.mkv" flat label=""').classes("upload-zone-cover")

    # ── Staging controls (hidden until first upload) ──────────────────────────────

    staging_area = ui.column().classes("w-full gap-3 px-1 pt-3 pb-1")
    staging_area.set_visibility(False)

    with staging_area:
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("check_circle", size="1rem").style("color:#10b981; flex-shrink:0")
            count_label = ui.label("").style(
                "color:#10b981; font-size:13px; font-weight:500;"
                "font-family:Inter,sans-serif; flex:1"
            )
            ui.button("Clear", icon="clear_all", on_click=_clear).props(
                "flat dense color=grey-6"
            ).classes("text-xs")

        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("mail_outline", size="1rem").style("color:#475569; flex-shrink:0")
            ui.label("Notify when done:").style(
                "color:#64748b; font-size:12px; font-family:Inter,sans-serif; white-space:nowrap"
            )
            email_input = ui.input(
                placeholder="you@example.com",
                value=user.get("email", "") if user else "",
            ).classes("flex-1").style("font-size:13px")

        with ui.row().classes("w-full justify-end"):
            start_btn = ui.button(
                "Submit Batch",
                icon="rocket_launch",
                on_click=_on_start,
            ).props("unelevated color=indigo-5").style(
                "font-size:13px; font-weight:600; padding:0 20px; height:38px"
            )


def _render_bulk_tab(user: dict | None) -> None:
    """Bulk upload zone — unlimited images, staged then submitted via Batch API."""
    staged_paths: list[Path] = []
    counters = {"skipped": 0}

    # ── Callbacks (defined before UI so we can pass them as on_* args) ────────
    # All UI variables are closed over and resolved at call-time (not def-time),
    # so it's safe to reference elements created after these functions.

    async def _on_bulk_staged(e: events.MultiUploadEventArguments) -> None:
        new_saved, new_skipped = await stage_bulk_files(e)
        staged_paths.extend(new_saved)
        counters["skipped"] += new_skipped

        n = len(staged_paths)
        sk = counters["skipped"]
        skip_txt = (
            f" · {sk} non-image file{'s' if sk != 1 else ''} skipped" if sk else ""
        )
        count_label.set_text(f"{n} image{'s' if n != 1 else ''} ready{skip_txt}")
        start_btn.set_text(f"Start Batch Processing · {n} image{'s' if n != 1 else ''}")

        # Update drop-zone hint to "staged" state
        hint_empty.set_visibility(False)
        staged_hint_label.set_text(
            f"{n} image{'s' if n != 1 else ''} staged — drop more to add"
        )
        hint_staged.set_visibility(True)

        staging_area.set_visibility(True)

    def _clear() -> None:
        staged_paths.clear()
        counters["skipped"] = 0
        staging_area.set_visibility(False)
        hint_staged.set_visibility(False)
        hint_empty.set_visibility(True)

    async def _on_start() -> None:
        n = len(staged_paths)
        if n == 0:
            return
        email = (email_input.value or "").strip()
        if not email:
            ui.notify(
                "Please enter an email address so we can notify you when results are ready.",
                type="warning",
                position="center",
            )
            return

        with ui.dialog() as dlg, ui.card().classes("p-6 gap-4").style(
            "min-width:380px; max-width:480px;"
            "background:#1e1c19; border:1px solid rgba(99,102,241,0.25);"
            "border-radius:12px;"
        ):
            ui.label("Confirm batch processing").classes("text-base font-semibold").style(
                "color:#f0ebe5"
            )
            with ui.column().classes("gap-2 py-1"):
                for line in [
                    f"{n} image{'s' if n != 1 else ''} will be submitted for extraction",
                    f"Results emailed to  {email}",
                    "Processing takes up to 24 hours",
                    "You can close this page — results are saved to your account",
                ]:
                    with ui.row().classes("items-start gap-2"):
                        ui.icon("chevron_right", size="1rem").style(
                            "color:#6366f1; margin-top:1px; flex-shrink:0"
                        )
                        ui.label(line).style("color:#94a3b8; font-size:13px; line-height:1.5")
            with ui.row().classes("justify-end gap-2 w-full pt-2"):
                ui.button("Cancel", on_click=lambda: dlg.submit(False)).props(
                    "flat color=white"
                ).classes("text-xs")
                ui.button(
                    f"Submit {n} images",
                    on_click=lambda: dlg.submit(True),
                ).props("unelevated color=indigo-5").classes("text-xs")

        if not await dlg:
            return

        show_processing(f"Queuing {n} images for batch extraction…")
        batch_error: Exception | None = None
        try:
            await handle_bulk_start(list(staged_paths), email, user)
        except Exception as exc:
            batch_error = exc
        finally:
            hide_processing()

        if batch_error is not None:
            ui.notify(f"Failed to queue batch: {batch_error}", type="negative", position="center")
            return

        from frontend.state import refresh_batch_jobs
        refresh_batch_jobs()
        ui.notify(
            f"Batch job queued · {n} images · you'll receive results at {email}",
            type="positive",
            position="center",
            timeout=8000,
        )
        _clear()

    # ── Drop zone ───────────────────────────────────────────────────────────────
    with ui.element("div").classes("upload-zone w-full").style(
        "position:relative; min-height:190px"
    ):
        with ui.column().classes("items-center justify-center gap-3").style(
            "position:absolute; inset:0; pointer-events:none; padding:36px 20px; z-index:5"
        ):
            # Empty-state hint
            hint_empty = ui.column().classes("items-center gap-2")
            with hint_empty:
                ui.icon("dynamic_feed", size="2rem").style("color:rgba(99,102,241,0.5)")
                with ui.column().classes("items-center gap-1"):
                    ui.label("Drop all product images here — no limit").style(
                        "color:#64748b; font-size:14px; font-weight:500;"
                        "font-family:Inter,sans-serif; letter-spacing:-0.1px"
                    )
                    ui.label(
                        "Images are queued and extracted via Anthropic Batch API · results in ~24 hrs"
                    ).style(
                        "color:#263344; font-size:12px; font-family:Inter,sans-serif;"
                        "text-align:center"
                    )
                with ui.element("div").style(
                    "margin-top:6px; padding:7px 20px;"
                    "border:1px solid rgba(99,102,241,0.22); border-radius:8px;"
                    "font-size:12px; font-weight:500; font-family:Inter,sans-serif;"
                ):
                    ui.label("Choose images").style(
                        "color:#818cf8; font-family:Inter,sans-serif; font-size:12px"
                    )

            # Staged-state hint (hidden until files are uploaded)
            hint_staged = ui.column().classes("items-center gap-2")
            hint_staged.set_visibility(False)
            with hint_staged:
                ui.icon("check_circle", size="2rem").style("color:rgba(16,185,129,0.6)")
                staged_hint_label = ui.label("").style(
                    "color:#10b981; font-size:14px; font-weight:500;"
                    "font-family:Inter,sans-serif"
                )
                ui.label("Drop more images to add them to the batch").style(
                    "color:#1e3a2e; font-size:12px; font-family:Inter,sans-serif"
                )

        # Transparent full-zone uploader overlay
        ui.upload(
            multiple=True,
            auto_upload=True,
            on_multi_upload=_on_bulk_staged,
        ).props('accept=".jpg,.jpeg,.png,.webp" flat label=""').classes(
            "upload-zone-cover"
        )

    # ── Staging info + controls (hidden until first upload) ────────────────────
    staging_area = ui.column().classes("w-full gap-3 px-1 pt-3 pb-1")
    staging_area.set_visibility(False)

    with staging_area:
        # Count + clear row
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("check_circle", size="1rem").style("color:#10b981; flex-shrink:0")
            count_label = ui.label("").style(
                "color:#10b981; font-size:13px; font-weight:500;"
                "font-family:Inter,sans-serif; flex:1"
            )
            ui.button("Clear", icon="clear_all", on_click=_clear).props(
                "flat dense color=grey-6"
            ).classes("text-xs")

        # Email notification row
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("mail_outline", size="1rem").style("color:#475569; flex-shrink:0")
            ui.label("Notify when done:").style(
                "color:#64748b; font-size:12px; font-family:Inter,sans-serif;"
                "white-space:nowrap"
            )
            email_input = ui.input(
                placeholder="you@example.com",
                value=user.get("email", "") if user else "",
            ).classes("flex-1").style("font-size:13px")

        # Start button row
        with ui.row().classes("w-full justify-end"):
            start_btn = ui.button(
                "Start Batch Processing",
                icon="rocket_launch",
                on_click=_on_start,
            ).props("unelevated color=indigo-5").style(
                "font-size:13px; font-weight:600; padding:0 20px; height:38px"
            )


# ── Legend ───────────────────────────────────────────────────────────────────

def render_legend():
    _PILL = (
        "padding:4px 12px; border-radius:20px; font-size:11px; font-weight:500;"
        "font-family:Inter,sans-serif; cursor:pointer; transition:all 0.15s; border:1px solid;"
    )
    _ACTIVE = _PILL + "background:rgba(99,102,241,0.15); color:#a5b4fc; border-color:rgba(99,102,241,0.35);"
    _INACTIVE = _PILL + "background:transparent; color:#334155; border-color:transparent;"

    current = {"filter": "all"}

    with ui.row().classes("w-full items-center justify-between"):
        # ── Source filter pills (far left) ────────────────────────────────────
        with ui.row().classes("items-center gap-1"):
            pills = {}

            def _set_filter(f: str):
                current["filter"] = f
                for k, el in pills.items():
                    el.style(_ACTIVE if k == f else _INACTIVE)
                filtered = (
                    row_data if f == "all"
                    else [r for r in row_data if r.get("_source") == f]
                )
                g = get_grid()
                if g:
                    g.options["rowData"] = filtered
                    g.update()

            for key, label in [("all", "All"), ("quick", "Quick Upload"), ("video", "Video"), ("batch", "Batch")]:
                el = ui.label(label).style(_ACTIVE if key == "all" else _INACTIVE)
                el.on("click", lambda k=key: _set_filter(k))
                pills[key] = el

            set_grid_source_filter(_set_filter)

        # ── Status legend (right) ─────────────────────────────────────────────
        with ui.row().classes("items-center gap-6").style(
            "font-size:11px; color:#3d5166; font-family:'Inter',sans-serif"
        ):
            for color, label in [
                ("#10b981", "OK"),
                ("#f59e0b", "Needs review"),
                ("#ef4444", "Duplicate"),
            ]:
                with ui.row().classes("items-center gap-2"):
                    ui.element("span").style(
                        f"width:7px;height:7px;border-radius:50%;"
                        f"background:{color};display:inline-block;"
                        f"box-shadow:0 0 5px {color}55"
                    )
                    ui.label(label)


# ── Grid ─────────────────────────────────────────────────────────────────────

def render_grid():
    grid = (
        ui.aggrid(
            {
                "defaultColDef": {
                    "resizable": True,
                    "sortable": True,
                    "filter": True,
                    "stopEditingWhenCellsLoseFocus": True,
                },
                "columnDefs": build_column_defs(),
                "rowData": row_data,
                "rowHeight": 54,
                "rowClassRules": {
                    "row-ok":     "data._status === 'ok'",
                    "row-warn":   "data._status === 'warn'",
                    "row-dupe":   "data._status === 'duplicate'",
                    "row-failed": "data._status === 'failed'",
                },
                "animateRows": True,
                "rowSelection": "multiple",
                "suppressRowClickSelection": True,
                "tooltipShowDelay": 400,
                "tooltipHideDelay": 3000,
            },
            theme="alpine",
        )
        .classes("w-full")
        .style("height: 560px; border-radius: 12px; overflow: hidden;")
    )

    def on_cell_change(e):
        updated = e.args["data"]
        for i, row in enumerate(row_data):
            if row["id"] == updated["id"]:
                row_data[i] = updated
                persist_row_edits(updated)
                break

    def on_cell_click(e):
        col = e.args.get("colId")
        row = e.args.get("data", {})
        if col != "_review":
            return

        if row.get("_status") == "failed":
            async def _show_failed_dialog():
                with ui.dialog() as dlg, ui.card().classes("p-6 gap-0").style("min-width:340px"):
                    ui.html(
                        '<p style="font-size:15px;font-weight:700;color:#f0ebe5;'
                        'font-family:Inter,sans-serif;letter-spacing:-0.3px;margin-bottom:8px">'
                        'No product detected</p>'
                    )
                    ui.html(
                        '<p style="font-size:13px;color:#52504c;font-family:Inter,sans-serif;'
                        'line-height:1.6;margin-bottom:20px">'
                        'The AI could not extract any product information from this image. '
                        'You can keep it in the list for reference or discard it.'
                        '</p>'
                    )
                    if row.get("thumbnail"):
                        ui.image(row["thumbnail"]).style(
                            "width:100%;height:140px;object-fit:cover;"
                            "border-radius:10px;margin-bottom:20px;"
                        )
                    with ui.row().classes("w-full gap-2 justify-end"):
                        ui.button("Keep", on_click=lambda: dlg.submit("keep")).props(
                            "flat"
                        ).style("color:#10b981;font-weight:600")
                        ui.button("Discard", on_click=lambda: dlg.submit("discard")).props(
                            "unelevated color=red-8"
                        ).style("font-weight:600")

                result = await dlg
                if result == "discard":
                    for i, r in enumerate(row_data):
                        if r["id"] == row["id"]:
                            row_data.pop(i)
                            break
                    g = get_grid()
                    if g:
                        g.options["rowData"] = list(row_data)
                        g.update()
                    ui.notify("Image discarded", type="info", position="center")

            ui.timer(0, _show_failed_dialog, once=True)
        else:
            open_review_drawer(row)

    grid.on("cellValueChanged", on_cell_change)
    grid.on("cellClicked", on_cell_click)
    set_grid(grid)
