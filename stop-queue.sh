#!/usr/bin/env bash
# Stop the OneDrive booking queue worker started by ./start-queue.sh.
#
# Graceful SIGTERM first, SIGKILL only if it refuses to exit.
# Does not touch the systemd service — use `sudo systemctl stop robot-booking-queue`.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$APP_DIR/data/queue.pid"

# If systemd owns the worker, stop it there.
if [ -f /etc/systemd/system/robot-booking-queue.service ]; then
  if systemctl is-active --quiet robot-booking-queue 2>/dev/null; then
    echo "Stopping via systemd (sudo may prompt)..."
    sudo systemctl stop robot-booking-queue
    echo "Stopped. It will start again on reboot (the unit is enabled)."
    echo "To keep it down across reboots: sudo systemctl disable robot-booking-queue"
  else
    echo "Not running (systemd unit is installed but inactive)."
  fi
  exit 0
fi

stop_pid() {
  local pid="$1"
  kill "$pid" 2>/dev/null || return 1
  for _ in $(seq 1 40); do
    kill -0 "$pid" 2>/dev/null || { echo "Stopped (PID $pid)"; return 0; }
    sleep 0.25
  done
  echo "Did not exit after 10s — sending SIGKILL to $pid" >&2
  kill -9 "$pid" 2>/dev/null || true
  echo "Killed (PID $pid)"
}

if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    stop_pid "$PID"
  else
    echo "Stale PID file (process $PID is gone)."
  fi
  rm -f "$PID_FILE"
  exit 0
fi

ORPHAN="$(pgrep -f "app.services.queue_processor" | head -1 || true)"
if [ -n "$ORPHAN" ]; then
  echo "No PID file; found worker process $ORPHAN."
  stop_pid "$ORPHAN"
else
  echo "Not running."
fi
