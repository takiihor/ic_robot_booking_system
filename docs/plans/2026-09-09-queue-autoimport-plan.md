# Queue Auto-Import + Banner Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Incoming `booking-*.json` files are auto-imported (approve-if-free) and a dismissable banner on all pages shows what was handled.

**Architecture:** Queue worker gains a DB-writing `auto_import()` step reusing `booking_service.save_request` / `approve_request`; `booking_requests` gains `auto_imported` + `source_file`; shared `render()` in `routes/common.py` injects recent auto-imports into `base.html` via `_queue_banner.html` partial with localStorage dismiss.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy 2.x + SQLite, pytest, vanilla JS localStorage.

---

### Task 1: DB columns + migration 3

**Files:**
- Modify: `app/models.py:117-143`
- Modify: `app/migrations.py:17,99-102`
- Test: `tests/test_queue_banner.py`

**Step 1: Write the failing test**

```python
def test_booking_request_has_auto_import_columns(db):
    from app.models import BookingRequest
    cols = {c.name for c in BookingRequest.__table__.columns}
    assert "auto_imported" in cols
    assert "source_file" in cols
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_queue_banner.py::test_booking_request_has_auto_import_columns -v`
Expected: FAIL with `assert "auto_imported" in cols`

**Step 3: Write minimal implementation**

In `app/models.py` inside `BookingRequest` (after `decided_at`):

```python
auto_imported: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
source_file: Mapped[str | None] = mapped_column(String(200), nullable=True)
```

Note: use Integer 0/1 for SQLite-friendliness (project keeps enums as plain strings). In `app/migrations.py`:

```python
SCHEMA_VERSION = 3
def _add_auto_import_columns(cursor):
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(booking_requests)").fetchall()}
    if not cols or "auto_imported" in cols:
        return
    cursor.execute("ALTER TABLE booking_requests ADD COLUMN auto_imported INTEGER NOT NULL DEFAULT 0")
    cursor.execute("ALTER TABLE booking_requests ADD COLUMN source_file VARCHAR(200)")
    log.info("Migration 3: added booking_requests.auto_imported/source_file")
MIGRATIONS = {1: _drop_response_id_unique, 2: _add_reservation_conflict_note, 3: _add_auto_import_columns}
```

Check `docs/plans/2026-09-09-queue-autoimport-design.md` for rationale. YAGNI: no extra index in v1.

**Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_queue_banner.py::test_booking_request_has_auto_import_columns -v`
Expected: PASS. Also run `.venv/bin/pytest tests/ -x -q` — must stay green.

**Step 5: Commit**

```bash
git add app/models.py app/migrations.py tests/test_queue_banner.py
git commit -m "feat: track auto-imported requests with source file"
```

---

### Task 2: Queue worker auto-import (approve-if-free)

**Files:**
- Modify: `app/services/queue_processor.py:266-382`
- Test: `tests/test_queue_processor.py`, `tests/test_queue_banner.py`

**Step 1: Write the failing test**

```python
def test_auto_import_approves_when_free(tmp_path, db, robots):
    from app.services import queue_processor as qp
    from tests.test_queue_processor import GOOD_OUTER_JSON, _write_booking_json
    p = _write_booking_json(tmp_path, "booking-100.json", GOOD_OUTER_JSON)
    result = qp.process_file(p)  # existing behaviour unchanged
    assert result.status == "PROCESSED"
    req = qp.auto_import(p, result)  # NEW
    assert req is not None
    assert req.status == "APPROVED"
    assert req.auto_imported is True
    assert len(req.reservations) > 0
```

Also add conflict case: pre-book the slot via `booking_service`, then second import with different Response ID must yield `PENDING` + 0 reservations.

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_queue_banner.py -v -k auto_import`
Expected: FAIL with `AttributeError: auto_import` / `function not defined`.

**Step 3: Write minimal implementation**

In `app/services/queue_processor.py` add (after `process_file`, before polling loop):

```python
def auto_import(file_path: Path, result: ProcessResult):
    """Create PENDING request, auto-approve iff AVAILABLE on single resource. Never raises."""
    from app.services import booking_service as svc
    from app.services.booking_parser import parse_booking_email
    if result.status != "PROCESSED" or result.availability in ("NO_RESOURCE",):
        return None
    try:
        outer = json.loads(file_path.read_bytes())
        parsed = parse_booking_email(outer.get("EmailBody", ""))
    except Exception as exc:
        log.warning("auto-import re-parse failed for %s: %s", file_path, exc)
        return None
    db = _get_session_factory()()
    try:
        if parsed.response_id:
            existing = db.scalar(select(BookingRequest).where(
                BookingRequest.response_id == parsed.response_id,
                BookingRequest.status.not_in(("CANCELLED", "REJECTED"))))
            if existing is not None:
                return existing  # idempotent, never double-book
        if parsed.start_date is None or not parsed.sessions:
            return None
        preferred, candidates = resolve_facility(db, parsed.facility)
        single = preferred or (candidates[0] if len(candidates) == 1 else None)
        # create PENDING first
        applicant = svc.upsert_applicant(db, name=parsed.applicant_name or "Unknown",
            sid_netid=parsed.sid_netid, department=parsed.department,
            email=parsed.email, phone=parsed.phone)
        req = BookingRequest(applicant_id=applicant.id, start_date=parsed.start_date,
            end_date=parsed.end_date or parsed.start_date, status="APPROVED" if False else "PENDING")
        req.sessions = parsed.sessions
        db.add(req)
        svc._apply_fields(...)  # reuse save path: use svc.save_request when creating new
        # Simpler: call svc.save_request(db, name=..., ...) then:
        # if single and availability AVAILABLE -> svc.approve_request(db, req, single.id)
        # set req.auto_imported=True; req.source_file=file_path.name; db.commit(); return req
    except svc.ValidationError:
        db.rollback()
        # leave as PENDING for staff (query it back and return)
    except Exception:
        db.rollback(); log.exception("auto-import failed for %s", file_path); return None
    finally:
        db.close()
```

Keep it minimal: reuse `svc.save_request` for creation then `svc.approve_request` only when `result.availability == "AVAILABLE"` and `single is not None`. Never `allow_conflicts=True`. Then hook into `scan_queue()` right after `result = process_file(entry)`: call `auto_import(entry, result)` inside try/except so loop never crashes. See `app/services/booking_service.py:152-217,273-319` for exact signatures.

**Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_queue_processor.py tests/test_queue_banner.py -q`
Expected: PASS, no regressions.

**Step 5: Commit**

```bash
git add app/services/queue_processor.py tests/test_queue_banner.py tests/test_queue_processor.py
git commit -m "feat: auto-import queue files, approve-if-free"
```

---

### Task 3: Banner context helper

**Files:**
- Modify: `app/routes/common.py:25-30`
- Test: `tests/test_queue_banner.py`

**Step 1: Write the failing test**

```python
def test_get_queue_banner_empty(db):
    from app.routes.common import get_queue_banner
    assert get_queue_banner(db)["total"] == 0

def test_get_queue_banner_counts_recent(db, robots, make_request=None):
    # create 1 APPROVED auto + 1 PENDING auto in last 24h, assert total==2, auto_approved==1, needs_review==1
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_queue_banner.py::test_get_queue_banner_empty -v`
Expected: FAIL with `ImportError / has no attribute get_queue_banner`.

**Step 3: Write minimal implementation**

In `app/routes/common.py`:

```python
from datetime import datetime, timedelta
from sqlalchemy import select
from app.models import BookingRequest
def get_queue_banner(db, hours: int = 24) -> dict:
    """Recent auto-imports for the global banner. Returns {total, auto_approved, needs_review, items}."""
    if db is None:
        return {"total": 0, "auto_approved": 0, "needs_review": 0, "items": [], "latest_at": None}
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        rows = list(db.scalars(select(BookingRequest).where(
            BookingRequest.auto_imported == True,  # noqa: E712
            BookingRequest.created_at >= cutoff).order_by(BookingRequest.created_at.desc()).limit(10)))
    except Exception:
        return {"total": 0, "auto_approved": 0, "needs_review": 0, "items": [], "latest_at": None}
    approved = [r for r in rows if r.status == "APPROVED"]
    items = [{"id": r.id, "applicant": r.applicant.name if r.applicant else "Unknown",
              "status": r.status, "start": r.start_date.isoformat(), "end": r.end_date.isoformat()} for r in rows[:3]]
    return {"total": len(rows), "auto_approved": len(approved),
            "needs_review": len(rows) - len(approved), "items": items,
            "latest_at": rows[0].created_at.isoformat() if rows else None}
```

And extend `render()`:

```python
def render(request, name, context):
    ...
    try:
        from app.db import SessionLocal
        _db = SessionLocal()
        try:
            context.setdefault("queue_banner", get_queue_banner(_db))
        finally:
            _db.close()
    except Exception:
        context.setdefault("queue_banner", {"total": 0, "items": []})
    return templates.TemplateResponse(request, name, context)
```

DRY: single injection point covers Calendar/Requests/Lessons/Resources/Search (all use `render()`).

**Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_queue_banner.py -q`
Expected: PASS.

**Step 5: Commit**

```bash
git add app/routes/common.py tests/test_queue_banner.py
git commit -m "feat: queue banner context helper"
```

---

### Task 4: Banner template + base.html + dismiss

**Files:**
- Create: `app/templates/_queue_banner.html`
- Modify: `app/templates/base.html:27-30`
- Test: `tests/test_queue_banner.py` (client test)

**Step 1: Write the failing test**

```python
def test_calendar_shows_banner(client, db):
    # seed one auto-imported APPROVED request, GET /calendar, assert b"incoming" in html
def test_no_banner_when_empty(client):
    # fresh DB, GET /calendar, assert b"queue-banner" not in html
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_queue_banner.py -v -k banner_html`
Expected: FAIL (no banner in HTML).

**Step 3: Write minimal implementation**

`app/templates/_queue_banner.html`:

```html
{% if queue_banner and queue_banner.total %}
<div class="msg msg-ok" id="queue-banner" data-latest="{{ queue_banner.latest_at or '' }}">
  <strong>{{ queue_banner.total }} incoming msg{{ 's' if queue_banner.total != 1 }} handled</strong>
  — {{ queue_banner.auto_approved }} auto-added to calendar,
  {{ queue_banner.needs_review }} need{{ 's' if queue_banner.needs_review == 1 else '' }} review.
  <a href="/requests">View Requests</a>
  <button class="btn btn-sm" type="button" onclick="dismissQueueBanner()">Dismiss</button>
</div>
<script>
function dismissQueueBanner() {
  try { localStorage.setItem('queue_banner_dismissed_at', document.getElementById('queue-banner').dataset.latest || new Date().toISOString()); } catch (e) {}
  document.getElementById('queue-banner').remove();
}
(function () {
  try {
    var el = document.getElementById('queue-banner');
    if (!el) return;
    var dismissed = localStorage.getItem('queue_banner_dismissed_at');
    if (dismissed && el.dataset.latest && el.dataset.latest <= dismissed) el.remove();
  } catch (e) {}
})();
</script>
{% endif %}
```

In `app/templates/base.html` inside `<main>` before `{% if flash %}`:

```html
{% include "_queue_banner.html" %}
```

Reuse existing `.msg-ok` styles in `app/static/app.css` — no new CSS (YAGNI).

**Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_queue_banner.py -q`
Expected: PASS. Run full suite `.venv/bin/pytest -q`.

**Step 5: Commit**

```bash
git add app/templates/_queue_banner.html app/templates/base.html tests/test_queue_banner.py
git commit -m "feat: global queue banner with dismiss"
```

---

### Task 5: Full verification + docs touch-up

**Files:**
- Modify: `README.md:184-206` (2 lines on auto-import behaviour)

**Step 1: Run full suite**

Run: `.venv/bin/pytest -q`
Expected: all PASS.

**Step 2: Manual smoke test**

Run:
```bash
echo '{"EmailBody": "Department: Test\nFacilities to Use: UR10e (01)\nResponse ID: SMOKE-1\nDate: 2026-10-20\nSession: PM\nName: Smoke Test\nSID/NetID: 00000000x\n"}' > /tmp/booking-smoke.json
BOOKING_QUEUE_DIR=/tmp .venv/bin/python -m app.services.queue_processor --once
.venv/bin/python -c "from app.db import SessionLocal; from app.models import BookingRequest; db=SessionLocal(); print([(r.id, r.status, r.auto_imported) for r in db.query(BookingRequest).all()])"
```

Expected: new APPROVED auto-imported request; second `--once` run creates nothing (idempotent).

**Step 3: Commit docs**

```bash
git add README.md
git commit -m "docs: queue auto-import behaviour"
```

