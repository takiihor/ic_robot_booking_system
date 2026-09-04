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
    SourceType,
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
class BlockedPeriod:
    """A run of consecutive unavailable days, and when the robot must be back.

    Consecutive days are merged so staff get one hand-back date per run rather
    than one per day: if the robot is wanted elsewhere on the 14th, 15th and
    16th, it has to be returned by the 13th — once.
    """

    start: date
    end: date
    sessions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)

    @property
    def return_by(self) -> date:
        """The day before the run starts — SPEC 10.6."""
        return self.start - timedelta(days=1)

    @property
    def is_single_day(self) -> bool:
        return self.start == self.end

    @property
    def date_label(self) -> str:
        if self.is_single_day:
            return self.start.strftime("%a %d %b %Y")
        if (self.start.month, self.start.year) == (self.end.month, self.end.year):
            return f"{self.start.strftime('%d')}–{self.end.strftime('%d %b %Y')}"
        return f"{self.start.strftime('%d %b')} – {self.end.strftime('%d %b %Y')}"

    @property
    def session_label(self) -> str:
        return ", ".join(self.sessions)

    @property
    def reason_label(self) -> str:
        return "; ".join(self.reasons)


def blocked_periods(availability: ResourceAvailability) -> list[BlockedPeriod]:
    """Group a resource's unavailable slots into runs of consecutive days.

    An out-of-service robot yields nothing: the whole resource is unavailable,
    which the panel states on its own, and there is no hand-back date to give
    for a robot that cannot be lent out at all.
    """
    if availability.status == OUT_OF_SERVICE:
        return []

    by_day: dict[date, SlotResult] = {}
    order: list[date] = []
    sessions: dict[date, list[str]] = {}
    reasons: dict[date, list[str]] = {}
    kinds: dict[date, list[str]] = {}

    for result in availability.slots:
        if result.available:
            continue
        day = result.slot.day
        if day not in by_day:
            by_day[day] = result
            order.append(day)
            sessions[day] = []
            reasons[day] = []
            kinds[day] = []
        if result.slot.session not in sessions[day]:
            sessions[day].append(result.slot.session)
        for conflict in result.conflicts:
            label = f'{conflict.short_label} "{conflict.title}"'
            if label not in reasons[day]:
                reasons[day].append(label)
            if conflict.source_type not in kinds[day]:
                kinds[day].append(conflict.source_type)

    periods: list[BlockedPeriod] = []
    for day in sorted(order):
        current = periods[-1] if periods else None
        if current is not None and day - current.end == timedelta(days=1):
            current.end = day
            for session in sessions[day]:
                if session not in current.sessions:
                    current.sessions.append(session)
            for reason in reasons[day]:
                if reason not in current.reasons:
                    current.reasons.append(reason)
            for kind in kinds[day]:
                if kind not in current.source_types:
                    current.source_types.append(kind)
            continue
        periods.append(
            BlockedPeriod(
                start=day,
                end=day,
                sessions=list(sessions[day]),
                reasons=list(reasons[day]),
                source_types=list(kinds[day]),
            )
        )

    for period in periods:
        period.sessions = [s for s in SESSION_ORDER if s in period.sessions]
    return periods


def _join(phrases: list[str]) -> str:
    """`A`, `A and B`, `A, B and C`."""
    if len(phrases) <= 1:
        return phrases[0] if phrases else ""
    return ", ".join(phrases[:-1]) + " and " + phrases[-1]


def unavailable_message(
    availability: ResourceAvailability,
    periods: list[BlockedPeriod],
    *,
    applicant_name: str | None = None,
) -> str:
    """Note for the applicant naming the reserved dates (SPEC 10.6).

    Wording is the lab's standard notice. "our lesson" only holds when every
    blocking entry really is a lesson; a maintenance window or another booking
    gets neutral wording and names what the robot is committed to instead.
    """
    if not periods:
        return ""

    all_lessons = bool(periods) and all(
        kind == SourceType.LESSON for period in periods for kind in period.source_types
    )

    phrases: list[str] = []
    for period in periods:
        phrase = period.date_label
        if period.sessions:
            phrase += f" ({period.session_label})"
        if not all_lessons and period.reasons:
            phrase += f" - {period.reason_label}"
        phrases.append(phrase)
    dates = _join(phrases)

    lines: list[str] = []
    if applicant_name:
        lines += [f"Dear {applicant_name},", ""]

    if all_lessons:
        lines.append(
            f"Please note that the robot will be reserved for our lesson on {dates}."
        )
        before = "before our lesson begins"
    else:
        lines.append(f"Please note that the robot will be reserved on {dates}.")
        before = "before that period begins"

    lines.append("")
    lines.append(
        "As a reminder, you are required to restore the robot to its original state "
        f"{before}. Once this booking period has concluded, you are welcome to collect "
        "the robot again for your own use."
    )
    return "\n".join(lines)


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
