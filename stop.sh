#!/usr/bin/env bash
# Stop the Robot Resource Booking System started by ./start.sh.
#
# Graceful SIGTERM first, SIGKILL only if it refuses to exit.
# Does not touch the systemd service — use `sudo systemctl stop robot-booking`.
set -euo pipefail

DIRECT=0
[ "${1:-}" = "--direct" ] || [ "${1:-}" = "--no-systemd" ] && DIRECT=1

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$APP_DIR/data/booking.pid"

# Load deploy/booking.env WITHOUT clobbering anything the caller already set,
# so `BOOKING_PORT=9000 ./start.sh` works as an override.
load_env() {
  local file="$1" line key val
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    key="${key//[[:space:]]/}"
    [ -n "$key" ] || continue
    [ -n "${!key:-}" ] || export "$key=$val"
  done < "$file"
}
load_env "$APP_DIR/deploy/booking.env"
PORT="${BOOKING_PORT:-8757}"

# If systemd owns the service, stop it there.
if [ "$DIRECT" -eq 0 ] && [ -f /etc/systemd/system/robot-booking.service ]; then
  if systemctl is-active --quiet robot-booking; then
    echo "Stopping via systemd (sudo may prompt)..."
    sudo systemctl stop robot-booking
    echo "Stopped. It will start again on reboot (the unit is enabled)."
    echo "To keep it down across reboots: sudo systemctl disable robot-booking"
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

# No PID file — fall back to whatever holds the port.
ORPHAN="$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -n "$ORPHAN" ]; then
  echo "No PID file; found process $ORPHAN on port $PORT."
  stop_pid "$ORPHAN"
else
  echo "Not running."
fi
