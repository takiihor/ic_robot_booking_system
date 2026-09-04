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

The parser ignores parenthetical qualifiers on labels, so the ICT form's
`End Date (Consecutive Booking only)` and `Remarks (if any)` are read correctly. If an
end-date label is not recognised but a second date appears in the start-date section, the
preview warns rather than quietly booking a single day.

The preview also says whether the applicant is **already on record** — matched on
SID/NetID, falling back to the name — with a count of their previous requests, how those
ended, and a link to the most recent one. A first-time applicant is called out as such,
and so is a match made on name alone or a name shared by more than one record. The request
page carries the same Returning / First booking marker.

### Requests, search and history

**Requests** lists everything with status, applicant and date-range filters. The search box
in the header matches applicant name, SID, NetID, Response ID or request number, and an
applicant's page shows their current, upcoming and historical bookings.

Rejecting or cancelling records the decision and keeps the history. Cancelling an approved
booking marks its reservations cancelled so the robot is free again, but nothing is
deleted through the UI.

### Correcting a request

Wrong information never has to be retyped:

- **Pending** — edit the fields and save.
- **Approved** — the same form becomes **Amend booking**. Saving re-checks availability,
  releases the current reservations and writes new ones to match, in one step. If the robot
  is no longer free the amendment is refused and the existing booking is left alone. The
  assigned robot can be changed here too.
- **Cancelled or rejected** — **Reopen as Pending** puts the record back into the normal
  flow, keeping its history and Response ID. Released reservations stay released;
  approving again writes fresh ones.
- **One slot only** — **Release** on a reservation (from the request page or the calendar
  pop-up) hands back a single session and leaves the rest of the booking in place. If it
  was the last active slot, the request itself is cancelled.

When a check finds conflicts, the panel adds a **Dates to hand the robot back** table and
a ready-to-send message for the applicant. Consecutive blocked days are merged into one
run so the student gets a single return-by date per run — the day before the run starts —
rather than one per day. Both sessions of a day collapse into one row, and an Out of
Service robot produces no hand-back dates because it cannot be lent out at all.

The message uses the lab's standard notice with the dates filled in:

```text
Dear Ding Changwen,

Please note that the robot will be reserved for our lesson on 15–17 Sep 2026 (AM)
and Mon 28 Sep 2026 (AM).

As a reminder, you are required to restore the robot to its original state before our
lesson begins. Once this booking period has concluded, you are welcome to collect the
robot again for your own use.
```

"our lesson" is only claimed when every blocking entry really is a lesson — a maintenance
window or another booking gets neutral wording and is named explicitly.

### Accepting over a conflict

A conflict is information, not a veto. When the availability panel shows a clash, the
decision panel gains **Accept anyway** next to the normal Accept. It lends the robot out
for the whole period and flags only the clashing sessions:

- each flagged reservation records what it shares the session with — e.g. *Shares this
  session with Lesson "ME3101" 09:30-12:20. The robot must be back before the lesson
  starts.*
- you can add your own instruction ("Return by 09:00 to the lab technician") which is
  appended to every flagged day;
- the request page lists the days the robot has to be handed back;
- the calendar draws those blocks striped with a ⚠, and both entries stay visible.

Nothing existing is moved or overwritten. An Out of Service or Retired robot can never be
overridden — it cannot be handed over at all.

A Response ID is unique among *live* requests only. Cancelled and rejected records keep
theirs for the audit trail but stop reserving it, so the same application can be entered
again without blanking the field that links it back to the Teams form.

Applicant name, department and contact details belong to the shared applicant record —
correcting them on one request updates that person's other bookings too, which the form
says. Correcting a SID/NetID moves it onto the existing applicant instead of creating a
second one; entering a SID/NetID that already belongs to someone else moves the request
to that person.

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
├── migrations.py        Forward-only SQLite migrations (PRAGMA user_version)
├── models.py            applicants, resources, booking_requests, reservations
├── security.py          Same-origin protection for form posts
├── seed.py              Initial resource setup
├── services/
│   ├── booking_parser.py    Booking email -> structured fields
│   ├── lesson_parser.py     Timetable text -> editable lesson rows
│   ├── availability.py      Slot expansion, conflict detection, alternatives
│   ├── booking_service.py   Save/amend/approve/reject/cancel/reopen, lessons, search
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
