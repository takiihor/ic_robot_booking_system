#!/usr/bin/env bash
# Render this checkout's systemd units and install them for the current user.
#
#   sudo ./deploy/install-systemd-service.sh              # install web + queue worker
#   sudo ./deploy/install-systemd-service.sh --web-only   # install web app only
#   sudo ./deploy/install-systemd-service.sh --queue-only # install queue worker only
#   ./deploy/install-systemd-service.sh --render          # inspect or verify only (web unit)
#   ./deploy/install-systemd-service.sh --render-queue    # inspect or verify only (queue unit)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
WEB_TEMPLATE="$APP_DIR/deploy/robot-booking.service"
QUEUE_TEMPLATE="$APP_DIR/deploy/robot-booking-queue.service"
WEB_SERVICE_PATH="/etc/systemd/system/robot-booking.service"
QUEUE_SERVICE_PATH="/etc/systemd/system/robot-booking-queue.service"

if [ "${1:-}" = "--help" ]; then
  sed -n '1,9p' "$0" | sed 's/^# \?//'
  exit 0
fi

INSTALL_WEB=1
INSTALL_QUEUE=1
if [ "${1:-}" = "--render" ]; then
  RENDER_ONLY=web
elif [ "${1:-}" = "--render-queue" ]; then
  RENDER_ONLY=queue
elif [ "$#" -eq 0 ]; then
  RENDER_ONLY=""
elif [ "${1:-}" = "--web-only" ]; then
  RENDER_ONLY=""
  INSTALL_QUEUE=0
elif [ "${1:-}" = "--queue-only" ]; then
  RENDER_ONLY=""
  INSTALL_WEB=0
else
  echo "Unknown option: ${1:-} (try --help)" >&2
  exit 2
fi

RUN_AS_USER="${SUDO_USER:-$(id -un)}"
RUN_AS_GROUP="$(id -gn "$RUN_AS_USER")"

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

render_unit() {
  local template="$1"
  sed \
    -e "s|__BOOKING_APP_DIR__|$(escape_sed_replacement "$APP_DIR")|g" \
    -e "s|__BOOKING_USER__|$(escape_sed_replacement "$RUN_AS_USER")|g" \
    -e "s|__BOOKING_GROUP__|$(escape_sed_replacement "$RUN_AS_GROUP")|g" \
    "$template"
}

if [ "$RENDER_ONLY" = "web" ]; then
  render_unit "$WEB_TEMPLATE"
  exit 0
fi

if [ "$RENDER_ONLY" = "queue" ]; then
  render_unit "$QUEUE_TEMPLATE"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

if [ "$INSTALL_WEB" -eq 1 ]; then
  render_unit "$WEB_TEMPLATE" | install -D -m 0644 /dev/stdin "$WEB_SERVICE_PATH"
  echo "Installed $WEB_SERVICE_PATH for $RUN_AS_USER from $APP_DIR."
  echo "Start it with: sudo systemctl enable --now robot-booking"
fi

if [ "$INSTALL_QUEUE" -eq 1 ]; then
  render_unit "$QUEUE_TEMPLATE" | install -D -m 0644 /dev/stdin "$QUEUE_SERVICE_PATH"
  echo "Installed $QUEUE_SERVICE_PATH for $RUN_AS_USER from $APP_DIR."
  echo "Start it with: sudo systemctl enable --now robot-booking-queue"
fi

systemctl daemon-reload
