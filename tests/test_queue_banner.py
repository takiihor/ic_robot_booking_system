"""Tests for queue auto-import + banner (plan Task 1-4)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import sessionmaker


def test_booking_request_has_auto_import_columns(db):
    from app.models import BookingRequest
    cols = {c.name for c in BookingRequest.__table__.columns}
    assert "auto_imported" in cols
    assert "source_file" in cols


@pytest.fixture(autouse=True)
def _wire_queue_to_test_db(db):
    from app.services import queue_processor as qp
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, future=True)
    qp.set_session_factory(factory)
    yield
    qp.set_session_factory(None)


def _write(directory, filename, data):
    from pathlib import Path
    import json as _json
    p = Path(directory) / filename
    p.write_text(_json.dumps(data), encoding="utf-8")
    return p


GOOD_EMAIL = (
    "Department: Test Department\n"
    "Facilities to Use: UR10e (01)\n"
    "Response ID: AUTO-001\n"
    "Booking Type: Research\n"
    "Date: 2026-10-01\n"
    "Session: PM\n"
    "Name: Jane Doe\n"
    "SID/NetID: 99999999x\n"
    "Email: jane.doe@example.com\n"
    "Booking for: Equipment testing\n"
)


def test_auto_import_approves_when_free(tmp_path, db, robots):
    from app.services import queue_processor as qp
    p = _write(tmp_path, "booking-100.json", {"EmailBody": GOOD_EMAIL})
    result = qp.process_file(p)
    assert result.status == "PROCESSED"
    req = qp.auto_import(p, result)
    assert req is not None
    assert req.status == "APPROVED"
    assert req.auto_imported in (True, 1)
    assert req.source_file == "booking-100.json"
    assert len(req.reservations) > 0


def test_auto_import_pending_on_conflict(tmp_path, db, robots):
    from app.services import queue_processor as qp
    from app.services import booking_service as svc
    from app.models import BookingRequest
    # First import takes the slot.
    p1 = _write(tmp_path, "booking-101.json", {"EmailBody": GOOD_EMAIL})
    req1 = qp.auto_import(p1, qp.process_file(p1))
    assert req1.status == "APPROVED"
    # Second request, different Response ID, same robot/date/session -> conflict.
    email2 = (GOOD_EMAIL.replace("AUTO-001", "AUTO-002")
              .replace("Jane Doe", "John Smith")
              .replace("99999999x", "88888888y")
              .replace("jane.doe@example.com", "john.smith@example.com"))
    p2 = _write(tmp_path, "booking-102.json", {"EmailBody": email2})
    result2 = qp.process_file(p2)
    assert result2.availability == "CONFLICT"
    req2 = qp.auto_import(p2, result2)
    assert req2 is not None
    assert req2.status == "PENDING"
    assert len(req2.reservations) == 0
    # No duplicate applicants for distinct SIDs.
    assert req1.applicant_id != req2.applicant_id


def test_auto_import_idempotent(tmp_path, db, robots):
    from app.services import queue_processor as qp
    from app.models import BookingRequest
    from sqlalchemy import select
    p = _write(tmp_path, "booking-103.json", {"EmailBody": GOOD_EMAIL})
    req1 = qp.auto_import(p, qp.process_file(p))
    req2 = qp.auto_import(p, qp.process_file(p))
    assert req1.id == req2.id
    rows = list(db.scalars(select(BookingRequest).where(
        BookingRequest.response_id == "AUTO-001",
        BookingRequest.status.not_in(("CANCELLED", "REJECTED")))))
    assert len(rows) == 1


def test_auto_import_ambiguous_facility_stays_pending(tmp_path, db, robots, monkeypatch):
    from app.services import queue_processor as qp
    monkeypatch.setenv("BOOKING_FACILITY_MAP",
                       json.dumps({"Autonomous Robot Platform": ["Collaborative Robots"]}))
    body = GOOD_EMAIL.replace("UR10e (01)", '["Autonomous Robot Platform"]'
                              ).replace("AUTO-001", "AUTO-AMB")
    p = _write(tmp_path, "booking-104.json", {"EmailBody": body})
    result = qp.process_file(p)
    assert result.availability == "NEEDS_RESOURCE_SELECTION"
    req = qp.auto_import(p, result)
    assert req is not None
    assert req.status == "PENDING"
    assert len(req.reservations) == 0


def test_get_queue_banner_empty(db):
    from app.routes.common import get_queue_banner
    banner = get_queue_banner(db)
    assert banner["total"] == 0
    assert banner["items"] == []


def test_get_queue_banner_counts_recent(tmp_path, db, robots):
    from app.routes.common import get_queue_banner
    from app.services import queue_processor as qp
    p1 = _write(tmp_path, "booking-201.json", {"EmailBody": GOOD_EMAIL})
    req1 = qp.auto_import(p1, qp.process_file(p1))
    assert req1.status == "APPROVED"
    email2 = (GOOD_EMAIL.replace("AUTO-001", "AUTO-202")
              .replace("99999999x", "77777777z"))
    p2 = _write(tmp_path, "booking-202.json", {"EmailBody": email2})
    # Force the second one pending by pre-booking is complex; instead flip it.
    req2 = qp.auto_import(p2, qp.process_file(p2))
    req2.status = "PENDING"
    db.flush()
    banner = get_queue_banner(db)
    assert banner["total"] == 2
    assert banner["auto_approved"] == 1
    assert banner["needs_review"] == 1
    assert banner["latest_at"] is not None
    assert len(banner["items"]) == 2


def _seed_banner_request(client, status="APPROVED"):
    from datetime import date
    from app.models import Applicant, BookingRequest
    db = client.db_factory()
    try:
        ap = Applicant(name="Banner Test", sid_netid="banner001")
        db.add(ap)
        db.flush()
        req = BookingRequest(applicant_id=ap.id, start_date=date(2026, 10, 1),
                             end_date=date(2026, 10, 1), status=status,
                             auto_imported=True, source_file="booking-banner.json")
        req.sessions = ["PM"]
        db.add(req)
        db.commit()
        return req.id
    finally:
        db.close()


def test_calendar_shows_banner_when_recent_imports(client):
    _seed_banner_request(client)
    resp = client.get("/calendar")
    assert resp.status_code == 200
    assert b"queue-banner" in resp.content
    assert b"incoming" in resp.content


def _seed_old_banner_request(client, days=7, status="APPROVED"):
    from datetime import date, timedelta
    from app.models import Applicant, BookingRequest, utcnow
    db = client.db_factory()
    try:
        ap = Applicant(name="Banner Test", sid_netid="banner002")
        db.add(ap)
        db.flush()
        req = BookingRequest(applicant_id=ap.id, start_date=date(2026, 10, 1),
                             end_date=date(2026, 10, 1), status=status,
                             auto_imported=True, source_file="booking-banner.json")
        req.sessions = ["PM"]
        req.created_at = utcnow() - timedelta(days=days)
        db.add(req)
        db.commit()
        return req.id
    finally:
        db.close()


def test_get_queue_banner_includes_old_imports(db):
    """Imports older than 24h must still be counted — the banner is dismissal-driven,
    so staff who check later still see what was handled."""
    from datetime import date, timedelta
    from app.models import Applicant, BookingRequest, utcnow
    from app.routes.common import get_queue_banner

    ap = Applicant(name="Old Import", sid_netid="old001")
    db.add(ap)
    db.flush()
    req = BookingRequest(applicant_id=ap.id, start_date=date(2026, 10, 1),
                         end_date=date(2026, 10, 1), status="APPROVED",
                         auto_imported=True, source_file="booking-old.json")
    req.sessions = ["PM"]
    req.created_at = utcnow() - timedelta(days=7)
    db.add(req)
    db.commit()

    banner = get_queue_banner(db)
    assert banner["total"] == 1
    assert banner["auto_approved"] == 1
    assert banner["latest_at"] == req.created_at.isoformat()


def test_calendar_shows_banner_for_old_import(client):
    """A week-old import must still show the banner (no silent 24h auto-hide)."""
    _seed_old_banner_request(client)
    resp = client.get("/calendar")
    assert resp.status_code == 200
    assert b"queue-banner" in resp.content
    assert b"incoming" in resp.content


def test_no_banner_when_empty(client):
    resp = client.get("/calendar")
    assert resp.status_code == 200
    assert b"queue-banner" not in resp.content
