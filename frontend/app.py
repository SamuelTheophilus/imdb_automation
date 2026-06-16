import asyncio
import logging
import os
from pathlib import Path

from nicegui import app, ui

# Importing this module registers the /login and /signup pages with NiceGUI.
import frontend.auth_pages  # noqa: F401
from backend.db import init_db, list_batch_jobs, list_extraction_versions, list_user_extractions
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
SAMPLES_DIR = Path("data/samples")

# Expose uploaded and sample images as static files.
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.add_static_files("/uploads", UPLOAD_DIR)
app.add_static_files("/samples", SAMPLES_DIR)

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
        log.info("[batch_poller] cycle #%d done — next in 5 min", cycle)
        await asyncio.sleep(300)


@app.on_startup
async def _start_batch_poller() -> None:
    asyncio.create_task(_batch_poll_loop())


_STATUS_STYLE = {
    "pending":   ("schedule",      "#f59e0b", "Processing"),
    "completed": ("check_circle",  "#10b981", "Complete"),
    "failed":    ("error_outline", "#ef4444", "Failed"),
}


def _render_batch_jobs_section(user_id: int) -> None:
    """Show the user's recent batch jobs with auto-refresh every 60 seconds.

    Only renders when there is at least one job in the last 48 hours.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    jobs = [j for j in list_batch_jobs(user_id) if j["submitted_at"] >= cutoff]
    if not jobs:
        return

    @ui.refreshable
    def _jobs_cards() -> None:
        fresh = [j for j in list_batch_jobs(user_id) if j["submitted_at"] >= cutoff]
        if not fresh:
            return

        with ui.column().classes("w-full gap-2"):
            ui.label("Batch Jobs").style(
                "color:#475569; font-size:11px; font-weight:600; letter-spacing:0.5px;"
                "text-transform:uppercase; font-family:Inter,sans-serif"
            )
            for job in fresh:
                icon_name, color, label = _STATUS_STYLE.get(
                    job["status"], ("help_outline", "#64748b", job["status"])
                )
                n_images = len(__import__("json").loads(job.get("image_paths_json") or "[]"))
                email = job.get("notify_email") or ""
                submitted = format_date(job["submitted_at"])

                with ui.row().classes("w-full items-center gap-3 px-4 py-3").style(
                    "background:#0f1117; border:1px solid rgba(240,225,205,0.06);"
                    "border-radius:10px;"
                ):
                    ui.icon(icon_name, size="1.1rem").style(f"color:{color}; flex-shrink:0")
                    with ui.column().classes("gap-0 flex-1"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f"{label} · {n_images} images").style(
                                f"color:{color}; font-size:13px; font-weight:500;"
                                "font-family:Inter,sans-serif"
                            )
                            if job["status"] == "completed" and job.get("result_count") is not None:
                                n = job["result_count"]
                                ui.label(f"· {n} product{'s' if n != 1 else ''} extracted").style(
                                    "color:#64748b; font-size:12px; font-family:Inter,sans-serif"
                                )
                        detail_parts = [f"Submitted {submitted}"]
                        if email:
                            detail_parts.append(f"notify {email}")
                        ui.label(" · ".join(detail_parts)).style(
                            "color:#334155; font-size:11px; font-family:Inter,sans-serif"
                        )
                    if job["status"] == "pending":
                        ui.spinner(size="sm").style("color:#f59e0b; flex-shrink:0")

    _jobs_cards()
    ui.timer(60, _jobs_cards.refresh)



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
    # Fire once for first-time users; the "?" header button lets anyone replay.
    async def _maybe_tour():
        tour_key = f"tour_shown_{user['id']}"
        if app.storage.user.get(tour_key):
            return
        app.storage.user[tour_key] = True
        client = ui.context.client  # capture before yielding context
        await asyncio.sleep(1.2)  # let the page fully render first
        _launch_tour(client)

    ui.timer(0, _maybe_tour, once=True)


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
