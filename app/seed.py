"""Create the initial robot resources on a fresh database.

Run once after first install:

    python -m app.seed

It is safe to re-run: existing resources are left untouched.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Resource, ResourceStatus

log = logging.getLogger(__name__)

DEFAULT_RESOURCES = [
    {
        "name": f"UR10e ({index:02d})",
        "model": "UR10e",
        "resource_group": "Collaborative Robots",
        "location": "Industrial Robot Laboratory",
        "status": ResourceStatus.ACTIVE,
    }
    for index in range(1, 6)
]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    init_db()
    with SessionLocal() as db:
        added = 0
        for spec in DEFAULT_RESOURCES:
            if db.scalar(select(Resource).where(Resource.name == spec["name"])) is None:
                db.add(Resource(**spec))
                added += 1
        db.commit()
    log.info("Seeding complete: %d resource(s) added, %d already present",
             added, len(DEFAULT_RESOURCES) - added)


if __name__ == "__main__":
    main()
