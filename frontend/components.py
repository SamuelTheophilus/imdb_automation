from pathlib import Path

from nicegui import events, ui

from backend.db import update_extraction_image_paths, update_extraction_status
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

from frontend.state import FIELDS, build_column_defs, get_grid, image_to_url, row_data, set_grid

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

    for key, _ in FIELDS:
        review_inputs[key].value = row.get(key, "")

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
    """Tabbed upload zone: Quick Upload (≤20 images) and Bulk Batch (unlimited)."""
    user = current_user()

    with ui.column().classes("w-full gap-0"):
        with ui.tabs().props(
            "align=left dense indicator-color=indigo-4 active-color=indigo-4"
        ).style("border-bottom:1px solid rgba(240,225,205,0.07)") as tabs:
            ui.tab("Quick Upload", icon="upload_file").props("no-caps")
            ui.tab("Bulk Batch", icon="dynamic_feed").props("no-caps").classes("bulk-batch-tab")

        with ui.tab_panels(tabs, value="Quick Upload").classes("w-full p-0"):
            with ui.tab_panel("Quick Upload").classes("p-0 pt-3"):
                _render_quick_tab()
            with ui.tab_panel("Bulk Batch").classes("p-0 pt-3"):
                _render_bulk_tab(user)


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

        ui.upload(
            multiple=True,
            on_multi_upload=handle_batch_upload,
            auto_upload=True,
        ).props('accept=".jpg,.jpeg,.png,.webp" flat label=""').classes(
            "upload-zone-cover"
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
        email = email_input.value.strip()
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
        try:
            await handle_bulk_start(list(staged_paths), email, user)
        except Exception as exc:
            hide_processing()
            ui.notify(f"Failed to queue batch: {exc}", type="negative", position="center")
            return

        hide_processing()
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
    with (
        ui.row()
        .classes("items-center gap-6")
        .style("font-size:11px; color:#3d5166; font-family:'Inter',sans-serif")
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
