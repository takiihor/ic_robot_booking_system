# Queue Auto-Import + Web UI Notification — Design

Date: 2026-09-09
Status: Approved
Approach: A — DB write + banner (no live polling)

## 1. Goal

When a `booking-*.json` arrives in `BOOKING_QUEUE_DIR` (OneDrive `IC-Booking-Queue`):

1. `queue_processor.py` detects it on next poll and parses it.
2. System auto-adds it to the calendar when safe.
3. Web UI shows a banner on all pages: "incoming msg handled".

## 2. Decisions (user-confirmed)

- Auto-add behavior: **auto-approve if free**. No conflict → `APPROVED` + reservations. Conflict / ambiguous / incomplete → `PENDING` for staff review.
- Notification: **banner on all pages** via `base.html`, with link to Requests.
- Freshness: **dismissable + remembered** via `localStorage` timestamp. Banner returns only when a newer auto-import exists.
- No live WebSocket/polling in v1. Banner refreshes on page load.

## 3. Architecture & data flow

```
booking-*.json
  -> queue_processor.scan_queue() [poll every BOOKING_QUEUE_POLL_SECONDS]
  -> parse_booking_email + resolve_facility + availability check
  -> auto_import(): save_request(PENDING) -> if AVAILABLE + single resource: approve_request()
  -> reservations (calendar source of truth)
  -> banner query (recent auto_imported requests) -> base.html
```

Worker stays a separate process (`start-queue.sh` / `robot-booking-queue.service`). Web UI only reads DB for the banner; no direct file access from web routes (keeps Docker volume separation clean).

## 4. Components

### 4.1 DB migration (`app/models.py`, `app/migrations.py`)

Add to `booking_requests`:

- `auto_imported BOOLEAN NOT NULL DEFAULT 0`
- `source_file TEXT NULL` (e.g. `booking-123.json`)

Idempotency: skip import if `source_file` already imported with same content hash (registry `processed.json` remains first guard), plus existing `response_id` uniqueness among live requests.

### 4.2 Worker (`app/services/queue_processor.py`)

New `auto_import(db, parsed, process_result) -> BookingRequest | None`:

- Uses `booking_service.upsert_applicant`, `save_request`, `approve_request`.
- Never passes `allow_conflicts=True`. Out-of-Service/Retired can never auto-approve.
- `NEEDS_RESOURCE_SELECTION` (facility maps to many robots, no exact match) → stays `PENDING`.
- Logs `auto-approved` vs `pending-review` with request id.

### 4.3 Banner UI (`app/templates/base.html`, `_queue_banner.html`, `app/routes/common.py`)

- `get_queue_banner(db)`: counts auto-imported requests with `created_at > now-24h`, returns latest 3 (id, applicant, dates, status).
- Partial shows e.g.: "2 incoming msgs handled — 1 auto-added to calendar, 1 needs review [View Requests] [Dismiss]".
- Hidden when no recent imports or when `BOOKING_QUEUE_DIR` disabled.
- Dismiss: `localStorage.setItem('queue_banner_dismissed_at', latest_created_at)`. Banner shows only if `latest_created_at > dismissed_at`.

## 5. Error handling

- Malformed JSON / missing `EmailBody` / parse-incomplete → no DB write, no banner; error in `queue.log` only.
- Duplicate `response_id` → stays `PENDING`, banner counts as "needs review".
- Conflict / ambiguous facility / OOS → `PENDING`, no reservations written.
- Partial OneDrive sync / locked file → `ERROR` result, retried next poll (hash differs / not marked processed).
- Empty `BOOKING_QUEUE_DIR` → worker exits 0, banner hidden.

## 6. Testing

- Extend `tests/test_queue_processor.py`: free → `APPROVED` + N reservations; conflict → `PENDING` + 0 reservations; same-hash rescan → no duplicate; duplicate `response_id` → `PENDING`.
- Route test: banner present on `/calendar` + `/requests` with recent auto-imports; absent otherwise.
- Manual: drop `booking-*.json`, run `python -m app.services.queue_processor --once`, verify calendar entry + banner + dismiss persistence.

## 7. Out of scope

- Live toast / polling endpoint (`/api/queue-status`) — defer to v2 if staff want real-time without reload.
- Auto-reassign / optimization, Teams/email notifications, student portal.
- Changing manual approve flow — staff final decision preserved for all non-free cases.
