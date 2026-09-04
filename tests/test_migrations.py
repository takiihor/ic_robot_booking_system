"""Schema migration tests — the live database must survive the rebuild."""
from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, text

from app.migrations import SCHEMA_VERSION, run_migrations

# booking_requests exactly as schema version 0 shipped it, UNIQUE and all.
LEGACY_SCHEMA = """
CREATE TABLE applicants (
    id INTEGER NOT NULL PRIMARY KEY, sid_netid VARCHAR(64) UNIQUE,
    name VARCHAR(200) NOT NULL, department VARCHAR(200), email VARCHAR(200),
    phone VARCHAR(64), created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
);
CREATE TABLE resources (
    id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE,
    model VARCHAR(120), resource_group VARCHAR(120) NOT NULL, location VARCHAR(200),
    status VARCHAR(32) NOT NULL, remarks TEXT,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
);
CREATE TABLE booking_requests (
    id INTEGER NOT NULL, response_id VARCHAR(120), applicant_id INTEGER NOT NULL,
    facility VARCHAR(200), booking_type VARCHAR(120), start_date DATE NOT NULL,
    end_date DATE NOT NULL, sessions_json TEXT NOT NULL, preferred_resource_id INTEGER,
    assigned_resource_id INTEGER, purpose TEXT, remarks TEXT, raw_text TEXT,
    status VARCHAR(32) NOT NULL, rejection_reason TEXT, decided_at DATETIME,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    PRIMARY KEY (id), UNIQUE (response_id),
    FOREIGN KEY(applicant_id) REFERENCES applicants (id),
    FOREIGN KEY(preferred_resource_id) REFERENCES resources (id),
    FOREIGN KEY(assigned_resource_id) REFERENCES resources (id)
);
CREATE INDEX ix_requests_status ON booking_requests (status);
CREATE TABLE reservations (
    id INTEGER NOT NULL PRIMARY KEY, resource_id INTEGER NOT NULL,
    source_type VARCHAR(32) NOT NULL, booking_request_id INTEGER,
    title VARCHAR(200) NOT NULL, start_at DATETIME NOT NULL, end_at DATETIME NOT NULL,
    status VARCHAR(32) NOT NULL, details TEXT,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    FOREIGN KEY(resource_id) REFERENCES resources (id),
    FOREIGN KEY(booking_request_id) REFERENCES booking_requests (id)
);
INSERT INTO applicants VALUES (1,'25104512r','DING Changwen','ME',NULL,NULL,'2026-01-01','2026-01-01');
INSERT INTO resources VALUES (1,'UR10e (01)','UR10e','Cobots','Lab','Active',NULL,'2026-01-01','2026-01-01');
INSERT INTO booking_requests VALUES
  (1,'R-1',1,'IRL','Research','2026-08-31','2026-08-31','["AM"]',1,1,'p','r','raw','APPROVED',NULL,NULL,'2026-01-01','2026-01-01'),
  (2,'R-2',1,'IRL','Research','2026-09-01','2026-09-01','["PM"]',1,NULL,'p','r','raw','CANCELLED',NULL,NULL,'2026-01-01','2026-01-01');
INSERT INTO reservations VALUES
  (1,1,'BOOKING',1,'DING Changwen','2026-08-31 08:30','2026-08-31 12:00','ACTIVE',NULL,'2026-01-01','2026-01-01');
"""


def _legacy_db(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.commit()
    connection.close()
    return path


def test_migration_drops_the_unique_constraint_and_keeps_the_data(tmp_path):
    path = _legacy_db(tmp_path)
    engine = create_engine(f"sqlite:///{path}", future=True)
    run_migrations(engine)

    with engine.connect() as connection:
        schema = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='booking_requests'")
        ).scalar()
        assert "UNIQUE (response_id)" not in schema
        assert connection.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
        assert connection.execute(text("SELECT COUNT(*) FROM booking_requests")).scalar() == 2
        assert connection.execute(text("SELECT COUNT(*) FROM reservations")).scalar() == 1
        assert connection.execute(
            text("SELECT raw_text FROM booking_requests WHERE id=1")
        ).scalar() == "raw"
        # the rename must not have repointed reservations at a temporary table
        reservations_schema = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE name='reservations'")
        ).scalar()
        assert "REFERENCES booking_requests" in reservations_schema
        assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert connection.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    engine.dispose()


def test_migration_allows_a_cancelled_response_id_to_be_reused(tmp_path):
    path = _legacy_db(tmp_path)
    engine = create_engine(f"sqlite:///{path}", future=True)
    run_migrations(engine)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO booking_requests (response_id, applicant_id, start_date, "
                "end_date, sessions_json, status, created_at, updated_at) VALUES "
                "('R-2', 1, '2026-09-07', '2026-09-07', '[\"AM\"]', 'PENDING', "
                "'2026-01-01', '2026-01-01')"
            )
        )
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM booking_requests WHERE response_id='R-2'")
        ).scalar() == 2
    engine.dispose()


def test_migration_is_idempotent(tmp_path):
    path = _legacy_db(tmp_path)
    engine = create_engine(f"sqlite:///{path}", future=True)
    run_migrations(engine)
    run_migrations(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM booking_requests")).scalar() == 2
        assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()
