# Robot Resource Booking System — SPEC.md

## 1. Purpose

Build a small internal web application to replace the current manual robot-booking workflow based on email, Excel, lesson timetables, and manual conflict checking.

The system will run on a server. Staff will use a normal web browser from a host PC on the same network/VPN.

The system is intentionally simple:

- Booking applications are copied from PolyU email and pasted into the web UI.
- Lesson timetable data is copied manually and pasted/imported into the web UI.
- The system parses the pasted text into structured data.
- Staff can correct parsed fields before saving.
- The system checks robot availability against existing bookings, lessons, maintenance, and out-of-service periods.
- Staff makes the final Accept/Reject decision.
- Approved bookings appear immediately on the Robot Resource Calendar.

The main goal is to make booking decisions fast, clear, and reliable.

Automatic Outlook/Teams/timetable integration is explicitly out of scope for the first version.

---

## 2. Primary User

Primary user: ICT staff responsible for approving and managing robot bookings.

Initial assumptions:

- Small number of staff users.
- Internal PolyU/network/VPN use.
- Low booking volume.
- No public student self-service portal in v1.

---

## 3. Current Workflow

Current process:

1. Receive a facilities booking application by PolyU email.
2. Open the existing Excel booking table.
3. Check whether the requested robot is available.
4. Open/check the lesson timetable.
5. Determine whether teaching or another booking conflicts with the request.
6. Accept or reject the application manually, currently via Teams.
7. Update the booking record manually.

Problems:

- Excel is record-oriented rather than calendar-oriented.
- Availability across several robots is difficult to see.
- Booking and lesson data are in separate places.
- Conflict checking depends on memory and manual comparison.
- It is difficult to see who is using a robot from a particular date/time.
- Historical bookings are difficult to search.

---

## 4. Target Workflow

```text
Receive booking email
        |
        v
Copy email text
        |
        v
New Request page
        |
        v
Paste -> Parse
        |
        v
Review/correct parsed fields
        |
        v
Check Availability
        |
        +---- Preferred robot available ----> Accept
        |
        +---- Conflict ----------------------> Show conflict
                                               + alternatives
        |
        v
Staff decision
   Accept / Reject
        |
        +---- Accept -> Create reservations -> Calendar updated
        |
        +---- Reject -> Record decision only
```

Lesson timetable workflow:

```text
Copy lesson timetable
        |
        v
Lesson Schedule page
        |
        v
Paste -> Parse
        |
        v
Review/correct rows
        |
        v
Select affected robot(s)
        |
        v
Import
        |
        v
Lesson reservations appear on Calendar
```

---

## 5. Scope

### 5.1 Must Have

1. Server-hosted web UI.
2. Robot Resource Calendar.
3. Booking application paste-and-parse.
4. Editable parsed request preview.
5. Lesson timetable paste-and-parse.
6. Editable lesson preview before import.
7. Robot/resource management.
8. Existing booking management.
9. Conflict detection.
10. Alternative robot availability.
11. Accept/Reject recording.
12. Booking/user search.
13. Click a calendar booking to see user and booking details.
14. Persistent database.
15. Basic audit timestamps.

### 5.2 Explicit Non-Goals for v1

Do not build the following unless requested later:

- Microsoft Graph / automatic Outlook email reading.
- Automatic Teams approval.
- Automatic lesson timetable integration.
- Student login or student booking portal.
- Complex role/permission system.
- AI agent workflow.
- Optimization/scheduling algorithms.
- Notifications.
- Mobile app.
- Enterprise ERP integration.
- Multi-stage approval workflow.
- Complex reporting/analytics.

Manual copy/paste is an intentional design decision for v1.

---

## 6. Recommended Technical Architecture

Use one small server-side application rather than separate frontend/backend projects.

### 6.1 Stack

Recommended:

- Python 3.12+
- FastAPI
- SQLAlchemy 2.x
- SQLite
- Jinja2 templates
- HTMX and/or small amounts of vanilla JavaScript
- pytest
- Uvicorn
- Docker for deployment

Avoid React unless a later requirement clearly justifies it.

This is a small internal CRUD/calendar system. A single FastAPI application is easier to develop, deploy, debug, and maintain.

### 6.2 Deployment

```text
Host PC Browser
      |
      | HTTP over LAN/VPN
      v
Server
+--------------------------------+
| FastAPI Web App                |
| - UI                           |
| - Parser                       |
| - Availability Engine          |
| - Booking Management           |
|                                |
| SQLite database                |
+--------------------------------+
```

Default server URL:

```text
http://<server-ip>:8080
```

Database file should be stored outside the container in a persistent volume:

```text
/data/booking.db
```

If the application is exposed outside a trusted LAN/VPN, HTTPS and authentication must be added before exposure.

---

## 7. Main Navigation

Keep navigation small:

```text
Calendar
New Request
Requests
Lesson Schedule
Resources
```

The Calendar is the default/home page.

Do not create a separate Users page initially. User history can be opened from search or booking details.

---

# 8. Calendar

## 8.1 Primary View

The main view is a **Resource Week Calendar**.

Rows = robots.

Columns = date/session.

| Robot | Mon AM | Mon PM | Tue AM | Tue PM | Wed AM | Wed PM |
|---|---|---|---|---|---|---|
| UR10e (01) | Available | DING | Lesson | Lesson | Available | Available |
| UR10e (02) | Available | Available | Available | CHAN | Available | Available |
| UR10e (03) | DING | DING | Available | Available | Available | Available |
| UR10e (04) | Out | Out | Out | Out | Out | Out |

Required controls:

- Previous week
- Today
- Next week
- Date picker
- Resource filter
- Booking type filter
- Search by applicant name / SID / NetID

## 8.2 Calendar Event Types

Use visually distinct event types:

- Research / normal booking
- Lesson
- Maintenance
- Out of service
- Internal block

Exact colors are implementation details, but event types must be visually distinguishable.

## 8.3 Booking Display

A booking block should show at least:

```text
DING Changwen
Research
```

A lesson block should show:

```text
ME3101
Lesson
```

An out-of-service block should show:

```text
Out of Service
```

## 8.4 Click Event

Clicking an occupied block opens a detail modal/panel.

For a normal booking show:

- Applicant name
- SID / NetID
- Department
- Email
- Phone
- Request ID
- Robot
- Date/time
- Booking purpose
- Remarks
- Booking status
- Created/approved timestamps

For lessons show:

- Course/module
- Date/time
- Location if available
- Robot(s)
- Notes

---

# 9. New Request

## 9.1 Input

The page contains one large textarea:

```text
Paste booking application email here
```

Primary action:

```text
Parse Request
```

Do not require staff to manually enter every field before parsing.

## 9.2 Booking Parser

The booking email is relatively structured. Use deterministic parsing first.

Recognized labels should include:

- Department
- Facilities to Use
- Response ID
- Booking Type
- Date
- End Date
- Session
- Name
- SID/NetID
- Tel
- Email
- Booking for
- Remarks

Parser behavior:

1. Normalize line endings and whitespace.
2. Remove simple email/Markdown formatting such as `**`.
3. Detect labels case-insensitively.
4. Capture multiline values until the next recognized label.
5. Parse ISO dates such as `2026-08-31`.
6. Detect AM and PM sessions from session text.
7. Recognize standard session times:
   - AM: 08:30–12:00
   - PM: 13:30–17:00
8. Search Remarks and other text for a preferred robot pattern such as:
   - `UR10e (03)`
   - `UR10e(03)`
9. Return warnings rather than silently guessing if important fields are missing.

No LLM dependency is required for v1.

## 9.3 Parsed Preview

Labels may carry a parenthetical qualifier — the ICT form sends
`End Date (Consecutive Booking only)` and `Remarks (if any)`. Bracketed qualifiers are
ignored when matching a label, so the field is still recognised.

When no end-date label is recognised the end date defaults to the start date. If the
start-date section contains a second date, an unrecognised label swallowed it: warn
instead of silently booking a single day.

## 9.4 Applicant History on the Preview

The preview states whether the applicant is already on record, so staff can judge a
request before approving it:

- matched on SID/NetID first, falling back to the name;
- shows how many previous requests exist and how they ended, plus the most recent one;
- says explicitly when there is no history at all;
- warns when the match was by name only, or when other records share the name.

After parsing, show editable fields:

- Response ID
- Applicant name
- SID / NetID
- Department
- Email
- Phone
- Facility
- Booking type
- Start date
- End date
- AM checkbox
- PM checkbox
- Preferred robot
- Purpose
- Remarks

Actions:

```text
Check Availability
Save as Pending
Cancel
```

Missing or uncertain fields should be highlighted.

Example:

```text
Preferred Robot: [ UR10e (03) v ]

Start Date: [2026-08-31]
End Date:   [2026-09-04]

[x] AM 08:30-12:00
[x] PM 13:30-17:00
```

---

# 10. Availability and Conflict Engine

This is the core business logic.

## 10.1 Requested Slots

A multi-day request is expanded into individual time slots.

Example:

```text
2026-08-31 AM
2026-08-31 PM
2026-09-01 AM
2026-09-01 PM
...
```

For a five-day AM+PM booking there are ten requested slots.

## 10.2 Conflict Definition

Two time ranges overlap when:

```text
requested_start < existing_end
AND
requested_end > existing_start
```

Conflicts include active reservations of type:

- Booking
- Lesson
- Maintenance
- Out of service / block

Cancelled and rejected records do not block availability.

## 10.3 Availability Result

For the preferred robot display every requested slot:

```text
UR10e (03)

31 Aug AM   Available
31 Aug PM   Available
01 Sep AM   Conflict - Lesson ME3101
01 Sep PM   Available
...
```

Overall status:

- `AVAILABLE` — all requested slots are free.
- `CONFLICT` — one or more requested slots overlap.
- `OUT OF SERVICE` — resource itself is disabled.

## 10.4 Alternative Resources

If the preferred robot is unavailable, check other active robots of the same relevant resource group/model.

Show only useful results:

```text
UR10e (01)   Fully Available
UR10e (05)   Fully Available
UR10e (02)   Conflict
```

Fully available alternatives should appear first.

Do not automatically change the requested robot. Staff must choose the final robot.

## 10.5 Final Approval Check

When staff clicks Accept, run the conflict check again immediately before writing reservations.

This prevents approving a request based on stale availability.

---

## 10.6 Hand-Back Dates

When a check finds conflicts, list the unavailable dates and say when the robot must be
returned.

- Consecutive unavailable days are merged into one run, so one hand-back date is given per
  run, not one per day.
- The hand-back date is the day before the run starts.
- Both sessions of the same day collapse into one row.
- An Out of Service robot produces no hand-back dates: it cannot be lent out at all.

Also produce a plain-text message staff can send the applicant. The standard wording is:

```text
Please note that the robot will be reserved for our lesson on <dates>.

As a reminder, you are required to restore the robot to its original state before our
lesson begins. Once this booking period has concluded, you are welcome to collect the
robot again for your own use.
```

`<dates>` is the list of unavailable runs, comma-separated with a final "and". The
"our lesson" wording is only used when every blocking entry is a lesson; a maintenance
window, block or other booking gets neutral wording and names what the robot is
committed to. The return-by date stays in the hand-back table.

---

# 11. Accept / Reject

## 11.1 Accept

When staff accepts:

1. Validate the request.
2. Re-run conflict check.
3. Require a selected resource.
4. Create one reservation row per requested date/session.
5. Mark request as Approved.
6. Record approval timestamp.
7. Calendar updates immediately.

The system does not automatically send email or approve in Teams in v1.

Staff may continue the external Teams action manually.

## 11.2 Reject

When staff rejects:

- Mark request as Rejected.
- Save optional rejection reason.
- Do not create calendar reservations.
- Record decision timestamp.

## 11.3 Cancel Approved Booking

An approved booking can be cancelled manually.

Cancellation:

- keeps historical records;
- marks request/reservations cancelled;
- removes the reservation from active conflict checking;
- remains visible in request history.

Do not hard-delete booking history through the normal UI.

## 11.4 Amend Approved Booking

Staff must be able to correct an approved booking without cancelling it and re-entering
the application.

Amending:

- re-runs the conflict check against live data (as approval does);
- is refused, leaving the booking untouched, if the robot is no longer free;
- otherwise marks the current reservations cancelled and writes new ones to match;
- may move the booking to a different active robot;
- keeps the request APPROVED and keeps the released rows as history.

## 11.5 Reopen a Closed Request

A cancelled or rejected request can be returned to PENDING.

Reopening:

- keeps the request, its history and its Response ID;
- clears the decision timestamp and reason;
- leaves released reservations cancelled — approving again writes new ones;
- is refused if the Response ID has since been taken by a live request.

## 11.6 Release a Single Reservation

A single slot of an approved booking can be cancelled on its own, leaving the rest of the
booking active. When the last active slot is released the request becomes CANCELLED.

## 11.7 Response ID Uniqueness

A Response ID is unique among requests that are neither CANCELLED nor REJECTED. Closed
records keep their Response ID for the audit trail but no longer reserve it, so the same
application can be re-entered after a cancellation.

## 11.8 Accept Over a Conflict

A conflict informs the decision; it does not veto it. Staff may approve a request whose
slots clash, because the robot is still lent out and simply has to come back in time.

Accepting over a conflict:

- requires an explicit second action ("Accept anyway"), never the normal Accept;
- is refused for an Out of Service or Retired robot, which cannot be handed over at all;
- books every requested slot, including the clashing ones;
- stamps each clashing reservation with a note naming what it shares the session with,
  plus an optional free-text instruction from staff;
- adds "the robot must be back before the lesson starts" when the clash is a lesson;
- flags those reservations on the calendar and lists the affected days on the request.

Existing reservations are never moved or overwritten. Both entries stay on the calendar.

---

# 12. Requests Page

Provide a simple searchable table.

Columns:

- Request ID
- Applicant
- SID/NetID
- Department
- Start
- End
- Robot
- Status
- Updated

Filters:

- Pending
- Approved
- Rejected
- Cancelled
- Applicant/SID search
- Date range

Click a row to open request details.

---

# 13. Lesson Schedule

## 13.1 Purpose

Lesson usage must occupy robot time exactly like a booking, so it participates in conflict checking.

Lesson data is manually copied and pasted.

## 13.2 Input

Provide:

```text
Paste lesson timetable here
```

Action:

```text
Parse Timetable
```

The parser should attempt to extract:

- Course/module code or title
- Date
- Start time
- End time
- Location
- Notes

Because timetable formats may vary, parsed rows must always be editable before import.

## 13.3 Robot Assignment

Timetable text may not identify exact robot IDs.

Before import, user must choose affected resources:

```text
Affected robots:
[x] UR10e (01)
[x] UR10e (02)
[x] UR10e (03)
[ ] UR10e (04)
[x] UR10e (05)
```

Allow:

```text
Select all active robots
```

## 13.4 Import

On confirmation, create lesson reservations for each selected robot.

Lesson reservations use the same availability data model as normal bookings.

If an imported lesson conflicts with an existing approved booking, show the conflict before import and require manual confirmation/correction.

Do not silently overwrite any reservation.

---

# 14. Resources

Provide a simple resource-management page.

Fields:

- Resource ID
- Display name
- Model
- Resource group
- Location
- Status
- Remarks

Example:

```text
Display Name: UR10e (03)
Model: UR10e
Group: Collaborative Robots
Status: Active
```

Statuses:

- Active
- Out of Service
- Retired

Out of Service resources are never suggested as available.

For temporary maintenance/outage, create a dated calendar block rather than changing historical bookings.

---

# 15. Data Model

Keep the schema small.

## 15.1 applicants

```text
id                  INTEGER PK
sid_netid           TEXT UNIQUE NULL
name                TEXT NOT NULL
department          TEXT NULL
email               TEXT NULL
phone               TEXT NULL
created_at          DATETIME
updated_at          DATETIME
```

When a new request uses an existing SID/NetID, reuse/update the applicant record rather than creating a duplicate.

## 15.2 resources

```text
id                  INTEGER PK
name                TEXT UNIQUE NOT NULL
model               TEXT NULL
resource_group      TEXT NOT NULL
location            TEXT NULL
status              TEXT NOT NULL
remarks             TEXT NULL
created_at          DATETIME
updated_at          DATETIME
```

## 15.3 booking_requests

```text
id                    INTEGER PK
response_id           TEXT UNIQUE NULL
applicant_id          INTEGER FK -> applicants.id

facility              TEXT NULL
booking_type          TEXT NULL

start_date            DATE NOT NULL
end_date              DATE NOT NULL
sessions_json         TEXT NOT NULL

preferred_resource_id INTEGER NULL FK -> resources.id
assigned_resource_id  INTEGER NULL FK -> resources.id

purpose               TEXT NULL
remarks               TEXT NULL
raw_text              TEXT NULL

status                TEXT NOT NULL
rejection_reason      TEXT NULL

created_at            DATETIME
updated_at            DATETIME
decided_at            DATETIME NULL
```

Request statuses:

```text
PENDING
APPROVED
REJECTED
CANCELLED
```

## 15.4 reservations

Use one table for every calendar-blocking event.

```text
id                  INTEGER PK
resource_id         INTEGER FK -> resources.id

source_type         TEXT NOT NULL
booking_request_id  INTEGER NULL FK -> booking_requests.id

title               TEXT NOT NULL
start_at            DATETIME NOT NULL
end_at              DATETIME NOT NULL

status              TEXT NOT NULL
details             TEXT NULL

created_at          DATETIME
updated_at          DATETIME
```

`source_type`:

```text
BOOKING
LESSON
MAINTENANCE
BLOCK
```

Reservation status:

```text
ACTIVE
CANCELLED
```

This table is the source of truth for the calendar and conflict engine.

Do not create separate booking, lesson, and maintenance calendar tables.

---

# 16. User Search / Booking History

Calendar and Requests pages should contain a search field.

Search by:

- Applicant name
- SID
- NetID
- Request ID

Example:

```text
Search: 25104512r
```

Result should show the applicant and related bookings.

Clicking the applicant should show:

```text
Applicant
- Name
- SID/NetID
- Department
- Email
- Phone

Bookings
- Current
- Upcoming
- Historical
```

This can be a simple page or modal.

No separate user-management module is required.

---

# 17. Server Routes / API Boundaries

The exact implementation may use HTML form routes and small JSON endpoints.

Suggested boundaries:

```text
GET  /                       Calendar
GET  /calendar

GET  /requests
GET  /requests/new
POST /requests/parse
POST /requests
GET  /requests/{id}
POST /requests/{id}/check
POST /requests/{id}/update
POST /requests/{id}/approve
POST /requests/{id}/reject
POST /requests/{id}/cancel
POST /requests/{id}/reopen

POST /reservations/{id}/cancel

GET  /lessons
POST /lessons/parse
POST /lessons/import

GET  /resources
POST /resources
POST /resources/{id}/update

GET  /search?q=
```

Do not create an unnecessarily large REST API for v1.

---

# 18. Validation Rules

## Booking Request

- Applicant name required.
- Start date required.
- End date required.
- End date must be greater than or equal to start date.
- At least one session required.
- Assigned robot required before approval.
- Assigned robot must be Active.
- Approval cannot proceed with unresolved conflict.

## Reservation

- Resource required.
- `start_at < end_at`.
- Active reservation must not overlap another active reservation for the same resource unless the conflict is explicitly resolved before creation.

## Parser

Parser failure must never cause silent data loss.

If a field cannot be parsed:

- leave it blank;
- show a warning;
- allow manual correction.

---

# 19. UI Principles

The UI should prioritize speed and visibility, not decoration.

Requirements:

- Desktop-first responsive web UI.
- Light theme by default.
- Calendar is the primary interface.
- Available slots should be visually quiet.
- Occupied slots should clearly show booking type and user/course.
- Avoid dense spreadsheet-like screens.
- Use modals/panels for details instead of navigating through many pages.
- Important actions must have clear labels:
  - Parse
  - Check Availability
  - Accept
  - Reject
  - Cancel Booking
- Confirm destructive actions such as cancellation.
- No unnecessary animations.

---

# 20. Security and Access

For the first internal deployment:

- Bind the service to the internal server/LAN or access through VPN.
- Do not expose the SQLite database directly.
- Validate all form input.
- Escape rendered user/pasted text.
- Use CSRF protection for state-changing web forms if applicable to the chosen implementation.

Authentication is not required for the initial single-user/internal-network build unless the server is accessible by untrusted users.

If broader access is needed later, add a simple authenticated staff account layer rather than redesigning the application.

---

# 21. Data Backup

SQLite is acceptable for this workload.

Minimum backup requirement:

- Database stored in persistent `/data`.
- Provide a simple documented backup command that copies `booking.db`.
- Database backup should be possible while the application is stopped.

Do not build a complex backup service in v1.

---

# 22. Logging

Log:

- Application startup/shutdown.
- Parser errors.
- Database errors.
- Approval/rejection/cancellation actions.
- Unexpected conflict-check failures.

Do not log full pasted email contents by default because they contain personal information.

---

# 23. Testing

Use pytest.

## Parser Tests

- Standard booking email parses correctly.
- Multiline Session field.
- Multiline Remarks field.
- Missing optional field.
- Invalid date.
- Preferred `UR10e (03)` detection.

## Conflict Engine Tests

- No overlap.
- Exact overlap.
- Partial overlap.
- Adjacent times are not conflicts.
- Lesson blocks booking.
- Maintenance blocks booking.
- Cancelled reservation does not block.
- Out-of-service resource is unavailable.
- Multi-day AM+PM request.
- Alternative resource selection.

## Booking Workflow Tests

- Save pending request.
- Approve available booking.
- Re-check conflict during approval.
- Reject request.
- Cancel approved request.
- Approved booking appears in calendar data.

---

# 24. Acceptance Criteria

The first usable release is complete when the following workflow works end-to-end:

1. Start the application on the server.
2. Open the web UI from another PC browser.
3. View all configured robots in the week calendar.
4. Paste a real booking request email.
5. Parse the email into structured fields.
6. Manually correct any parsed field.
7. Check the requested robot against existing reservations.
8. See exact conflicting dates/sessions if there is a conflict.
9. See fully available alternative robots.
10. Accept an available request.
11. Immediately see the applicant name on the selected robot's calendar.
12. Click the booking and view applicant/request details.
13. Search the applicant by name or SID/NetID.
14. Paste/import lesson timetable data.
15. See lesson blocks on the calendar.
16. Confirm future booking requests conflict with imported lessons.
17. Reject or cancel a request without deleting its history.
18. Restart the server and confirm data persists.

---

# 25. Suggested Project Structure

```text
robot-booking/
├── app/
│   ├── main.py
│   ├── db.py
│   ├── models.py
│   ├── schemas.py
│   ├── services/
│   │   ├── booking_parser.py
│   │   ├── lesson_parser.py
│   │   ├── availability.py
│   │   └── booking_service.py
│   ├── routes/
│   │   ├── calendar.py
│   │   ├── requests.py
│   │   ├── lessons.py
│   │   └── resources.py
│   ├── templates/
│   └── static/
├── tests/
├── data/
├── Dockerfile
├── requirements.txt
├── README.md
└── SPEC.md
```

Do not create extra service layers, repositories, event buses, message queues, or microservices unless a concrete requirement appears.

---

# 26. Development Order

## Phase 1 — Core Data and Calendar

1. FastAPI app skeleton.
2. SQLite database.
3. Resource model and resource CRUD.
4. Reservation model.
5. Week resource calendar.
6. Manual reservation creation for development/testing.

**Deliverable:** staff can see robot occupancy in a useful calendar.

## Phase 2 — Booking Request Workflow

1. Applicant model.
2. Booking request model.
3. Booking email parser.
4. Parsed/editable preview.
5. Conflict engine.
6. Availability result.
7. Accept/Reject.
8. Booking detail modal.

**Deliverable:** pasted email can become an approved calendar booking.

## Phase 3 — Lesson Schedule

1. Lesson paste screen.
2. Basic timetable parser.
3. Editable preview.
4. Robot selection.
5. Lesson import.
6. Lesson conflict checking.

**Deliverable:** lessons block robots automatically after manual timetable import.

## Phase 4 — Operational Finish

1. Search by name/SID/request ID.
2. Cancellation.
3. Logging.
4. Backup documentation.
5. Docker deployment.
6. End-to-end tests.

**Deliverable:** ready for routine internal use.

---

# 27. Final Design Principles

The coding agent should preserve these principles throughout implementation:

1. Calendar is the main operational UI.
2. Reservations are the source of truth for availability.
3. Email and timetable copy/paste are acceptable and intentional.
4. Parsing must always be reviewable/editable by staff.
5. Conflict detection must be deterministic, not AI-based.
6. Staff makes the final booking decision.
7. Store history; do not silently overwrite or hard-delete bookings.
8. Prefer one simple server application over a distributed architecture.
9. Do not add features that are not required by this SPEC.
10. Optimize for clarity and maintainability rather than architectural sophistication.
::: ​​