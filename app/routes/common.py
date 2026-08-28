"""Shared template setup and small form helpers used by every route module."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import SESSION_LABELS, SESSION_ORDER
from app.models import RequestStatus, ResourceStatus, SourceType

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
    return templates.TemplateResponse(request, name, context)


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
