"""Conflict engine tests — SPEC 23."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models import ReservationStatus, ResourceStatus, SourceType
from app.services import availability as av


def _slots(start="2026-08-31", end="2026-08-31", sessions=("AM",)):
    return av.expand_slots(date.fromisoformat(start), date.fromisoformat(end), list(sessions))


# --- overlap primitive -----------------------------------------------------

@pytest.mark.parametrize(
    "a_start,a_end,b_start,b_end,expected",
    [
        ("2026-08-31T08:30", "2026-08-31T12:00", "2026-08-31T13:30", "2026-08-31T17:00", False),
        ("2026-08-31T08:30", "2026-08-31T12:00", "2026-08-31T08:30", "2026-08-31T12:00", True),
        ("2026-08-31T08:30", "2026-08-31T12:00", "2026-08-31T11:00", "2026-08-31T14:00", True),
        ("2026-08-31T08:30", "2026-08-31T12:00", "2026-08-31T12:00", "2026-08-31T15:00", False),
        ("2026-08-31T09:00", "2026-08-31T10:00", "2026-08-31T08:00", "2026-08-31T18:00", True),
    ],
    ids=["no-overlap", "exact-overlap", "partial-overlap", "adjacent", "enclosed"],
)
def test_overlap_rule(a_start, a_end, b_start, b_end, expected):
    assert (
        av.overlaps(
            datetime.fromisoformat(a_start),
            datetime.fromisoformat(a_end),
            datetime.fromisoformat(b_start),
            datetime.fromisoformat(b_end),
        )
        is expected
    )


# --- slot expansion --------------------------------------------------------

def test_multi_day_am_pm_request_expands_to_ten_slots():
    slots = _slots("2026-08-31", "2026-09-04", ("AM", "PM"))
    assert len(slots) == 10
    assert slots[0].start_at == datetime(2026, 8, 31, 8, 30)
    assert slots[0].end_at == datetime(2026, 8, 31, 12, 0)
    assert slots[1].start_at == datetime(2026, 8, 31, 13, 30)
    assert slots[-1].day == date(2026, 9, 4)


def test_expansion_keeps_session_order_and_rejects_reversed_dates():
    assert [s.session for s in _slots(sessions=("PM", "AM"))] == ["AM", "PM"]
    assert _slots("2026-09-04", "2026-08-31", ("AM",)) == []
    assert _slots(sessions=()) == []


# --- resource checks -------------------------------------------------------

def test_free_resource_is_available(db, robots):
    result = av.check_resource(db, robots[0], _slots("2026-08-31", "2026-09-04", ("AM", "PM")))
    assert result.status == av.AVAILABLE
    assert all(s.available for s in result.slots)


def test_lesson_blocks_a_booking(db, robots, make_reservation):
    make_reservation(robots[2], "2026-09-01T09:30", "2026-09-01T12:20", title="ME3101")
    result = av.check_resource(db, robots[2], _slots("2026-08-31", "2026-09-02", ("AM", "PM")))

    assert result.status == av.CONFLICT
    blocked = [s for s in result.slots if not s.available]
    assert [s.slot.label for s in blocked] == ["01 Sep AM"]
    assert "ME3101" in blocked[0].description


def test_maintenance_blocks_a_booking(db, robots, make_reservation):
    make_reservation(
        robots[0],
        "2026-08-31T08:00",
        "2026-08-31T18:00",
        source_type=SourceType.MAINTENANCE,
        title="Annual calibration",
    )
    result = av.check_resource(db, robots[0], _slots("2026-08-31", "2026-08-31", ("AM", "PM")))
    assert result.status == av.CONFLICT
    assert result.conflict_count == 2


def test_adjacent_reservation_is_not_a_conflict(db, robots, make_reservation):
    make_reservation(robots[0], "2026-08-31T12:00", "2026-08-31T13:30", title="Setup")
    result = av.check_resource(db, robots[0], _slots("2026-08-31", "2026-08-31", ("AM", "PM")))
    assert result.status == av.AVAILABLE


def test_cancelled_reservation_does_not_block(db, robots, make_reservation):
    make_reservation(
        robots[0],
        "2026-08-31T08:30",
        "2026-08-31T12:00",
        status=ReservationStatus.CANCELLED,
    )
    result = av.check_resource(db, robots[0], _slots())
    assert result.status == av.AVAILABLE


def test_out_of_service_resource_is_unavailable(db, robots):
    out_of_service = robots[3]
    assert out_of_service.status == ResourceStatus.OUT_OF_SERVICE
    result = av.check_resource(db, out_of_service, _slots())
    assert result.status == av.OUT_OF_SERVICE
    assert result.fully_available is False


# --- alternatives ----------------------------------------------------------

def test_alternatives_exclude_out_of_service_and_rank_free_robots_first(
    db, robots, make_reservation
):
    make_reservation(robots[2], "2026-08-31T08:30", "2026-08-31T12:00", title="ME3101")
    make_reservation(robots[1], "2026-08-31T08:30", "2026-08-31T12:00", title="ME3101")

    report = av.build_report(db, _slots(), robots[2])

    assert report.status == av.CONFLICT
    names = [a.resource.name for a in report.alternatives]
    assert "UR10e (04)" not in names, "out-of-service robot must never be suggested"
    assert names == ["UR10e (01)", "UR10e (05)", "UR10e (02)"]
    assert [a.resource.name for a in report.available_alternatives] == [
        "UR10e (01)",
        "UR10e (05)",
    ]


def test_alternatives_stay_inside_the_resource_group(db, robots, make_reservation):
    from app.models import Resource

    db.add(
        Resource(
            name="KUKA (01)",
            model="KR6",
            resource_group="Industrial Robots",
            status=ResourceStatus.ACTIVE,
        )
    )
    db.commit()
    make_reservation(robots[0], "2026-08-31T08:30", "2026-08-31T12:00")

    report = av.build_report(db, _slots(), robots[0])
    assert "KUKA (01)" not in [a.resource.name for a in report.alternatives]


def test_report_without_a_preferred_resource_still_lists_alternatives(db, robots):
    report = av.build_report(db, _slots(), None)
    assert report.preferred is None
    assert len(report.alternatives) == 4  # the out-of-service robot is excluded


def test_check_request_ignores_the_requests_own_reservations(db, robots, make_request):
    from app.models import Reservation

    booking = make_request(start="2026-08-31", end="2026-08-31", sessions=("AM",))
    db.add(
        Reservation(
            resource_id=robots[0].id,
            source_type=SourceType.BOOKING,
            booking_request_id=booking.id,
            title="DING Changwen",
            start_at=datetime(2026, 8, 31, 8, 30),
            end_at=datetime(2026, 8, 31, 12, 0),
            status=ReservationStatus.ACTIVE,
        )
    )
    db.commit()

    report = av.check_request(db, booking, robots[0])
    assert report.status == av.AVAILABLE
