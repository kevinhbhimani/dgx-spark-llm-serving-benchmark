#!/usr/bin/env bash
# Confirm cross node traffic is on ConnectX and not the campus path. Run on Diya.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

echo "[verify] name resolution should show 10.10.10.x"
getent hosts "$DIYA_HOST" "$HOEDUS_HOST"

echo "[verify] route to Hoedus should leave via $CX_IFACE"
ip route get "$HOEDUS_IP"

echo "[verify] latency to Hoedus should be near 1 ms, not 64 to 132 ms"
ping -c 5 "$HOEDUS_HOST"

echo "[verify] note the $CX_IFACE byte counters, rerun during a tp2 sweep, they should climb"
ip -s link show "$CX_IFACE" | sed -n '1,6p'
