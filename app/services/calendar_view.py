"""Resource week calendar grid — SPEC 8.

Rows are robots, columns are date + session. Display buckets partition the
whole day (split at 12:45, between the AM and PM session times) so that a
reservation with unusual hours is still visible somewhere rather than hidden.
Conflict checking always uses the exact session times, never these buckets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import SESSION_ORDER
from app.models import Reservation, ReservationStatus, Resource, ResourceStatus

# Boundary between the AM and PM display buckets.
BUCKET_SPLIT = time(12, 45)


@dataclass
class Cell:
    day: date
    session: str
    reservations: list[Reservation] = field(default_factory=list)

    @property
    def is_free(self) -> bool:
        return not self.reservations

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}-{self.session}"


@dataclass
class Row:
    resource: Resource
    cells: list[Cell]


@dataclass
class WeekGrid:
    start: date
    days: list[date]
    columns: list[tuple[date, str]]
    rows: list[Row]

    @property
    def end(self) -> date:
        return self.days[-1]


def week_start(day: date) -> date:
    """Monday of the week containing `day`."""
    return day - timedelta(days=day.weekday())


def _bucket_window(day: date, session: str) -> tuple[datetime, datetime]:
    if session == "AM":
        return datetime.combine(day, time.min), datetime.combine(day, BUCKET_SPLIT)
    return datetime.combine(day, BUCKET_SPLIT), datetime.combine(day + timedelta(days=1), time.min)


def build_week_grid(
    db: Session,
    start: date,
    *,
    resource_ids: list[int] | None = None,
    source_types: list[str] | None = None,
    search: str | None = None,
    days: int = 7,
) -> WeekGrid:
    """Build the week grid, optionally filtered by resource, type or search."""
    monday = week_start(start)
    day_list = [monday + timedelta(days=i) for i in range(days)]
    columns = [(d, s) for d in day_list for s in SESSION_ORDER]

    resource_stmt = select(Resource).where(Resource.status != ResourceStatus.RETIRED)
    if resource_ids:
        resource_stmt = resource_stmt.where(Resource.id.in_(resource_ids))
    resources = list(db.scalars(resource_stmt.order_by(Resource.name)))

    window_start = datetime.combine(day_list[0], time.min)
    window_end = datetime.combine(day_list[-1] + timedelta(days=1), time.min)

    stmt = (
        select(Reservation)
        .options(selectinload(Reservation.booking_request))
        .where(Reservation.status == ReservationStatus.ACTIVE)
        .where(Reservation.start_at < window_end)
        .where(Reservation.end_at > window_start)
        .order_by(Reservation.start_at)
    )
    if resources:
        stmt = stmt.where(Reservation.resource_id.in_([r.id for r in resources]))
    if source_types:
        stmt = stmt.where(Reservation.source_type.in_(source_types))
    reservations = list(db.scalars(stmt))

    if search:
        term = search.strip().lower()
        reservations = [r for r in reservations if _matches(r, term)]

    by_resource: dict[int, list[Reservation]] = {}
    for reservation in reservations:
        by_resource.setdefault(reservation.resource_id, []).append(reservation)

    rows: list[Row] = []
    for resource in resources:
        owned = by_resource.get(resource.id, [])
        cells: list[Cell] = []
        for day, session in columns:
            bucket_start, bucket_end = _bucket_window(day, session)
            cells.append(
                Cell(
                    day=day,
                    session=session,
                    reservations=[
                        r
                        for r in owned
                        if r.start_at < bucket_end and r.end_at > bucket_start
                    ],
                )
            )
        rows.append(Row(resource=resource, cells=cells))

    return WeekGrid(start=monday, days=day_list, columns=columns, rows=rows)


def _matches(reservation: Reservation, term: str) -> bool:
    """Match a reservation against an applicant name / SID / NetID search."""
    haystack = [reservation.title, reservation.details or ""]
    request = reservation.booking_request
    if request is not None:
        applicant = request.applicant
        haystack += [applicant.name, applicant.sid_netid or "", request.response_id or ""]
    return any(term in value.lower() for value in haystack if value)
