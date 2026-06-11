import os
from pathlib import Path

from nicegui import app, ui

# Importing this module registers the /login and /signup pages with NiceGUI.
import frontend.auth_pages  # noqa: F401
from backend.db import init_db, list_extraction_versions, list_user_extractions
from backend.normalizer import load_canonical_brands
from frontend.auth_pages import require_user
from frontend.components import (
    open_review_drawer,
    render_grid,
    render_header,
    render_legend,
    render_review_drawer,
    render_upload_zone,
)
from frontend.state import db_record_to_row, row_data
from frontend.styles import STYLES
from frontend.utils import format_date
UPLOAD_DIR = Path("data/uploads")

# Expose uploaded images as normal static files. AG Grid rows only store these
# short URLs, which keeps click/edit websocket payloads small.
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.add_static_files("/uploads", UPLOAD_DIR)


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
        render_legend()
        render_grid()


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
                    ui.label(
                        f"Weight: {record.get('weight') or '-'}"
                    )

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
    init_db()

    brands_csv = Path("data/canonical_brands.csv")
    if brands_csv.exists():
        load_canonical_brands(brands_csv)

    ui.run(
        favicon="🔎",
        title="IMDB AutoFill",
        port=5200,
        dark=True,
        reload=False,
        storage_secret="imdb-secret-key",
    )
