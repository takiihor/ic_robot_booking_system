#!/usr/bin/env bash
# Start the OneDrive booking queue worker in the background.
#
#   ./start-queue.sh                     poll BOOKING_QUEUE_DIR continuously
#   BOOKING_QUEUE_POLL_SECONDS=5 ./start.sh  override the poll interval
#
# Settings live in deploy/booking.env. If systemd is managing the worker,
# this script refuses to start a second copy — use systemctl instead.
# The worker only reads the booking database; it never creates bookings,
# approves anything, or sends notifications (see app/services/queue_processor.py).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$APP_DIR/data/queue.pid"
LOG_FILE="$APP_DIR/data/queue.log"
WORKER="$APP_DIR/.venv/bin/python"

# Load deploy/booking.env WITHOUT clobbering anything the caller already set.
# Handles values containing spaces (e.g. OneDrive paths) — split on the
# FIRST '=' only, exactly like start.sh.
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

QUEUE_DIR="${BOOKING_QUEUE_DIR:-}"
POLL="${BOOKING_QUEUE_POLL_SECONDS:-10}"

# --- if systemd owns the worker, drive it through systemd -------------------
if [ -f /etc/systemd/system/robot-booking-queue.service ]; then
  if systemctl is-active --quiet robot-booking-queue 2>/dev/null; then
    echo "Already running under systemd (PID $(systemctl show robot-booking-queue -p MainPID --value))."
  else
    echo "Starting via systemd (sudo may prompt)..."
    sudo systemctl start robot-booking-queue
  fi
  systemctl --no-pager --lines=0 status robot-booking-queue 2>/dev/null | head -3 || true
  exit 0
fi

if [ -z "$QUEUE_DIR" ]; then
  echo "BOOKING_QUEUE_DIR is empty — the queue worker is disabled." >&2
  echo "Set it in deploy/booking.env first." >&2
  exit 1
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PID_FILE")). Use ./stop-queue.sh first." >&2
  exit 1
fi

[ -x "$WORKER" ] || { echo "Missing $WORKER — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 1; }

mkdir -p "$APP_DIR/data"
cd "$APP_DIR"

nohup "$WORKER" -m app.services.queue_processor >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# --- wait until the worker reports it is polling -----------------------------
for _ in $(seq 1 40); do
  if grep -q "Queue processor started" "$LOG_FILE" 2>/dev/null; then
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Queue worker started (PID $(cat "$PID_FILE")) polling:"
      echo "  $QUEUE_DIR"
      echo "Logs:  tail -f $LOG_FILE"
      exit 0
    fi
    break
  fi
  kill -0 "$(cat "$PID_FILE")" 2>/dev/null || break
  sleep 0.25
done

echo "Failed to start — last 20 log lines:" >&2
tail -20 "$LOG_FILE" >&2
rm -f "$PID_FILE"
exit 1
