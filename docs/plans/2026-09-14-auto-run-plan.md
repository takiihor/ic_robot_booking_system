# Auto-Run Web + Queue Worker Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Delete the stale Windows `8757` portproxy so local `robot-booking` binds, and leave both systemd units enabled for auto-start.

**Architecture:** Ops-only fix, no code changes. One Windows Admin step (remove proxy) plus one WSL sudo step (restart/enable services). Mirrored WSL networking then serves the WSL app on Windows `127.0.0.1:8757` automatically.

**Tech Stack:** Windows `netsh`, systemd (`robot-booking`, `robot-booking-queue`), uvicorn/FastAPI health check.

---

## Preconditions (already verified 2026-09-14)

- `robot-booking-queue`: `active (running)` since Sep-11, PID 252, polling OneDrive queue every 10s.
- `robot-booking`: `enabled` + `Restart=always`, but crash-looping (`activating (auto-restart)`, ~44k restarts) with `[Errno 98] address already in use` on `0.0.0.0:8757`.
- Windows holds `127.0.0.1:8757` via `netsh interface portproxy` → `158.132.155.21:8757` (service `iphlpsvc`, since 9/11). Remote refuses connections; Windows-local `healthz` also fails. Proxy is stale.
- WSL `/etc/wsl.conf` has `systemd=true`; `.wslconfig` has `networkingMode=mirrored` + `localhostForwarding=true`.
- No worktree needed: this plan touches no code, only live system state.

### Task 1: Delete stale Windows portproxy (Admin)

**Files:**
- Modify: none (live Windows `netsh` state only)

**Step 1: Open an elevated PowerShell and show current proxies**

Run: `netsh interface portproxy show all`
Expected: row `127.0.0.1  8757  158.132.155.21  8757` present (plus unrelated `8085` row — leave it).

**Step 2: Delete only the 8757 forwarding**

Run: `netsh interface portproxy delete v4tov4 listenport=8757 listenaddress=127.0.0.1`
Expected: no output (success). Requires Administrator; non-elevated shell errors with access denied.

**Step 3: Verify removal and free listener**

Run: `netsh interface portproxy show all`
Expected: no `8757` row.

Run: `Get-NetTCPConnection -LocalPort 8757 -State Listen`
Expected: no output (nothing listening). If a row persists, note its `OwningProcess` and stop before Task 2.

**Step 4: Checkpoint — do NOT commit (no repo files changed)**

---

### Task 2: Restart and enable both services in WSL (sudo)

**Files:**
- Modify: none in git. Deletes stale untracked `data/queue.pid` on disk (holds dead PID `17416`; real worker is systemd PID `252`). `data/*.pid` is gitignored.

**Step 1: Remove the stale queue pidfile**

Run: `rm -f data/queue.pid`
Expected: `cat data/queue.pid` → `No such file or directory`. (Prevents `./start-queue.sh` mis-reporting while systemd owns the worker.)

**Step 2: Enable both units and restart the web app**

Run: `sudo systemctl enable robot-booking robot-booking-queue && sudo systemctl restart robot-booking`
Expected: `Created symlink ...` lines (if not already enabled) and no error. `sudo` will prompt for password — run in a real terminal.

**Step 3: Verify both units are active**

Run: `systemctl is-active robot-booking robot-booking-queue`
Expected:
```
active
active
```

Run: `systemctl --no-pager status robot-booking | head -5`
Expected: `Active: active (running)`, NOT `activating (auto-restart)`.

**Step 4: Verify health from WSL and Windows**

Run: `curl -sf http://127.0.0.1:8757/healthz`
Expected: `{"status":"ok"}`.

Run (Windows PowerShell, non-admin OK): `Invoke-WebRequest -Uri http://127.0.0.1:8757/healthz -TimeoutSec 5 | Select-Object StatusCode,Content`
Expected: `StatusCode 200`, content `{"status":"ok"}` (mirrored localhost serves the WSL app).

**Step 5: Confirm no new crash loop**

Run: `journalctl -u robot-booking --no-pager -n 5`
Expected: `Application startup complete` + `Uvicorn running on http://0.0.0.0:8757`, no `[Errno 98]` lines after the restart timestamp.

**Step 6: Commit nothing (no repo files changed)**

If `docs/plans/2026-09-14-auto-run-plan.md` itself is new/untracked, optionally track it:

```bash
git add -f docs/plans/2026-09-14-auto-run-plan.md
git commit -m "docs: auto-run web + queue implementation plan"
```

Note: `docs/` is gitignored — `-f` is required. Do NOT `git add -A`; unrelated work is staged/unstaged in this checkout.

---

## Rollback

- If the remote `158.132.155.21:8757` is needed again, re-add the proxy on a **different** listen port (never `8757`):
  `netsh interface portproxy add v4tov4 listenport=8756 listenaddress=127.0.0.1 connectport=8757 connectaddress=158.132.155.21` (Admin).
- If the web app must move instead: set `BOOKING_PORT=8758` in `deploy/booking.env`, then `sudo systemctl restart robot-booking`.

## Out of scope

- Docker deployment, port `8758` migration, auth/HTTPS, `deploy/allow-lan-access.sh` firewall changes.
