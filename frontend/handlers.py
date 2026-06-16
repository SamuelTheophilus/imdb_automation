import logging
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
from nicegui import events, ui

from backend.db import create_extraction, delete_extraction, update_extraction_fields
from backend.pipeline import PipelineResult, run_pipeline
from frontend.auth_pages import current_user
from frontend.state import FIELDS, get_grid, result_to_row, row_data, row_to_export_dict

QUICK_UPLOAD_LIMIT = 20
BULK_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})

SAMPLES: dict[str, dict] = {
    "milo": {
        "label": "MILO + BLUE BAND",
        "category": "Chocolate Drink · Margarine",
        "note": "7 images · 2 products",
        "dir": Path("data/samples/milo"),
        "thumbnail": "/samples/milo/S233576818_584688925.jpg",
    },
    "lux": {
        "label": "LUX Bright Impress",
        "category": "Bar Soap",
        "note": "4 images · 1 product",
        "dir": Path("data/samples/lux"),
        "thumbnail": "/samples/lux/S233457181_584380459.jpg",
    },
    "vibe": {
        "label": "VIBE Energy",
        "category": "Energy Drink",
        "note": "6 images · 1 product",
        "dir": Path("data/samples/vibe"),
        "thumbnail": "/samples/vibe/S231045463_577932905.jpg",
    },
}


async def handle_batch_upload(e: events.MultiUploadEventArguments):
    # Import here to avoid a circular import (components imports handlers).
    from frontend.components import hide_processing, show_processing

    client = ui.context.client
    user = current_user()
    if not user:
        ui.notify("Please log in before uploading", type="warning", position="center")
        return
    if not e.files:
        ui.notify("No files selected", type="warning", position="center")
        return
    if len(e.files) > QUICK_UPLOAD_LIMIT:
        ui.notify(
            f"Quick Upload is limited to {QUICK_UPLOAD_LIMIT} images. "
            f"You selected {len(e.files)} — use the Bulk Batch tab for larger sets.",
            type="warning",
            position="center",
            timeout=6000,
        )
        return

    upload_dir = Path("data/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    filenames: list[str] = []
    for file in e.files:
        filename = file.name
        suffix = Path(filename).suffix or ".jpg"
        saved_path = upload_dir / f"{Path(filename).stem}_{uuid4().hex[:8]}{suffix}"
        await file.save(saved_path)
        saved_paths.append(saved_path)
        filenames.append(filename)

    upload_label = (
        f"{len(saved_paths)} image"
        if len(saved_paths) == 1
        else f"{len(saved_paths)} images"
    )
    show_processing(f"Processing {upload_label}…")

    try:
        results: list[PipelineResult] = await run_pipeline(saved_paths)
    except Exception as exc:
        if getattr(client, "_deleted", False):
            return
        hide_processing()
        ui.notify(f"Processing failed: {exc}", type="negative", position="center")
        return

    original_filename = ", ".join(filenames)
    for result in results:
        extraction_id = create_extraction(
            user_id=user["id"],
            original_filename=original_filename,
            result=result,
        )
        row = result_to_row(result, len(row_data))
        row["db_id"] = extraction_id
        row_data.append(row)

    if getattr(client, "_deleted", False):
        return

    grid = get_grid()
    if grid:
        grid.options["rowData"] = list(row_data)
        grid.update()

    hide_processing()

    if any(result.has_duplicates for result in results):
        ui.notify(
            f"Possible duplicate detected in {upload_label}",
            type="warning",
            position="center",
        )
    else:
        n = len(results)
        label = "product group" if n == 1 else "product groups"
        ui.notify(f"{n} {label} extracted", type="positive", position="center")


def _export_filename(ext: str) -> str:
    user = current_user()
    username = (user["username"] if user else "user").lower().replace(" ", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"imdb_autofill_export_{username}_{timestamp}.{ext}"


def do_export_csv():
    if not row_data:
        ui.notify("No data to export yet", type="warning", position="center")
        return
    df = pd.DataFrame([row_to_export_dict(row) for row in row_data])
    path = Path("data") / _export_filename("csv")
    path.parent.mkdir(exist_ok=True)
    df.to_csv(path, index=False)
    ui.download(str(path))
    ui.notify("CSV exported", type="positive", position="center")


def do_export_excel():
    if not row_data:
        ui.notify("No data to export yet", type="warning", position="center")
        return
    df = pd.DataFrame([row_to_export_dict(row) for row in row_data])
    path = Path("data") / _export_filename("xlsx")
    path.parent.mkdir(exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
        ws = writer.sheets["Sheet1"]
        from openpyxl.styles import Font
        bold = Font(bold=True)
        for cell in ws[1]:
            cell.font = bold
    ui.download(str(path))
    ui.notify("Excel exported", type="positive", position="center")


def persist_row_edits(row: dict) -> None:
    """Save edited grid/drawer values back to SQLite when a row has a db id."""
    extraction_id = row.get("db_id")
    if not extraction_id:
        return
    update_extraction_fields(
        int(extraction_id),
        {key: row.get(key, "") for key, _ in FIELDS},
    )


def do_delete_row(row: dict) -> None:
    """Handle the backend deletion of a record and all its associated images."""
    extraction_id = row.get("db_id")
    if extraction_id:
        delete_extraction(int(extraction_id))

    paths = row.get("image_paths") or [row.get("image_path", "")]
    for raw_path in paths:
        p = Path(raw_path)
        if p.exists():
            p.unlink()


async def handle_sample(sample_key: str) -> None:
    from frontend.components import hide_processing, show_processing

    sample = SAMPLES.get(sample_key)
    if not sample:
        return

    user = current_user()
    if not user:
        ui.notify("Please log in before running a sample", type="warning", position="center")
        return

    upload_dir = Path("data/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Copy sample images into uploads/ with unique names so they're served correctly
    saved_paths: list[Path] = []
    for src in sorted(sample["dir"].glob("*.jpg")):
        dst = upload_dir / f"sample_{sample_key}_{uuid4().hex[:8]}{src.suffix}"
        shutil.copy2(src, dst)
        saved_paths.append(dst)

    show_processing(f"Processing {sample['label']}…")

    try:
        results: list[PipelineResult] = await run_pipeline(saved_paths)
    except Exception as exc:
        hide_processing()
        ui.notify(f"Sample extraction failed: {exc}", type="negative", position="center")
        return

    for result in results:
        extraction_id = create_extraction(
            user_id=user["id"],
            original_filename=f"[Sample] {sample['label']}",
            result=result,
        )
        row = result_to_row(result, len(row_data))
        row["db_id"] = extraction_id
        row_data.append(row)

    grid = get_grid()
    if grid:
        grid.options["rowData"] = list(row_data)
        grid.update()

    hide_processing()

    n = len(results)
    label = "product" if n == 1 else "products"
    ui.notify(f"{n} {label} extracted from {sample['label']} sample", type="positive", position="center")


# ── Bulk Batch upload ─────────────────────────────────────────────────────────

async def stage_bulk_files(
    e: events.MultiUploadEventArguments,
) -> tuple[list[Path], int]:
    """Save uploaded files to disk; return (saved_image_paths, skipped_count).

    Non-image extensions are counted as skipped and not saved.
    """
    upload_dir = Path("data/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    skipped = 0
    for file in e.files:
        suffix = Path(file.name).suffix.lower()
        if suffix not in BULK_IMAGE_EXTS:
            skipped += 1
            continue
        path = upload_dir / f"bulk_{uuid4().hex[:8]}{suffix}"
        await file.save(path)
        saved.append(path)
    return saved, skipped


async def handle_bulk_start(
    paths: list[Path],
    notify_email: str,
    user: dict | None,
) -> None:
    """Submit staged images to the Anthropic Batch API and store the job."""
    if not user:
        raise ValueError("You must be logged in to submit a batch job.")
    from backend.batch_processor import submit_bulk_batch
    await submit_bulk_batch(paths, notify_email, user["id"])
