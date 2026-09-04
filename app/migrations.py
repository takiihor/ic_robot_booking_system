"""Tiny forward-only schema migrations, keyed on SQLite's `user_version`.

The app owns a single SQLite file, so a full migration framework would be more
machinery than the problem deserves. Each step is idempotent; `init_db()` calls
`run_migrations()` on every start, inside one transaction that is verified with
`PRAGMA foreign_key_check` before it commits.
"""
from __future__ import annotations

import logging
import sqlite3

from sqlalchemy import Engine

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Column list of booking_requests as of schema version 1, minus UNIQUE(response_id).
_BOOKING_REQUESTS_V1 = """
    id INTEGER NOT NULL,
    response_id VARCHAR(120),
    applicant_id INTEGER NOT NULL,
    facility VARCHAR(200),
    booking_type VARCHAR(120),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    sessions_json TEXT NOT NULL,
    preferred_resource_id INTEGER,
    assigned_resource_id INTEGER,
    purpose TEXT,
    remarks TEXT,
    raw_text TEXT,
    status VARCHAR(32) NOT NULL,
    rejection_reason TEXT,
    decided_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(applicant_id) REFERENCES applicants (id),
    FOREIGN KEY(preferred_resource_id) REFERENCES resources (id),
    FOREIGN KEY(assigned_resource_id) REFERENCES resources (id)
"""


def _drop_response_id_unique(cursor: sqlite3.Cursor) -> None:
    """Migration 1 — let cancelled/rejected requests keep their Response ID.

    SQLite cannot drop an inline UNIQUE constraint, so the table is rebuilt
    following the documented 12-step procedure: build the replacement under a
    temporary name, copy, drop the original, then rename into place. Renaming
    *away* from `booking_requests` first would rewrite the foreign key in
    `reservations` to point at the temporary name.

    Uniqueness among *live* requests now lives in booking_service.save_request,
    which is what lets staff re-enter an application after cancelling it.
    """
    definition = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='booking_requests'"
    ).fetchone()
    if definition is None or "UNIQUE (response_id)" not in definition[0]:
        return  # fresh database, or already migrated

    columns = ", ".join(
        row[1] for row in cursor.execute("PRAGMA table_info(booking_requests)").fetchall()
    )
    cursor.execute(f"CREATE TABLE booking_requests_new ({_BOOKING_REQUESTS_V1})")
    cursor.execute(
        f"INSERT INTO booking_requests_new ({columns}) SELECT {columns} FROM booking_requests"
    )
    cursor.execute("DROP TABLE booking_requests")
    # legacy_alter_table keeps the rename from touching other tables' schemas,
    # which would otherwise trip over reservations' now-dangling reference.
    cursor.execute("PRAGMA legacy_alter_table=ON")
    try:
        cursor.execute("ALTER TABLE booking_requests_new RENAME TO booking_requests")
    finally:
        cursor.execute("PRAGMA legacy_alter_table=OFF")
    cursor.execute("CREATE INDEX ix_requests_status ON booking_requests (status)")
    cursor.execute(
        "CREATE INDEX ix_booking_requests_response_id ON booking_requests (response_id)"
    )
    log.info("Migration 1: dropped UNIQUE(response_id) from booking_requests")


def _add_reservation_conflict_note(cursor: sqlite3.Cursor) -> None:
    """Migration 2 — record why a slot was approved over an existing booking.

    Staff can accept a request that clashes (SPEC 11.8); the note says what the
    session is shared with so the calendar can flag it.
    """
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(reservations)").fetchall()}
    if not columns or "conflict_note" in columns:
        return  # fresh database, or already migrated
    cursor.execute("ALTER TABLE reservations ADD COLUMN conflict_note TEXT")
    log.info("Migration 2: added reservations.conflict_note")


MIGRATIONS = {
    1: _drop_response_id_unique,
    2: _add_reservation_conflict_note,
}


def run_migrations(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    raw = engine.raw_connection()
    connection = raw.driver_connection
    previous_isolation = connection.isolation_level
    # Autocommit mode: PRAGMA foreign_keys is a no-op inside a transaction, and
    # rebuilding a table with foreign keys on would cascade into reservations.
    connection.isolation_level = None
    cursor = connection.cursor()
    try:
        current = cursor.execute("PRAGMA user_version").fetchone()[0] or 0
        if current >= SCHEMA_VERSION:
            return

        cursor.execute("PRAGMA foreign_keys=OFF")
        try:
            cursor.execute("BEGIN")
            try:
                for version in range(current + 1, SCHEMA_VERSION + 1):
                    MIGRATIONS[version](cursor)
                cursor.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(
                        f"Migration to schema {SCHEMA_VERSION} broke referential "
                        f"integrity: {violations[:5]}"
                    )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise
        finally:
            cursor.execute("PRAGMA foreign_keys=ON")
        log.info("Schema migrated from version %d to %d", current, SCHEMA_VERSION)
    finally:
        cursor.close()
        connection.isolation_level = previous_isolation
        raw.close()
