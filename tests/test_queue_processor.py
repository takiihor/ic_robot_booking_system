"""Tests for the booking queue processor."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.services.queue_processor import (
    _Registry,
    _sha256,
    process_file,
    scan_queue,
    run_once,
    set_session_factory,
    resolve_facility,
    _normalise_facility,
    _FILE_RE,
)


# ---------------------------------------------------------------------------
# Sanitised fixtures — no real personal data
# ---------------------------------------------------------------------------

GOOD_EMAIL_BODY = (
    "Department: Test Department\n"
    "Facilities to Use: UR10e (01)\n"
    "Response ID: TEST-001\n"
    "Booking Type: Research\n"
    "Date: 2026-10-01\n"
    "Session: PM\n"
    "Name: Jane Doe\n"
    "SID/NetID: 99999999x\n"
    "Email: jane.doe@example.com\n"
    "Booking for: Equipment testing\n"
)

GOOD_OUTER_JSON = {
    "@odata.etag": '"1"',
    "ItemInternalId": "1",
    "ID": 1,
    "Title": "IC Booking",
    "EmailBody": GOOD_EMAIL_BODY,
    "ReceivedAt": "2026-10-01",
    "Status": "NEW",
}

MULTI_SESSION_EMAIL_BODY = (
    "Department: Engineering\n"
    "Facilities to Use: UR10e (02)\n"
    "Response ID: TEST-002\n"
    "Booking Type: Multi-day\n"
    "Date: 2026-10-05\n"
    "End Date: 2026-10-06\n"
    "Session: AM\nPM\n"
    "Name: John Smith\n"
    "SID/NetID: 88888888y\n"
    "Email: john.smith@example.com\n"
    "Booking for: Calibration work\n"
)

MULTI_SESSION_OUTER = {
    "EmailBody": MULTI_SESSION_EMAIL_BODY,
    "Title": "IC Booking",
    "ReceivedAt": "2026-10-05",
    "Status": "NEW",
}

ICT_STYLE_EMAIL_BODY = (
    "CAUTION: External email.\n\n"
    "Department: PolyU - ME - Department of Mechanical Engineering (FENG-ME)\n"
    "Facilities to Use: [\"Autonomous Robot Platform\"]\n"
    "Response ID: ICT-349-STD\n"
    "Booking Type: [RECOMMEND] 3.5 - 7 hours (Single Day Booking)\n"
    "Date: 2026-09-01\n"
    "End Date (Consecutive Booking only):\n"
    "Session: [\"PM Session, 3.5 hours (13.30 - 17.00)\"]\n"
    "Name: Lai Tsz Him\n"
    "SID/NetID: 25031443d\n"
    "Email: 25031443d@connect.polyu.hk\n"
    "Booking for: Support for Start-up Company\n"
)

ICT_OUTER = {
    "EmailBody": ICT_STYLE_EMAIL_BODY,
    "Title": "IC Booking",
    "ReceivedAt": "2026-09-07",
    "Status": "NEW",
}


def _write_booking_json(directory: Path, filename: str, data: dict) -> Path:
    p = directory / filename
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _inject_session_factory(db, robots):
    """Wire the queue processor to the test database for every test.

    Uses StaticPool so every session shares the same in-memory database
    connection — required because in-memory SQLite is per-connection.
    """
    from sqlalchemy.pool import StaticPool

    engine = db.get_bind()
    factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        future=True,
    )
    set_session_factory(factory)
    yield
    set_session_factory(None)


# ---------------------------------------------------------------------------
# Tests: file naming regex
# ---------------------------------------------------------------------------

class TestFileRegex:
    @pytest.mark.parametrize("name,expected", [
        ("booking-123.json", True),
        ("booking-abc.json", True),
        ("booking-ICT-349-STD.json", True),
        ("other-file.json", False),
        ("booking-.json", False),
        ("Booking-1.JSON", True),
    ])
    def test_regex(self, name, expected):
        assert bool(_FILE_RE.match(name)) == expected


# ---------------------------------------------------------------------------
# Tests: registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_new_file_is_not_processed(self, tmp_path):
        reg = _Registry(tmp_path / "reg.json")
        assert not reg.already_processed(Path("f.json"), "abc")

    def test_mark_and_skip(self, tmp_path):
        reg = _Registry(tmp_path / "reg.json")
        p = Path("f.json")
        reg.mark(p, "abc")
        reg.save()

        reg2 = _Registry(tmp_path / "reg.json")
        assert reg2.already_processed(p, "abc")

    def test_different_hash_is_not_skipped(self, tmp_path):
        reg = _Registry(tmp_path / "reg.json")
        p = Path("f.json")
        reg.mark(p, "abc")
        assert not reg.already_processed(p, "def")

    def test_corrupt_registry_starts_fresh(self, tmp_path):
        rp = tmp_path / "reg.json"
        rp.write_text("NOT JSON {{{")
        reg = _Registry(rp)
        assert not reg.already_processed(Path("f.json"), "x")


# ---------------------------------------------------------------------------
# Tests: process_file
# ---------------------------------------------------------------------------

class TestProcessFile:
    def test_good_file(self, tmp_path, robots):
        p = _write_booking_json(tmp_path, "booking-123.json", GOOD_OUTER_JSON)
        result = process_file(p)

        assert result.status == "PROCESSED"
        assert result.booking_id == "123"
        assert result.response_id == "TEST-001"
        assert result.date == "2026-10-01"
        assert result.start == "13:30"
        assert result.end == "17:00"
        assert result.availability in ("AVAILABLE", "CONFLICT")
        assert result.error is None

    def test_missing_email_body(self, tmp_path, robots):
        p = _write_booking_json(tmp_path, "booking-999.json", {"Title": "no body"})
        result = process_file(p)
        assert result.status == "ERROR"
        assert "Missing EmailBody" in result.error

    def test_malformed_json(self, tmp_path, robots):
        p = tmp_path / "booking-bad.json"
        p.write_text("NOT JSON {{{", encoding="utf-8")
        result = process_file(p)
        assert result.status == "ERROR"
        assert "Malformed JSON" in result.error

    def test_nonexistent_file(self):
        result = process_file(Path("/nonexistent/booking-0.json"))
        assert result.status == "ERROR"
        assert "Cannot read" in result.error

    def test_multi_session(self, tmp_path, robots):
        p = _write_booking_json(tmp_path, "booking-multi.json", MULTI_SESSION_OUTER)
        result = process_file(p)
        assert result.status == "PROCESSED"
        assert result.response_id == "TEST-002"
        assert result.start == "08:30"
        assert result.end == "17:00"

    def test_ict_style_email(self, tmp_path, robots, monkeypatch):
        # Map "Autonomous Robot Platform" to the test resource group
        monkeypatch.setenv(
            "BOOKING_FACILITY_MAP",
            json.dumps({"Autonomous Robot Platform": ["Collaborative Robots"]}),
        )
        p = _write_booking_json(tmp_path, "booking-ict.json", ICT_OUTER)
        result = process_file(p)
        assert result.status == "PROCESSED"
        assert result.response_id == "ICT-349-STD"
        assert result.date == "2026-09-01"
        assert result.normalised_facility == "Autonomous Robot Platform"
        assert result.availability == "NEEDS_RESOURCE_SELECTION"
        assert len(result.candidates) > 0
        # All test robots are in "Collaborative Robots" group
        for cand in result.candidates:
            assert cand.group == "Collaborative Robots"
            assert cand.available is True  # no conflicts in fresh test DB


# ---------------------------------------------------------------------------
# Tests: scan_queue (integration)
# ---------------------------------------------------------------------------

class TestScanQueue:
    def test_processes_new_files(self, tmp_path, robots):
        _write_booking_json(tmp_path, "booking-10.json", GOOD_OUTER_JSON)
        _write_booking_json(tmp_path, "booking-20.json", MULTI_SESSION_OUTER)

        reg = _Registry(tmp_path / "reg.json")
        results = scan_queue(tmp_path, reg)

        assert len(results) == 2
        ids = {r.booking_id for r in results}
        assert ids == {"10", "20"}

    def test_skips_already_processed(self, tmp_path, robots):
        _write_booking_json(tmp_path, "booking-10.json", GOOD_OUTER_JSON)
        _write_booking_json(tmp_path, "booking-20.json", GOOD_OUTER_JSON)
        reg = _Registry(tmp_path / "reg.json")

        results1 = scan_queue(tmp_path, reg)
        assert len(results1) == 2

        # Second pass — same files, should skip
        results2 = scan_queue(tmp_path, reg)
        assert len(results2) == 0

    def test_reprocesses_on_content_change(self, tmp_path, robots):
        _write_booking_json(tmp_path, "booking-10.json", GOOD_OUTER_JSON)
        reg = _Registry(tmp_path / "reg.json")
        scan_queue(tmp_path, reg)

        # Change the file content
        modified = GOOD_OUTER_JSON.copy()
        modified["EmailBody"] = GOOD_EMAIL_BODY.replace("TEST-001", "TEST-001-REVISED")
        _write_booking_json(tmp_path, "booking-10.json", modified)

        results = scan_queue(tmp_path, reg)
        assert len(results) == 1
        assert results[0].response_id == "TEST-001-REVISED"

    def test_ignores_non_booking_files(self, tmp_path, robots):
        tmp_path / "readme.txt"
        (tmp_path / "readme.txt").write_text("hello")
        (tmp_path / "other-123.json").write_text("{}")
        _write_booking_json(tmp_path, "booking-5.json", GOOD_OUTER_JSON)

        reg = _Registry(tmp_path / "reg.json")
        results = scan_queue(tmp_path, reg)
        assert len(results) == 1
        assert results[0].booking_id == "5"

    def test_handles_missing_directory(self):
        reg = _Registry(Path("/tmp/nonexistent/reg.json"))
        results = scan_queue(Path("/tmp/nonexistent"), reg)
        assert results == []


# ---------------------------------------------------------------------------
# Tests: run_once (CLI wrapper)
# ---------------------------------------------------------------------------

class TestRunOnce:
    def test_returns_results(self, tmp_path, robots, monkeypatch):
        _write_booking_json(tmp_path, "booking-7.json", GOOD_OUTER_JSON)
        monkeypatch.setenv("BOOKING_QUEUE_DIR", str(tmp_path))
        results = run_once()
        assert len(results) == 1
        assert results[0].booking_id == "7"

    def test_disabled_when_no_dir(self, monkeypatch):
        monkeypatch.delenv("BOOKING_QUEUE_DIR", raising=False)
        results = run_once()
        assert results == []


# ---------------------------------------------------------------------------
# Tests: facility resolution
# ---------------------------------------------------------------------------

class TestFacilityResolution:
    def test_normalise_strips_json_array(self):
        assert _normalise_facility('["Autonomous Robot Platform"]') == "Autonomous Robot Platform"

    def test_normalise_strips_whitespace(self):
        assert _normalise_facility("  UR10e (01)  ") == "UR10e (01)"

    def test_normalise_empty(self):
        assert _normalise_facility(None) is None
        assert _normalise_facility("") is None
        assert _normalise_facility("[]") is None

    def test_exact_name_match(self, db, robots):
        preferred, candidates = resolve_facility(db, "UR10e (01)")
        assert preferred is not None
        assert preferred.name == "UR10e (01)"
        assert len(candidates) == 1

    def test_case_insensitive_match(self, db, robots):
        preferred, candidates = resolve_facility(db, "ur10e (01)")
        assert preferred is not None
        assert preferred.name == "UR10e (01)"

    def test_json_array_facility_exact_match(self, db, robots):
        preferred, candidates = resolve_facility(db, '["UR10e (01)"]')
        assert preferred is not None
        assert preferred.name == "UR10e (01)"

    def test_generic_facility_resolves_via_map(self, db, robots, monkeypatch):
        monkeypatch.setenv(
            "BOOKING_FACILITY_MAP",
            json.dumps({"Autonomous Robot Platform": ["Collaborative Robots"]}),
        )
        preferred, candidates = resolve_facility(db, '["Autonomous Robot Platform"]')
        assert preferred is None
        # 4 active UR robots (UR10e 04 is out of service in test fixtures)
        assert len(candidates) == 4

    def test_no_match_returns_empty(self, db, robots):
        preferred, candidates = resolve_facility(db, "Nonexistent Facility")
        assert preferred is None
        assert candidates == []

    def test_no_facility_returns_empty(self, db, robots):
        preferred, candidates = resolve_facility(db, None)
        assert preferred is None
        assert candidates == []
