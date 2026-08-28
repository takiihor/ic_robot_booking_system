"""Resource management routes — SPEC 14."""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Reservation, ReservationStatus, Resource, ResourceStatus
from app.routes.common import form_str, render

log = logging.getLogger(__name__)
router = APIRouter(prefix="/resources")


@router.get("", response_class=HTMLResponse)
def resources_page(request: Request, db: Session = Depends(get_db)):
    resources = list(db.scalars(select(Resource).order_by(Resource.name)))
    counts = dict(
        db.execute(
            select(Reservation.resource_id, func.count(Reservation.id))
            .where(Reservation.status == ReservationStatus.ACTIVE)
            .group_by(Reservation.resource_id)
        ).all()
    )
    return render(
        request,
        "resources.html",
        {"nav": "resources", "resources": resources, "counts": counts},
    )


def _apply(form, resource: Resource) -> list[str]:
    """Copy validated form values onto a resource; returns any errors."""
    errors: list[str] = []
    name = form_str(form, "name")
    if not name:
        errors.append("Display name is required.")
    status = form_str(form, "status") or ResourceStatus.ACTIVE
    if status not in ResourceStatus.ALL:
        errors.append(f"Unknown status {status!r}.")
    if errors:
        return errors

    resource.name = name
    resource.model = form_str(form, "model")
    resource.resource_group = form_str(form, "resource_group") or "General"
    resource.location = form_str(form, "location")
    resource.status = status
    resource.remarks = form_str(form, "remarks")
    return []


@router.post("", response_class=HTMLResponse)
async def create_resource(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    resource = Resource(name="", resource_group="General", status=ResourceStatus.ACTIVE)
    errors = _apply(form, resource)
    if not errors:
        existing = db.scalar(select(Resource).where(Resource.name == resource.name))
        if existing is not None:
            errors.append(f"A resource named {resource.name} already exists.")
    if errors:
        db.rollback()
        return RedirectResponse(
            f"/resources?flash={quote('; '.join(errors))}&level=err", status_code=303
        )

    db.add(resource)
    db.commit()
    log.info("Resource %s created", resource.name)
    return RedirectResponse(f"/resources?flash={quote(resource.name + ' added')}", status_code=303)


@router.post("/{resource_id}/update", response_class=HTMLResponse)
async def update_resource(resource_id: int, request: Request, db: Session = Depends(get_db)):
    resource = db.get(Resource, resource_id)
    if resource is None:
        return RedirectResponse("/resources?flash=Resource+not+found&level=err", status_code=303)

    form = await request.form()
    errors = _apply(form, resource)
    if not errors:
        clash = db.scalar(select(Resource).where(Resource.name == resource.name))
        if clash is not None and clash.id != resource.id:
            errors.append(f"A resource named {resource.name} already exists.")
    if errors:
        db.rollback()
        return RedirectResponse(
            f"/resources?flash={quote('; '.join(errors))}&level=err", status_code=303
        )

    db.commit()
    log.info("Resource %s updated (status=%s)", resource.name, resource.status)
    return RedirectResponse(
        f"/resources?flash={quote(resource.name + ' updated')}", status_code=303
    )
