"""Lesson timetable routes — SPEC 13."""
from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Resource, ResourceStatus
from app.routes.common import parse_time, render
from app.services import booking_service as svc
from app.services.lesson_parser import parse_timetable

log = logging.getLogger(__name__)
router = APIRouter(prefix="/lessons")


def _active_resources(db: Session) -> list[Resource]:
    return list(
        db.scalars(
            select(Resource)
            .where(Resource.status == ResourceStatus.ACTIVE)
            .order_by(Resource.name)
        )
    )


def _rows_from_form(form) -> tuple[list[dict], list[str]]:
    """Rebuild the editable lesson rows from the preview form.

    Returns (importable rows, per-row problems). Rows the user blanked out or
    left incomplete are reported rather than silently dropped (SPEC 18).
    """
    courses = form.getlist("course")
    dates = form.getlist("date")
    starts = form.getlist("start_time")
    ends = form.getlist("end_time")
    locations = form.getlist("location")
    notes = form.getlist("notes")
    keep = {str(v) for v in form.getlist("keep")}

    rows: list[dict] = []
    problems: list[str] = []
    for index, course in enumerate(courses):
        if str(index) not in keep:
            continue
        course = (course or "").strip()
        raw_date = (dates[index] if index < len(dates) else "").strip()
        start = parse_time(starts[index] if index < len(starts) else "")
        end = parse_time(ends[index] if index < len(ends) else "")

        label = course or f"row {index + 1}"
        if not course:
            problems.append(f"Row {index + 1}: course/module is required.")
            continue
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            problems.append(f"{label}: a valid date is required.")
            continue
        if start is None or end is None:
            problems.append(f"{label}: start and end time are required.")
            continue
        if start >= end:
            problems.append(f"{label}: the start time must be before the end time.")
            continue

        rows.append(
            {
                "course": course,
                "start_at": datetime.combine(day, start),
                "end_at": datetime.combine(day, end),
                "location": (locations[index] if index < len(locations) else "").strip(),
                "notes": (notes[index] if index < len(notes) else "").strip(),
            }
        )
    return rows, problems


def _preview_rows(rows: list[dict]) -> list[dict]:
    """Turn importable rows back into the form shape for re-rendering."""
    return [
        {
            "course": r["course"],
            "date": r["start_at"].date().isoformat(),
            "start_time": r["start_at"].strftime("%H:%M"),
            "end_time": r["end_at"].strftime("%H:%M"),
            "location": r["location"],
            "notes": r["notes"],
            "warnings": [],
        }
        for r in rows
    ]


@router.get("", response_class=HTMLResponse)
def lessons_page(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "lessons.html",
        {
            "nav": "lessons",
            "resources": _active_resources(db),
            "rows": [],
            "parsed": False,
            "warnings": [],
            "raw_text": "",
            "selected_ids": [],
            "conflicts": [],
        },
    )


@router.post("/parse", response_class=HTMLResponse)
async def parse_lessons(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    raw_text = str(form.get("raw_text") or "")
    parsed = parse_timetable(raw_text)
    log.info("Parsed timetable: %d rows, %d warnings", len(parsed.rows), len(parsed.warnings))

    rows = []
    for row in parsed.rows:
        item = row.to_form()
        item["warnings"] = row.warnings
        rows.append(item)

    return render(
        request,
        "lessons.html",
        {
            "nav": "lessons",
            "resources": _active_resources(db),
            "rows": rows,
            "parsed": True,
            "warnings": parsed.warnings,
            "raw_text": raw_text,
            "selected_ids": [],
            "conflicts": [],
        },
    )


@router.post("/import", response_class=HTMLResponse)
async def import_lessons(request: Request, db: Session = Depends(get_db)):
    """Check conflicts first; only import once staff has confirmed them."""
    form = await request.form()
    resource_ids = [int(v) for v in form.getlist("resource_ids") if str(v).isdigit()]
    confirmed = bool(form.get("confirm_conflicts"))
    rows, problems = _rows_from_form(form)

    def rerender(extra: dict) -> HTMLResponse:
        context = {
            "nav": "lessons",
            "resources": _active_resources(db),
            "rows": _preview_rows(rows),
            "parsed": True,
            "warnings": [],
            "raw_text": str(form.get("raw_text") or ""),
            "selected_ids": resource_ids,
            "conflicts": [],
        }
        context.update(extra)
        return render(request, "lessons.html", context)

    if problems or not rows or not resource_ids:
        errors = list(problems)
        if not rows:
            errors.append("Select at least one complete lesson row to import.")
        if not resource_ids:
            errors.append("Select at least one affected robot.")
        return rerender({"errors": errors})

    conflicts = svc.check_lesson_conflicts(db, rows, resource_ids)
    if conflicts and not confirmed:
        # SPEC 13.4 — show the clash and require an explicit confirmation.
        return rerender({"conflicts": conflicts})

    try:
        created = svc.import_lessons(db, rows, resource_ids)
        db.commit()
    except svc.ValidationError as exc:
        db.rollback()
        return rerender({"errors": exc.errors})

    first_day = min(r["start_at"].date() for r in rows).isoformat()
    message = f"Imported+{len(created)}+lesson+reservations"
    return RedirectResponse(f"/calendar?date={first_day}&flash={message}", status_code=303)
