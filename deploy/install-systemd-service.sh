#!/usr/bin/env bash
# Render this checkout's systemd unit and install it for the current user.
#
#   sudo ./deploy/install-systemd-service.sh
#   ./deploy/install-systemd-service.sh --render  # inspect or verify only
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEMPLATE="$APP_DIR/deploy/robot-booking.service"
SERVICE_PATH="/etc/systemd/system/robot-booking.service"

if [ "${1:-}" = "--help" ]; then
  sed -n '1,5p' "$0" | sed 's/^# \?//'
  exit 0
fi

if [ "${1:-}" = "--render" ]; then
  RENDER_ONLY=1
elif [ "$#" -eq 0 ]; then
  RENDER_ONLY=0
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
  sed \
    -e "s|__BOOKING_APP_DIR__|$(escape_sed_replacement "$APP_DIR")|g" \
    -e "s|__BOOKING_USER__|$(escape_sed_replacement "$RUN_AS_USER")|g" \
    -e "s|__BOOKING_GROUP__|$(escape_sed_replacement "$RUN_AS_GROUP")|g" \
    "$TEMPLATE"
}

if [ "$RENDER_ONLY" -eq 1 ]; then
  render_unit
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

render_unit | install -D -m 0644 /dev/stdin "$SERVICE_PATH"
systemctl daemon-reload
echo "Installed $SERVICE_PATH for $RUN_AS_USER from $APP_DIR."
echo "Start it with: sudo systemctl enable --now robot-booking"
