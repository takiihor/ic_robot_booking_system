"""Automated queue processor for incoming booking JSON files.

Polls a configurable directory for booking-*.json files, runs each through the
existing parser and availability engine, and records results in a durable
processed-file registry.  Does NOT create bookings, send notifications, or
modify production data.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.booking_parser import parse_booking_email
from app.services import availability as av
from app.models import Resource, ResourceStatus
from sqlalchemy import select
from sqlalchemy.orm import Session

# Lazy import — tests can override this before calling process_file.
_session_factory = None


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        from app.db import SessionLocal
        _session_factory = SessionLocal
    return _session_factory


def set_session_factory(factory) -> None:
    """Override the session factory (for testing)."""
    global _session_factory
    _session_factory = factory

log = logging.getLogger(__name__)

# Regex: booking-<id>.json  (id = digits, letters, hyphens)
_FILE_RE = re.compile(r"^booking-(.+)\.json$", re.IGNORECASE)

# Registry filename stored alongside processed files
_REGISTRY = "processed.json"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def queue_dir() -> Path | None:
    """Return the configured queue directory, or None if disabled."""
    raw = _env("BOOKING_QUEUE_DIR")
    if not raw:
        return None
    return Path(raw)


def poll_interval() -> float:
    """Seconds between polls."""
    try:
        return max(1.0, float(_env("BOOKING_QUEUE_POLL_SECONDS", "10")))
    except ValueError:
        return 10.0


def _facility_map() -> dict[str, list[str]]:
    """Load facility-to-resource-group mapping from BOOKING_FACILITY_MAP.

    Expected JSON format: {"Autonomous Robot Platform": ["Collaborative Robots"]}
    Keys are facility names as they appear in booking emails.
    Values are lists of resource group names that satisfy that facility.
    """
    raw = _env("BOOKING_FACILITY_MAP")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        log.warning("Invalid BOOKING_FACILITY_MAP JSON — ignoring")
    return {}


# ---------------------------------------------------------------------------
# Facility resolution
# ---------------------------------------------------------------------------

def _normalise_facility(raw: str | None) -> str | None:
    """Strip JSON array syntax and whitespace from a facility value.

    The ICT form outputs facilities as JSON arrays, e.g.:
        ["Autonomous Robot Platform"]
    We normalise this to a plain string for matching.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip leading/trailing JSON array brackets and quotes
    if text.startswith("["):
        text = text[1:]
    if text.endswith("]"):
        text = text[:-1]
    text = text.strip().strip('"').strip("'")
    text = text.strip()
    return text or None


def resolve_facility(
    db: Session, facility: str | None
) -> tuple[Resource | None, list[Resource]]:
    """Resolve a parsed facility string to a preferred resource and candidates.

    Resolution order:
    1. Exact match on resource name (e.g. "UR10e (03)")
    2. Facility-map lookup: facility -> resource groups -> active resources
    3. If nothing matches, return (None, [])

    Returns (preferred, candidates) where:
    - preferred: the single best-matching resource, or None
    - candidates: all eligible active resources for availability checking
    """
    normalised = _normalise_facility(facility)
    if not normalised:
        return None, []

    # 1. Exact name match
    resource = db.scalar(
        select(Resource).where(
            Resource.name == normalised,
            Resource.status == ResourceStatus.ACTIVE,
        )
    )
    if resource is not None:
        return resource, [resource]

    # 2. Case-insensitive contains match on resource name
    all_active = list(db.scalars(
        select(Resource)
        .where(Resource.status == ResourceStatus.ACTIVE)
        .order_by(Resource.name)
    ))
    norm_lower = normalised.lower()
    for r in all_active:
        if norm_lower in r.name.lower():
            return r, [r]

    # 3. Facility-map: facility -> resource groups
    facility_map = _facility_map()
    target_groups: list[str] = []
    for map_key, groups in facility_map.items():
        if map_key.lower() == norm_lower or norm_lower in map_key.lower():
            target_groups.extend(groups)
    if target_groups:
        candidates = [
            r for r in all_active
            if r.resource_group in target_groups
        ]
        if candidates:
            return None, candidates

    # 4. No match at all
    return None, []


# ---------------------------------------------------------------------------
# Processed-file registry
# ---------------------------------------------------------------------------

class _Registry:
    """Tracks which files have been processed.

    Stored as a JSON dict mapping file paths to their SHA-256 hashes.  When a
    file is re-written with identical content the hash matches and it is
    skipped.  When content changes (e.g. corrected re-export) the hash differs
    and the file is re-processed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                log.warning("Corrupt registry %s — starting fresh", self._path)
                self._data = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def already_processed(self, file_path: Path, content_hash: str) -> bool:
        return self._data.get(str(file_path)) == content_hash

    def mark(self, file_path: Path, content_hash: str) -> None:
        self._data[str(file_path)] = content_hash


# ---------------------------------------------------------------------------
# Result data class
# ---------------------------------------------------------------------------

@dataclass
class CandidateResult:
    """Availability result for one candidate resource."""
    resource: str = ""
    group: str = ""
    available: bool = False
    conflicts: list[str] = field(default_factory=list)


@dataclass
class ProcessResult:
    """Structured result for one processed booking JSON."""
    file: str = ""
    booking_id: str = ""
    response_id: str | None = None
    facility: str | None = None
    normalised_facility: str | None = None
    date: str | None = None
    start: str | None = None
    end: str | None = None
    availability: str = "UNKNOWN"
    preferred_resource: str | None = None
    candidates: list[CandidateResult] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    status: str = "PENDING"
    error: str | None = None
    processed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract_booking_id(filename: str) -> str:
    m = _FILE_RE.match(filename)
    return m.group(1) if m else filename


def process_file(file_path: Path) -> ProcessResult:
    """Process a single booking JSON file through parser + availability engine.

    Returns a ProcessResult.  Never raises — all failures are captured in the
    result.
    """
    result = ProcessResult(file=str(file_path), processed_at=datetime.now().isoformat())
    result.booking_id = _extract_booking_id(file_path.name)

    try:
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        result.status = "ERROR"
        result.error = f"Cannot read file: {exc}"
        return result

    # --- Parse outer JSON ---
    try:
        outer = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.status = "ERROR"
        result.error = f"Malformed JSON: {exc}"
        return result

    email_body = outer.get("EmailBody", "")
    if not email_body:
        result.status = "ERROR"
        result.error = "Missing EmailBody field"
        return result

    # --- Run existing parser ---
    try:
        parsed = parse_booking_email(email_body)
    except Exception as exc:
        result.status = "ERROR"
        result.error = f"Parser failure: {exc}"
        return result

    result.response_id = parsed.response_id
    result.facility = parsed.facility
    result.normalised_facility = _normalise_facility(parsed.facility)
    result.date = parsed.start_date.isoformat() if parsed.start_date else None

    # Derive start/end from sessions
    from app.config import SESSION_TIMES as _ST
    if parsed.sessions:
        starts = [_ST[s][0] for s in parsed.sessions if s in _ST]
        ends = [_ST[s][1] for s in parsed.sessions if s in _ST]
        if starts:
            earliest = min(starts)
            result.start = f"{earliest.hour:02d}:{earliest.minute:02d}"
        if ends:
            latest = max(ends)
            result.end = f"{latest.hour:02d}:{latest.minute:02d}"

    if parsed.start_date is None or not parsed.sessions:
        result.status = "PARSE_INCOMPLETE"
        result.error = "Missing date or sessions after parsing"
        return result

    # --- Run availability engine ---
    end_date = parsed.end_date or parsed.start_date
    slots = av.expand_slots(parsed.start_date, end_date, parsed.sessions)
    if not slots:
        result.status = "PARSE_INCOMPLETE"
        result.error = "No bookable slots after expansion"
        return result

    db = _get_session_factory()()
    try:
        preferred, candidates = resolve_facility(db, parsed.facility)

        if preferred is not None:
            # Exact match — check just that resource
            report = av.build_report(db, slots, preferred, include_alternatives=False)
            result.preferred_resource = preferred.name
            if report.preferred is not None:
                result.availability = report.preferred.status
                for sr in report.preferred.slots:
                    if not sr.available:
                        for c in sr.conflicts:
                            result.conflicts.append(
                                f'{c.short_label} "{c.title}" '
                                f'{c.start_at.strftime("%H:%M")}-{c.end_at.strftime("%H:%M")}'
                            )
            else:
                result.availability = "UNAVAILABLE"
        elif candidates:
            # Multiple candidates — check each
            result.availability = "NEEDS_RESOURCE_SELECTION"
            for cand in candidates:
                report = av.build_report(db, slots, cand, include_alternatives=False)
                cr = CandidateResult(
                    resource=cand.name,
                    group=cand.resource_group,
                    available=report.status == av.AVAILABLE,
                )
                if report.preferred is not None:
                    for sr in report.preferred.slots:
                        if not sr.available:
                            for c in sr.conflicts:
                                cr.conflicts.append(
                                    f'{c.short_label} "{c.title}" '
                                    f'{c.start_at.strftime("%H:%M")}-{c.end_at.strftime("%H:%M")}'
                                )
                result.candidates.append(cr)
        else:
            result.availability = "NO_RESOURCE"
    except Exception as exc:
        result.status = "ERROR"
        result.error = f"Availability engine failure: {exc}"
        return result
    finally:
        db.close()

    result.status = "PROCESSED"
    return result


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def scan_queue(queue: Path, registry: _Registry) -> list[ProcessResult]:
    """Scan the queue directory and process any new files.

    Returns results for all files attempted (including already-processed skips,
    which have status='SKIPPED').
    """
    results: list[ProcessResult] = []

    if not queue.exists():
        log.warning("Queue directory does not exist: %s", queue)
        return results

    for entry in sorted(queue.iterdir()):
        if not entry.is_file():
            continue
        if not _FILE_RE.match(entry.name):
            continue

        try:
            content = entry.read_bytes()
        except OSError as exc:
            log.error("Cannot read %s: %s", entry, exc)
            continue

        content_hash = _sha256(content)

        if registry.already_processed(entry, content_hash):
            log.debug("Skipping already-processed %s", entry.name)
            continue

        log.info("Processing %s", entry.name)
        result = process_file(entry)
        registry.mark(entry, content_hash)
        results.append(result)

        log.info(
            "  %s — %s: %s (availability=%s, conflicts=%d)",
            result.booking_id,
            result.status,
            result.response_id or "no-response-id",
            result.availability,
            len(result.conflicts),
        )

    registry.save()
    return results


def run_once(queue: Path | None = None) -> list[ProcessResult]:
    """Single scan pass — useful for CLI and tests."""
    q = queue or queue_dir()
    if q is None:
        log.info("Queue processor disabled (BOOKING_QUEUE_DIR not set)")
        return []
    registry = _Registry(q / _REGISTRY)
    return scan_queue(q, registry)


def run_loop() -> None:
    """Continuous polling loop — call from a background thread or script."""
    q = queue_dir()
    if q is None:
        log.error("BOOKING_QUEUE_DIR not set — cannot start queue processor")
        return
    log.info("Queue processor started: polling %s every %.0fs", q, poll_interval())
    registry = _Registry(q / _REGISTRY)
    while True:
        try:
            scan_queue(q, registry)
        except Exception:
            log.exception("Unhandled error in queue scan")
        time.sleep(poll_interval())


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    # Allow one-shot mode: python -m app.services.queue_processor --once
    if "--once" in sys.argv:
        results = run_once()
        for r in results:
            print(json.dumps(r.to_dict(), indent=2))
        if not results:
            print("No new files to process.")
    else:
        run_loop()
