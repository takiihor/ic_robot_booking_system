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
    assert "already used by live request" in excinfo.value.errors[0]


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


# --- correcting a request after it has been decided -------------------------

def test_cancelled_request_frees_its_response_id_for_re_entry(db, robots):
    """The wrong-information case: cancel, then re-enter the same application."""
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.cancel_request(db, booking, "wrong dates")
    db.commit()

    replacement = _save(db, preferred_resource_id=robots[2].id, start_date=date(2026, 9, 7),
                        end_date=date(2026, 9, 11))
    assert replacement.id != booking.id
    assert replacement.response_id == booking.response_id == "R-2026-0142"
    assert booking.status == RequestStatus.CANCELLED


def test_live_request_still_holds_its_response_id(db, robots):
    _save(db, preferred_resource_id=robots[2].id)
    with pytest.raises(svc.ValidationError) as excinfo:
        _save(db, preferred_resource_id=robots[1].id)
    assert "already used by live request" in excinfo.value.errors[0]


def test_reopen_returns_a_cancelled_request_to_pending(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    svc.cancel_request(db, booking, "applicant withdrew")
    db.commit()

    svc.reopen_request(db, booking)
    db.commit()

    assert booking.status == RequestStatus.PENDING
    assert booking.decided_at is None
    assert booking.rejection_reason is None
    # released slots stay released; approving again writes fresh ones
    assert all(r.status == ReservationStatus.CANCELLED for r in booking.reservations)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()
    assert sum(1 for r in booking.reservations if r.status == ReservationStatus.ACTIVE) == 10


def test_reopen_rejects_a_pending_request(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    with pytest.raises(svc.ValidationError) as excinfo:
        svc.reopen_request(db, booking)
    assert "cancelled or rejected" in excinfo.value.errors[0]


def test_reopen_blocked_when_the_response_id_was_taken_meanwhile(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.cancel_request(db, booking, "typo")
    db.commit()
    _save(db, preferred_resource_id=robots[2].id)  # re-entered under the same Response ID

    with pytest.raises(svc.ValidationError) as excinfo:
        svc.reopen_request(db, booking)
    assert "already used by live request" in excinfo.value.errors[0]


def test_save_request_refuses_to_edit_an_approved_booking(db, robots):
    """The service, not just the route, keeps request and calendar in step."""
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()

    with pytest.raises(svc.ValidationError) as excinfo:
        _save(db, request=booking, start_date=date(2026, 9, 7), end_date=date(2026, 9, 11))
    assert "Only pending requests can be edited" in excinfo.value.errors[0]


def _amend(db, booking, **overrides):
    payload = dict(
        resource_id=booking.assigned_resource_id,
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
        remarks=None,
    )
    payload.update(overrides)
    return svc.amend_request(db, booking, **payload)


def test_amend_rewrites_reservations_to_match_the_new_dates(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()

    created = _amend(db, booking, start_date=date(2026, 9, 7), end_date=date(2026, 9, 8),
                     sessions=["AM"])
    db.commit()

    assert booking.status == RequestStatus.APPROVED
    assert len(created) == 2  # 2 days x AM
    active = [r for r in booking.reservations if r.status == ReservationStatus.ACTIVE]
    assert len(active) == 2
    assert {r.start_at.date() for r in active} == {date(2026, 9, 7), date(2026, 9, 8)}
    # the 10 original slots are released, not deleted (SPEC 11.3)
    assert len(booking.reservations) == 12


def test_amend_can_move_a_booking_to_another_robot(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()

    _amend(db, booking, resource_id=robots[1].id)
    db.commit()

    assert booking.assigned_resource_id == robots[1].id
    active = [r for r in booking.reservations if r.status == ReservationStatus.ACTIVE]
    assert {r.resource_id for r in active} == {robots[1].id}


def test_amend_is_refused_when_the_new_dates_clash(db, robots, make_reservation):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()
    make_reservation(robots[2], "2026-09-07T08:30", "2026-09-07T12:00")

    with pytest.raises(svc.ValidationError) as excinfo:
        _amend(db, booking, start_date=date(2026, 9, 7), end_date=date(2026, 9, 7),
               sessions=["AM"])
    assert "is not free for" in excinfo.value.errors[0]
    db.rollback()
    # nothing was released: the original booking survives the failed amendment
    assert sum(1 for r in booking.reservations if r.status == ReservationStatus.ACTIVE) == 10


def test_amend_rejects_an_out_of_service_robot(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()

    with pytest.raises(svc.ValidationError) as excinfo:
        _amend(db, booking, resource_id=robots[3].id)  # (04) is out of service
    assert "cannot be booked" in excinfo.value.errors[0]


def test_amend_refuses_a_pending_request(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    with pytest.raises(svc.ValidationError) as excinfo:
        _amend(db, booking, resource_id=robots[2].id)
    assert "Only approved bookings can be amended" in excinfo.value.errors[0]


# --- releasing a single slot -----------------------------------------------

def test_releasing_one_slot_keeps_the_rest_of_the_booking(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    reservations = svc.approve_request(db, booking, robots[2].id)
    db.commit()

    svc.cancel_reservation(db, reservations[0])
    db.commit()

    assert booking.status == RequestStatus.APPROVED
    assert sum(1 for r in booking.reservations if r.status == ReservationStatus.ACTIVE) == 9


def test_releasing_the_last_slot_cancels_the_request(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id, end_date=date(2026, 8, 31),
                    sessions=["AM"])
    reservations = svc.approve_request(db, booking, robots[2].id)
    db.commit()
    assert len(reservations) == 1

    svc.cancel_reservation(db, reservations[0])
    db.commit()

    assert booking.status == RequestStatus.CANCELLED
    assert booking.decided_at is not None


def test_releasing_an_already_cancelled_slot_is_refused(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    reservations = svc.approve_request(db, booking, robots[2].id)
    db.commit()
    svc.cancel_reservation(db, reservations[0])
    db.commit()

    with pytest.raises(svc.ValidationError) as excinfo:
        svc.cancel_reservation(db, reservations[0])
    assert "already cancelled" in excinfo.value.errors[0]


# --- applicant records ------------------------------------------------------

def test_editing_a_sid_corrects_the_applicant_instead_of_forking_one(db, robots):
    booking = _save(db, preferred_resource_id=robots[2].id)
    original = booking.applicant_id

    _save(db, request=booking, sid_netid="25104513r")

    assert booking.applicant_id == original
    assert booking.applicant.sid_netid == "25104513r"


def test_editing_a_sid_to_an_existing_person_moves_the_request(db, robots):
    other = _save(db, response_id="R-OTHER", name="CHAN Tai Man", sid_netid="99999999x",
                  preferred_resource_id=robots[2].id)
    booking = _save(db, preferred_resource_id=robots[2].id)
    assert booking.applicant_id != other.applicant_id

    _save(db, request=booking, name="CHAN Tai Man", sid_netid="99999999x")

    assert booking.applicant_id == other.applicant_id


# --- accepting over a conflict -----------------------------------------------

def test_approval_over_a_lesson_books_the_robot_and_flags_the_day(db, robots, make_reservation):
    """The lending policy: a lesson does not veto the loan, it flags the day."""
    make_reservation(robots[2], "2026-08-31T09:30", "2026-08-31T12:20", title="ME3101")
    booking = _save(db, preferred_resource_id=robots[2].id)

    reservations = svc.approve_request(db, booking, robots[2].id, allow_conflicts=True)
    db.commit()

    assert booking.status == RequestStatus.APPROVED
    assert len(reservations) == 10  # the whole loan period is still booked
    flagged = booking.clashing_reservations
    assert len(flagged) == 1
    assert flagged[0].start_at == datetime(2026, 8, 31, 8, 30)
    assert 'Lesson "ME3101"' in flagged[0].conflict_note
    assert "back before the lesson starts" in flagged[0].conflict_note
    # untouched days carry no note
    assert all(r.conflict_note is None for r in reservations if r not in flagged)


def test_approval_over_a_conflict_still_needs_the_explicit_override(db, robots, make_reservation):
    make_reservation(robots[2], "2026-08-31T09:30", "2026-08-31T12:20", title="ME3101")
    booking = _save(db, preferred_resource_id=robots[2].id)

    with pytest.raises(svc.ValidationError) as excinfo:
        svc.approve_request(db, booking, robots[2].id)
    assert "Accept anyway" in excinfo.value.errors[0]
    assert booking.status == RequestStatus.PENDING


def test_an_out_of_service_robot_can_never_be_overridden(db, robots):
    booking = _save(db, preferred_resource_id=robots[3].id)
    with pytest.raises(svc.ValidationError) as excinfo:
        svc.approve_request(db, booking, robots[3].id, allow_conflicts=True)
    assert "cannot be booked" in excinfo.value.errors[0]
    assert booking.status == RequestStatus.PENDING


def test_a_staff_note_is_appended_to_every_flagged_day(db, robots, make_reservation):
    make_reservation(robots[2], "2026-08-31T09:30", "2026-08-31T12:20", title="ME3101")
    make_reservation(robots[2], "2026-09-01T09:30", "2026-09-01T12:20", title="ME3101")
    booking = _save(db, preferred_resource_id=robots[2].id)

    svc.approve_request(db, booking, robots[2].id, allow_conflicts=True,
                        conflict_note="Return by 09:00 to the lab technician.")
    db.commit()

    flagged = booking.clashing_reservations
    assert len(flagged) == 2
    assert all("Return by 09:00" in r.conflict_note for r in flagged)


def test_overriding_a_clash_with_another_booking_is_allowed_and_named(db, robots):
    """Double-lending is staff's call, but the note says who else holds it."""
    first = _save(db, preferred_resource_id=robots[2].id, response_id="R-FIRST",
                  end_date=date(2026, 8, 31), sessions=["AM"])
    svc.approve_request(db, first, robots[2].id)
    db.commit()

    second = _save(db, response_id="R-SECOND", name="CHAN Tai Man", sid_netid="88888888y",
                   preferred_resource_id=robots[2].id, end_date=date(2026, 8, 31),
                   sessions=["AM"])
    svc.approve_request(db, second, robots[2].id, allow_conflicts=True)
    db.commit()

    note = second.clashing_reservations[0].conflict_note
    assert "Research" in note and "DING Changwen" in note
    assert "lesson" not in note  # no lesson involved, so no return-early wording


def test_amend_can_also_be_forced_over_a_clash(db, robots, make_reservation):
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id)
    db.commit()
    make_reservation(robots[2], "2026-09-07T09:30", "2026-09-07T12:20", title="ME3101")

    _amend(db, booking, start_date=date(2026, 9, 7), end_date=date(2026, 9, 7),
           sessions=["AM"], allow_conflicts=True)
    db.commit()

    assert booking.status == RequestStatus.APPROVED
    assert len(booking.clashing_reservations) == 1
    assert 'Lesson "ME3101"' in booking.clashing_reservations[0].conflict_note


def test_a_clean_reamendment_clears_the_flag(db, robots, make_reservation):
    make_reservation(robots[2], "2026-08-31T09:30", "2026-08-31T12:20", title="ME3101")
    booking = _save(db, preferred_resource_id=robots[2].id)
    svc.approve_request(db, booking, robots[2].id, allow_conflicts=True)
    db.commit()
    assert booking.clashing_reservations

    # move it to a clear week; the new slots carry no note
    _amend(db, booking, start_date=date(2026, 9, 21), end_date=date(2026, 9, 21),
           sessions=["AM"])
    db.commit()
    assert booking.clashing_reservations == []


# --- is this applicant already on record? -----------------------------------

def test_history_lookup_reports_a_first_time_applicant(db, robots):
    history = svc.lookup_applicant_history(db, name="Nobody Here", sid_netid="00000000a")
    assert history.is_known is False
    assert history.total == 0
    assert history.last_request is None
    assert history.summary == "No previous booking on record."


def test_history_lookup_matches_on_sid_and_counts_past_requests(db, robots):
    first = _save(db, preferred_resource_id=robots[2].id, response_id="R-H1")
    svc.approve_request(db, first, robots[2].id)
    second = _save(db, preferred_resource_id=robots[2].id, response_id="R-H2",
                   start_date=date(2026, 10, 5), end_date=date(2026, 10, 5), sessions=["AM"])
    svc.cancel_request(db, second, "withdrawn")
    db.commit()

    history = svc.lookup_applicant_history(db, name="DING Changwen", sid_netid="25104512r")
    assert history.is_known
    assert history.matched_on == "sid"
    assert history.total == 2
    assert history.count(RequestStatus.APPROVED) == 1
    assert history.count(RequestStatus.CANCELLED) == 1
    assert history.last_request.id == second.id  # latest by start date
    assert "2 previous requests" in history.summary


def test_history_falls_back_to_the_name_when_the_sid_is_missing(db, robots):
    _save(db, preferred_resource_id=robots[2].id, response_id="R-H3")

    history = svc.lookup_applicant_history(db, name="ding changwen", sid_netid=None)
    assert history.is_known
    assert history.matched_on == "name"
    assert history.total == 1


def test_history_flags_other_people_sharing_the_name(db, robots):
    _save(db, preferred_resource_id=robots[2].id, response_id="R-H4")
    _save(db, preferred_resource_id=robots[2].id, response_id="R-H5",
          name="DING Changwen", sid_netid="99999999x")

    history = svc.lookup_applicant_history(db, name="DING Changwen", sid_netid="25104512r")
    assert history.matched_on == "sid"
    assert [a.sid_netid for a in history.same_name] == ["99999999x"]


def test_history_prefers_the_sid_match_over_a_name_collision(db, robots):
    _save(db, preferred_resource_id=robots[2].id, response_id="R-H6")  # 25104512r
    other = _save(db, preferred_resource_id=robots[2].id, response_id="R-H7",
                  name="DING Changwen", sid_netid="99999999x")

    history = svc.lookup_applicant_history(db, name="DING Changwen", sid_netid="99999999x")
    assert history.applicant.id == other.applicant_id
    assert history.total == 1
