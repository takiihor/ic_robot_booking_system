"""Availability and conflict engine — SPEC 10.

Deterministic, no heuristics. `reservations` is the only source of truth:
a slot is blocked when an ACTIVE reservation on the same resource overlaps it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import SESSION_ORDER, SESSION_TIMES
from app.models import (
    BookingRequest,
    Reservation,
    ReservationStatus,
    Resource,
    ResourceStatus,
)

AVAILABLE = "AVAILABLE"
CONFLICT = "CONFLICT"
OUT_OF_SERVICE = "OUT OF SERVICE"


@dataclass(frozen=True)
class Slot:
    """One requested date + session, expanded to a concrete datetime range."""

    day: date
    session: str
    start_at: datetime
    end_at: datetime

    @property
    def label(self) -> str:
        return f"{self.day.strftime('%d %b')} {self.session}"


@dataclass
class SlotResult:
    slot: Slot
    conflicts: list[Reservation] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return not self.conflicts

    @property
    def description(self) -> str:
        if self.available:
            return "Available"
        parts = [f"{r.short_label} {r.title}" for r in self.conflicts]
        return "Conflict - " + "; ".join(parts)


@dataclass
class ResourceAvailability:
    resource: Resource
    slots: list[SlotResult] = field(default_factory=list)
    status: str = AVAILABLE

    @property
    def fully_available(self) -> bool:
        return self.status == AVAILABLE

    @property
    def conflict_count(self) -> int:
        return sum(1 for s in self.slots if not s.available)

    @property
    def summary(self) -> str:
        if self.status == OUT_OF_SERVICE:
            return f"{self.resource.status}"
        if self.fully_available:
            return "Fully Available"
        return f"Conflict ({self.conflict_count} of {len(self.slots)} slots)"


@dataclass
class AvailabilityReport:
    slots: list[Slot]
    preferred: ResourceAvailability | None
    alternatives: list[ResourceAvailability] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.preferred.status if self.preferred else CONFLICT

    @property
    def available_alternatives(self) -> list[ResourceAvailability]:
        return [a for a in self.alternatives if a.fully_available]


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """SPEC 10.2 — half-open ranges, so adjacent times do not conflict."""
    return a_start < b_end and a_end > b_start


def expand_slots(
    start_date: date, end_date: date, sessions: list[str], *, weekdays_only: bool = False
) -> list[Slot]:
    """Expand a multi-day request into individual session slots (SPEC 10.1)."""
    ordered = [s for s in SESSION_ORDER if s in sessions]
    if not ordered or end_date < start_date:
        return []

    slots: list[Slot] = []
    day = start_date
    while day <= end_date:
        if not (weekdays_only and day.weekday() >= 5):
            for session in ordered:
                start_time, end_time = SESSION_TIMES[session]
                slots.append(
                    Slot(
                        day=day,
                        session=session,
                        start_at=datetime.combine(day, start_time),
                        end_at=datetime.combine(day, end_time),
                    )
                )
        day += timedelta(days=1)
    return slots


def slots_for_request(request: BookingRequest) -> list[Slot]:
    return expand_slots(request.start_date, request.end_date, request.sessions)


def active_reservations(
    db: Session,
    resource_ids: list[int],
    window_start: datetime,
    window_end: datetime,
    *,
    exclude_request_id: int | None = None,
) -> list[Reservation]:
    """Every ACTIVE reservation for these resources overlapping the window."""
    if not resource_ids:
        return []
    stmt = (
        select(Reservation)
        .where(Reservation.resource_id.in_(resource_ids))
        .where(Reservation.status == ReservationStatus.ACTIVE)
        .where(Reservation.start_at < window_end)
        .where(Reservation.end_at > window_start)
        .order_by(Reservation.start_at)
    )
    if exclude_request_id is not None:
        stmt = stmt.where(
            (Reservation.booking_request_id.is_(None))
            | (Reservation.booking_request_id != exclude_request_id)
        )
    return list(db.scalars(stmt))


def check_resource(
    db: Session,
    resource: Resource,
    slots: list[Slot],
    *,
    exclude_request_id: int | None = None,
    existing: list[Reservation] | None = None,
) -> ResourceAvailability:
    """Check one resource against a list of requested slots."""
    result = ResourceAvailability(resource=resource, slots=[])

    if not slots:
        result.status = CONFLICT if resource.is_active else OUT_OF_SERVICE
        return result

    if not resource.is_active:
        result.slots = [SlotResult(slot=s) for s in slots]
        result.status = OUT_OF_SERVICE
        return result

    if existing is None:
        existing = active_reservations(
            db,
            [resource.id],
            min(s.start_at for s in slots),
            max(s.end_at for s in slots),
            exclude_request_id=exclude_request_id,
        )
    reservations = [r for r in existing if r.resource_id == resource.id]

    for slot in slots:
        hits = [
            r for r in reservations if overlaps(slot.start_at, slot.end_at, r.start_at, r.end_at)
        ]
        result.slots.append(SlotResult(slot=slot, conflicts=hits))

    result.status = AVAILABLE if all(s.available for s in result.slots) else CONFLICT
    return result


def candidate_resources(db: Session, preferred: Resource | None) -> list[Resource]:
    """Active resources in the same group (or model) as the preferred robot."""
    stmt = select(Resource).where(Resource.status == ResourceStatus.ACTIVE)
    if preferred is not None:
        stmt = stmt.where(Resource.id != preferred.id)
        if preferred.resource_group:
            stmt = stmt.where(Resource.resource_group == preferred.resource_group)
    return list(db.scalars(stmt.order_by(Resource.name)))


def build_report(
    db: Session,
    slots: list[Slot],
    preferred: Resource | None,
    *,
    exclude_request_id: int | None = None,
    include_alternatives: bool = True,
) -> AvailabilityReport:
    """Check the preferred robot, then rank alternatives (SPEC 10.3-10.4)."""
    report = AvailabilityReport(slots=slots, preferred=None)
    if not slots:
        return report

    window_start = min(s.start_at for s in slots)
    window_end = max(s.end_at for s in slots)

    others = candidate_resources(db, preferred) if include_alternatives else []
    all_resources = ([preferred] if preferred else []) + others
    reservations = active_reservations(
        db,
        [r.id for r in all_resources],
        window_start,
        window_end,
        exclude_request_id=exclude_request_id,
    )

    if preferred is not None:
        report.preferred = check_resource(db, preferred, slots, existing=reservations)

    checked = [check_resource(db, r, slots, existing=reservations) for r in others]
    # Fully available alternatives first, then fewest conflicts, then name.
    checked.sort(key=lambda a: (not a.fully_available, a.conflict_count, a.resource.name))
    report.alternatives = checked
    return report


def check_request(
    db: Session, request: BookingRequest, resource: Resource | None = None, **kwargs
) -> AvailabilityReport:
    """Availability report for a stored request against its chosen resource."""
    target = resource or request.assigned_resource or request.preferred_resource
    return build_report(
        db,
        slots_for_request(request),
        target,
        exclude_request_id=request.id,
        **kwargs,
    )
