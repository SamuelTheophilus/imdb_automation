from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.pipeline import PipelineResult

DB_PATH = Path("data/imdb_auto_fill.sqlite3")
RECORD_FIELDS: list[tuple[str, str]] = [
    ("barcode", "Barcode"),
    ("category_type", "Category"),
    ("segment_type", "Segment"),
    ("manufacturer", "Manufacturer"),
    ("brand", "Brand"),
    ("product_name", "Product Name"),
    ("weight", "Weight"),
    ("unit", "Unit"),
    ("packaging_type", "Packaging"),
    ("country_of_origin", "Country of Origin"),
    ("promotional_messages", "Promo"),
    ("variant", "Variant"),
    ("fragrance_flavor", "Fragrance / Flavor"),
    ("addons", "Add-ons"),
    ("tagline", "Tagline"),
]


_NEW_EXTRACTION_COLUMNS = [
    "variant",
    "fragrance_flavor",
    "addons",
    "tagline",
    "image_paths_json",
]


def _migrate_extractions(conn: sqlite3.Connection) -> None:
    """Add any columns introduced after the initial schema was created."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(extractions)")}
    for col in _NEW_EXTRACTION_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE extractions ADD COLUMN {col} TEXT")


def _migrate_users(conn: sqlite3.Connection) -> None:
    """Add the email column to users if it doesn't exist yet."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "email" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")


def _utc_now() -> str:
    """Store timestamps as sortable ISO-8601 UTC strings."""
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    """Create one SQLite connection with row dictionaries enabled.

    SQLite is file-based, so we keep this helper small and open short-lived
    connections around each operation instead of sharing one global connection
    across NiceGUI event handlers.
    """
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the application tables if this is the first run."""
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS extractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                image_path TEXT NOT NULL,
                status TEXT NOT NULL,
                normalized_fields_json TEXT NOT NULL,
                low_confidence_fields_json TEXT NOT NULL,
                duplicate_suggestions_json TEXT NOT NULL,
                confidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                barcode TEXT,
                category_type TEXT,
                segment_type TEXT,
                manufacturer TEXT,
                brand TEXT,
                product_name TEXT,
                weight TEXT,
                unit TEXT,
                packaging_type TEXT,
                country_of_origin TEXT,
                promotional_messages TEXT,
                variant TEXT,
                fragrance_flavor TEXT,
                addons TEXT,
                tagline TEXT,
                image_paths_json TEXT,

                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS extraction_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extraction_id INTEGER NOT NULL,
                version_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                record_json TEXT NOT NULL,
                confidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY(extraction_id) REFERENCES extractions(id)
            );
            """
        )
        _migrate_users(conn)
        _migrate_extractions(conn)
        _seed_missing_versions(conn)


def create_user(username: str, password_hash: str, email: str | None = None) -> int:
    """Persist a new user and return its id."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (username, password_hash, email, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username, password_hash, email or None, _utc_now()),
        )
        return int(cursor.lastrowid)


def get_user_by_username(username: str) -> dict[str, Any] | None:
    """Fetch one user row by username for login/signup checks."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def delete_extraction(extraction_id: int) -> None:
    """Remove an extraction and its associated versions from the database."""
    with get_connection() as conn:
        # If your schema doesn't have ON DELETE CASCADE, delete versions first
        conn.execute(
            "DELETE FROM extraction_versions WHERE extraction_id = ?", (extraction_id,)
        )
        conn.execute("DELETE FROM extractions WHERE id = ?", (extraction_id,))


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Fetch one user row by id after a session token has been verified."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Fetch one user row by email for duplicate-email checks."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    return dict(row) if row else None


def update_user_password(user_id: int, password_hash: str) -> None:
    """Overwrite the bcrypt hash for a user (reset or change-password flows)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )


def _record_values_from_result(result: PipelineResult) -> dict[str, str | None]:
    """Convert a PipelineResult record into the editable/exportable columns."""
    values: dict[str, str | None] = {}
    for key, _ in RECORD_FIELDS:
        value = getattr(result.record, key)
        values[key] = str(value) if value is not None else None
    return values


def _confidence_values_from_result(result: PipelineResult) -> dict[str, float]:
    """Keep confidence scores as JSON because they are metadata, not grid columns."""
    return {
        key: getattr(result.record, f"{key}_confidence") for key, _ in RECORD_FIELDS
    }


def _record_values_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, str | None]:
    """Extract only product fields from a database row for version snapshots."""
    return {key: row[key] for key, _ in RECORD_FIELDS}


def _next_version_number(conn: sqlite3.Connection, extraction_id: int) -> int:
    """Return the next version number for one extraction."""
    current = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0)
        FROM extraction_versions
        WHERE extraction_id = ?
        """,
        (extraction_id,),
    ).fetchone()[0]
    return int(current) + 1


def _insert_version(
    conn: sqlite3.Connection,
    *,
    extraction_id: int,
    reason: str,
    record_values: dict[str, Any],
    confidence_json: str,
    created_at: str | None = None,
) -> None:
    """Write one immutable snapshot of an extraction's editable values."""
    conn.execute(
        """
        INSERT INTO extraction_versions (
            extraction_id, version_number, reason,
            record_json, confidence_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            extraction_id,
            _next_version_number(conn, extraction_id),
            reason,
            json.dumps(record_values),
            confidence_json,
            created_at or _utc_now(),
        ),
    )


def _seed_missing_versions(conn: sqlite3.Connection) -> None:
    """Create baseline versions for rows saved before versioning existed."""
    rows = conn.execute(
        """
        SELECT e.*
        FROM extractions e
        LEFT JOIN extraction_versions v ON v.extraction_id = e.id
        WHERE v.id IS NULL
        """
    ).fetchall()
    for row in rows:
        _insert_version(
            conn,
            extraction_id=row["id"],
            reason="created",
            record_values=_record_values_from_row(row),
            confidence_json=row["confidence_json"],
            created_at=row["created_at"],
        )


def create_extraction(
    *,
    user_id: int,
    original_filename: str,
    result: PipelineResult,
) -> int:
    """Save a completed extraction and return the database row id."""
    values = _record_values_from_result(result)
    confidence = _confidence_values_from_result(result)
    now = _utc_now()

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO extractions (
                user_id, original_filename, image_path, status,
                normalized_fields_json, low_confidence_fields_json,
                duplicate_suggestions_json, confidence_json,
                created_at, updated_at,
                barcode, category_type, segment_type, manufacturer, brand,
                product_name, weight, unit, packaging_type, country_of_origin,
                promotional_messages, variant, fragrance_flavor, addons, tagline,
                image_paths_json
            )
            VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?
            )
            """,
            (
                user_id,
                original_filename,
                result.image_path,
                "duplicate"
                if result.has_duplicates
                else "warn"
                if result.has_low_confidence
                else "ok",
                json.dumps(result.normalized_fields),
                json.dumps(result.low_confidence_fields),
                json.dumps(result.duplicate_suggestions),
                json.dumps(confidence),
                now,
                now,
                values["barcode"],
                values["category_type"],
                values["segment_type"],
                values["manufacturer"],
                values["brand"],
                values["product_name"],
                values["weight"],
                values["unit"],
                values["packaging_type"],
                values["country_of_origin"],
                values["promotional_messages"],
                values["variant"],
                values["fragrance_flavor"],
                values["addons"],
                values["tagline"],
                json.dumps(result.image_paths),
            ),
        )
        extraction_id = int(cursor.lastrowid)
        _insert_version(
            conn,
            extraction_id=extraction_id,
            reason="created",
            record_values=values,
            confidence_json=json.dumps(confidence),
            created_at=now,
        )
        return extraction_id


def update_extraction_fields(extraction_id: int, values: dict[str, Any]) -> None:
    """Persist user edits from the grid or review drawer.

    Only fields declared in `RECORD_FIELDS` can be updated. This prevents UI-only keys
    such as thumbnails or status markers from being accidentally written as SQL
    columns.
    """
    editable_keys = [key for key, _ in RECORD_FIELDS if key in values]
    if not editable_keys:
        return

    assignments = ", ".join(f"{key} = ?" for key in editable_keys)
    params = [values.get(key) or None for key in editable_keys]
    now = _utc_now()
    params.extend([now, extraction_id])

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE extractions
            SET {assignments}, updated_at = ?
            WHERE id = ?
            """,
            params,
        )
        updated = conn.execute(
            "SELECT * FROM extractions WHERE id = ?",
            (extraction_id,),
        ).fetchone()
        if updated:
            _insert_version(
                conn,
                extraction_id=extraction_id,
                reason="modified",
                record_values=_record_values_from_row(updated),
                confidence_json=updated["confidence_json"],
                created_at=now,
            )


def update_extraction_status(extraction_id: int, status: str) -> None:
    """Update the status column of an extraction after manual review."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE extractions SET status = ?, updated_at = ? WHERE id = ?",
            (status, _utc_now(), extraction_id),
        )


def update_extraction_image_paths(extraction_id: int, image_paths: list[str]) -> None:
    """Update the image group for an extraction after an image is removed."""
    primary = image_paths[0] if image_paths else ""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE extractions
            SET image_path = ?, image_paths_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (primary, json.dumps(image_paths), _utc_now(), extraction_id),
        )


def list_user_extractions(user_id: int) -> list[dict[str, Any]]:
    """Return all saved extractions for a user's history view."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM extractions
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_extraction_versions(extraction_id: int) -> list[dict[str, Any]]:
    """Return immutable snapshots for one extraction, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM extraction_versions
            WHERE extraction_id = ?
            ORDER BY version_number DESC
            """,
            (extraction_id,),
        ).fetchall()
    versions = []
    for row in rows:
        item = dict(row)
        item["record"] = json.loads(item["record_json"])
        item["confidence"] = json.loads(item["confidence_json"])
        versions.append(item)
    return versions
