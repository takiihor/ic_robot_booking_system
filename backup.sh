#!/usr/bin/env sh
# Back up the booking database (SPEC 21).
#
#   ./backup.sh [destination-directory]
#
# Stop the application first for a guaranteed-consistent copy:
#   docker compose stop booking && ./backup.sh /srv/backups && docker compose start booking
set -eu

DB="${BOOKING_DB_PATH:-./data/booking.db}"
DEST="${1:-./backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"

[ -f "$DB" ] || { echo "No database at $DB" >&2; exit 1; }
mkdir -p "$DEST"

if command -v sqlite3 >/dev/null 2>&1; then
  # Consistent even while the application is running.
  sqlite3 "$DB" ".backup '$DEST/booking-$STAMP.db'"
else
  # Fall back to a plain copy — stop the application first.
  cp "$DB" "$DEST/booking-$STAMP.db"
fi

echo "Backup written to $DEST/booking-$STAMP.db"
