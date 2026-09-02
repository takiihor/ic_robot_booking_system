"""Lesson timetable parser — SPEC 13.2.

Timetable formats vary, so this is a best-effort row extractor. It handles
tab/Markdown/column-aligned text pasted out of Excel or a web timetable, uses
header names when they are available, and falls back to scanning each line for
a date, a time range and a course code. Every parsed row is editable before
import (SPEC 13.2), so partial results are useful rather than fatal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time

from app.services.booking_parser import parse_date

COURSE_PATTERN = re.compile(r"\b([A-Z]{2,4}\s?\d{3,5}[A-Z]?)\b")

_SEP = r"(?:-|–|—|to|until|~|/)"
# `09:30-12:20`, `9.30 am - 12.20 pm`
TIME_RANGE_PATTERN = re.compile(
    rf"(\d{{1,2}})[:.](\d{{2}})\s*(am|pm)?\s*{_SEP}\s*(\d{{1,2}})[:.](\d{{2}})\s*(am|pm)?",
    re.I,
)
# `9am - 12pm`, `2 pm to 4 pm` — bare hours only count with a meridiem.
TIME_RANGE_MERIDIEM = re.compile(
    rf"(\d{{1,2}})()\s*(am|pm)\s*{_SEP}\s*(\d{{1,2}})()\s*(am|pm)",
    re.I,
)
# Some timetable pages render a time cell as two stacked values. The browser
# paste normaliser converts that to `15:30 / 18:30`; plain text copied from
# another source may simply contain a space between the two values.
TIME_PAIR_PATTERN = re.compile(
    r"(\d{1,2})[:.](\d{2})\s*(am|pm)?\s+(\d{1,2})[:.](\d{2})\s*(am|pm)?",
    re.I,
)
SINGLE_TIME_PATTERN = re.compile(
    r"^\s*(\d{1,2})[:.](\d{2})\s*(am|pm)?\s*$",
    re.I,
)
DATE_TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|\b\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{2,4}\b"
    r"|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}\b"
)

_HEADER_WORDS = {
    "date", "course", "subject", "module", "time", "start", "end", "venue",
    "location", "locations", "room",
}
MARKDOWN_TABLE_DIVIDER = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)

# Ordered aliases. For the PolyU table, `Locations` is the per-class venue
# and must win over the earlier, broader `Location` column.
TABLE_FIELD_ALIASES = {
    "course": (
        "module",
        "course module",
        "course",
        "module code",
        "subject code",
        "subject",
    ),
    "date": ("date",),
    "time": ("time", "class time"),
    "start": ("start time", "start"),
    "end": ("end time", "end"),
    "location": ("locations", "venue", "location", "room"),
    "notes": ("remarks", "remark", "notes", "note"),
}


@dataclass
class LessonRow:
    """One parsed timetable line, ready for the editable preview."""

    course: str = ""
    day: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    location: str = ""
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    source_line: str = ""

    @property
    def is_complete(self) -> bool:
        return bool(
            self.course and self.day and self.start_time and self.end_time
            and self.start_time < self.end_time
        )

    def to_form(self) -> dict[str, str]:
        return {
            "course": self.course,
            "date": self.day.isoformat() if self.day else "",
            "start_time": self.start_time.strftime("%H:%M") if self.start_time else "",
            "end_time": self.end_time.strftime("%H:%M") if self.end_time else "",
            "location": self.location,
            "notes": self.notes,
        }


@dataclass
class ParsedTimetable:
    rows: list[LessonRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TableColumns:
    """Recognised timetable column positions for a pasted table."""

    width: int = 0
    course: int | None = None
    day: int | None = None
    time: int | None = None
    start: int | None = None
    end: int | None = None
    location: int | None = None
    notes: int | None = None


def _to_time(hour: str, minute: str | None, meridiem: str | None) -> time | None:
    try:
        h = int(hour)
        m = int(minute) if minute else 0
    except ValueError:
        return None
    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and h < 12:
            h += 12
        elif meridiem == "am" and h == 12:
            h = 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return time(h, m)


def _strip_dates(text: str) -> str:
    """Blank out date tokens so `2026-09-01` is never read as a time range."""
    return DATE_TOKEN.sub(lambda m: " " * len(m.group(0)), text)


def parse_time_range(text: str) -> tuple[time | None, time | None]:
    """Extract a start/end pair, including stacked timetable times."""
    cleaned = _strip_dates(text)
    match = (
        TIME_RANGE_PATTERN.search(cleaned)
        or TIME_RANGE_MERIDIEM.search(cleaned)
        or TIME_PAIR_PATTERN.search(cleaned)
    )
    if not match:
        return None, None
    start_mer, end_mer = match.group(3), match.group(6)
    # `13:30-17:00 pm` — a trailing meridiem applies to both halves.
    start = _to_time(match.group(1), match.group(2), start_mer or end_mer)
    end = _to_time(match.group(4), match.group(5), end_mer)
    if start and end and end <= start and end.hour < 12 and not end_mer:
        end = time(end.hour + 12, end.minute)  # 9:30-1:00 means 13:00
    return start, end


def parse_time_value(text: str) -> time | None:
    """Parse a single time value from a header-mapped Start/End column."""
    match = SINGLE_TIME_PATTERN.match(text or "")
    if not match:
        return None
    return _to_time(match.group(1), match.group(2), match.group(3))


def _has_time_range(text: str) -> bool:
    return parse_time_range(text) != (None, None)


def normalize_timetable(text: str) -> str:
    """Normalise line endings and markdown, but keep tabs and column padding."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    return text.strip("\n")


def _split_columns(line: str, *, keep_empty: bool = False) -> list[str]:
    """Split a conventional pasted table row without losing tab alignment."""
    if "\t" in line:
        parts = line.split("\t")
    elif "|" in line:
        parts = line.strip().strip("|").split("|")
    elif re.search(r" {2,}", line):
        parts = re.split(r" {2,}", line)
    else:
        parts = [line]
    stripped = [p.strip() for p in parts]
    return stripped if keep_empty else [p for p in stripped if p]


def _looks_like_header(line: str) -> bool:
    cells = [c.lower().strip(" :*") for c in _split_columns(line)]
    if not cells:
        return False
    hits = sum(1 for c in cells if c in _HEADER_WORDS)
    return hits >= 2 and not DATE_TOKEN.search(line)


def _normalise_header(cell: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", cell.lower()).strip()


def _header_index(headers: list[str], field: str) -> int | None:
    normalised = [_normalise_header(cell) for cell in headers]
    for alias in TABLE_FIELD_ALIASES[field]:
        for index, header in enumerate(normalised):
            if header == alias:
                return index
    return None


def _table_columns(cells: list[str]) -> TableColumns | None:
    """Return a schema when a pasted row is a recognisable table header."""
    columns = TableColumns(
        width=len(cells),
        course=_header_index(cells, "course"),
        day=_header_index(cells, "date"),
        time=_header_index(cells, "time"),
        start=_header_index(cells, "start"),
        end=_header_index(cells, "end"),
        location=_header_index(cells, "location"),
        notes=_header_index(cells, "notes"),
    )
    recognised = sum(
        value is not None
        for value in (
            columns.course,
            columns.day,
            columns.time,
            columns.start,
            columns.end,
            columns.location,
            columns.notes,
        )
    )
    return columns if recognised >= 2 else None


def _cell(cells: list[str], index: int | None) -> str:
    return cells[index].strip() if index is not None and index < len(cells) else ""


def _parse_header_mapped_row(
    cells: list[str],
    columns: TableColumns,
    raw: str,
    inherited_course: str,
) -> tuple[LessonRow | None, str]:
    """Parse one data row with known columns.

    HTML table rowspans are flattened by the browser before submission. The
    small course carry-over below also handles plain-text copies that retain a
    blank cell where the module was visually merged.
    """
    row = LessonRow(source_line=raw)
    course = _cell(cells, columns.course)
    if course:
        match = COURSE_PATTERN.search(course.upper())
        row.course = match.group(1).replace(" ", "") if match else course
        inherited_course = row.course
    elif inherited_course:
        row.course = inherited_course
        row.warnings.append("Module inherited from preceding table row")
    else:
        row.warnings.append("No course code found")

    date_value = _cell(cells, columns.day)
    row.day = parse_date(date_value) if date_value else None
    if row.day is None:
        row.warnings.append("No date found")

    if columns.start is not None or columns.end is not None:
        row.start_time = parse_time_value(_cell(cells, columns.start))
        row.end_time = parse_time_value(_cell(cells, columns.end))
    else:
        row.start_time, row.end_time = parse_time_range(_cell(cells, columns.time))
    if row.start_time is None or row.end_time is None:
        row.warnings.append("No start/end time found")

    row.location = _cell(cells, columns.location)
    row.notes = _cell(cells, columns.notes)
    if columns.notes is None and len(cells) > columns.width:
        row.notes = " ".join(cell for cell in cells[columns.width:] if cell).strip()

    # A header row can be partial (for example Module + Time + Locations), so
    # preserve it for the editable preview whenever it has either date or time.
    if row.day is None and row.start_time is None:
        return None, inherited_course
    return row, inherited_course


VENUE_PATTERN = re.compile(r"\b(room|rm\.?|lab|laborator\w*|venue|hall|studio|block)\b", re.I)
POLYU_VENUE_CODE = re.compile(r"\b[A-Z]\d{3,4}[A-Z]?(?:-Z\d{1,2})?\b", re.I)


def _looks_like_venue(cell: str) -> bool:
    return bool(VENUE_PATTERN.search(cell) or POLYU_VENUE_CODE.search(cell))


VENUE_PHRASE = re.compile(
    r"\b(?:room|rm\.?|lab|laboratory|venue|hall|studio)\s+[A-Za-z]{0,3}\s?\d{1,4}[A-Za-z]?\b",
    re.I,
)


def _parse_free_text_line(row: LessonRow, raw: str) -> LessonRow | None:
    """Parse a line that has no column structure, e.g. a sentence."""
    date_match = DATE_TOKEN.search(raw)
    row.day = parse_date(date_match.group(0)) if date_match else None
    if row.day is None:
        row.warnings.append("No date found")

    row.start_time, row.end_time = parse_time_range(raw)
    if row.start_time is None or row.end_time is None:
        row.warnings.append("No start/end time found")

    venue_match = VENUE_PHRASE.search(raw)
    row.location = venue_match.group(0) if venue_match else ""

    # Look for the course code outside the date, time and venue fragments.
    remainder = _strip_dates(raw)
    for pattern in (TIME_RANGE_PATTERN, TIME_RANGE_MERIDIEM, TIME_PAIR_PATTERN):
        remainder = pattern.sub(" ", remainder)
    if row.location:
        remainder = remainder.replace(row.location, " ")
    course_match = COURSE_PATTERN.search(remainder.upper())
    row.course = course_match.group(1).replace(" ", "") if course_match else ""
    if not row.course:
        row.warnings.append("No course code found")

    if row.day is None and row.start_time is None:
        return None
    return row


def parse_line(line: str) -> LessonRow | None:
    """Parse one timetable line into a LessonRow, or None if it holds nothing.

    Columns are classified before anything is assigned, so a room code such as
    `Room FG601` is never mistaken for a course code.
    """
    raw = line.strip()
    if not raw or _looks_like_header(raw):
        return None

    row = LessonRow(source_line=raw)
    columns = _split_columns(raw)

    if len(columns) <= 1:
        return _parse_free_text_line(row, raw)

    date_cells, time_cells, venue_cells, other_cells = [], [], [], []
    for cell in columns:
        if DATE_TOKEN.search(cell):
            date_cells.append(cell)
        elif _has_time_range(cell):
            time_cells.append(cell)
        elif _looks_like_venue(cell):
            venue_cells.append(cell)
        else:
            other_cells.append(cell)

    row.day = parse_date(date_cells[0]) if date_cells else None
    if row.day is None:
        row.warnings.append("No date found")

    row.start_time, row.end_time = parse_time_range(time_cells[0] if time_cells else raw)
    if row.start_time is None or row.end_time is None:
        row.warnings.append("No start/end time found")

    # Course code: only from columns that are not a date, time or venue.
    for cell in other_cells:
        match = COURSE_PATTERN.search(cell.upper())
        if match:
            row.course = match.group(1).replace(" ", "")
            other_cells = [c for c in other_cells if c != cell]
            break
    else:
        # No recognisable code — fall back to the first plain column, if any.
        if other_cells:
            row.course = other_cells.pop(0)
    if not row.course:
        row.warnings.append("No course code found")

    row.location = venue_cells[0] if venue_cells else ""
    row.notes = " ".join(venue_cells[1:] + other_cells).strip()

    # A line with neither a date nor a time is not a lesson row.
    if row.day is None and row.start_time is None:
        return None
    return row


def parse_timetable(raw: str) -> ParsedTimetable:
    """Parse pasted timetable text into editable lesson rows."""
    result = ParsedTimetable()
    text = normalize_timetable(raw or "")
    if not text.strip():
        result.warnings.append("Nothing to parse — the pasted text is empty.")
        return result

    skipped = 0
    table_columns: TableColumns | None = None
    inherited_course = ""
    for line in text.split("\n"):
        if not line.strip() or MARKDOWN_TABLE_DIVIDER.match(line):
            continue

        cells = _split_columns(line, keep_empty=True)
        header = _table_columns(cells)
        if header is not None and not DATE_TOKEN.search(line):
            table_columns = header
            inherited_course = ""
            continue

        if table_columns is not None and len(cells) > 1:
            row, inherited_course = _parse_header_mapped_row(
                cells, table_columns, line.strip(), inherited_course
            )
            if row is None:
                skipped += 1
            else:
                result.rows.append(row)
            continue

        if _looks_like_header(line):
            continue
        row = parse_line(line)
        if row is None:
            skipped += 1
        else:
            result.rows.append(row)

    if skipped:
        result.warnings.append(
            f"{skipped} line(s) had no date or time and were skipped — "
            "add them manually if they are lessons."
        )

    if not result.rows:
        result.warnings.append(
            "No lesson rows recognised. Check the pasted text, or add rows manually."
        )
    else:
        incomplete = sum(1 for r in result.rows if not r.is_complete)
        if incomplete:
            result.warnings.append(
                f"{incomplete} of {len(result.rows)} rows are incomplete — "
                "correct them before importing."
            )
    return result


def build_lesson_datetimes(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    return datetime.combine(day, start), datetime.combine(day, end)
