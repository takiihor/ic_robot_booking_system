#!/usr/bin/env bash
# Start the Robot Resource Booking System in the background.
#
#   ./start.sh                     start on the configured host/port
#   BOOKING_PORT=9000 ./start.sh   override the port for one run
#   ./start.sh --direct            run it here, ignoring the systemd unit
#
# Settings live in deploy/booking.env. If systemd is managing the service,
# this script refuses to start a second copy — use systemctl instead.
set -euo pipefail

DIRECT=0
for arg in "$@"; do
  case "$arg" in
    --direct|--no-systemd) DIRECT=1 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$APP_DIR/data/booking.pid"
LOG_FILE="$APP_DIR/data/booking.log"
UVICORN="$APP_DIR/.venv/bin/uvicorn"

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
HOST="${BOOKING_HOST:-0.0.0.0}"
PORT="${BOOKING_PORT:-8757}"

show_urls() {
  local host="$1" port="$2"
  echo "Reachable at:"
  if [ "$host" = "0.0.0.0" ]; then
    ip -4 -o addr show scope global 2>/dev/null \
      | awk -v p="$port" '$2 !~ /^(docker|br-)/ {split($4,a,"/"); print "  http://" a[1] ":" p}'
  else
    echo "  http://$host:$port"
  fi
}

# --- if systemd owns the service, drive it through systemd -----------------
if [ "$DIRECT" -eq 0 ] && [ -f /etc/systemd/system/robot-booking.service ]; then
  if systemctl is-active --quiet robot-booking; then
    echo "Already running under systemd (PID $(systemctl show robot-booking -p MainPID --value))."
  else
    echo "Starting via systemd (sudo may prompt)..."
    sudo systemctl start robot-booking
  fi
  systemctl --no-pager --lines=0 status robot-booking | head -3
  show_urls "$HOST" "$PORT"
  exit 0
fi

if [ "$DIRECT" -eq 1 ] && systemctl is-active --quiet robot-booking 2>/dev/null; then
  echo "The systemd service is still running and holds port $PORT." >&2
  echo "Stop it first:  sudo systemctl stop robot-booking" >&2
  exit 1
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PID_FILE")). Use ./stop.sh first." >&2
  exit 1
fi

if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "Port $PORT is already in use by another process:" >&2
  ss -ltnp 2>/dev/null | grep ":$PORT " >&2 || true
  exit 1
fi

[ -x "$UVICORN" ] || { echo "Missing $UVICORN — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 1; }

mkdir -p "$APP_DIR/data"
cd "$APP_DIR"

nohup "$UVICORN" app.main:app --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# --- wait until it actually answers ---------------------------------------
for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "Started (PID $(cat "$PID_FILE")) on $HOST:$PORT"
    echo "Logs:  tail -f $LOG_FILE"
    show_urls "$HOST" "$PORT"
    exit 0
  fi
  sleep 0.25
done

echo "Failed to start — last 20 log lines:" >&2
tail -20 "$LOG_FILE" >&2
rm -f "$PID_FILE"
exit 1
