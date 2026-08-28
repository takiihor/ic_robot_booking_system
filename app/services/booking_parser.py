"""Deterministic booking-email parser — SPEC 9.2.

No LLM involved. The email is label/value structured, so we normalise the
text, find known labels case-insensitively, and capture each value until the
next recognised label. Anything we cannot read is left blank with a warning
so staff can correct it in the preview (SPEC 18: never silent data loss).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

# Canonical field name -> accepted label spellings (lower-cased, punctuation-free).
LABEL_ALIASES: dict[str, list[str]] = {
    "department": ["department", "dept"],
    "facility": ["facilities to use", "facility to use", "facilities", "facility"],
    "response_id": ["response id", "responseid", "response no", "reference id"],
    "booking_type": ["booking type", "type of booking"],
    "start_date": ["date", "start date", "booking date", "from date"],
    "end_date": ["end date", "to date", "until"],
    "session": ["session", "sessions", "time slot", "time"],
    "name": ["name", "applicant name", "applicant"],
    "sid_netid": ["sid netid", "sid net id", "sid", "netid", "net id", "student id"],
    "phone": ["tel", "telephone", "phone", "contact no", "contact number", "mobile"],
    "email": ["email", "e mail", "email address"],
    "purpose": ["booking for", "purpose", "purpose of booking", "reason"],
    "remarks": ["remarks", "remark", "notes", "additional information", "comments"],
}

# Longest labels first so "end date" wins over "date".
_FLAT_LABELS: list[tuple[str, str]] = sorted(
    ((alias, canonical) for canonical, aliases in LABEL_ALIASES.items() for alias in aliases),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

ROBOT_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9\-]{1,15})\s*\(\s*(\d{1,3})\s*\)")

_IMPORTANT_FIELDS = {
    "name": "Applicant name",
    "start_date": "Start date",
    "session": "Session",
}


@dataclass
class ParsedRequest:
    """Result of parsing one booking email."""

    response_id: str | None = None
    name: str | None = None
    sid_netid: str | None = None
    department: str | None = None
    email: str | None = None
    phone: str | None = None
    facility: str | None = None
    booking_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    sessions: list[str] = field(default_factory=list)
    preferred_robot: str | None = None
    purpose: str | None = None
    remarks: str | None = None
    raw_text: str = ""
    warnings: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)

    def to_form(self) -> dict[str, object]:
        """Shape used by the editable preview template."""
        return {
            "response_id": self.response_id or "",
            "name": self.name or "",
            "sid_netid": self.sid_netid or "",
            "department": self.department or "",
            "email": self.email or "",
            "phone": self.phone or "",
            "facility": self.facility or "",
            "booking_type": self.booking_type or "",
            "start_date": self.start_date.isoformat() if self.start_date else "",
            "end_date": self.end_date.isoformat() if self.end_date else "",
            "sessions": list(self.sessions),
            "preferred_robot": self.preferred_robot or "",
            "purpose": self.purpose or "",
            "remarks": self.remarks or "",
            "raw_text": self.raw_text,
        }


def normalize(text: str) -> str:
    """Normalise line endings and strip light email/Markdown formatting."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("​", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)  # **bold**
    text = re.sub(r"^\s*[*_]{1,2}\s*", "", text, flags=re.M)  # leading bullets/emphasis
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _label_key(fragment: str) -> str:
    """Reduce a candidate label to a comparable key."""
    fragment = fragment.strip().strip("*_-–—#>").strip()
    fragment = re.sub(r"[^a-z0-9 ]+", " ", fragment.lower())
    return re.sub(r"\s+", " ", fragment).strip()


def _match_label(line: str) -> tuple[str, str] | None:
    """Return (canonical_field, inline_value) when `line` starts a known label."""
    stripped = line.strip()
    if not stripped:
        return None

    head, sep, tail = stripped.partition(":")
    if sep and len(head) <= 40:
        key = _label_key(head)
        for alias, canonical in _FLAT_LABELS:
            if key == alias:
                return canonical, tail.strip()

    # Label alone on its own line (value follows on the next lines).
    key = _label_key(stripped)
    for alias, canonical in _FLAT_LABELS:
        if key == alias:
            return canonical, ""
    return None


def _collect_fields(text: str) -> dict[str, str]:
    """Walk the lines once, accumulating multiline values under each label."""
    fields: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        value = "\n".join(buffer).strip()
        # First occurrence wins: quoted reply chains repeat labels further down.
        if value and not fields.get(current):
            fields[current] = value

    for line in text.split("\n"):
        matched = _match_label(line)
        if matched:
            flush()
            current, inline = matched
            buffer = [inline] if inline else []
        elif current is not None:
            buffer.append(line)
    flush()
    return fields


_DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %y",
]


def parse_date(value: str | None) -> date | None:
    """Parse the first date-looking token in `value`; ISO is tried first."""
    if not value:
        return None
    text = value.strip()
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    candidates = [text.split("\n")[0].strip()]
    numeric = re.search(r"\b\d{1,4}[/.\-]\d{1,2}[/.\-]\d{2,4}\b", text)
    if numeric:
        candidates.append(numeric.group(0))
    worded = re.search(
        r"\b\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{2,4}\b|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}\b", text
    )
    if worded:
        candidates.append(worded.group(0))

    for candidate in candidates:
        cleaned = candidate.replace(",", " ").strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
    return None


def parse_sessions(value: str | None) -> list[str]:
    """Detect AM/PM sessions from free-form session text."""
    if not value:
        return []
    text = value.lower()
    found: list[str] = []
    if re.search(r"\bam\b|morning|08\s*[:.]\s*30|8\s*[:.]\s*30", text):
        found.append("AM")
    if re.search(r"\bpm\b|afternoon|13\s*[:.]\s*30|1\s*[:.]\s*30", text):
        found.append("PM")
    if re.search(r"whole day|full day|all day|whole-day|full-day", text):
        found = ["AM", "PM"]
    return found


def find_preferred_robot(*texts: str | None) -> str | None:
    """Find a `UR10e (03)` / `UR10e(03)` style resource reference."""
    for text in texts:
        if not text:
            continue
        match = ROBOT_PATTERN.search(text)
        if match:
            return f"{match.group(1)} ({int(match.group(2)):02d})"
    return None


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("-–—").strip()
    if not cleaned or cleaned.lower() in {"n/a", "na", "nil", "none", "-"}:
        return None
    return cleaned


def _extract_email(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        if match:
            return match.group(0).rstrip(".")
    return None


def parse_booking_email(raw: str) -> ParsedRequest:
    """Parse a pasted booking application email into structured fields."""
    result = ParsedRequest(raw_text=raw or "")
    text = normalize(raw or "")
    if not text:
        result.warnings.append("Nothing to parse — the pasted text is empty.")
        return result

    fields = _collect_fields(text)
    result.fields = fields

    result.response_id = _clean_value(fields.get("response_id"))
    result.name = _clean_value(fields.get("name"))
    result.department = _clean_value(fields.get("department"))
    result.phone = _clean_value(fields.get("phone"))
    result.facility = _clean_value(fields.get("facility"))
    result.booking_type = _clean_value(fields.get("booking_type"))
    result.purpose = _clean_value(fields.get("purpose"))
    result.remarks = _clean_value(fields.get("remarks"))

    sid = _clean_value(fields.get("sid_netid"))
    if sid:
        sid = sid.split("\n")[0].strip()
    result.sid_netid = sid

    email_field = _clean_value(fields.get("email"))
    result.email = _extract_email(email_field, text)

    # Dates
    start_raw = fields.get("start_date")
    end_raw = fields.get("end_date")
    result.start_date = parse_date(start_raw)
    if start_raw and result.start_date is None:
        result.warnings.append(f"Could not read the start date from {start_raw.strip()!r}.")
    result.end_date = parse_date(end_raw)
    if end_raw and result.end_date is None:
        result.warnings.append(f"Could not read the end date from {end_raw.strip()!r}.")
    if result.end_date is None and result.start_date is not None:
        result.end_date = result.start_date
    if (
        result.start_date
        and result.end_date
        and result.end_date < result.start_date
    ):
        result.warnings.append("End date is before the start date — please correct it.")

    # Sessions
    result.sessions = parse_sessions(fields.get("session"))

    # Preferred robot: remarks first, then purpose/facility, then whole email.
    result.preferred_robot = find_preferred_robot(
        result.remarks, result.purpose, result.facility, text
    )

    for key, label in _IMPORTANT_FIELDS.items():
        if key == "session":
            if not result.sessions:
                result.warnings.append("No AM/PM session detected — please choose one.")
        elif key == "start_date":
            if result.start_date is None:
                result.warnings.append("Start date is missing — please enter it.")
        elif not getattr(result, key):
            result.warnings.append(f"{label} is missing — please enter it.")

    if not result.sid_netid:
        result.warnings.append("SID/NetID not found — applicant history may not link up.")
    if not result.preferred_robot:
        result.warnings.append("No preferred robot detected — please choose one.")

    return result
