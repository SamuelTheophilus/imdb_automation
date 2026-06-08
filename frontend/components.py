from nicegui import ui

from frontend.auth_pages import current_user, logout

# from frontend.utils import format_date
from frontend.handlers import (
    do_delete_row,
    do_export_csv,
    do_export_excel,
    handle_batch_upload,
    persist_row_edits,
)
from frontend.state import FIELDS, build_column_defs, get_grid, image_to_url, row_data, set_grid

review_drawer = None
review_carousel_container = None
review_title = None
review_inputs: dict[str, ui.input] = {}
review_row_id: int | None = None


def render_header():
    with (
        ui.row()
        .classes("w-full items-center justify-between px-6 py-4")
        .style("border-bottom: 1px solid rgba(255,255,255,0.06); background: #0a0a0f;")
    ):
        with ui.row().classes("items-center gap-3"):
            # ui.label("⬡").style("color:#6366f1; font-size:1.4rem")
            # ui.label("IMDB AutoFill").classes("text-base font-medium mono").style(
            #     "letter-spacing:-0.3px; color:#e5e7eb"
            # )
            ui.link("⬡", "/").style(
                "color:#6366f1; font-size:1.4rem; text-decoration:none"
            )
            ui.link("IMDB AutoFill", "/").classes("text-base font-medium mono").style(
                "letter-spacing:-0.3px; color:#e5e7eb; text-decoration:none"
            )
            ui.badge("beta").props("color=indigo").classes("text-xs")

        with ui.row().classes("gap-2 item-center"):
            user = current_user()
            if user:
                username = user["username"][:5]
                ui.label(f"{username}...").classes(
                    "text-xs text-gray-400 self-center q-py-xs"
                )
                ui.button(
                    "History",
                    icon="history",
                    on_click=lambda: ui.navigate.to("/history"),
                ).props("flat color=white").classes("text-xs")
            ui.button("Export CSV", icon="download", on_click=do_export_csv).props(
                "flat color=white"
            ).classes("text-xs")
            ui.button(
                "Export Excel", icon="table_chart", on_click=do_export_excel
            ).props("flat color=white").classes("text-xs")
            ui.button("Log out", icon="logout", on_click=logout).props(
                "flat color=white"
            ).classes("text-xs")


def render_review_drawer():
    """Create the right-side row editor used by `open_review_drawer`.

    The grid is optimized for scanning many records. This drawer is optimized
    for correction: bigger image at the top, then one editable input per field.
    """
    global review_drawer, review_carousel_container, review_title, review_inputs

    review_inputs = {}
    review_drawer = (
        ui.right_drawer(value=False, fixed=True, bordered=True)
        .classes("bg-[#0f0f17] p-4")
        .style("width: 440px;")
    )
    with review_drawer:
        with ui.row().classes("w-full items-center justify-between"):
            review_title = ui.label("Review extraction").classes(
                "text-base font-medium"
            )
            ui.button(icon="close", on_click=review_drawer.hide).props(
                "flat round dense color=white"
            )

        # Carousel container — rebuilt each time the drawer opens.
        # ui.column() is used here rather than ui.element("div") because
        # NiceGUI's dynamic child injection (clear + with) is designed for
        # layout containers, not raw HTML element wrappers.
        review_carousel_container = ui.column().classes("w-full p-0 gap-0")

        with ui.column().classes("w-full gap-2 mt-3"):
            for key, label in FIELDS:
                review_inputs[key] = ui.input(label).classes("w-full")

        with ui.row().classes("w-full justify-end gap-8 mt-3"):
            ui.button("Save", icon="save", on_click=save_review_drawer).props(
                "color=indigo"
            )
            ui.button(
                "Delete", icon="delete", on_click=delete_from_review_drawer
            ).props("flat color=red")


def open_review_drawer(row: dict):
    """Populate and open the drawer for the clicked grid row."""
    global review_row_id
    if review_drawer is None:
        return

    review_row_id = row["id"]
    if review_title is not None:
        review_title.text = row.get("product_name") or "Review extraction"

    if review_carousel_container is not None:
        try:
            paths: list[str] = row.get("image_paths") or [row.get("image_path", "")]
            urls = [image_to_url(p) for p in paths if p]

            review_carousel_container.clear()
            with review_carousel_container:
                if len(urls) > 1:
                    with (
                        ui.carousel(animated=True, arrows=True, navigation=True)
                        .props("infinite")
                        .classes("w-full rounded border border-gray-800")
                        .style("height: 320px; background: #050508;")
                    ):
                        for i, url in enumerate(urls):
                            with ui.carousel_slide(name=f"slide-{i}").classes(
                                "p-0 flex items-center justify-center"
                            ):
                                ui.image(url).style(
                                    "max-height: 320px; max-width: 100%; object-fit: contain;"
                                )
                else:
                    url = urls[0] if urls else ""
                    ui.image(url).classes("w-full rounded border border-gray-800").style(
                        "max-height: 320px; object-fit: contain; background: #050508; display:block;"
                    )
        except Exception as exc:
            print(f"[components] carousel build error: {exc}")

    for key, _ in FIELDS:
        review_inputs[key].value = row.get(key, "")

    review_drawer.show()


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
        ui.notify("Saved edits", type="positive")
        return


async def delete_from_review_drawer():
    """Remove the current row from state, the grid, and the database."""
    global review_row_id
    if review_row_id is None:
        return

        # 1. Find the row to delete
    row_to_delete = next((r for r in row_data if r["id"] == review_row_id), None)
    if not row_to_delete:
        return

    # 2. Add a confirmation dialog (standard UX for deletion)
    with ui.dialog() as dialog, ui.card():
        ui.label("Are you sure you want to delete this record?")
        with ui.row().classes("justify-end gap-2 w-full"):
            ui.button("Yes", on_click=lambda: dialog.submit(True)).props("color=red")
            ui.button("No", on_click=lambda: dialog.submit(False)).props("outline")

    result = await dialog
    if not result:
        return

    # 3. Perform backend deletion
    do_delete_row(row_to_delete)

    # 4. Update local state (modifying the list in-place for state.py)
    row_data[:] = [r for r in row_data if r["id"] != review_row_id]

    # 5. Refresh the AG Grid
    grid = get_grid()
    if grid:
        grid.options["rowData"] = list(row_data)
        grid.update()

    # 6. Cleanup and notify
    review_drawer.hide()
    ui.notify("Record deleted", type="info")


def render_upload_zone():
    with ui.element("div").classes("upload-zone w-full"):
        with ui.column().classes("items-center justify-center py-8 gap-2"):
            ui.icon("cloud_upload", size="2.2rem").style("color: rgba(255,255,255,0.2)")
            ui.label("Drop product images here or click to upload").style(
                "color: rgba(255,255,255,0.35); font-size:13px"
            )
            ui.label("PNG · JPG · WEBP").style(
                "color: rgba(255,255,255,0.18); font-size:11px; font-family: DM Mono"
            )
            ui.upload(
                multiple=True,
                on_multi_upload=handle_batch_upload,
                auto_upload=True,
            ).props('accept=".jpg,.jpeg,.png,.webp" flat label="Choose files"').classes(
                "text-xs mt-1"
            )


def render_legend():
    with (
        ui.row()
        .classes("items-center gap-6")
        .style("font-size:11px; color:rgba(255,255,255,0.3); font-family: DM Mono")
    ):
        for color, label in [
            ("#10b981", "High confidence"),
            ("#f59e0b", "Needs review"),
            ("#ef4444", "Possible duplicate"),
        ]:
            with ui.row().classes("items-center gap-1"):
                ui.element("span").style(
                    f"width:8px;height:8px;border-radius:50%;"
                    f"background:{color};display:inline-block"
                )
                ui.label(label)


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
                "rowHeight": 52,
                "rowClassRules": {
                    "row-ok": "data._status === 'ok'",
                    "row-warn": "data._status === 'warn'",
                    "row-dupe": "data._status === 'duplicate'",
                },
                "animateRows": True,
                "rowSelection": "multiple",
                "suppressRowClickSelection": True,
            },
            theme="alpine",
        )
        .classes("w-full")
        .style("height: 540px")
    )

    def on_cell_change(e):
        updated = e.args["data"]
        for i, row in enumerate(row_data):
            if row["id"] == updated["id"]:
                row_data[i] = updated
                persist_row_edits(updated)
                break

    def on_cell_click(e):
        # Editable cells should keep their normal AG Grid editing behavior.
        # The drawer opens only from the explicit Review button column.
        if e.args.get("colId") == "_review":
            open_review_drawer(e.args["data"])

    grid.on("cellValueChanged", on_cell_change)
    grid.on("cellClicked", on_cell_click)
    set_grid(grid)
