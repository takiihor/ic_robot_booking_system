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
from app.models import (
    Reservation,
    Resource,
    ResourceStatus,
    SourceType,
    distinct_borrower_styles,
)
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

    # Borrowers visible this week, for the color legend (color + name, stable hues).
    # NOTE: global borrower_color_name() hashes into 12 slots and collides
    # (e.g. applicant:14 and applicant:20 are both teal), so resolve the
    # visible set to distinct entries — otherwise different borrowers look
    # identical in the week grid.
    visible_keys: list[str] = []
    for row in grid.rows:
        for cell in row.cells:
            for reservation in cell.reservations:
                if reservation.source_type != SourceType.BOOKING:
                    continue
                key = reservation.borrower_key or f"title:{(reservation.title or '').strip().lower()}"
                visible_keys.append(key)
    styles = distinct_borrower_styles(visible_keys)
    # Attach week-distinct display colors to each reservation so the grid and
    # the legend match. Falls back to the global stable colors.
    for row in grid.rows:
        for cell in row.cells:
            for reservation in cell.reservations:
                if reservation.source_type == SourceType.BOOKING:
                    bkey = reservation.borrower_key or f"title:{(reservation.title or '').strip().lower()}"
                    style = styles.get(bkey)
                    if style:
                        reservation.display_color_var = style["color_var"]  # type: ignore[attr-defined]
                        reservation.display_text_hex = style["text_hex"]  # type: ignore[attr-defined]
                    else:
                        reservation.display_color_var = reservation.color_var  # type: ignore[attr-defined]
                        reservation.display_text_hex = reservation.text_hex  # type: ignore[attr-defined]
                else:
                    reservation.display_color_var = reservation.color_var  # type: ignore[attr-defined]
                    reservation.display_text_hex = reservation.text_hex  # type: ignore[attr-defined]
    seen: dict[str, dict[str, str]] = {}
    for row in grid.rows:
        for cell in row.cells:
            for reservation in cell.reservations:
                if reservation.source_type != SourceType.BOOKING:
                    continue
                key = reservation.borrower_key or f"title:{(reservation.title or '').strip().lower()}"
                if key in seen:
                    continue
                booking = reservation.booking_request
                try:
                    label = booking.applicant.name if booking is not None else reservation.title
                except Exception:
                    label = reservation.title
                style = styles.get(key, {})
                seen[key] = {
                    "key": key,
                    "label": label or reservation.title,
                    "color": style.get("color_var", reservation.color_var),
                    "color_hex": style.get("text_hex", reservation.text_hex),
                    "text_hex": style.get("text_hex", reservation.text_hex),
                }
    borrower_legend = sorted(seen.values(), key=lambda item: item["label"].lower())
    # Per-event display colors matching the legend (distinct within this week).
    borrower_colors = {k: v["color_var"] for k, v in styles.items()}
    borrower_texts = {k: v["text_hex"] for k, v in styles.items()}

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
            "borrower_legend": borrower_legend,
            "borrower_colors": borrower_colors,
            "borrower_texts": borrower_texts,
            "week_anchor": anchor.isoformat(),
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
    # When opened from the week grid, match the week's distinct borrower
    # colors (passed as ?date=YYYY-MM-DD); otherwise fall back to the
    # reservation's global stable colors.
    display_color_var = reservation.color_var
    display_text_hex = reservation.text_hex
    raw_date = request.query_params.get("date", "")
    if raw_date and reservation.source_type == SourceType.BOOKING:
        try:
            anchor = date.fromisoformat(raw_date)
        except ValueError:
            anchor = None
        if anchor is not None:
            week = build_week_grid(db, anchor)
            keys = [
                r.borrower_key or f"title:{(r.title or '').strip().lower()}"
                for row in week.rows
                for cell in row.cells
                for r in cell.reservations
                if r.source_type == SourceType.BOOKING and (r.borrower_key or r.title)
            ]
            week_styles = distinct_borrower_styles(keys)
            key = reservation.borrower_key
            # borrower_key needs booking_request; ensure it is loaded.
            if key is None:
                key = f"title:{(reservation.title or '').strip().lower()}"
            if key in week_styles:
                display_color_var = week_styles[key]["color_var"]
                display_text_hex = week_styles[key]["text_hex"]
    return templates.TemplateResponse(
        request,
        "_reservation_detail.html",
        {
            "reservation": reservation,
            "display_color_var": display_color_var,
            "display_text_hex": display_text_hex,
        },
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

    booking = reservation.booking_request
    try:
        svc.cancel_reservation(db, reservation)
        db.commit()
    except svc.ValidationError as exc:
        db.rollback()
        message, level = "; ".join(exc.errors), "err"
    else:
        message, level = f"{reservation.title} cancelled.", "ok"
        if booking is not None:
            # Releasing one slot of an approved booking no longer forces staff to
            # cancel the whole request and re-enter the remaining days.
            remaining = sum(1 for r in booking.reservations if r.status == "ACTIVE")
            message = (
                f"Slot released from request #{booking.id} — {remaining} still booked."
                if remaining
                else f"Last slot released — request #{booking.id} is now cancelled."
            )
    separator = "&" if "?" in redirect_to else "?"
    return RedirectResponse(
        f"{redirect_to}{separator}flash={quote(message)}&level={level}", status_code=303
    )
