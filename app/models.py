"""SQLAlchemy models — SPEC section 15.

Four tables only: applicants, resources, booking_requests, reservations.
`reservations` is the single source of truth for the calendar and for the
conflict engine (bookings, lessons, maintenance and blocks all live here).
"""
from __future__ import annotations

import hashlib
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


# --- borrower highlight colors (Design.md §2.2 curated palette) ---
# Each borrower (applicant) maps deterministically to one palette entry, so the
# same person is always the same color on every page and week. Color is always
# paired with the borrower name — never the only signal.
BORROWER_PALETTE = (
    "sky",
    "cobalt",
    "teal",
    "emerald",
    "amber",
    "coral",
    "rose",
    "fuchsia",
    "lavender",
    "violet",
    "sunflower",
    "mint",
)


def borrower_color_name(key: str) -> str:
    """Stable palette entry for a borrower key (e.g. ``"applicant:3"``)."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return BORROWER_PALETTE[int(digest, 16) % len(BORROWER_PALETTE)]


# Deep, text-safe tone per palette entry: readable borrower name colors on a
# white background WITHOUT relying on color-mix(), so the identity survives
# in every browser (background tints are progressive enhancement on top).
BORROWER_TEXT_HEX = {
    "sky": "#2F6FBD",
    "cobalt": "#2F4BC4",
    "teal": "#0E7C74",
    "emerald": "#1B7A4C",
    "amber": "#96590F",
    "coral": "#BE4133",
    "rose": "#BE2B60",
    "fuchsia": "#963DA0",
    "lavender": "#5C51C6",
    "violet": "#553BC0",
    "sunflower": "#8F6E0E",
    "mint": "#1C8457",
}

# Flat text tones for non-booking event types (mirror of the type tints).
TYPE_TEXT_HEX = {
    SourceType.LESSON: "#553BC0",
    SourceType.MAINTENANCE: "#96590F",
    SourceType.BLOCK: "#4A5158",
}


def borrower_text_hex(key: str) -> str:
    """Stable readable text color for a borrower key."""
    return BORROWER_TEXT_HEX[borrower_color_name(key)]


def distinct_borrower_names(keys) -> dict[str, str]:
    """Assign distinct palette entries to the given borrower keys.

    The global :func:`borrower_color_name` hashes into 12 slots, so two
    different borrowers routinely collide (e.g. ``applicant:14`` and
    ``applicant:20`` are both ``teal``) and appear identical in the UI.
    This resolves the visible set to distinct entries (up to the palette
    size) via hash-preference + linear probing, so borrowers shown together
    always get different text colors. Deterministic for a given key set.
    """
    uniq = sorted(set(keys))
    if not uniq:
        return {}
    prefs: list[tuple[int, str]] = []
    for key in uniq:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        prefs.append((int(digest, 16) % len(BORROWER_PALETTE), key))
    # Serve most-constrained first for stability: sorted by preferred slot.
    prefs.sort()
    resolved: dict[str, str] = {}
    used: set[str] = set()
    for pref, key in prefs:
        idx = pref
        for _ in range(len(BORROWER_PALETTE)):
            name = BORROWER_PALETTE[idx]
            if name not in used:
                resolved[key] = name
                used.add(name)
                break
            idx = (idx + 1) % len(BORROWER_PALETTE)
        else:  # pragma: no cover — palette exhausted (>12 visible borrowers)
            resolved[key] = BORROWER_PALETTE[pref]
    return resolved


def distinct_borrower_styles(keys) -> dict[str, dict[str, str]]:
    """Mapping ``key -> {color_var, text_hex}`` with distinct entries."""
    return {
        key: {
            "color_var": f"var(--{name})",
            "text_hex": BORROWER_TEXT_HEX[name],
            "color_name": name,
        }
        for key, name in distinct_borrower_names(keys).items()
    }


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

    @property
    def color_name(self) -> str:
        """Palette entry identifying this borrower (stable across pages)."""
        if self.id is not None:
            return borrower_color_name(f"applicant:{self.id}")
        return borrower_color_name(f"name:{(self.name or '').strip().lower()}")

    @property
    def color_var(self) -> str:
        return f"var(--{self.color_name})"

    @property
    def text_hex(self) -> str:
        """Readable borrower name color (plain hex, no color-mix needed)."""
        if self.id is not None:
            return borrower_text_hex(f"applicant:{self.id}")
        return borrower_text_hex(f"name:{(self.name or '').strip().lower()}")


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
    auto_imported: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    source_file: Mapped[str | None] = mapped_column(String(200), nullable=True)

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

    @property
    def borrower_key(self) -> str | None:
        """Stable identity key for borrower coloring (BOOKING only)."""
        if self.source_type != SourceType.BOOKING:
            return None
        request = self.booking_request
        if request is not None:
            if request.applicant_id is not None:
                return f"applicant:{request.applicant_id}"
            try:
                name = request.applicant.name
            except Exception:  # detached / missing applicant
                name = ""
            if name:
                return f"name:{name.strip().lower()}"
        return f"title:{(self.title or '').strip().lower()}"

    @property
    def color_var(self) -> str:
        """CSS color for highlighting this event.

        BOOKING events take the borrower's color; other types keep their
        type color so the event kind stays recognizable.
        """
        if self.source_type == SourceType.BOOKING:
            return f"var(--{borrower_color_name(self.borrower_key or 'title:')})"
        if self.source_type == SourceType.LESSON:
            return "var(--c-lesson)"
        if self.source_type == SourceType.MAINTENANCE:
            return "var(--c-maint)"
        return "var(--c-block)"

    @property
    def text_hex(self) -> str:
        """Readable event-title color (plain hex, works without color-mix).

        BOOKING events take the borrower's text tone; other types keep their
        type tone so the event kind stays recognizable.
        """
        if self.source_type == SourceType.BOOKING:
            return borrower_text_hex(self.borrower_key or "title:")
        return TYPE_TEXT_HEX.get(self.source_type, "#4A5158")


Index("ix_reservations_lookup", Reservation.resource_id, Reservation.start_at, Reservation.end_at)
Index("ix_reservations_status", Reservation.status)
Index("ix_requests_status", BookingRequest.status)
