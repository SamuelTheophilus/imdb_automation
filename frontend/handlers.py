import logging
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
from nicegui import events, ui

from backend.db import create_extraction, delete_extraction, list_user_extractions, update_extraction_fields
from backend.pipeline import PipelineResult, run_pipeline
from frontend.auth_pages import current_user
from frontend.state import FIELDS, failed_row, get_client_model, get_grid, reapply_source_filter, result_to_row, row_data, row_to_export_dict

QUICK_UPLOAD_LIMIT = 20
BULK_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


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

    results: list[PipelineResult] = []
    pipeline_error: Exception | None = None
    try:
        existing_records = list_user_extractions(user["id"])
        results = await run_pipeline(
            saved_paths,
            existing_records=existing_records,
            model_display_name=get_client_model(),
        )
    except Exception as exc:
        pipeline_error = exc
    finally:
        if not getattr(client, "_deleted", False):
            hide_processing()
            from frontend.state import reset_quick_upload
            reset_quick_upload()

    if pipeline_error is not None:
        if not getattr(client, "_deleted", False):
            ui.notify(f"Processing failed: {pipeline_error}", type="negative", position="center")
        return

    if getattr(client, "_deleted", False):
        return

    original_filename = ", ".join(filenames)

    covered: set[str] = set()
    for result in results:
        for p in result.image_paths:
            covered.add(str(p))

    for result in results:
        extraction_id = create_extraction(
            user_id=user["id"],
            original_filename=original_filename,
            result=result,
            barcode_audit=result.barcode_audit,
        )
        row = result_to_row(result, len(row_data))
        row["db_id"] = extraction_id
        row_data.append(row)

    reapply_source_filter()

    # Images with no product detected — show bulk keep/discard dialog
    skipped_paths = [p for p in saved_paths if str(p) not in covered]
    if skipped_paths:
        show_skipped_batch_dialog([str(p) for p in skipped_paths])
        return

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
    return f"predictions.{ext}"


_EXPORT_OK_STATUSES = {"ok", "duplicate", "warn"}


def _rows_for_export(scope: str) -> list[dict]:
    if scope == "ok":
        return [r for r in row_data if r.get("_status") == "ok"]
    return [r for r in row_data if r.get("_status") in _EXPORT_OK_STATUSES]


def do_export_csv(scope: str = "all"):
    rows = _rows_for_export(scope)
    if not rows:
        ui.notify("No data to export", type="warning", position="center")
        return
    df = pd.DataFrame([row_to_export_dict(row) for row in rows])
    path = Path("data") / _export_filename("csv")
    path.parent.mkdir(exist_ok=True)
    df.to_csv(path, index=False)
    ui.download(str(path))
    label = "approved" if scope == "ok" else "all reviewed"
    ui.notify(f"CSV exported ({label}, {len(rows)} rows)", type="positive", position="center")


def do_export_excel(scope: str = "all"):
    rows = _rows_for_export(scope)
    if not rows:
        ui.notify("No data to export", type="warning", position="center")
        return
    df = pd.DataFrame([row_to_export_dict(row) for row in rows])
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
    label = "approved" if scope == "ok" else "all reviewed"
    ui.notify(f"Excel exported ({label}, {len(rows)} rows)", type="positive", position="center")


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


async def _show_no_product_dialog(path: Path, current: int, total: int) -> None:
    """Show a per-image dialog when no product was detected.
    Keep → add as a grid entry. Discard or Close → do nothing.
    """
    from frontend.state import image_to_url

    counter = f"Image {current} of {total} — " if total > 1 else ""

    with ui.dialog() as dlg, ui.card().style(
        "min-width:380px; max-width:440px; background:#1e1c19;"
        "border:1px solid rgba(240,225,205,0.09); border-radius:16px; padding:0; overflow:hidden;"
    ):
        # ── Header ────────────────────────────────────────────────────────────
        with ui.row().classes("w-full items-center justify-between px-5 py-4").style(
            "border-bottom:1px solid rgba(240,225,205,0.07)"
        ):
            ui.html(
                f'<p style="font-size:14px;font-weight:700;color:#f0ebe5;'
                f'font-family:Inter,sans-serif;letter-spacing:-0.3px;margin:0">'
                f'{counter}No product detected</p>'
            )
            ui.button(icon="close", on_click=lambda: dlg.submit("close")).props(
                "flat round dense"
            ).style("color:#475569")

        # ── Image + message ───────────────────────────────────────────────────
        with ui.column().classes("w-full px-5 py-4 gap-3"):
            ui.html(
                '<p style="font-size:13px;color:#52504c;font-family:Inter,sans-serif;'
                'line-height:1.6;margin:0">'
                f'The AI could not detect any product in <strong style="color:#a09890">'
                f'{path.name}</strong>. Keep it in the list or discard it?</p>'
            )
            url = image_to_url(str(path))
            if url:
                ui.image(url).style(
                    "width:100%; height:180px; object-fit:cover; border-radius:10px;"
                    "border:1px solid rgba(240,225,205,0.07);"
                )

        # ── Actions ───────────────────────────────────────────────────────────
        with ui.row().classes("w-full justify-between px-5 py-4").style(
            "border-top:1px solid rgba(240,225,205,0.07)"
        ):
            ui.button("Discard", on_click=lambda: dlg.submit("discard")).props(
                "flat"
            ).style("color:#ef4444;font-weight:600;font-family:Inter,sans-serif")
            ui.button("Keep", on_click=lambda: dlg.submit("keep")).props(
                "unelevated color=indigo-5"
            ).style("font-weight:600;font-family:Inter,sans-serif")

    result = await dlg
    if result == "keep":
        row_data.append(failed_row(str(path), "No product detected", len(row_data)))
        reapply_source_filter()


# ── Skipped-image review dialog ───────────────────────────────────────────────

def show_skipped_batch_dialog(skipped_paths: list[str]) -> None:
    """Show a single bulk keep/discard dialog for images that had no product detected."""
    if not skipped_paths:
        return

    from frontend.state import failed_row, get_grid, image_to_url, row_data

    _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv"}

    def _is_video(p: str) -> bool:
        return Path(p).suffix.lower() in _VIDEO_EXTS

    n = len(skipped_paths)
    decisions: dict[str, bool] = {p: False for p in skipped_paths}  # False = discard

    _CARD = (
        "background:#0d1117; border:1px solid rgba(255,255,255,0.08);"
        "border-radius:14px; padding:0; overflow:hidden; min-width:680px; max-width:780px;"
    )
    _HDR = "border-bottom:1px solid rgba(255,255,255,0.06);"
    _COL_HDR = (
        "font-size:10px; font-weight:600; color:#475569; font-family:Inter,sans-serif;"
        "letter-spacing:0.5px; text-transform:uppercase;"
    )
    _SEL_KEEP = (
        "font-size:11px; font-weight:600; color:#10b981; font-family:Inter,sans-serif;"
        "cursor:pointer; padding:4px 10px;"
        "border:1px solid rgba(16,185,129,0.5); background:rgba(16,185,129,0.08); border-radius:6px;"
    )
    _UNSEL_KEEP = (
        "font-size:11px; font-weight:600; color:#10b981; font-family:Inter,sans-serif;"
        "cursor:pointer; padding:4px 10px;"
        "border:1px solid transparent; background:transparent; border-radius:6px; opacity:0.35;"
    )
    _SEL_DISC = (
        "font-size:11px; font-weight:600; color:#ef4444; font-family:Inter,sans-serif;"
        "cursor:pointer; padding:4px 10px;"
        "border:1px solid rgba(239,68,68,0.5); background:rgba(239,68,68,0.08); border-radius:6px;"
    )
    _UNSEL_DISC = (
        "font-size:11px; font-weight:600; color:#ef4444; font-family:Inter,sans-serif;"
        "cursor:pointer; padding:4px 10px;"
        "border:1px solid transparent; background:transparent; border-radius:6px; opacity:0.35;"
    )

    btn_states: dict[str, dict] = {}

    with ui.dialog() as dlg, ui.card().style(_CARD):
        # Header
        with ui.row().classes("w-full items-center justify-between px-5 py-4").style(_HDR):
            with ui.column().classes("gap-0"):
                ui.label(
                    f"{n} image{'s' if n != 1 else ''} had no product detected"
                ).style(
                    "font-size:15px; font-weight:700; color:#e2e8f0; font-family:Inter,sans-serif;"
                )
                ui.label("Review each image and choose what to do.").style(
                    "font-size:12px; color:#475569; font-family:Inter,sans-serif;"
                )
            ui.button(icon="close", on_click=dlg.close).props("flat round dense").style("color:#475569;")

        # Column headers
        with ui.row().classes("w-full px-5 py-2 gap-0").style(
            "background:rgba(255,255,255,0.02); border-bottom:1px solid rgba(255,255,255,0.04);"
        ):
            ui.label("Image").style(_COL_HDR + " flex:0 0 96px;")
            ui.label("File").style(_COL_HDR + " flex:1;")
            ui.label("Action").style(_COL_HDR + " flex:0 0 170px; text-align:right;")

        # Scrollable rows
        with ui.scroll_area().style("max-height:360px; width:100%;"):
            for path in skipped_paths:
                p_obj = Path(path)
                url = image_to_url(path) if p_obj.exists() else None
                fname = p_obj.name if p_obj.name else path

                with ui.row().classes("w-full items-center px-5 py-3 gap-3").style(
                    "border-bottom:1px solid rgba(255,255,255,0.03);"
                ):
                    if url and _is_video(path):
                        ui.html(
                            f'<video src="{url}" muted preload="metadata"'
                            f' style="width:80px; height:64px; object-fit:cover;'
                            f' border-radius:6px; border:1px solid rgba(255,255,255,0.07);'
                            f' flex-shrink:0; display:block;"></video>'
                        )
                    elif url:
                        ui.image(url).style(
                            "width:80px; height:64px; object-fit:cover; border-radius:6px;"
                            "border:1px solid rgba(255,255,255,0.07); flex-shrink:0;"
                        )
                    else:
                        ui.html(
                            '<div style="width:80px; height:64px; border-radius:6px;'
                            ' border:1px solid rgba(255,255,255,0.07); display:flex;'
                            ' align-items:center; justify-content:center; flex-shrink:0;">'
                            '<span style="font-size:10px; color:#334155;'
                            ' font-family:Inter,sans-serif;">No preview</span>'
                            '</div>'
                        )

                    ui.label(fname).style(
                        "font-size:12px; color:#64748b; font-family:Inter,sans-serif;"
                        "flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"
                    )

                    with ui.row().classes("gap-2 items-center").style(
                        "flex:0 0 170px; justify-content:flex-end;"
                    ):
                        keep_el = ui.label("Keep").style(_UNSEL_KEEP)
                        disc_el = ui.label("Discard").style(_SEL_DISC)
                        btn_states[path] = {"keep": keep_el, "disc": disc_el}

                    def _set_keep(p=path):
                        decisions[p] = True
                        btn_states[p]["keep"].style(_SEL_KEEP)
                        btn_states[p]["disc"].style(_UNSEL_DISC)

                    def _set_discard(p=path):
                        decisions[p] = False
                        btn_states[p]["keep"].style(_UNSEL_KEEP)
                        btn_states[p]["disc"].style(_SEL_DISC)

                    keep_el.on("click", _set_keep)
                    disc_el.on("click", _set_discard)

        # Footer
        def _keep_all():
            for p in skipped_paths:
                decisions[p] = True
                btn_states[p]["keep"].style(_SEL_KEEP)
                btn_states[p]["disc"].style(_UNSEL_DISC)

        def _discard_all():
            for p in skipped_paths:
                decisions[p] = False
                btn_states[p]["keep"].style(_UNSEL_KEEP)
                btn_states[p]["disc"].style(_SEL_DISC)

        def _apply():
            kept = [p for p, keep in decisions.items() if keep]
            for p in kept:
                from frontend.state import FIELDS as _FIELDS
                row: dict = {
                    "id":          len(row_data),
                    "thumbnail":   image_to_url(p),
                    "image_path":  p,
                    "image_paths": [p],
                    "_status":     "warn",
                    "_normalized": "",
                    "_low":        "product_name",
                    "product_name": "",
                }
                for key, _ in _FIELDS:
                    if key not in row:
                        row[key] = ""
                row_data.append(row)
            reapply_source_filter()
            dlg.close()
            if kept:
                k = len(kept)
                ui.notify(
                    f"{k} image{'s' if k != 1 else ''} added to the grid",
                    type="positive",
                    position="center",
                )

        with ui.row().classes("w-full items-center justify-between px-5 py-4").style(
            "border-top:1px solid rgba(255,255,255,0.06);"
        ):
            with ui.row().classes("gap-3"):
                ui.button("Keep all", on_click=_keep_all).props("flat color=positive").style(
                    "font-size:11px; font-family:Inter,sans-serif; font-weight:600;"
                )
                ui.button("Discard all", on_click=_discard_all).props("flat color=negative").style(
                    "font-size:11px; font-family:Inter,sans-serif; font-weight:600;"
                )
            with ui.row().classes("gap-3"):
                ui.button("Cancel", on_click=dlg.close).props("flat").style(
                    "color:#475569; font-family:Inter,sans-serif;"
                )
                ui.button("Apply", on_click=_apply).props("unelevated").style(
                    "background:#6366f1; color:#fff; font-family:Inter,sans-serif;"
                    "font-size:13px; font-weight:600; border-radius:8px; padding:6px 18px;"
                )

    dlg.open()


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
    """Submit staged images to the selected provider's Batch API and store the job."""
    if not user:
        raise ValueError("You must be logged in to submit a batch job.")
    from backend.batch_processor import submit_bulk_batch
    await submit_bulk_batch(paths, notify_email, user["id"], model_display_name=get_client_model())
