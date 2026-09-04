"""Booking request lifecycle — SPEC 11.

Rules enforced here: an approval always re-runs the conflict check against
live data (SPEC 10.5), and nothing is ever hard-deleted (SPEC 11.3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import SESSION_LABELS
from app.models import (
    Applicant,
    BookingRequest,
    Reservation,
    ReservationStatus,
    RequestStatus,
    Resource,
    ResourceStatus,
    SourceType,
    utcnow,
)
from app.services import availability as av

log = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised when a request or action fails the SPEC 18 validation rules."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --------------------------------------------------------------------------
# Applicants
# --------------------------------------------------------------------------

def upsert_applicant(
    db: Session,
    *,
    name: str,
    sid_netid: str | None = None,
    department: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    current: Applicant | None = None,
) -> Applicant:
    """Reuse the applicant with this SID/NetID rather than duplicating them.

    `current` is the applicant a request is already attached to. Passing it
    switches this from "find or create" to "correct in place": a typo fixed in
    the SID/NetID field moves onto the existing record instead of forking a
    second applicant and splitting that person's booking history. The request
    only moves to a different applicant when the new SID/NetID already belongs
    to one — i.e. when staff are fixing who the booking is actually for.
    """
    sid = (sid_netid or "").strip() or None
    applicant: Applicant | None = None
    if sid:
        applicant = db.scalar(select(Applicant).where(Applicant.sid_netid == sid))

    if applicant is None:
        if current is not None:
            applicant = current
            if sid:
                applicant.sid_netid = sid
        else:
            if not sid:
                applicant = db.scalar(
                    select(Applicant)
                    .where(Applicant.sid_netid.is_(None))
                    .where(Applicant.name == name.strip())
                )
            if applicant is None:
                applicant = Applicant(name=name.strip(), sid_netid=sid)
                db.add(applicant)

    applicant.name = name.strip() or applicant.name
    for attr, value in (
        ("department", department),
        ("email", email),
        ("phone", phone),
    ):
        cleaned = (value or "").strip()
        if cleaned:
            setattr(applicant, attr, cleaned)
    db.flush()
    return applicant


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_request_fields(
    *,
    name: str | None,
    start_date: date | None,
    end_date: date | None,
    sessions: list[str],
) -> list[str]:
    errors: list[str] = []
    if not (name or "").strip():
        errors.append("Applicant name is required.")
    if start_date is None:
        errors.append("Start date is required.")
    if end_date is None:
        errors.append("End date is required.")
    if start_date and end_date and end_date < start_date:
        errors.append("End date must be on or after the start date.")
    if not sessions:
        errors.append("At least one session (AM or PM) is required.")
    return errors


#: Statuses that no longer hold a Response ID against a new entry.
CLOSED_STATUSES = (RequestStatus.CANCELLED, RequestStatus.REJECTED)


def response_id_errors(
    db: Session, response_id: str | None, request: BookingRequest | None = None
) -> list[str]:
    """Response IDs are unique among *live* requests only.

    A cancelled or rejected request keeps its Response ID for the audit trail
    but stops reserving it, so staff can re-enter the same application without
    having to blank the field and lose the link back to the Teams form.
    """
    if not response_id:
        return []
    clash = db.scalar(
        select(BookingRequest)
        .where(BookingRequest.response_id == response_id)
        .where(BookingRequest.status.not_in(CLOSED_STATUSES))
        .order_by(BookingRequest.id)
    )
    if clash is None or (request is not None and clash.id == request.id):
        return []
    return [f"Response ID {response_id} is already used by live request #{clash.id}."]


# --------------------------------------------------------------------------
# Create / update
# --------------------------------------------------------------------------

def save_request(
    db: Session,
    *,
    request: BookingRequest | None = None,
    name: str,
    sid_netid: str | None,
    department: str | None,
    email: str | None,
    phone: str | None,
    response_id: str | None,
    facility: str | None,
    booking_type: str | None,
    start_date: date | None,
    end_date: date | None,
    sessions: list[str],
    preferred_resource_id: int | None,
    assigned_resource_id: int | None = None,
    purpose: str | None,
    remarks: str | None,
    raw_text: str | None = None,
) -> BookingRequest:
    """Create or edit a *pending* booking request from preview form data.

    An approved request must go through `amend_request` instead: editing one
    here would leave its reservations sitting on the old dates.
    """
    if request is not None and request.status != RequestStatus.PENDING:
        raise ValidationError(
            [
                f"Only pending requests can be edited directly (this one is "
                f"{request.status}). Amend the approved booking or reopen it first."
            ]
        )

    response_id = (response_id or "").strip() or None
    errors = validate_request_fields(
        name=name, start_date=start_date, end_date=end_date, sessions=sessions
    )
    errors += response_id_errors(db, response_id, request)
    if errors:
        raise ValidationError(errors)

    if request is None:
        request = BookingRequest(status=RequestStatus.PENDING)

    _apply_fields(
        db,
        request,
        name=name,
        sid_netid=sid_netid,
        department=department,
        email=email,
        phone=phone,
        response_id=response_id,
        facility=facility,
        booking_type=booking_type,
        start_date=start_date,
        end_date=end_date,
        sessions=sessions,
        preferred_resource_id=preferred_resource_id,
        assigned_resource_id=assigned_resource_id,
        purpose=purpose,
        remarks=remarks,
        raw_text=raw_text,
    )
    return request


def _apply_fields(
    db: Session,
    request: BookingRequest,
    *,
    name: str,
    sid_netid: str | None,
    department: str | None,
    email: str | None,
    phone: str | None,
    response_id: str | None,
    facility: str | None,
    booking_type: str | None,
    start_date: date | None,
    end_date: date | None,
    sessions: list[str],
    preferred_resource_id: int | None,
    assigned_resource_id: int | None = None,
    purpose: str | None,
    remarks: str | None,
    raw_text: str | None = None,
) -> None:
    """Write validated form values onto a request. No status or slot logic."""
    applicant = upsert_applicant(
        db,
        name=name,
        sid_netid=sid_netid,
        department=department,
        email=email,
        phone=phone,
        current=request.applicant if request.applicant_id else None,
    )
    request.applicant_id = applicant.id
    db.add(request)  # no-op when the request is already persistent
    request.response_id = response_id
    request.facility = (facility or "").strip() or None
    request.booking_type = (booking_type or "").strip() or None
    request.start_date = start_date
    request.end_date = end_date
    request.sessions = sessions
    request.preferred_resource_id = preferred_resource_id
    if assigned_resource_id is not None:
        request.assigned_resource_id = assigned_resource_id
    request.purpose = (purpose or "").strip() or None
    request.remarks = (remarks or "").strip() or None
    if raw_text is not None:
        request.raw_text = raw_text
    db.flush()


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def approve_request(
    db: Session,
    request: BookingRequest,
    resource_id: int | None,
    *,
    allow_conflicts: bool = False,
    conflict_note: str | None = None,
) -> list[Reservation]:
    """Approve a request: re-check conflicts, then write one row per slot.

    `allow_conflicts` is the deliberate override (SPEC 11.8): the robot is still
    lent out, and every clashing slot is stamped with a note saying what it
    shares the session with. An Out of Service robot is never overridable — it
    physically cannot be handed over.
    """
    errors: list[str] = []
    if request.status not in (RequestStatus.PENDING,):
        errors.append(f"Only pending requests can be approved (this one is {request.status}).")

    resource = db.get(Resource, resource_id) if resource_id else None
    if resource is None:
        errors.append("A robot must be selected before approval.")
    elif resource.status != ResourceStatus.ACTIVE:
        errors.append(f"{resource.name} is {resource.status} and cannot be booked.")
    if errors:
        raise ValidationError(errors)

    slots = av.slots_for_request(request)
    if not slots:
        raise ValidationError(["This request has no bookable sessions."])

    report = _recheck(db, request, resource, slots, "Approval", allow_conflicts=allow_conflicts)
    created = _write_reservations(db, request, resource, slots, report, conflict_note)

    request.assigned_resource_id = resource.id
    request.status = RequestStatus.APPROVED
    request.decided_at = utcnow()
    db.flush()
    clashes = sum(1 for r in created if r.conflict_note)
    log.info(
        "Request %s approved on %s (%d reservations, %d over existing entries)",
        request.id,
        resource.name,
        len(created),
        clashes,
    )
    return created


def _clash_note(slot_result: av.SlotResult, staff_note: str | None) -> str | None:
    """Human-readable record of what this slot was knowingly booked over."""
    if slot_result.available:
        return None
    parts = [
        f'{c.short_label} "{c.title}" '
        f'{c.start_at.strftime("%H:%M")}-{c.end_at.strftime("%H:%M")}'
        for c in slot_result.conflicts
    ]
    note = "Shares this session with " + "; ".join(parts) + "."
    if any(c.source_type == SourceType.LESSON for c in slot_result.conflicts):
        note += " The robot must be back before the lesson starts."
    staff_note = (staff_note or "").strip()
    if staff_note:
        note += f" {staff_note}"
    return note


def _write_reservations(
    db: Session,
    request: BookingRequest,
    resource: Resource,
    slots: list[av.Slot],
    report: av.AvailabilityReport | None = None,
    staff_note: str | None = None,
) -> list[Reservation]:
    """One ACTIVE BOOKING reservation per requested slot.

    When `report` shows a slot was booked over something, that reservation is
    stamped with a conflict note so the calendar can flag the day.
    """
    notes: dict[tuple[datetime, datetime], str | None] = {}
    if report is not None and report.preferred is not None:
        notes = {
            (s.slot.start_at, s.slot.end_at): _clash_note(s, staff_note)
            for s in report.preferred.slots
        }

    applicant = request.applicant
    details = "\n".join(
        part
        for part in [
            f"Applicant: {applicant.name}",
            f"SID/NetID: {applicant.sid_netid}" if applicant.sid_netid else "",
            f"Purpose: {request.purpose}" if request.purpose else "",
        ]
        if part
    )

    created: list[Reservation] = []
    for slot in slots:
        reservation = Reservation(
            resource_id=resource.id,
            source_type=SourceType.BOOKING,
            title=applicant.name,
            start_at=slot.start_at,
            end_at=slot.end_at,
            status=ReservationStatus.ACTIVE,
            details=details or None,
            conflict_note=notes.get((slot.start_at, slot.end_at)),
        )
        # Append through the relationship rather than setting the FK by hand, so
        # request.reservations stays correct in this session — amend_request and
        # the last-slot cascade both read that collection back.
        request.reservations.append(reservation)
        created.append(reservation)
    return created


def _recheck(
    db: Session,
    request: BookingRequest,
    resource: Resource,
    slots: list[av.Slot],
    action: str,
    *,
    allow_conflicts: bool = False,
) -> av.AvailabilityReport:
    """SPEC 10.5 — re-run the conflict check against live data before writing.

    Returns the report so the caller can stamp the clashing slots. Conflicts
    stop the write unless staff explicitly chose to go ahead anyway.
    """
    report = av.build_report(
        db, slots, resource, exclude_request_id=request.id, include_alternatives=False
    )
    if report.status == av.AVAILABLE or allow_conflicts:
        return report

    conflicting = [s.slot.label for s in report.preferred.slots if not s.available]
    log.warning("%s blocked for request %s on %s: %s", action, request.id, resource.name, conflicting)
    raise ValidationError(
        [
            f"{resource.name} is not free for: {', '.join(conflicting)}. "
            "Re-run Check Availability, pick another robot, or use “Accept anyway” "
            "to lend it out over the clash."
        ]
    )


def amend_request(
    db: Session,
    request: BookingRequest,
    *,
    resource_id: int | None,
    name: str,
    sid_netid: str | None,
    department: str | None,
    email: str | None,
    phone: str | None,
    response_id: str | None,
    facility: str | None,
    booking_type: str | None,
    start_date: date | None,
    end_date: date | None,
    sessions: list[str],
    preferred_resource_id: int | None,
    purpose: str | None,
    remarks: str | None,
    allow_conflicts: bool = False,
    conflict_note: str | None = None,
) -> list[Reservation]:
    """Edit an approved booking and rebuild its reservations to match.

    Without this the only way to fix an approved booking is to cancel it and
    retype the whole application. The old slots are released and new ones
    written in one transaction, so the calendar never disagrees with the
    request it came from.
    """
    if request.status != RequestStatus.APPROVED:
        raise ValidationError(
            [f"Only approved bookings can be amended (this one is {request.status})."]
        )

    response_id = (response_id or "").strip() or None
    errors = validate_request_fields(
        name=name, start_date=start_date, end_date=end_date, sessions=sessions
    )
    errors += response_id_errors(db, response_id, request)

    resource = db.get(Resource, resource_id) if resource_id else request.assigned_resource
    if resource is None:
        errors.append("A robot must be assigned to the amended booking.")
    elif resource.status != ResourceStatus.ACTIVE:
        errors.append(f"{resource.name} is {resource.status} and cannot be booked.")
    if errors:
        raise ValidationError(errors)

    _apply_fields(
        db,
        request,
        name=name,
        sid_netid=sid_netid,
        department=department,
        email=email,
        phone=phone,
        response_id=response_id,
        facility=facility,
        booking_type=booking_type,
        start_date=start_date,
        end_date=end_date,
        sessions=sessions,
        preferred_resource_id=preferred_resource_id,
        purpose=purpose,
        remarks=remarks,
    )

    slots = av.slots_for_request(request)
    if not slots:
        raise ValidationError(["This request has no bookable sessions."])
    report = _recheck(
        db, request, resource, slots, "Amendment", allow_conflicts=allow_conflicts
    )

    released = 0
    for reservation in request.reservations:
        if reservation.status == ReservationStatus.ACTIVE:
            reservation.status = ReservationStatus.CANCELLED
            released += 1

    created = _write_reservations(db, request, resource, slots, report, conflict_note)
    request.assigned_resource_id = resource.id
    db.flush()
    log.info(
        "Request %s amended on %s (%d released, %d written)",
        request.id,
        resource.name,
        released,
        len(created),
    )
    return created


def reject_request(db: Session, request: BookingRequest, reason: str | None) -> BookingRequest:
    if request.status != RequestStatus.PENDING:
        raise ValidationError(
            [f"Only pending requests can be rejected (this one is {request.status})."]
        )
    request.status = RequestStatus.REJECTED
    request.rejection_reason = (reason or "").strip() or None
    request.decided_at = utcnow()
    db.flush()
    log.info("Request %s rejected", request.id)
    return request


def cancel_request(db: Session, request: BookingRequest, reason: str | None = None) -> BookingRequest:
    """Cancel a request and its reservations, keeping the history (SPEC 11.3)."""
    if request.status not in (RequestStatus.PENDING, RequestStatus.APPROVED):
        raise ValidationError([f"A {request.status} request cannot be cancelled."])

    for reservation in request.reservations:
        reservation.status = ReservationStatus.CANCELLED

    request.status = RequestStatus.CANCELLED
    if reason:
        request.rejection_reason = reason.strip()
    request.decided_at = utcnow()
    db.flush()
    log.info("Request %s cancelled (%d reservations released)", request.id, len(request.reservations))
    return request


def reopen_request(db: Session, request: BookingRequest) -> BookingRequest:
    """Put a cancelled or rejected request back into PENDING.

    A wrong decision used to be unrecoverable — the record was frozen and the
    application had to be retyped. Reopening keeps the same record (and its
    history) and returns it to the normal approve/reject flow. The released
    reservations stay cancelled; approving again writes fresh ones.
    """
    if request.status not in CLOSED_STATUSES:
        raise ValidationError(
            [f"Only cancelled or rejected requests can be reopened (this one is {request.status})."]
        )
    errors = response_id_errors(db, request.response_id, request)
    if errors:
        raise ValidationError(errors)

    request.status = RequestStatus.PENDING
    request.decided_at = None
    request.rejection_reason = None
    db.flush()
    log.info("Request %s reopened as pending", request.id)
    return request


# --------------------------------------------------------------------------
# Search — SPEC 16
# --------------------------------------------------------------------------

def search_requests(db: Session, query: str) -> list[BookingRequest]:
    """Search by applicant name, SID/NetID, response ID or request ID."""
    term = (query or "").strip()
    if not term:
        return []
    like = f"%{term}%"
    conditions = [
        Applicant.name.ilike(like),
        Applicant.sid_netid.ilike(like),
        BookingRequest.response_id.ilike(like),
    ]
    if term.isdigit():
        conditions.append(BookingRequest.id == int(term))
    stmt = (
        select(BookingRequest)
        .join(Applicant)
        .where(or_(*conditions))
        .order_by(BookingRequest.start_date.desc())
    )
    return list(db.scalars(stmt))


@dataclass
class ApplicantHistory:
    """Whether the person on a pasted email has been here before (SPEC 9.4)."""

    applicant: Applicant | None = None
    matched_on: str = ""  # "sid" | "name"
    requests: list[BookingRequest] = None  # type: ignore[assignment]
    same_name: list[Applicant] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.requests is None:
            self.requests = []
        if self.same_name is None:
            self.same_name = []

    @property
    def is_known(self) -> bool:
        return self.applicant is not None

    @property
    def total(self) -> int:
        return len(self.requests)

    def count(self, status: str) -> int:
        return sum(1 for r in self.requests if r.status == status)

    @property
    def last_request(self) -> BookingRequest | None:
        """Most recent booking by start date."""
        return max(self.requests, key=lambda r: r.start_date, default=None)

    @property
    def summary(self) -> str:
        if not self.is_known:
            return "No previous booking on record."
        parts = [f"{self.total} previous request{'s' if self.total != 1 else ''}"]
        for status in (RequestStatus.APPROVED, RequestStatus.CANCELLED, RequestStatus.REJECTED):
            found = self.count(status)
            if found:
                parts.append(f"{found} {status.lower()}")
        return ", ".join(parts) + "."


def lookup_applicant_history(
    db: Session, *, name: str | None, sid_netid: str | None
) -> ApplicantHistory:
    """Find this applicant's booking history before the request is saved.

    SID/NetID is authoritative; the name is a fallback so a returning student is
    still recognised when the form omitted their ID. `same_name` reports other
    people sharing the name so staff can spot a mix-up.
    """
    sid = (sid_netid or "").strip() or None
    cleaned_name = (name or "").strip()

    applicant: Applicant | None = None
    matched_on = ""
    if sid:
        applicant = db.scalar(select(Applicant).where(Applicant.sid_netid == sid))
        if applicant is not None:
            matched_on = "sid"

    by_name: list[Applicant] = []
    if cleaned_name:
        by_name = list(
            db.scalars(select(Applicant).where(Applicant.name.ilike(cleaned_name)))
        )
    if applicant is None and by_name:
        applicant = by_name[0]
        matched_on = "name"

    history = ApplicantHistory(
        applicant=applicant,
        matched_on=matched_on,
        same_name=[a for a in by_name if applicant is None or a.id != applicant.id],
    )
    if applicant is not None:
        history.requests = list(
            db.scalars(
                select(BookingRequest)
                .where(BookingRequest.applicant_id == applicant.id)
                .order_by(BookingRequest.start_date.desc())
            )
        )
    return history


def search_applicants(db: Session, query: str) -> list[Applicant]:
    term = (query or "").strip()
    if not term:
        return []
    like = f"%{term}%"
    stmt = (
        select(Applicant)
        .where(or_(Applicant.name.ilike(like), Applicant.sid_netid.ilike(like)))
        .order_by(Applicant.name)
    )
    return list(db.scalars(stmt))


def session_label(session: str) -> str:
    return SESSION_LABELS.get(session, session)


# --------------------------------------------------------------------------
# Lessons — SPEC 13.4
# --------------------------------------------------------------------------

@dataclass
class LessonConflict:
    """One lesson row that clashes with an existing active reservation."""

    resource: Resource
    course: str
    start_at: datetime
    end_at: datetime
    existing: Reservation

    @property
    def label(self) -> str:
        return (
            f"{self.resource.name}: {self.course} on "
            f"{self.start_at.strftime('%d %b %H:%M')}-{self.end_at.strftime('%H:%M')} "
            f"clashes with {self.existing.type_label} \"{self.existing.title}\""
        )


def check_lesson_conflicts(
    db: Session, lessons: list[dict], resource_ids: list[int]
) -> list[LessonConflict]:
    """Find clashes between the lessons to import and existing reservations."""
    if not lessons or not resource_ids:
        return []
    window_start = min(l["start_at"] for l in lessons)
    window_end = max(l["end_at"] for l in lessons)
    existing = av.active_reservations(db, resource_ids, window_start, window_end)
    resources = {r.id: r for r in db.scalars(select(Resource).where(Resource.id.in_(resource_ids)))}

    conflicts: list[LessonConflict] = []
    for lesson in lessons:
        for resource_id in resource_ids:
            for reservation in existing:
                if reservation.resource_id != resource_id:
                    continue
                if av.overlaps(
                    lesson["start_at"], lesson["end_at"], reservation.start_at, reservation.end_at
                ):
                    conflicts.append(
                        LessonConflict(
                            resource=resources[resource_id],
                            course=lesson["course"],
                            start_at=lesson["start_at"],
                            end_at=lesson["end_at"],
                            existing=reservation,
                        )
                    )
    return conflicts


def import_lessons(
    db: Session, lessons: list[dict], resource_ids: list[int]
) -> list[Reservation]:
    """Create one LESSON reservation per lesson per selected robot."""
    if not lessons:
        raise ValidationError(["There are no lesson rows to import."])
    if not resource_ids:
        raise ValidationError(["Select at least one affected robot."])

    resources = list(db.scalars(select(Resource).where(Resource.id.in_(resource_ids))))
    if len(resources) != len(set(resource_ids)):
        raise ValidationError(["One of the selected robots no longer exists."])

    created: list[Reservation] = []
    for resource in resources:
        for lesson in lessons:
            details = "\n".join(
                part
                for part in [
                    f"Location: {lesson['location']}" if lesson.get("location") else "",
                    f"Notes: {lesson['notes']}" if lesson.get("notes") else "",
                ]
                if part
            )
            reservation = Reservation(
                resource_id=resource.id,
                source_type=SourceType.LESSON,
                title=lesson["course"],
                start_at=lesson["start_at"],
                end_at=lesson["end_at"],
                status=ReservationStatus.ACTIVE,
                details=details or None,
            )
            db.add(reservation)
            created.append(reservation)
    db.flush()
    log.info("Imported %d lesson reservations across %d resources", len(created), len(resources))
    return created


# --------------------------------------------------------------------------
# Manual reservations (maintenance / blocks) — SPEC 14
# --------------------------------------------------------------------------

def create_manual_reservation(
    db: Session,
    *,
    resource_id: int,
    source_type: str,
    title: str,
    start_at: datetime,
    end_at: datetime,
    details: str | None = None,
    allow_overlap: bool = False,
) -> Reservation:
    """Create a maintenance/block/lesson reservation directly on the calendar."""
    errors: list[str] = []
    resource = db.get(Resource, resource_id)
    if resource is None:
        errors.append("Select a robot.")
    if source_type not in SourceType.ALL:
        errors.append(f"Unknown reservation type {source_type!r}.")
    if not (title or "").strip():
        errors.append("A title is required.")
    if start_at is None or end_at is None:
        errors.append("Start and end date/time are required.")
    elif start_at >= end_at:
        errors.append("The start time must be before the end time.")
    if errors:
        raise ValidationError(errors)

    if not allow_overlap:
        clashes = av.active_reservations(db, [resource_id], start_at, end_at)
        clashes = [c for c in clashes if av.overlaps(start_at, end_at, c.start_at, c.end_at)]
        if clashes:
            raise ValidationError(
                [
                    f"{resource.name} already has: "
                    + "; ".join(f'{c.type_label} "{c.title}"' for c in clashes)
                ]
            )

    reservation = Reservation(
        resource_id=resource_id,
        source_type=source_type,
        title=title.strip(),
        start_at=start_at,
        end_at=end_at,
        status=ReservationStatus.ACTIVE,
        details=(details or "").strip() or None,
    )
    db.add(reservation)
    db.flush()
    log.info("Manual %s reservation %s created on %s", source_type, reservation.id, resource.name)
    return reservation


def cancel_reservation(db: Session, reservation: Reservation) -> Reservation:
    """Cancel a single reservation, keeping it in history (SPEC 11.3).

    Releasing one slot of an approved booking is allowed: staff needed a way to
    hand back a single day without cancelling the whole request and re-entering
    the rest. When the last active slot goes, the request follows it to
    CANCELLED so the list and the calendar agree.
    """
    if reservation.status != ReservationStatus.ACTIVE:
        raise ValidationError(["This entry is already cancelled."])

    reservation.status = ReservationStatus.CANCELLED
    db.flush()

    request = reservation.booking_request
    if request is not None and request.status == RequestStatus.APPROVED:
        if not any(r.status == ReservationStatus.ACTIVE for r in request.reservations):
            request.status = RequestStatus.CANCELLED
            request.decided_at = utcnow()
            db.flush()
            log.info("Request %s cancelled — its last slot was released", request.id)

    log.info("Reservation %s cancelled", reservation.id)
    return reservation
