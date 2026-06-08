from pathlib import Path
from uuid import uuid4

import pandas as pd
from nicegui import events, ui

from backend.db import create_extraction, delete_extraction, update_extraction_fields
from backend.pipeline import PipelineResult, run_pipeline
from frontend.auth_pages import current_user
from frontend.state import FIELDS, get_grid, result_to_row, row_data, row_to_export_dict


async def handle_batch_upload(e: events.MultiUploadEventArguments):
    client = ui.context.client
    user = current_user()
    if not user:
        ui.notify("Please log in before uploading", type="warning")
        return
    if not e.files:
        ui.notify("No files selected", type="warning")
        return

    # Persist the uploaded image in the project data folder. The extraction
    # history stores this path and the review drawer uses it for the large image.
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

    # persistent notification that we update instead of creating new ones
    upload_label = f"{len(saved_paths)} image" if len(saved_paths) == 1 else f"{len(saved_paths)} images"
    notification = ui.notification(
        message=f"Processing {upload_label}…",
        spinner=True,
        timeout=None,
        type="ongoing",
    )

    try:
        results: list[PipelineResult] = await run_pipeline(saved_paths)
    except Exception as exc:
        if getattr(client, "_deleted", False):
            return
        notification.spinner = False
        notification.type = "negative"
        notification.message = f"Failed: {exc}"
        notification.timeout = 3
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

    notification.spinner = False
    notification.timeout = 3
    if any(result.has_duplicates for result in results):
        notification.type = "warning"
        notification.message = f"Possible duplicate in {upload_label}"
    else:
        notification.type = "positive"
        notification.message = f"{len(results)} product group(s) extracted"


def do_export_csv():
    if not row_data:
        ui.notify("No data to export yet", type="warning")
        return
    df = pd.DataFrame([row_to_export_dict(row) for row in row_data])
    path = Path("data/predictions.csv")
    path.parent.mkdir(exist_ok=True)
    df.to_csv(path, index=False)
    ui.download(str(path))
    ui.notify("CSV exported ✓", type="positive")


def do_export_excel():
    if not row_data:
        ui.notify("No data to export yet", type="warning")
        return
    df = pd.DataFrame([row_to_export_dict(row) for row in row_data])
    path = Path("data/predictions.xlsx")
    path.parent.mkdir(exist_ok=True)
    df.to_excel(path, index=False)
    ui.download(str(path))
    ui.notify("Excel exported ✓", type="positive")


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
