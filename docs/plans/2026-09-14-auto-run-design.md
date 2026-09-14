# Auto-Run Web + Queue Worker — Design

Date: 2026-09-14
Status: Approved
Approach: A — delete stale Windows portproxy, keep local on 8757

## 1. Goal

Both services start automatically and stay up:

1. `robot-booking` (FastAPI serves backend API + HTML frontend on `:8757`)
2. `robot-booking-queue` (OneDrive `booking-*.json` poller)

Backend + frontend are one process (`app/main.py` mounts `/static`, Jinja2 templates). No separate frontend service exists.

## 2. Decisions (user-confirmed)

- Port conflict: **delete stale proxy**. Windows `127.0.0.1:8757 → 158.132.155.21:8757` points to a dead remote (connection refused) and blocks WSL bind under `networkingMode=mirrored`.
- Keep local port **8757** (docs, bookmarks, `deploy/booking.env` unchanged).
- Both systemd units **enabled + `Restart=always`**.

## 3. Architecture & data flow

```
browser → 127.0.0.1:8757 (Windows == WSL via mirrored localhost)
  → uvicorn app.main:app → data/booking.db
booking-*.json → queue_processor (poll BOOKING_QUEUE_DIR)
  → save_request → approve-if-free → same booking.db → banner in UI
```

No Windows proxy. WSL systemd owns both processes.

## 4. Components

### 4.1 Windows (one-time, Admin)

- `netsh interface portproxy delete v4tov4 listenport=8757 listenaddress=127.0.0.1`
- Verify: `netsh interface portproxy show all` has no `8757`.

### 4.2 WSL systemd

- `robot-booking.service` + `robot-booking-queue.service` already installed at `/etc/systemd/system/`, both `enabled`.
- After proxy delete: `sudo systemctl restart robot-booking`, `systemctl status` both `active`.
- Remove stale `data/queue.pid` (holds dead `17416`; real worker is systemd PID `252`) so manual scripts stop mis-reporting.

## 5. Error handling

- Bind still fails → `journalctl -u robot-booking` shows `[Errno 98]`; re-check `netsh show` / `powershell Get-NetTCPConnection -LocalPort 8757`.
- Remote `158.132.155.21:8757` needed again later → re-add proxy on a *different* listen port, never `8757`.
- DB locked / OneDrive missing → worker logs to `data/queue.log`, retries next poll; web unaffected.

## 6. Testing

- `systemctl is-active robot-booking robot-booking-queue` → both `active`.
- `curl -sf http://127.0.0.1:8757/healthz` from WSL + `Invoke-WebRequest http://127.0.0.1:8757/healthz` from Windows → `{"status":"ok"}`.
- Reboot check: after restart both units come back without manual `./start.sh`.
- Drop `booking-*.json` in queue dir → processed within poll interval, banner appears.

## 7. Out of scope

- Moving to Docker (`restart: unless-stopped`) — heavier, same port conflict, not in use.
- Changing to port `8758`, auth/HTTPS, public exposure (app has no login; keep LAN/VPN only per `deploy/allow-lan-access.sh`).
