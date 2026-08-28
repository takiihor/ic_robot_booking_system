"""Application configuration.

Everything is environment-overridable so the Docker image can point the
database at the persistent /data volume without code changes.
"""
from __future__ import annotations

import os
from datetime import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Database file. In Docker this is /data/booking.db (a persistent volume).
DB_PATH = Path(os.getenv("BOOKING_DB_PATH", BASE_DIR / "data" / "booking.db"))
DATABASE_URL = os.getenv("BOOKING_DATABASE_URL", f"sqlite:///{DB_PATH}")

SECRET_KEY = os.getenv("BOOKING_SECRET_KEY", "dev-insecure-change-me")

# Standard teaching sessions (SPEC 9.2).
SESSION_TIMES: dict[str, tuple[time, time]] = {
    "AM": (time(8, 30), time(12, 0)),
    "PM": (time(13, 30), time(17, 0)),
}
SESSION_ORDER = ["AM", "PM"]

SESSION_LABELS = {
    "AM": "AM 08:30-12:00",
    "PM": "PM 13:30-17:00",
}
