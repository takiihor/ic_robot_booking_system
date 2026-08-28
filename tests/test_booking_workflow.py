"""Booking workflow tests — SPEC 23."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models import ReservationStatus, RequestStatus, ResourceStatus, SourceType
from app.services import availability as av
from app.services import booking_service as svc
from app.services.calendar_view import build_week_grid


def _save(db, **overrides):
    payload = dict(
        name="DING Changwen",
        sid_netid="25104512r",
        department="Mechanical Engineering",
        email="changwen.ding@connect.polyu.hk",
        phone="51234567",
        response_id="R-2026-0142",
        facility="Industrial Robot Laboratory",
        booking_type="Research",
        start_date=date(2026, 8, 31),
        end_date=date(2026, 9, 4),
        sessions=["AM", "PM"],
        preferred_resource_id=None,
        purpose="Robotic grasping experiments",
        remarks="Prefer UR10e (03)",
    )
    payload.update(overrides)
    booking = svc.save_request(db, **payload)
    db.commit()
    return booking


# --- saving ----------------------------------------------------------------

def test_save_pending_request(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)

    assert booking.id is not None
    assert booking.status == RequestStatus.PENDING
    assert booking.sessions == ["AM", "PM"]
    assert booking.applicant.sid_netid == "25104512r"
    assert booking.decided_at is None


def test_same_sid_reuses_the_applicant_record(db, robots):
    first = _save(db, response_id="R-1")
    second = _save(db, response_id="R-2", department="Updated Department")

    assert first.applicant_id == second.applicant_id
    assert second.applicant.department == "Updated Department"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"name": "  "}, "Applicant name is required."),
        ({"start_date": None}, "Start date is required."),
        ({"end_date": None}, "End date is required."),
        (
            {"start_date": date(2026, 9, 4), "end_date": date(2026, 8, 31)},
            "End date must be on or after the start date.",
        ),
        ({"sessions": []}, "At least one session (AM or PM) is required."),
    ],
)
def test_validation_rejects_bad_requests(db, robots, overrides, expected):
    with pytest.raises(svc.ValidationError) as excinfo:
        _save(db, **overrides)
    assert expected in excinfo.value.errors


def test_duplicate_response_id_is_rejected(db, robots):
    _save(db, response_id="R-DUP")
    with pytest.raises(svc.ValidationError) as excinfo:
        svc.save_request(
            db,
            name="Someone Else",
            sid_netid="99999999x",
            department=None,
            email=None,
            phone=None,
            response_id="R-DUP",
            facility=None,
            booking_type=None,
            start_date=date(2026, 8, 31),
            end_date=date(2026, 8, 31),
            sessions=["AM"],
            preferred_resource_id=None,
            purpose=None,
            remarks=None,
        )
    assert "already exists" in excinfo.value.errors[0]


# --- approval --------------------------------------------------------------

def test_approve_available_booking_creates_one_reservation_per_slot(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    reservations = svc.approve_request(db, booking, robots[2].id)
    db.commit()

    assert len(reservations) == 10  # 5 days x AM+PM
    assert booking.status == RequestStatus.APPROVED
    assert booking.assigned_resource_id == robots[2].id
    assert booking.decided_at is not None
    assert all(r.source_type == SourceType.BOOKING for r in reservations)
    assert all(r.title == "DING Changwen" for r in reservations)
    assert reservations[0].start_at == datetime(2026, 8, 31, 8, 30)


def test_approval_re_runs_the_conflict_check(db, robots, make_reservation):
    booking = _save(db, preferred_resource_id=robots[2].id)
    # Availability looked fine, then a lesson landed mid-request.
    make_reservation(robots[2], "2026-09-01T09:30", "2026-09-01T12:20", title="ME3101")

    with pytest.raises(svc.ValidationError) as excinfo:
        svc.approve_request(db, booking, robots[2].id)

    assert "01 Sep AM" in excinfo.value.errors[0]
    assert booking.status == RequestStatus.PENDING
    assert booking.reservations == []


def test_approval_requires_a_resource(db, robots):
    booking = _save(db)
    with pytest.raises(svc.ValidationError) as excinfo:
        svc.approve_request(db, booking, None)
    assert "A robot must be selected before approval." in excinfo.value.errors


def test_approval_rejects_an_inactive_resource(db, robots):
    booking = _save(db)
    with pytest.raises(svc.ValidationError) as excinfo:
        svc.approve_request(db, booking, robots[3].id)
    assert "Out of Service" in excinfo.value.errors[0]


def test_a_request_cannot_be_approved_twice(db, robots):
    booking = _save(db)
    svc.approve_request(db, booking, robots[0].id)
    db.commit()
    with pytest.raises(svc.ValidationError):
        svc.approve_request(db, booking, robots[1].id)


def test_approved_booking_appears_in_calendar_data(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()

    grid = build_week_grid(db, date(2026, 8, 31))
    row = next(r for r in grid.rows if r.resource.id == robots[2].id)
    monday_am = row.cells[0]

    assert [r.title for r in monday_am.reservations] == ["DING Changwen"]
    assert monday_am.reservations[0].source_type == SourceType.BOOKING


def test_approved_booking_blocks_the_next_request(db, robots):
    first = _save(db, preferred_resource_id=robots[2].id, response_id="R-1")
    svc.approve_request(db, first, robots[2].id)
    db.commit()

    second = _save(db, response_id="R-2", sid_netid="20000000a", name="CHAN Tai Man")
    report = av.check_request(db, second, robots[2])
    assert report.status == av.CONFLICT
    assert report.preferred.conflict_count == 10


# --- rejection and cancellation -------------------------------------------

def test_reject_request_records_the_decision_without_reservations(db, robots):
    booking = _save(db)
    svc.reject_request(db, booking, "Robot reserved for teaching that week")
    db.commit()

    assert booking.status == RequestStatus.REJECTED
    assert booking.rejection_reason == "Robot reserved for teaching that week"
    assert booking.decided_at is not None
    assert booking.reservations == []


def test_cancel_approved_request_keeps_history_and_frees_the_robot(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()

    svc.cancel_request(db, booking, "Applicant withdrew")
    db.commit()

    assert booking.status == RequestStatus.CANCELLED
    assert len(booking.reservations) == 10, "history is kept, not deleted"
    assert all(r.status == ReservationStatus.CANCELLED for r in booking.reservations)

    other = _save(db, response_id="R-OTHER", sid_netid="30000000b", name="LEE Siu Ming")
    assert av.check_request(db, other, robots[2]).status == av.AVAILABLE


def test_cancelled_request_disappears_from_the_calendar(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()
    svc.cancel_request(db, booking)
    db.commit()

    grid = build_week_grid(db, date(2026, 8, 31))
    row = next(r for r in grid.rows if r.resource.id == robots[2].id)
    assert all(cell.is_free for cell in row.cells)


def test_a_rejected_request_cannot_be_cancelled(db, robots):
    booking = _save(db)
    svc.reject_request(db, booking, None)
    db.commit()
    with pytest.raises(svc.ValidationError):
        svc.cancel_request(db, booking)


# --- search ----------------------------------------------------------------

def test_search_finds_requests_by_name_sid_and_id(db, robots):
    booking = _save(db)

    assert [r.id for r in svc.search_requests(db, "ding")] == [booking.id]
    assert [r.id for r in svc.search_requests(db, "25104512R")] == [booking.id]
    assert [r.id for r in svc.search_requests(db, str(booking.id))] == [booking.id]
    assert svc.search_requests(db, "nobody") == []
    assert svc.search_requests(db, "") == []


def test_search_finds_applicants(db, robots):
    _save(db)
    found = svc.search_applicants(db, "25104512r")
    assert [a.name for a in found] == ["DING Changwen"]


# --- manual reservations ---------------------------------------------------

def test_manual_maintenance_block_conflicts_with_a_booking(db, robots):
    svc.create_manual_reservation(
        db,
        resource_id=robots[0].id,
        source_type=SourceType.MAINTENANCE,
        title="Annual calibration",
        start_at=datetime(2026, 8, 31, 8, 0),
        end_at=datetime(2026, 8, 31, 18, 0),
    )
    db.commit()

    with pytest.raises(svc.ValidationError) as excinfo:
        svc.create_manual_reservation(
            db,
            resource_id=robots[0].id,
            source_type=SourceType.BLOCK,
            title="Overlapping block",
            start_at=datetime(2026, 8, 31, 9, 0),
            end_at=datetime(2026, 8, 31, 10, 0),
        )
    assert "already has" in excinfo.value.errors[0]


def test_manual_reservation_rejects_a_reversed_time_range(db, robots):
    with pytest.raises(svc.ValidationError) as excinfo:
        svc.create_manual_reservation(
            db,
            resource_id=robots[0].id,
            source_type=SourceType.BLOCK,
            title="Bad range",
            start_at=datetime(2026, 8, 31, 12, 0),
            end_at=datetime(2026, 8, 31, 9, 0),
        )
    assert "before the end time" in excinfo.value.errors[0]
