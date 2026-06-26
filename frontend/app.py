import asyncio
import logging
import os
from pathlib import Path

from nicegui import app, ui

# Importing this module registers the /login and /signup pages with NiceGUI.
import frontend.auth_pages  # noqa: F401
from backend.db import delete_batch_job, init_db, list_batch_jobs, list_brand_catalog, list_extraction_versions, list_user_extractions, mark_tour_shown
from frontend.state import db_record_to_row, set_batch_jobs_refresh, switch_to_batch_view
from backend.normalizer import load_canonical_brands
from frontend.auth_pages import require_user
from frontend.components import (
    _launch_tour,
    open_review_drawer,
    render_grid,
    render_header,
    render_legend,
    render_review_drawer,
    render_upload_zone,
)
from frontend.info_page import register_coming_soon
from frontend.state import db_record_to_row, row_data
from frontend.styles import STYLES
from frontend.tour import TOUR_JS, TOUR_SAMPLE_ROW
from frontend.utils import format_date

log = logging.getLogger(__name__)

_DRIVER_CDN = (
    '<link rel="stylesheet"'
    ' href="https://cdn.jsdelivr.net/npm/driver.js@1.3.1/dist/driver.css"/>\n'
    '<script src="https://cdn.jsdelivr.net/npm/driver.js@1.3.1/dist/driver.js.iife.js">'
    "</script>"
)
UPLOAD_DIR = Path("data/uploads")

# Expose uploaded images as static files.
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.add_static_files("/uploads", UPLOAD_DIR)



# ── Background batch poller ───────────────────────────────────────────────────

async def _batch_poll_loop() -> None:
    """Poll pending batch jobs every 5 minutes, run forever as background task."""
    log.info("[batch_poller] background loop started — first poll in 60s")
    await asyncio.sleep(60)  # let the server settle on startup
    cycle = 0
    while True:
        cycle += 1
        log.info("[batch_poller] ── poll cycle #%d ──────────────────────────", cycle)
        try:
            from backend.batch_processor import poll_pending_jobs
            await poll_pending_jobs()
        except Exception as exc:
            log.error("[batch_poller] unhandled error in cycle #%d: %s", cycle, exc)
        log.info("[batch_poller] cycle #%d done — next in 2 min", cycle)
        await asyncio.sleep(120)


@app.on_startup
async def _start_batch_poller() -> None:
    asyncio.create_task(_batch_poll_loop())


_STATUS_STYLE = {
    "pending":   ("schedule",      "#f59e0b", "Processing"),
    "completed": ("check_circle",  "#10b981", "Complete"),
    "failed":    ("error_outline", "#ef4444", "Failed"),
}


def _render_batch_jobs_section(user_id: int) -> None:
    """Batch jobs panel — always rendered, auto-refreshes every 10 seconds.

    When a job transitions to completed, the grid is also reloaded from DB
    so results appear without a page refresh.
    """
    from datetime import datetime, timedelta, timezone
    from frontend.state import get_grid, row_data

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    _last_statuses: dict[int, str] = {}
    collapsed = {"v": False}  # survives refreshes

    @ui.refreshable
    def _jobs_cards() -> None:
        fresh = [j for j in list_batch_jobs(user_id) if j["submitted_at"] >= cutoff]

        # Detect jobs that just completed and reload the grid
        for job in fresh:
            prev = _last_statuses.get(job["id"])
            if prev == "pending" and job["status"] == "completed":
                from frontend.state import reapply_source_filter
                saved = list_user_extractions(user_id)
                row_data.clear()
                row_data.extend(db_record_to_row(r, i) for i, r in enumerate(saved))
                reapply_source_filter()
            _last_statuses[job["id"]] = job["status"]

        if not fresh:
            return

        # ── Header row: label + collapse toggle ───────────────────────────────
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Batch Jobs").style(
                "color:#475569; font-size:11px; font-weight:600; letter-spacing:0.5px;"
                "text-transform:uppercase; font-family:Inter,sans-serif"
            )
            toggle_btn = ui.button(
                icon="expand_more" if collapsed["v"] else "expand_less"
            ).props("flat round dense").style("color:#334155; opacity:0.6")

        jobs_list = ui.column().classes("w-full gap-2")
        jobs_list.set_visibility(not collapsed["v"])

        def _toggle():
            collapsed["v"] = not collapsed["v"]
            jobs_list.set_visibility(not collapsed["v"])
            toggle_btn.props(f"icon={'expand_more' if collapsed['v'] else 'expand_less'}")

        toggle_btn.on("click", _toggle)

        with jobs_list:
            for job in fresh:
                icon_name, color, label = _STATUS_STYLE.get(
                    job["status"], ("help_outline", "#64748b", job["status"])
                )
                n_items = len(__import__("json").loads(job.get("image_paths_json") or "[]"))
                is_video = job.get("provider") == "video"
                item_unit = "video" if is_video else "image"
                email = job.get("notify_email") or ""
                submitted = format_date(job["submitted_at"])

                with ui.row().classes("w-full items-center gap-3 px-4 py-3").style(
                    "background:#0f1117; border:1px solid rgba(240,225,205,0.06);"
                    "border-radius:10px;"
                ):
                    ui.icon(icon_name, size="1.1rem").style(f"color:{color}; flex-shrink:0")
                    with ui.column().classes("gap-0 flex-1"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f"{label} · {n_items} {item_unit}{'s' if n_items != 1 else ''}").style(
                                f"color:{color}; font-size:13px; font-weight:500;"
                                "font-family:Inter,sans-serif"
                            )
                            if job["status"] == "completed" and job.get("result_count") is not None:
                                n = job["result_count"]
                                ui.label(f"· {n} product{'s' if n != 1 else ''} extracted").style(
                                    "color:#64748b; font-size:12px; font-family:Inter,sans-serif"
                                )
                                if n:
                                    ui.label("· View results →").style(
                                        "color:#818cf8; font-size:12px; font-family:Inter,sans-serif;"
                                        "cursor:pointer; text-decoration:underline; text-underline-offset:2px"
                                    ).on("click", lambda: switch_to_batch_view())
                                skipped = job.get("skipped_count") or 0
                                if skipped:
                                    import json as _json
                                    from pathlib import Path as _Path
                                    from frontend.handlers import show_skipped_batch_dialog

                                    _names = _json.loads(job.get("skipped_names_json") or "[]")
                                    _all_paths = _json.loads(job.get("image_paths_json") or "[]")
                                    _names_set = set(_names)
                                    _skipped_paths = [
                                        p for p in _all_paths if _Path(p).name in _names_set
                                    ]
                                    _matched = {_Path(p).name for p in _skipped_paths}
                                    for _nm in _names:
                                        if _nm not in _matched:
                                            _skipped_paths.append(_nm)

                                    def _open_review(sp=_skipped_paths):
                                        show_skipped_batch_dialog(sp)

                                    ui.label(
                                        f"· {skipped} skipped — Review"
                                    ).style(
                                        "color:#ef4444; font-size:12px; font-family:Inter,sans-serif;"
                                        "cursor:pointer; text-decoration:underline;"
                                        "text-underline-offset:2px; opacity:0.8;"
                                    ).on("click", _open_review)
                        detail_parts = [f"Submitted {submitted}"]
                        if email:
                            detail_parts.append(f"notify {email}")
                        ui.label(" · ".join(detail_parts)).style(
                            "color:#334155; font-size:11px; font-family:Inter,sans-serif"
                        )
                    if job["status"] == "pending":
                        ui.spinner(size="sm").style("color:#f59e0b; flex-shrink:0")
                    # Clear button — only for completed/failed jobs
                    if job["status"] in ("completed", "failed"):
                        def _clear(jid=job["id"]):
                            delete_batch_job(jid)
                            _jobs_cards.refresh()
                        ui.button(icon="close", on_click=_clear).props(
                            "flat round dense"
                        ).style("color:#334155; opacity:0.5; flex-shrink:0").tooltip("Clear")

    _jobs_cards()
    set_batch_jobs_refresh(_jobs_cards.refresh)
    ui.timer(10, _jobs_cards.refresh)



@ui.page("/")
def main_page():
    user = require_user()
    if not user:
        return

    ui.dark_mode().enable()
    ui.colors(
        primary="#6366f1",
        secondary="#818cf8",
        positive="#10b981",
        negative="#ef4444",
        warning="#f59e0b",
    )
    ui.add_head_html(STYLES)
    ui.add_head_html(_DRIVER_CDN)

    # Hydrate the grid from SQLite on page load so a restart does not lose
    # prior uploads. The in-memory rows remain the active editing/export source
    # for the current browser session.
    saved_records = list_user_extractions(user["id"])
    row_data.clear()
    row_data.extend(
        db_record_to_row(record, idx) for idx, record in enumerate(saved_records)
    )


    render_header()
    render_review_drawer()

    with ui.column().classes("w-full px-6 py-5 gap-5"):
        render_upload_zone()
        _render_batch_jobs_section(user["id"])
        render_legend()
        render_grid()


    # ── Tour ─────────────────────────────────────────────────────────────────
    # tour_shown is stored per-user in the database so it survives server
    # restarts, logouts, and different browsers.
    if not user.get("tour_shown"):
        mark_tour_shown(user["id"])

        async def _launch_tour_delayed():
            client = ui.context.client
            await asyncio.sleep(1.2)
            _launch_tour(client)

        ui.timer(0, _launch_tour_delayed, once=True)


@ui.page("/history")
def history_page():
    user = require_user()
    if not user:
        return

    ui.dark_mode().enable()
    ui.colors(
        primary="#6366f1",
        secondary="#818cf8",
        positive="#10b981",
        negative="#ef4444",
        warning="#f59e0b",
    )
    ui.add_head_html(STYLES)
    render_header()
    render_review_drawer()

    rows = list_user_extractions(user["id"])
    with ui.column().classes("w-full px-6 py-5 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Upload history").classes("text-lg font-medium")
            ui.button(
                "Back to workspace",
                icon="arrow_back",
                on_click=lambda: ui.navigate.to("/"),
            ).props("flat color=white")

        if not rows:
            ui.label("No uploads yet.").classes("text-sm text-gray-400")

        for idx, record in enumerate(rows):
            row = db_record_to_row(record, idx)
            versions = list_extraction_versions(record["id"])

            def review(saved_row=row) -> None:
                # Put the selected historical row into the same in-memory row
                # collection used by the drawer save path. This keeps history
                # edits and workspace edits using one persistence path.
                row_data.clear()
                row_data.append(saved_row)
                open_review_drawer(saved_row)

            with ui.card().classes("w-full p-4 gap-3"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.column().classes("gap-1"):
                        ui.label(
                            record.get("product_name") or record["original_filename"]
                        ).classes("text-base font-medium")
                        record_created_at = format_date(record["created_at"])
                        record_updated_at = format_date(record["updated_at"])
                        ui.label(
                            f"Created: {record_created_at} · Modified: {record_updated_at}"
                        ).classes("text-xs text-gray-400")

                    with ui.row().classes("items-center gap-2"):
                        ui.badge(record["status"]).props(
                            "color=green"
                            if record["status"] == "ok"
                            else "color=orange"
                            if record["status"] == "warn"
                            else "color=red"
                        )
                        ui.button("Review", icon="edit", on_click=review).props(
                            "color=indigo"
                        )

                with ui.row().classes("gap-6 text-sm text-gray-300"):
                    ui.label(f"File: {record['original_filename']}")
                    ui.label(f"Brand: {record.get('brand') or '-'}")
                    ui.label(f"Weight: {record.get('weight') or '-'}")

                with ui.expansion(
                    f"Versions ({len(versions)})", icon="history"
                ).classes("w-full"):
                    for version in versions:
                        product_name = version["record"].get("product_name") or "-"
                        created_at = format_date(version["created_at"])
                        ui.label(
                            f"v{version['version_number']} · {version['reason']} · "
                            f"{created_at} · {product_name}"
                        ).classes("text-xs text-gray-400")


@ui.page("/catalog")
def catalog_page():
    user = require_user()
    if not user:
        return

    ui.dark_mode().enable()
    ui.colors(
        primary="#6366f1",
        secondary="#818cf8",
        positive="#10b981",
        negative="#ef4444",
        warning="#f59e0b",
    )
    ui.add_head_html(STYLES)
    render_header()

    brands = list_brand_catalog()

    with ui.column().classes("w-full px-6 py-5 gap-5"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-1"):
                ui.label("Brand Catalog").style(
                    "font-size:20px; font-weight:600; color:#e2e8f0;"
                    "font-family:Inter,sans-serif; letter-spacing:-0.3px;"
                )
                ui.label(
                    f"{len(brands)} brand{'s' if len(brands) != 1 else ''} learned from your extractions"
                ).style("font-size:13px; color:#475569; font-family:Inter,sans-serif;")
            ui.button(
                "Back to workspace", icon="arrow_back",
                on_click=lambda: ui.navigate.to("/"),
            ).props("flat color=white").classes("text-xs")

        if not brands:
            with ui.column().classes("w-full items-center py-16 gap-3"):
                ui.icon("auto_awesome", size="2.5rem").style("color:rgba(99,102,241,0.3)")
                ui.label("No brands yet").style(
                    "font-size:15px; color:#475569; font-family:Inter,sans-serif;"
                )
                ui.label(
                    "Upload and extract products -- every brand you process is cataloged here."
                ).style("font-size:13px; color:#334155; font-family:Inter,sans-serif;")
        else:
            # Table header
            with ui.row().classes("w-full px-4 py-2 gap-0").style(
                "border-bottom:1px solid rgba(255,255,255,0.06);"
            ):
                for label, width in [
                    ("Brand", "25%"), ("Manufacturer", "20%"), ("Category", "15%"),
                    ("Country", "15%"), ("Packaging", "12%"), ("Products", "13%"),
                ]:
                    ui.label(label).style(
                        f"width:{width}; font-size:11px; font-weight:600; color:#475569;"
                        "text-transform:uppercase; letter-spacing:0.4px;"
                        "font-family:Inter,sans-serif;"
                    )

            for brand in brands:
                with ui.row().classes("w-full px-4 py-3 gap-0 items-center catalog-row").style(
                    "border-bottom:1px solid rgba(255,255,255,0.04);"
                ):
                    ui.label(brand["brand"] or "").style(
                        "width:25%; font-size:13px; font-weight:500; color:#e2e8f0;"
                        "font-family:Inter,sans-serif;"
                    )
                    ui.label(brand.get("manufacturer") or "--").style(
                        "width:20%; font-size:12px; color:#94a3b8;"
                        "font-family:Inter,sans-serif;"
                    )
                    ui.label(brand.get("category_type") or "--").style(
                        "width:15%; font-size:12px; color:#94a3b8;"
                        "font-family:Inter,sans-serif;"
                    )
                    ui.label(brand.get("country_of_origin") or "--").style(
                        "width:15%; font-size:12px; color:#94a3b8;"
                        "font-family:Inter,sans-serif;"
                    )
                    ui.label(brand.get("packaging_type") or "--").style(
                        "width:12%; font-size:12px; color:#94a3b8;"
                        "font-family:Inter,sans-serif;"
                    )
                    count = brand.get("product_count", 0)
                    ui.html(
                        f'<span style="width:13%; font-size:12px; font-weight:600;'
                        f' color:#818cf8; font-family:Inter,sans-serif;">'
                        f'{count} product{"s" if count != 1 else ""}</span>'
                    )


if __name__ in {"__main__", "__mp_main__"}:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Keep NiceGUI / uvicorn / httpx chatter at WARNING so batch logs stand out
    for _noisy in ("uvicorn", "uvicorn.access", "httpx", "httpcore", "nicegui"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    register_coming_soon()
    init_db()

    brands_csv = Path("data/canonical_brands.csv")
    if brands_csv.exists():
        load_canonical_brands(brands_csv)

    log.info("IMDB AutoFill starting up")

    ui.run(
        favicon="🔎",
        title="IMDB AutoFill",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5300")),
        dark=True,
        reload=False,
        storage_secret=os.getenv("STORAGE_SECRET", "imdb-secret-key"),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
