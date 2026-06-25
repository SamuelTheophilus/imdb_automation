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
    "source",
    "batch_job_id",
    "video_path",
    "barcode_audit_json",
]


def _migrate_extractions(conn: sqlite3.Connection) -> None:
    """Add any columns introduced after the initial schema was created."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(extractions)")}
    for col in _NEW_EXTRACTION_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE extractions ADD COLUMN {col} TEXT")


def _migrate_users(conn: sqlite3.Connection) -> None:
    """Add columns to users that were introduced after the initial schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "email" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "tour_shown" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN tour_shown INTEGER DEFAULT 0")


def _migrate_batch_jobs(conn: sqlite3.Connection) -> None:
    """Add columns to batch_jobs introduced after the initial schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(batch_jobs)")}
    if "skipped_count" not in existing:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN skipped_count INTEGER")
    if "skipped_names_json" not in existing:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN skipped_names_json TEXT")
    if "provider" not in existing:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN provider TEXT DEFAULT 'anthropic'")


def _migrate_extraction_costs(conn: sqlite3.Connection) -> None:
    """Add cost_usd and model_used columns to extractions if missing."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(extractions)")}
    if "cost_usd" not in existing:
        conn.execute("ALTER TABLE extractions ADD COLUMN cost_usd REAL")
    if "model_used" not in existing:
        conn.execute("ALTER TABLE extractions ADD COLUMN model_used TEXT")


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

            CREATE TABLE IF NOT EXISTS batch_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                anthropic_batch_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                image_paths_json TEXT NOT NULL,
                request_map_json TEXT NOT NULL,
                notify_email TEXT,
                submitted_at TEXT NOT NULL,
                completed_at TEXT,
                result_count INTEGER,
                skipped_count INTEGER,
                error_message TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS brand_catalog (
                brand TEXT PRIMARY KEY,
                manufacturer TEXT,
                category_type TEXT,
                segment_type TEXT,
                country_of_origin TEXT,
                packaging_type TEXT,
                product_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _migrate_users(conn)
        _migrate_extractions(conn)
        _migrate_batch_jobs(conn)
        _migrate_extraction_costs(conn)
        _seed_missing_versions(conn)
        _backfill_brand_catalog(conn)


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


def get_extraction_by_id(extraction_id: int) -> dict[str, Any] | None:
    """Fetch a single extraction record by its primary key."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM extractions WHERE id = ?", (extraction_id,)
        ).fetchone()
    return dict(row) if row else None


def merge_extractions(primary_id: int, secondary_id: int, merged_values: dict) -> None:
    """Overwrite the primary record with merged field values, clear its duplicate
    status, and permanently delete the secondary record.

    The caller supplies the winning value for every editable field.  After the
    merge the primary is marked 'ok' and its duplicate_suggestions_json is
    cleared so it no longer appears as a duplicate in the grid.
    """
    update_extraction_fields(primary_id, merged_values)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE extractions
               SET status = 'warn',
                   duplicate_suggestions_json = '[]',
                   updated_at = ?
             WHERE id = ?
            """,
            (_utc_now(), primary_id),
        )
    delete_extraction(secondary_id)


def upsert_brand_catalog(
    brand: str,
    *,
    manufacturer: str | None = None,
    category_type: str | None = None,
    segment_type: str | None = None,
    country_of_origin: str | None = None,
    packaging_type: str | None = None,
) -> None:
    """Insert a new brand entry or increment the product count for an existing one.

    On conflict we keep existing non-null values and only overwrite with new
    non-null values, so the catalog never loses confirmed data.
    """
    now = _utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO brand_catalog
                (brand, manufacturer, category_type, segment_type,
                 country_of_origin, packaging_type, product_count,
                 first_seen_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(brand) DO UPDATE SET
                manufacturer     = COALESCE(excluded.manufacturer,     manufacturer),
                category_type    = COALESCE(excluded.category_type,    category_type),
                segment_type     = COALESCE(excluded.segment_type,     segment_type),
                country_of_origin= COALESCE(excluded.country_of_origin,country_of_origin),
                packaging_type   = COALESCE(excluded.packaging_type,   packaging_type),
                product_count    = product_count + 1,
                updated_at       = excluded.updated_at
            """,
            (brand, manufacturer, category_type, segment_type,
             country_of_origin, packaging_type, now, now),
        )


def get_brand_profile(brand: str) -> dict[str, Any] | None:
    """Return the catalog entry for a single brand, or None if not yet seen."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM brand_catalog WHERE brand = ?", (brand,)
        ).fetchone()
    return dict(row) if row else None


def list_brand_catalog() -> list[dict[str, Any]]:
    """Return all brand catalog entries ordered by product count descending."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM brand_catalog ORDER BY product_count DESC, brand ASC"
        ).fetchall()
    return [dict(r) for r in rows]


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


def mark_tour_shown(user_id: int) -> None:
    """Persist that this user has completed the onboarding tour."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET tour_shown = 1 WHERE id = ?",
            (user_id,),
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


def _backfill_brand_catalog(conn: sqlite3.Connection) -> None:
    """Populate brand_catalog from existing extractions on first startup after the
    feature was added.  Safe to call repeatedly -- upsert is idempotent."""
    rows = conn.execute(
        "SELECT brand, manufacturer, category_type, segment_type, country_of_origin,"
        " packaging_type, created_at FROM extractions"
        " WHERE brand IS NOT NULL AND brand != '' AND status != 'duplicate'"
        " ORDER BY created_at ASC"
    ).fetchall()
    for row in rows:
        now = row["created_at"]
        conn.execute(
            """
            INSERT INTO brand_catalog
                (brand, manufacturer, category_type, segment_type,
                 country_of_origin, packaging_type, product_count,
                 first_seen_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(brand) DO UPDATE SET
                manufacturer      = COALESCE(excluded.manufacturer,      manufacturer),
                category_type     = COALESCE(excluded.category_type,     category_type),
                segment_type      = COALESCE(excluded.segment_type,      segment_type),
                country_of_origin = COALESCE(excluded.country_of_origin, country_of_origin),
                packaging_type    = COALESCE(excluded.packaging_type,    packaging_type),
                product_count     = product_count + 1,
                updated_at        = excluded.updated_at
            """,
            (row["brand"], row["manufacturer"], row["category_type"], row["segment_type"],
             row["country_of_origin"], row["packaging_type"], now, now),
        )


def create_extraction(
    *,
    user_id: int,
    original_filename: str,
    result: PipelineResult,
    source: str = "quick",
    batch_job_id: int | None = None,
    video_path: str | None = None,
    barcode_audit: dict | None = None,
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
                image_paths_json, source, batch_job_id,
                cost_usd, model_used, video_path, barcode_audit_json
            )
            VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?
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
                source,
                batch_job_id,
                result.cost_usd,
                result.model_used,
                video_path,
                json.dumps(barcode_audit) if barcode_audit else None,
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

    # Only catalog confirmed or low-confidence extractions -- duplicates are not
    # yet resolved so counting them would inflate brand product_count.
    if values.get("brand") and not result.has_duplicates:
        upsert_brand_catalog(
            values["brand"],
            manufacturer=values.get("manufacturer"),
            category_type=values.get("category_type"),
            segment_type=values.get("segment_type"),
            country_of_origin=values.get("country_of_origin"),
            packaging_type=values.get("packaging_type"),
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


# ── Batch jobs ────────────────────────────────────────────────────────────────

def create_batch_job(
    *,
    user_id: int,
    anthropic_batch_id: str,
    image_paths: list,
    request_map: dict,
    notify_email: str | None = None,
    provider: str = "anthropic",
) -> int:
    """Persist a new batch job and return its id."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO batch_jobs (
                user_id, anthropic_batch_id, status,
                image_paths_json, request_map_json,
                notify_email, submitted_at, provider
            ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                anthropic_batch_id,
                json.dumps([str(p) for p in image_paths]),
                json.dumps(request_map),
                notify_email or None,
                _utc_now(),
                provider,
            ),
        )
        return int(cursor.lastrowid)


def list_pending_batch_jobs() -> list[dict[str, Any]]:
    """Return all jobs in 'pending' status across all users (for the poller)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM batch_jobs WHERE status = 'pending' ORDER BY submitted_at ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def list_batch_jobs(user_id: int) -> list[dict[str, Any]]:
    """Return all batch jobs for one user, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM batch_jobs WHERE user_id = ? ORDER BY submitted_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_batch_job(job_id: int) -> dict[str, Any] | None:
    """Fetch a single batch job by its id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def update_batch_job_status(
    job_id: int,
    status: str,
    *,
    result_count: int | None = None,
    skipped_count: int | None = None,
    skipped_names: list[str] | None = None,
    error_message: str | None = None,
) -> None:
    """Update the status of a batch job, optionally recording result/skipped counts or error."""
    import json as _json
    now = _utc_now()
    skipped_names_json = _json.dumps(skipped_names) if skipped_names is not None else None
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE batch_jobs
            SET status = ?,
                completed_at = CASE WHEN ? IN ('completed', 'failed') THEN ? ELSE completed_at END,
                result_count = COALESCE(?, result_count),
                skipped_count = COALESCE(?, skipped_count),
                skipped_names_json = COALESCE(?, skipped_names_json),
                error_message = COALESCE(?, error_message)
            WHERE id = ?
            """,
            (status, status, now, result_count, skipped_count, skipped_names_json, error_message, job_id),
        )


def delete_batch_job(job_id: int) -> None:
    """Remove a batch job record."""
    with get_connection() as conn:
        conn.execute("DELETE FROM batch_jobs WHERE id = ?", (job_id,))
