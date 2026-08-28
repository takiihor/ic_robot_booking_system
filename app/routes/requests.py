"""Booking request routes — SPEC 9, 11, 12."""
from __future__ import annotations

import logging
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import (
    Applicant,
    BookingRequest,
    RequestStatus,
    Resource,
    ResourceStatus,
)
from app.routes.common import (
    form_date,
    form_int,
    form_sessions,
    form_str,
    render,
)
from app.services import availability as av
from app.services import booking_service as svc
from app.services.booking_parser import parse_booking_email

log = logging.getLogger(__name__)
router = APIRouter(prefix="/requests")


def _active_resources(db: Session) -> list[Resource]:
    return list(
        db.scalars(
            select(Resource)
            .where(Resource.status == ResourceStatus.ACTIVE)
            .order_by(Resource.name)
        )
    )


def _resource_by_name(db: Session, name: str | None) -> Resource | None:
    """Match a parsed robot label such as `UR10e (03)`, ignoring spacing."""
    if not name:
        return None
    cleaned = name.strip()
    resource = db.scalar(select(Resource).where(Resource.name == cleaned))
    if resource is not None:
        return resource
    squashed = cleaned.replace(" ", "").lower()
    for candidate in db.scalars(select(Resource)):
        if candidate.name.replace(" ", "").lower() == squashed:
            return candidate
    return None


def _empty_form() -> dict:
    return {
        "response_id": "", "name": "", "sid_netid": "", "department": "", "email": "",
        "phone": "", "facility": "", "booking_type": "", "start_date": "", "end_date": "",
        "sessions": [], "preferred_robot": "", "purpose": "", "remarks": "", "raw_text": "",
    }


def _form_payload(form) -> dict:
    return {
        "response_id": form_str(form, "response_id") or "",
        "name": form_str(form, "name") or "",
        "sid_netid": form_str(form, "sid_netid") or "",
        "department": form_str(form, "department") or "",
        "email": form_str(form, "email") or "",
        "phone": form_str(form, "phone") or "",
        "facility": form_str(form, "facility") or "",
        "booking_type": form_str(form, "booking_type") or "",
        "start_date": form_str(form, "start_date") or "",
        "end_date": form_str(form, "end_date") or "",
        "sessions": form_sessions(form),
        "preferred_robot": form_str(form, "preferred_resource_id") or "",
        "purpose": form_str(form, "purpose") or "",
        "remarks": form_str(form, "remarks") or "",
        "raw_text": form.get("raw_text") or "",
    }


# --------------------------------------------------------------------------
# List — SPEC 12
# --------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def list_requests(request: Request, db: Session = Depends(get_db)):
    statuses = [s for s in request.query_params.getlist("status") if s in RequestStatus.ALL]
    q = request.query_params.get("q", "").strip()
    date_from = request.query_params.get("from", "").strip()
    date_to = request.query_params.get("to", "").strip()

    stmt = (
        select(BookingRequest)
        .join(Applicant)
        .options(
            selectinload(BookingRequest.applicant),
            selectinload(BookingRequest.assigned_resource),
            selectinload(BookingRequest.preferred_resource),
        )
    )
    if statuses:
        stmt = stmt.where(BookingRequest.status.in_(statuses))
    if q:
        like = f"%{q}%"
        conditions = [
            Applicant.name.ilike(like),
            Applicant.sid_netid.ilike(like),
            BookingRequest.response_id.ilike(like),
        ]
        if q.isdigit():
            conditions.append(BookingRequest.id == int(q))
        stmt = stmt.where(or_(*conditions))
    try:
        if date_from:
            stmt = stmt.where(BookingRequest.end_date >= date.fromisoformat(date_from))
        if date_to:
            stmt = stmt.where(BookingRequest.start_date <= date.fromisoformat(date_to))
    except ValueError:
        pass

    rows = list(db.scalars(stmt.order_by(BookingRequest.updated_at.desc())))
    return render(
        request,
        "requests_list.html",
        {
            "nav": "requests",
            "requests": rows,
            "statuses": statuses,
            "q": q,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


# --------------------------------------------------------------------------
# New request: paste -> parse -> preview — SPEC 9
# --------------------------------------------------------------------------

@router.get("/new", response_class=HTMLResponse)
def new_request(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "request_new.html",
        {
            "nav": "new",
            "resources": _active_resources(db),
            "form": _empty_form(),
            "parsed": False,
            "warnings": [],
            "flagged": set(),
        },
    )


@router.post("/parse", response_class=HTMLResponse)
async def parse_request(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    raw_text = str(form.get("raw_text") or "")
    parsed = parse_booking_email(raw_text)

    payload = parsed.to_form()
    resource = _resource_by_name(db, parsed.preferred_robot)
    payload["preferred_robot"] = str(resource.id) if resource else ""
    if parsed.preferred_robot and resource is None:
        parsed.warnings.append(
            f"Preferred robot {parsed.preferred_robot!r} is not a configured resource — "
            "choose one from the list."
        )

    flagged = {key for key, value in payload.items() if key != "raw_text" and not value}
    if not payload["sessions"]:
        flagged.add("sessions")
    log.info("Parsed booking email (%d warnings)", len(parsed.warnings))

    return render(
        request,
        "request_new.html",
        {
            "nav": "new",
            "resources": _active_resources(db),
            "form": payload,
            "parsed": True,
            "warnings": parsed.warnings,
            "flagged": flagged,
        },
    )


@router.post("", response_class=HTMLResponse)
async def create_request(request: Request, db: Session = Depends(get_db)):
    """Save the reviewed preview as a pending request, optionally then checking."""
    form = await request.form()
    payload = _form_payload(form)
    action = form_str(form, "action") or "save"

    try:
        booking = svc.save_request(
            db,
            name=payload["name"],
            sid_netid=payload["sid_netid"],
            department=payload["department"],
            email=payload["email"],
            phone=payload["phone"],
            response_id=payload["response_id"],
            facility=payload["facility"],
            booking_type=payload["booking_type"],
            start_date=form_date(form, "start_date"),
            end_date=form_date(form, "end_date"),
            sessions=payload["sessions"],
            preferred_resource_id=form_int(form, "preferred_resource_id"),
            purpose=payload["purpose"],
            remarks=payload["remarks"],
            raw_text=payload["raw_text"],
        )
        db.commit()
    except svc.ValidationError as exc:
        db.rollback()
        return render(
            request,
            "request_new.html",
            {
                "nav": "new",
                "resources": _active_resources(db),
                "form": payload,
                "parsed": True,
                "warnings": [],
                "errors": exc.errors,
                "flagged": set(),
            },
        )

    log.info("Booking request %s saved as pending", booking.id)
    suffix = "?flash=Request+saved+as+pending"
    if action == "check":
        suffix = "?check=1&flash=Request+saved+—+availability+checked+below"
    return RedirectResponse(f"/requests/{booking.id}{suffix}", status_code=303)


# --------------------------------------------------------------------------
# Detail, availability and decisions — SPEC 10, 11
# --------------------------------------------------------------------------

def _load(db: Session, request_id: int) -> BookingRequest | None:
    return db.get(BookingRequest, request_id)


def _detail_context(
    db: Session, booking: BookingRequest, report: av.AvailabilityReport | None
) -> dict:
    return {
        "nav": "requests",
        "req": booking,
        "resources": _active_resources(db),
        "report": report,
        "AVAILABLE": av.AVAILABLE,
        "OUT_OF_SERVICE": av.OUT_OF_SERVICE,
    }


@router.get("/{request_id}", response_class=HTMLResponse)
def request_detail(request_id: int, request: Request, db: Session = Depends(get_db)):
    booking = _load(db, request_id)
    if booking is None:
        return RedirectResponse("/requests?flash=Request+not+found&level=err", status_code=303)

    report = None
    if request.query_params.get("check") and booking.status == RequestStatus.PENDING:
        report = av.check_request(db, booking)
    return render(request, "request_detail.html", _detail_context(db, booking, report))


@router.post("/{request_id}/check", response_class=HTMLResponse)
async def check_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    booking = _load(db, request_id)
    if booking is None:
        return RedirectResponse("/requests?flash=Request+not+found&level=err", status_code=303)

    form = await request.form()
    resource_id = form_int(form, "resource_id")
    resource = db.get(Resource, resource_id) if resource_id else None
    report = av.check_request(db, booking, resource)

    context = _detail_context(db, booking, report)
    context["checked_resource_id"] = resource.id if resource else None
    if report.preferred is None:
        context["flash"] = "Choose a robot to check availability against."
        context["flash_level"] = "warn"
    return render(request, "request_detail.html", context)


@router.post("/{request_id}/update", response_class=HTMLResponse)
async def update_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    booking = _load(db, request_id)
    if booking is None:
        return RedirectResponse("/requests?flash=Request+not+found&level=err", status_code=303)
    if booking.status != RequestStatus.PENDING:
        return RedirectResponse(
            f"/requests/{request_id}?flash=Only+pending+requests+can+be+edited&level=warn",
            status_code=303,
        )

    form = await request.form()
    payload = _form_payload(form)
    try:
        svc.save_request(
            db,
            request=booking,
            name=payload["name"],
            sid_netid=payload["sid_netid"],
            department=payload["department"],
            email=payload["email"],
            phone=payload["phone"],
            response_id=payload["response_id"],
            facility=payload["facility"],
            booking_type=payload["booking_type"],
            start_date=form_date(form, "start_date"),
            end_date=form_date(form, "end_date"),
            sessions=payload["sessions"],
            preferred_resource_id=form_int(form, "preferred_resource_id"),
            assigned_resource_id=form_int(form, "assigned_resource_id"),
            purpose=payload["purpose"],
            remarks=payload["remarks"],
        )
        db.commit()
    except svc.ValidationError as exc:
        db.rollback()
        context = _detail_context(db, booking, None)
        context["errors"] = exc.errors
        return render(request, "request_detail.html", context)

    return RedirectResponse(f"/requests/{request_id}?flash=Request+updated", status_code=303)


@router.post("/{request_id}/approve", response_class=HTMLResponse)
async def approve(request_id: int, request: Request, db: Session = Depends(get_db)):
    booking = _load(db, request_id)
    if booking is None:
        return RedirectResponse("/requests?flash=Request+not+found&level=err", status_code=303)

    form = await request.form()
    resource_id = form_int(form, "resource_id") or booking.preferred_resource_id
    try:
        reservations = svc.approve_request(db, booking, resource_id)
        db.commit()
    except svc.ValidationError as exc:
        db.rollback()
        report = av.check_request(db, booking, db.get(Resource, resource_id) if resource_id else None)
        context = _detail_context(db, booking, report)
        context["errors"] = exc.errors
        return render(request, "request_detail.html", context)

    message = quote(
        f"Approved — {len(reservations)} slots booked on {booking.assigned_resource.name}."
    )
    return RedirectResponse(f"/requests/{request_id}?flash={message}", status_code=303)


@router.post("/{request_id}/reject", response_class=HTMLResponse)
async def reject(request_id: int, request: Request, db: Session = Depends(get_db)):
    booking = _load(db, request_id)
    if booking is None:
        return RedirectResponse("/requests?flash=Request+not+found&level=err", status_code=303)

    form = await request.form()
    try:
        svc.reject_request(db, booking, form_str(form, "rejection_reason"))
        db.commit()
    except svc.ValidationError as exc:
        db.rollback()
        context = _detail_context(db, booking, None)
        context["errors"] = exc.errors
        return render(request, "request_detail.html", context)
    return RedirectResponse(f"/requests/{request_id}?flash=Request+rejected", status_code=303)


@router.post("/{request_id}/cancel", response_class=HTMLResponse)
async def cancel(request_id: int, request: Request, db: Session = Depends(get_db)):
    booking = _load(db, request_id)
    if booking is None:
        return RedirectResponse("/requests?flash=Request+not+found&level=err", status_code=303)

    form = await request.form()
    try:
        svc.cancel_request(db, booking, form_str(form, "cancel_reason"))
        db.commit()
    except svc.ValidationError as exc:
        db.rollback()
        context = _detail_context(db, booking, None)
        context["errors"] = exc.errors
        return render(request, "request_detail.html", context)
    return RedirectResponse(
        f"/requests/{request_id}?flash=Booking+cancelled+—+history+kept", status_code=303
    )
