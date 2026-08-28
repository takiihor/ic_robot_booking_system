#!/usr/bin/env bash
# Open the booking system's port to the local network only.
#
#   sudo ./deploy/allow-lan-access.sh
#
# ufw defaults to DROP on input, so the port must be opened explicitly.
# The rule is scoped to the server's own connected subnet, NOT the whole
# internet: 158.132.x.x is a public address and the app has no login, so an
# unscoped rule would expose applicant names, SIDs, emails and phone numbers.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="$(grep -E '^BOOKING_PORT=' "$APP_DIR/deploy/booking.env" | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8757}"

# The subnet of the interface holding the default LAN address.
IFACE="$(ip -4 -o addr show scope global | awk '$2 !~ /^(docker|br-|tun|tailscale)/ {print $2; exit}')"
CIDR="$(ip -4 -o addr show dev "$IFACE" scope global | awk '{print $4; exit}')"
SUBNET="$(python3 -c "import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))" "$CIDR")"

echo "Interface : $IFACE"
echo "Subnet    : $SUBNET"
echo "Port      : $PORT"
echo

ufw allow from "$SUBNET" to any port "$PORT" proto tcp comment "robot booking (LAN only)"
ufw reload

echo
echo "--- ufw rules for $PORT ---"
ufw status numbered | grep -E "(^Status|$PORT)" || true

echo
echo "--- reachability from the server ---"
if curl -sf -o /dev/null "http://127.0.0.1:$PORT/healthz"; then
  echo "  app is up"
else
  echo "  WARNING: app is not responding on 127.0.0.1:$PORT" >&2
fi

IP="$(ip -4 -o addr show dev "$IFACE" scope global | awk '{split($4,a,"/"); print a[1]; exit}')"
echo
echo "Staff on $SUBNET can now open:"
echo "    http://$IP:$PORT"
