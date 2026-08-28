"""Test fixtures: an isolated in-memory database per test."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest

TEST_DB = Path(tempfile.mkdtemp()) / "test.db"
os.environ["BOOKING_DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["BOOKING_DB_PATH"] = str(TEST_DB)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import (  # noqa: E402
    Applicant,
    Base,
    BookingRequest,
    Reservation,
    ReservationStatus,
    RequestStatus,
    Resource,
    ResourceStatus,
    SourceType,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def robots(db):
    """Five UR10e robots; (04) is out of service."""
    made = []
    for index in range(1, 6):
        made.append(
            Resource(
                name=f"UR10e ({index:02d})",
                model="UR10e",
                resource_group="Collaborative Robots",
                location="Industrial Robot Lab",
                status=(
                    ResourceStatus.OUT_OF_SERVICE if index == 4 else ResourceStatus.ACTIVE
                ),
            )
        )
    db.add_all(made)
    db.commit()
    return made


@pytest.fixture
def applicant(db):
    person = Applicant(
        name="DING Changwen",
        sid_netid="25104512r",
        department="Mechanical Engineering",
        email="changwen.ding@connect.polyu.hk",
        phone="51234567",
    )
    db.add(person)
    db.commit()
    return person


@pytest.fixture
def make_request(db, applicant):
    def _make(start="2026-08-31", end="2026-09-04", sessions=("AM", "PM"), **kwargs):
        booking = BookingRequest(
            applicant_id=applicant.id,
            start_date=date.fromisoformat(start),
            end_date=date.fromisoformat(end),
            status=RequestStatus.PENDING,
            **kwargs,
        )
        booking.sessions = list(sessions)
        db.add(booking)
        db.commit()
        return booking

    return _make


@pytest.fixture
def make_reservation(db):
    def _make(
        resource,
        start: str,
        end: str,
        *,
        source_type=SourceType.LESSON,
        title="ME3101",
        status=ReservationStatus.ACTIVE,
    ):
        reservation = Reservation(
            resource_id=resource.id,
            source_type=source_type,
            title=title,
            start_at=datetime.fromisoformat(start),
            end_at=datetime.fromisoformat(end),
            status=status,
        )
        db.add(reservation)
        db.commit()
        return reservation

    return _make


@pytest.fixture
def client():
    """TestClient backed by a fresh file-based database."""
    from fastapi.testclient import TestClient

    from app.db import SessionLocal, engine
    from app.main import app

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as test_client:
        test_client.db_factory = SessionLocal
        yield test_client
    Base.metadata.drop_all(engine)


SAMPLE_EMAIL = """
**Department:** Department of Mechanical Engineering
**Facilities to Use:** Industrial Robot Laboratory
**Response ID:** R-2026-0142
**Booking Type:** Research

**Date:** 2026-08-31
**End Date:** 2026-09-04

**Session:**
AM 08:30-12:00
PM 13:30-17:00

**Name:** DING Changwen
**SID/NetID:** 25104512r
**Tel:** 5123 4567
**Email:** changwen.ding@connect.polyu.hk

**Booking for:** Robotic grasping experiments for MSc project

**Remarks:**
Prefer UR10e (03) if possible.
Need the force/torque sensor mounted before the first session.
"""
