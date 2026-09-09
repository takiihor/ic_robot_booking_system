"""Tests for queue auto-import + banner (plan Task 1-4)."""
from __future__ import annotations


def test_booking_request_has_auto_import_columns(db):
    from app.models import BookingRequest
    cols = {c.name for c in BookingRequest.__table__.columns}
    assert "auto_imported" in cols
    assert "source_file" in cols
