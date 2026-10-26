"""Shared template setup and small form helpers used by every route module."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import SESSION_LABELS, SESSION_ORDER
from app.models import BookingRequest, RequestStatus, ResourceStatus, SourceType

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals.update(
    SESSION_ORDER=SESSION_ORDER,
    SESSION_LABELS=SESSION_LABELS,
    REQUEST_STATUSES=RequestStatus.ALL,
    RESOURCE_STATUSES=ResourceStatus.ALL,
    SOURCE_TYPE_LABELS=SourceType.LABELS,
    today=date.today,
)


def render(request: Request, name: str, context: dict) -> object:
    """Render a template with the flash message pulled off the query string."""
    context.setdefault("q", request.query_params.get("q", ""))
    context.setdefault("flash", request.query_params.get("flash"))
    context.setdefault("flash_level", request.query_params.get("level", "ok"))
    if "queue_banner" not in context:
        try:
            from app.db import SessionLocal
            _db = SessionLocal()
            try:
                context["queue_banner"] = get_queue_banner(_db)
            finally:
                _db.close()
        except Exception:
            context["queue_banner"] = {"total": 0, "auto_approved": 0,
                                       "needs_review": 0, "items": [], "latest_at": None}
    return templates.TemplateResponse(request, name, context)


def get_queue_banner(db: Session | None) -> dict:
    """Queue auto-imports for the global banner.

    Returns {total, auto_approved, needs_review, items, latest_at} where items
    holds at most 3 {id, applicant, status, start, end} dicts, newest first.

    The banner is dismissal-driven (not time-windowed): it reflects every
    auto-import that has not been dismissed in the browser, so staff who check
    the site later still see what was handled. Hiding happens in the template
    via localStorage (see _queue_banner.html), keyed on latest_at.

    Never raises — a broken banner must not break the page.
    """
    empty = {"total": 0, "auto_approved": 0, "needs_review": 0,
             "items": [], "latest_at": None}
    if db is None:
        return empty
    try:
        rows = list(db.scalars(
            select(BookingRequest)
            .where(BookingRequest.auto_imported == True)  # noqa: E712
            .order_by(BookingRequest.created_at.desc())
            .limit(10)
        ))
    except Exception:
        return empty
    approved = [r for r in rows if r.status == RequestStatus.APPROVED]
    items = [
        {"id": r.id,
         "applicant": r.applicant.name if r.applicant else "Unknown",
         "status": r.status,
         "start": r.start_date.isoformat(),
         "end": r.end_date.isoformat()}
        for r in rows[:3]
    ]
    return {"total": len(rows), "auto_approved": len(approved),
            "needs_review": len(rows) - len(approved), "items": items,
            "latest_at": rows[0].created_at.isoformat() if rows else None}


def form_str(form, key: str) -> str | None:
    value = form.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def form_date(form, key: str) -> date | None:
    value = form_str(form, key)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an <input type=datetime-local> value."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_time(value: str | None) -> time | None:
    """Parse an <input type=time> value."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def form_datetime(form, key: str) -> datetime | None:
    return parse_datetime(form_str(form, key))


def form_int(form, key: str) -> int | None:
    value = form_str(form, key)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def form_sessions(form) -> list[str]:
    """Read the AM/PM checkboxes, preserving canonical order."""
    chosen = {str(v) for v in form.getlist("sessions")}
    for session in SESSION_ORDER:
        if form.get(f"session_{session.lower()}"):
            chosen.add(session)
    return [s for s in SESSION_ORDER if s in chosen]
