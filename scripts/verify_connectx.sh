#!/usr/bin/env bash
# Confirm cross node traffic is on ConnectX and not the campus path. Run on Diya.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

echo "[verify] name resolution should show 10.10.10.x"
getent hosts "$DIYA_HOST" "$HOEDUS_HOST"

echo "[verify] route to Hoedus"
ip route get "$HOEDUS_IP"

echo "[verify] latency to Hoedus (expect < 2 ms)"
ping -c 5 "$HOEDUS_HOST"

echo "[verify] byte counters per interface (rerun during a tp2 sweep to confirm traffic)"
IFS=',' read -ra _cx_ifaces <<< "$CX_IFACE"
for iface in "${_cx_ifaces[@]}"; do
  echo "--- $iface ---"
  ip -s link show "$iface" | sed -n '1,6p'
done
