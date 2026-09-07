# Robot Resource Booking System

An internal web application for managing robot bookings, lesson reservations, maintenance periods and out-of-service blocks.

Staff can paste booking emails or lesson timetables into the system. The parser extracts the information into editable fields, checks robot availability, and shows conflicts before staff approve a booking.

Approved bookings appear on the Robot Resource Calendar immediately.

Built to [SPEC.md](SPEC.md).

---

## What the system does

- Shows robot availability in a weekly calendar.
- Parses booking application emails into editable fields.
- Parses lesson timetables into lesson reservations.
- Checks booking conflicts before approval.
- Suggests other available robots when appropriate.
- Supports pending, approved, rejected and cancelled requests.
- Supports maintenance and out-of-service blocks.
- Keeps booking and applicant history.
- Allows individual booking slots to be released.

Staff always make the final booking decision. The system does not automatically reassign robots.

---

## Quick start

### 1. Create the Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Create the initial robot resources

Run this once:

```bash
.venv/bin/python -m app.seed
```

### 3. Start the application

```bash
./start.sh
```

Stop it with:

```bash
./stop.sh
```

The default address is:

```text
http://<server-ip>:8757
```

The SQLite database is created automatically at:

```text
data/booking.db
```

Host, port and database settings are stored in `deploy/booking.env`.

---

## Main workflow

### Calendar

The home page shows a weekly calendar for all robot resources.

It displays:

- research bookings
- lessons
- maintenance
- out-of-service blocks

Click a calendar entry to view its booking information.

You can also filter the calendar by resource, booking type or applicant information.

### New booking request

1. Open **New Request**.
2. Paste the booking application email.
3. Click **Parse Request**.
4. Review and correct the extracted fields.
5. Click **Check Availability**.
6. Save the request as pending or approve/reject it.

The parser does not guess missing information. Fields that cannot be read are left for staff to correct.

### Availability checking

The system expands a request into individual booking sessions and checks each session against existing reservations.

Possible conflicts include:

- another booking
- a lesson
- maintenance
- an out-of-service block

If the preferred robot is unavailable, the system can show fully available alternatives from the same resource group.

Availability is checked again immediately before approval so stale results cannot be approved accidentally.

### Lesson schedule

1. Open **Lesson Schedule**.
2. Paste the timetable.
3. Click **Parse Timetable**.
4. Review the parsed rows.
5. Select the robots used by the lesson.
6. Import the schedule.

The timetable parser supports common pasted formats such as tab-separated, pipe-separated and column-aligned text.

If a lesson overlaps an existing booking, the conflict is shown before import. Existing records are not silently overwritten.

### Requests and history

The **Requests** page lets staff search and filter booking requests.

You can search by information such as:

- applicant
- SID / NetID
- Response ID
- request number

Booking history is kept even after a request is rejected or cancelled.

### Correcting a request

- **Pending** requests can be edited directly.
- **Approved** requests can be amended. Availability is checked again before changes are saved.
- **Rejected or cancelled** requests can be reopened as pending.
- Individual reservation slots can be released without cancelling the whole booking.

### Conflicts

A normal conflict is shown to staff for a decision.

Where allowed, staff can choose **Accept anyway** and keep both reservations visible. The affected sessions are clearly flagged.

An **Out of Service** or **Retired** robot cannot be overridden.

---

## Run as an always-on Linux service

For a server installation, systemd is recommended.

```bash
sudo ./deploy/install-systemd-service.sh
sudo systemctl enable --now robot-booking
```

Check the service:

```bash
systemctl status robot-booking
journalctl -u robot-booking -f
```

After installing the service, `./start.sh` and `./stop.sh` use systemd automatically.

If you change `deploy/booking.env`, restart the service:

```bash
sudo systemctl restart robot-booking
```

---

## Docker deployment

```bash
docker compose up -d --build
docker compose exec booking python -m app.seed
```

Run the seed command only for the initial resource setup.

The application listens on port `8757` by default. The database is stored on a mounted volume so it survives image rebuilds.

---

## Backups

The application uses a single SQLite database.

Create a backup with:

```bash
./backup.sh /srv/backups
```

When `sqlite3` is available, the script uses SQLite's backup mechanism so the application can remain running.

To restore a backup:

1. Stop the application.
2. Replace `data/booking.db` with the backup file.
3. Remove any old `booking.db-wal` or `booking.db-shm` files beside it.
4. Start the application again.

---

## Tests

Run the test suite with:

```bash
.venv/bin/pytest
```

Tests cover the booking parser, timetable parser, conflict checking and booking workflow.

---

## Configuration

Settings are stored in `deploy/booking.env`.

| Variable | Default | Purpose |
|---|---|---|
| `BOOKING_DB_PATH` | `./data/booking.db` | SQLite database path |
| `BOOKING_DATABASE_URL` | derived from `BOOKING_DB_PATH` | Full SQLAlchemy database URL |
| `BOOKING_LOG_LEVEL` | `INFO` | Application log level |
| `BOOKING_HOST` | `0.0.0.0` | Network interface to bind |
| `BOOKING_PORT` | `8757` | Web server port |

A value supplied directly in the environment overrides the file. For example:

```bash
BOOKING_PORT=9000 ./start.sh
```

---

## Security

The application currently has no login system. It is intended for use on a trusted internal network or behind a VPN.

Do not expose it directly to the public internet without adding authentication and HTTPS.

For tighter network access, bind `BOOKING_HOST` to an internal or VPN interface instead of `0.0.0.0`.

---

## Project structure

```text
app/
├── main.py              FastAPI application
├── config.py            Application configuration
├── db.py                Database setup
├── models.py            Database models
├── security.py          Form-post protection
├── seed.py              Initial robot resources
├── services/            Parsing, availability and booking logic
├── routes/              Web routes
├── templates/           Jinja2 templates
└── static/              CSS and static files

deploy/                  Deployment configuration and systemd files
data/                    SQLite database
start.sh                 Start the application
stop.sh                  Stop the application
backup.sh                Back up the database
tests/                    Automated tests
```

---

## Design note

`reservations` is the source of truth for calendar occupancy and conflict checking. Bookings, lessons, maintenance and resource blocks are checked through the same reservation model.

Conflict checking is deterministic. The application does not use an LLM for availability decisions.
