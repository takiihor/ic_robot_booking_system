# Robot Resource Booking System

An internal web application that replaces the manual robot-booking workflow built on
email, Excel and manual conflict checking.

Staff paste a booking application email or a lesson timetable, the system parses it into
structured fields, staff correct anything the parser got wrong, and the availability
engine checks the requested robot against every existing booking, lesson, maintenance
window and out-of-service block. Staff make the final Accept/Reject decision; approved
bookings appear on the Robot Resource Calendar immediately.

Built to [SPEC.md](SPEC.md).

---

## Quick start (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.seed     # create the five UR10e resources (once)

./start.sh                       # start in the background
./stop.sh                        # stop it
```

`start.sh` prints every URL the server is reachable on and waits until the app actually
answers before returning. The database is created automatically at `data/booking.db`.

Host, port and database path live in **`deploy/booking.env`** — the one place to change
them. They are read by `start.sh`, `stop.sh` and the systemd unit alike. The default port is **8757**,
chosen because it is uncommon enough that development tooling will not squat on it.
Override for a single run with `BOOKING_PORT=9000 ./start.sh`.

## Always-on service (recommended for the server)

`start.sh` dies with the terminal. To keep the system running across logout, crash and
reboot, install the systemd unit:

```bash
sudo ./deploy/install-systemd-service.sh
sudo systemctl enable --now robot-booking

systemctl status robot-booking
journalctl -u robot-booking -f
```

`Restart=always` with `StartLimitIntervalSec=0` means systemd restarts it after any exit,
however many times, and `enable` starts it at boot.

Once the unit is installed, `./start.sh` and `./stop.sh` **delegate to systemd** rather than
launching a second copy, so they stay the commands you use either way (they call `sudo
systemctl`, which may prompt for a password). `./stop.sh` stops the service until the next
reboot; to keep it down permanently use `sudo systemctl disable robot-booking`.

After editing `deploy/booking.env`, apply it with `sudo systemctl restart robot-booking`.
If the checkout moves or a different Linux user should run the service, re-run the installer;
it renders those values into `/etc/systemd/system/robot-booking.service`.

## Deployment (Docker)

```bash
docker compose up -d --build
docker compose exec booking python -m app.seed   # first run only
```

The application listens on port 8757 and stores its database on a mounted volume at
`/data/booking.db`, so the data survives image rebuilds.

Staff reach it from any PC on the same LAN/VPN at:

```
http://<server-ip>:8757
```

There is nothing to install on the client — the app serves its own HTML.

### Security

There is no login. The service is meant to sit on a trusted internal network or behind a
VPN. Cross-site form posts are rejected (`app/security.py`), all rendered user and pasted
text is escaped, and full email bodies are never written to the logs.

**Before exposing this to anything untrusted, add HTTPS and an authenticated staff account
layer.** To restrict access, set `BOOKING_HOST` in `deploy/booking.env` to a single
interface (e.g. a VPN address) instead of `0.0.0.0`. For Docker, change `"8757:8757"` in
`docker-compose.yml` to `"<internal-ip>:8757:8757"`.

## Backups

The database is a single SQLite file. To back it up:

```bash
./backup.sh /srv/backups
```

`backup.sh` uses `sqlite3 .backup` when available, which is consistent even while the
application runs. Without `sqlite3` it falls back to a plain copy — stop the application
first:

```bash
docker compose stop booking && ./backup.sh /srv/backups && docker compose start booking
```

Restore by stopping the app and copying a backup file over `data/booking.db` (delete any
`booking.db-wal` / `booking.db-shm` alongside it).

## Tests

```bash
.venv/bin/pytest
```

Covers the email parser, the timetable parser, the conflict engine, the booking workflow,
and the whole acceptance workflow end-to-end over real HTTP routes.

---

## Using it

### Calendar

The home page is a **resource week calendar**: one row per robot, two columns per day
(AM 08:30–12:00, PM 13:30–17:00). Event types are colour-coded — research booking, lesson,
maintenance, out-of-service block. Click any block to see who holds it and open the full
request. Filter by resource, booking type or applicant name/SID/NetID, and use
**+ Maintenance / Block** to reserve robot time directly.

Out-of-service robots are drawn hatched and are never offered as an alternative.

### New Request

Paste the application email and press **Parse Request**. The parser recognises the usual
labels (Department, Facilities to Use, Response ID, Booking Type, Date, End Date, Session,
Name, SID/NetID, Tel, Email, Booking for, Remarks), reads multi-line values, strips
Markdown bold, and finds a preferred robot written as `UR10e (03)` or `UR10e(03)`.

Anything it could not read is left blank, highlighted, and listed as a warning — it never
guesses. Correct the fields, then **Check Availability** or **Save as Pending**.

### Availability

The request detail page expands the booking into individual slots (a five-day AM+PM
request is ten slots) and reports each one:

```
UR10e (03)
31 Aug AM   Available
01 Sep AM   Conflict - Lesson ME3101
```

If the preferred robot is not free, fully available alternatives from the same resource
group are listed first. The system never reassigns the robot for you.

**Accept** re-runs the conflict check immediately before writing anything, so a request
can never be approved on stale availability.

### Lesson Schedule

Paste the timetable and press **Parse Timetable**. The parser handles tab-separated,
pipe-separated and column-aligned text as well as single-line free text, dropping header
rows. Every row stays editable. Choose the affected robots, then import — one lesson
reservation is created per robot.

If an imported lesson would clash with an existing booking, the clash is shown and an
explicit confirmation is required. Nothing is ever overwritten.

### Requests, search and history

**Requests** lists everything with status, applicant and date-range filters. The search box
in the header matches applicant name, SID, NetID, Response ID or request number, and an
applicant's page shows their current, upcoming and historical bookings.

Rejecting or cancelling records the decision and keeps the history. Cancelling an approved
booking marks its reservations cancelled so the robot is free again, but nothing is
deleted through the UI.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BOOKING_DB_PATH` | `./data/booking.db` | SQLite file location |
| `BOOKING_DATABASE_URL` | derived from `BOOKING_DB_PATH` | Full SQLAlchemy URL |
| `BOOKING_LOG_LEVEL` | `INFO` | Root log level |
| `BOOKING_HOST` | `0.0.0.0` | Interface to bind (`start.sh` / systemd) |
| `BOOKING_PORT` | `8757` | Port to listen on (`start.sh` / systemd) |

Set these in `deploy/booking.env`. An environment variable set by the caller wins over the
file, so `BOOKING_PORT=9000 ./start.sh` works for one-off runs.

## Project layout

```
app/
├── main.py              FastAPI app, logging, error pages
├── config.py            Session times, database location
├── db.py                Engine, session, schema creation
├── models.py            applicants, resources, booking_requests, reservations
├── security.py          Same-origin protection for form posts
├── seed.py              Initial resource setup
├── services/
│   ├── booking_parser.py    Booking email -> structured fields
│   ├── lesson_parser.py     Timetable text -> editable lesson rows
│   ├── availability.py      Slot expansion, conflict detection, alternatives
│   ├── booking_service.py   Save/approve/reject/cancel, lesson import, search
│   └── calendar_view.py     Resource week grid
├── routes/              calendar, requests, lessons, resources, search
├── templates/           Jinja2 (autoescaped)
└── static/app.css
deploy/
├── booking.env          Host, port, database path — single source of truth
└── robot-booking.service  systemd unit (always-on)
start.sh / stop.sh       Background start and graceful stop
tests/                   Parser, conflict-engine, workflow and end-to-end tests
```

### Design rules

`reservations` is the single source of truth for the calendar and for conflict checking —
bookings, lessons, maintenance and blocks all live in that one table. Two ranges conflict
when `requested_start < existing_end AND requested_end > existing_start`, so adjacent
sessions never clash. Cancelled and rejected records stop blocking but stay in the record.

Conflict detection is deterministic; there is no LLM anywhere in the system.
