"""Search and applicant history — SPEC 16."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Applicant
from app.routes.common import render
from app.services import booking_service as svc

router = APIRouter()


@router.get("/search", response_class=HTMLResponse)
def search(request: Request, db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    applicants = svc.search_applicants(db, q) if q else []
    requests_found = svc.search_requests(db, q) if q else []
    return render(
        request,
        "search.html",
        {"nav": "requests", "q": q, "applicants": applicants, "requests": requests_found},
    )


@router.get("/applicants/{applicant_id}", response_class=HTMLResponse)
def applicant_detail(applicant_id: int, request: Request, db: Session = Depends(get_db)):
    applicant = db.get(Applicant, applicant_id)
    if applicant is None:
        return RedirectResponse("/search?flash=Applicant+not+found&level=err", status_code=303)

    today = date.today()
    current, upcoming, past = [], [], []
    for booking in sorted(applicant.requests, key=lambda r: r.start_date, reverse=True):
        if booking.end_date < today:
            past.append(booking)
        elif booking.start_date > today:
            upcoming.append(booking)
        else:
            current.append(booking)

    return render(
        request,
        "applicant.html",
        {
            "nav": "requests",
            "applicant": applicant,
            "current": current,
            "upcoming": upcoming,
            "past": past,
        },
    )
