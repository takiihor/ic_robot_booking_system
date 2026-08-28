"""Calendar routes — SPEC 8 and the manual reservation entry point (SPEC 14)."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Reservation, Resource, ResourceStatus, SourceType
from app.routes.common import parse_datetime, render, templates
from app.services import booking_service as svc
from app.services.calendar_view import build_week_grid, week_start

log = logging.getLogger(__name__)
router = APIRouter()


def _filter_query(resource_ids: list[int], source_type: str | None, q: str | None) -> str:
    params: list[tuple[str, str]] = []
    params += [("resource_id", str(rid)) for rid in resource_ids]
    if source_type:
        params.append(("source_type", source_type))
    if q:
        params.append(("q", q))
    return ("&" + urlencode(params)) if params else ""


@router.get("/", response_class=HTMLResponse)
@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, db: Session = Depends(get_db)):
    raw_date = request.query_params.get("date", "")
    try:
        anchor = date.fromisoformat(raw_date) if raw_date else date.today()
    except ValueError:
        anchor = date.today()

    resource_ids = [int(v) for v in request.query_params.getlist("resource_id") if v.isdigit()]
    source_types = [v for v in request.query_params.getlist("source_type") if v in SourceType.ALL]
    q = request.query_params.get("q", "").strip()

    grid = build_week_grid(
        db,
        anchor,
        resource_ids=resource_ids or None,
        source_types=source_types or None,
        search=q or None,
    )
    all_resources = list(
        db.scalars(
            select(Resource)
            .where(Resource.status != ResourceStatus.RETIRED)
            .order_by(Resource.name)
        )
    )

    monday = week_start(anchor)
    default_start = datetime.combine(max(anchor, date.today()), time(8, 30))
    filter_qs = _filter_query(resource_ids, source_types[0] if source_types else None, q)

    return render(
        request,
        "calendar.html",
        {
            "nav": "calendar",
            "grid": grid,
            "all_resources": all_resources,
            "resource_ids": resource_ids,
            "selected_types": source_types,
            "source_types": list(SourceType.LABELS.items()),
            "selected_date": anchor.isoformat(),
            "prev_week": (monday - timedelta(days=7)).isoformat(),
            "next_week": (monday + timedelta(days=7)).isoformat(),
            "today": date.today().isoformat(),
            "today_date": date.today(),
            "filter_qs": filter_qs,
            "current_url": f"/calendar?date={anchor.isoformat()}{filter_qs}",
            "default_block_start": default_start.strftime("%Y-%m-%dT%H:%M"),
            "default_block_end": (default_start + timedelta(hours=3, minutes=30)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "q": q,
        },
    )


@router.get("/reservations/{reservation_id}/detail", response_class=HTMLResponse)
def reservation_detail(reservation_id: int, request: Request, db: Session = Depends(get_db)):
    """HTML fragment for the calendar click-through modal (SPEC 8.4)."""
    reservation = db.get(Reservation, reservation_id)
    if reservation is None:
        return HTMLResponse('<p class="msg msg-err">Reservation not found.</p>', status_code=404)
    return templates.TemplateResponse(
        request, "_reservation_detail.html", {"reservation": reservation}
    )


@router.post("/reservations")
def create_reservation(
    request: Request,
    db: Session = Depends(get_db),
    resource_id: int = Form(...),
    source_type: str = Form(...),
    title: str = Form(...),
    start_at: str = Form(...),
    end_at: str = Form(...),
    details: str = Form(""),
    redirect_to: str = Form("/calendar"),
):
    """Create a maintenance/block/lesson reservation straight from the calendar."""
    try:
        svc.create_manual_reservation(
            db,
            resource_id=resource_id,
            source_type=source_type,
            title=title,
            start_at=parse_datetime(start_at),
            end_at=parse_datetime(end_at),
            details=details,
        )
        db.commit()
        message, level = f"{title} added to the calendar.", "ok"
    except svc.ValidationError as exc:
        db.rollback()
        message, level = "; ".join(exc.errors), "err"

    separator = "&" if "?" in redirect_to else "?"
    return RedirectResponse(
        f"{redirect_to}{separator}flash={quote(message)}&level={level}", status_code=303
    )


@router.post("/reservations/{reservation_id}/cancel")
def cancel_single_reservation(
    reservation_id: int,
    db: Session = Depends(get_db),
    redirect_to: str = Form("/calendar"),
):
    reservation = db.get(Reservation, reservation_id)
    if reservation is None:
        return RedirectResponse("/calendar?flash=Reservation%20not%20found&level=err", 303)
    if reservation.booking_request_id is not None:
        message, level = (
            "This slot belongs to an approved booking — cancel the request instead.",
            "warn",
        )
    else:
        svc.cancel_reservation(db, reservation)
        db.commit()
        message, level = f"{reservation.title} cancelled.", "ok"
    separator = "&" if "?" in redirect_to else "?"
    return RedirectResponse(
        f"{redirect_to}{separator}flash={quote(message)}&level={level}", status_code=303
    )
