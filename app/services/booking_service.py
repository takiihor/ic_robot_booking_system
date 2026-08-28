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
) -> Applicant:
    """Reuse the applicant with this SID/NetID rather than duplicating them."""
    applicant: Applicant | None = None
    sid = (sid_netid or "").strip() or None
    if sid:
        applicant = db.scalar(select(Applicant).where(Applicant.sid_netid == sid))
    if applicant is None and not sid:
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
    """Create or update a pending booking request from preview form data."""
    errors = validate_request_fields(
        name=name, start_date=start_date, end_date=end_date, sessions=sessions
    )
    response_id = (response_id or "").strip() or None
    if response_id:
        clash = db.scalar(
            select(BookingRequest).where(BookingRequest.response_id == response_id)
        )
        if clash and (request is None or clash.id != request.id):
            errors.append(f"Response ID {response_id} already exists (request #{clash.id}).")
    if errors:
        raise ValidationError(errors)

    applicant = upsert_applicant(
        db,
        name=name,
        sid_netid=sid_netid,
        department=department,
        email=email,
        phone=phone,
    )

    if request is None:
        request = BookingRequest(applicant_id=applicant.id, status=RequestStatus.PENDING)
        db.add(request)

    request.applicant_id = applicant.id
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
    return request


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def approve_request(
    db: Session, request: BookingRequest, resource_id: int | None
) -> list[Reservation]:
    """Approve a request: re-check conflicts, then write one row per slot."""
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

    # SPEC 10.5 — re-check immediately before writing.
    report = av.build_report(
        db, slots, resource, exclude_request_id=request.id, include_alternatives=False
    )
    if report.status != av.AVAILABLE:
        conflicting = [s.slot.label for s in report.preferred.slots if not s.available]
        log.warning(
            "Approval blocked for request %s on %s: %s", request.id, resource.name, conflicting
        )
        raise ValidationError(
            [
                f"{resource.name} is no longer free for: {', '.join(conflicting)}. "
                "Re-run Check Availability."
            ]
        )

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
            booking_request_id=request.id,
            title=applicant.name,
            start_at=slot.start_at,
            end_at=slot.end_at,
            status=ReservationStatus.ACTIVE,
            details=details or None,
        )
        db.add(reservation)
        created.append(reservation)

    request.assigned_resource_id = resource.id
    request.status = RequestStatus.APPROVED
    request.decided_at = utcnow()
    db.flush()
    log.info(
        "Request %s approved on %s (%d reservations)", request.id, resource.name, len(created)
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
    """Cancel a single reservation, keeping it in history (SPEC 11.3)."""
    reservation.status = ReservationStatus.CANCELLED
    db.flush()
    log.info("Reservation %s cancelled", reservation.id)
    return reservation
