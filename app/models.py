"""SQLAlchemy models — SPEC section 15.

Four tables only: applicants, resources, booking_requests, reservations.
`reservations` is the single source of truth for the calendar and for the
conflict engine (bookings, lessons, maintenance and blocks all live here).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# --- enum-ish constants (kept as plain strings for SQLite friendliness) ---

class ResourceStatus:
    ACTIVE = "Active"
    OUT_OF_SERVICE = "Out of Service"
    RETIRED = "Retired"
    ALL = [ACTIVE, OUT_OF_SERVICE, RETIRED]


class RequestStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    ALL = [PENDING, APPROVED, REJECTED, CANCELLED]


class SourceType:
    BOOKING = "BOOKING"
    LESSON = "LESSON"
    MAINTENANCE = "MAINTENANCE"
    BLOCK = "BLOCK"
    ALL = [BOOKING, LESSON, MAINTENANCE, BLOCK]

    # Long labels for filters and detail panels.
    LABELS = {
        BOOKING: "Research / Booking",
        LESSON: "Lesson",
        MAINTENANCE: "Maintenance",
        BLOCK: "Out of service / Block",
    }
    # Short labels for calendar blocks and conflict lines (SPEC 8.3).
    SHORT_LABELS = {
        BOOKING: "Research",
        LESSON: "Lesson",
        MAINTENANCE: "Maintenance",
        BLOCK: "Out of Service",
    }


class ReservationStatus:
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


class Applicant(TimestampMixin, Base):
    __tablename__ = "applicants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sid_netid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    department: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(64))

    requests: Mapped[list["BookingRequest"]] = relationship(
        back_populates="applicant", order_by="BookingRequest.start_date"
    )


class Resource(TimestampMixin, Base):
    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    resource_group: Mapped[str] = mapped_column(String(120), nullable=False, default="General")
    location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ResourceStatus.ACTIVE)
    remarks: Mapped[str | None] = mapped_column(Text)

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="resource")

    @property
    def is_active(self) -> bool:
        return self.status == ResourceStatus.ACTIVE


class BookingRequest(TimestampMixin, Base):
    __tablename__ = "booking_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Not unique: a cancelled or rejected request keeps its Response ID, so the
    # same application can be re-entered later (uniqueness among live requests
    # is enforced in booking_service.save_request).
    response_id: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicants.id"), nullable=False)

    facility: Mapped[str | None] = mapped_column(String(200))
    booking_type: Mapped[str | None] = mapped_column(String(120))

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    sessions_json: Mapped[str] = mapped_column(Text, nullable=False, default='["AM"]')

    preferred_resource_id: Mapped[int | None] = mapped_column(ForeignKey("resources.id"))
    assigned_resource_id: Mapped[int | None] = mapped_column(ForeignKey("resources.id"))

    purpose: Mapped[str | None] = mapped_column(Text)
    remarks: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RequestStatus.PENDING)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)

    applicant: Mapped[Applicant] = relationship(back_populates="requests")
    preferred_resource: Mapped[Resource | None] = relationship(
        foreign_keys=[preferred_resource_id]
    )
    assigned_resource: Mapped[Resource | None] = relationship(foreign_keys=[assigned_resource_id])
    reservations: Mapped[list["Reservation"]] = relationship(
        back_populates="booking_request", cascade="all, delete-orphan"
    )

    @property
    def sessions(self) -> list[str]:
        try:
            value = json.loads(self.sessions_json or "[]")
        except json.JSONDecodeError:
            return []
        return [s for s in value if isinstance(s, str)]

    @sessions.setter
    def sessions(self, value: list[str]) -> None:
        self.sessions_json = json.dumps(list(value))

    @property
    def clashing_reservations(self) -> list["Reservation"]:
        """Active slots approved over something else — the days to chase."""
        return sorted(
            (
                r
                for r in self.reservations
                if r.status == ReservationStatus.ACTIVE and r.conflict_note
            ),
            key=lambda r: r.start_at,
        )


class Reservation(TimestampMixin, Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id"), nullable=False)

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    booking_request_id: Mapped[int | None] = mapped_column(ForeignKey("booking_requests.id"))

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ReservationStatus.ACTIVE
    )
    details: Mapped[str | None] = mapped_column(Text)
    # Set when staff knowingly approved this slot over an existing reservation
    # (SPEC 11.8). Records what it shares the session with, so the calendar can
    # flag it and staff know which days need the robot handed back early.
    conflict_note: Mapped[str | None] = mapped_column(Text)

    resource: Mapped[Resource] = relationship(back_populates="reservations")
    booking_request: Mapped[BookingRequest | None] = relationship(back_populates="reservations")

    @property
    def type_label(self) -> str:
        return SourceType.LABELS.get(self.source_type, self.source_type)

    @property
    def short_label(self) -> str:
        return SourceType.SHORT_LABELS.get(self.source_type, self.source_type)

    @property
    def has_clash(self) -> bool:
        return bool(self.conflict_note)


Index("ix_reservations_lookup", Reservation.resource_id, Reservation.start_at, Reservation.end_at)
Index("ix_reservations_status", Reservation.status)
Index("ix_requests_status", BookingRequest.status)
