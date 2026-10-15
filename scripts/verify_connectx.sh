#!/usr/bin/env bash
# Confirm cross node traffic is on ConnectX and not the campus path. Run on Diya.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

echo "[verify] name resolution should show 10.10.10.x"
getent hosts "$DIYA_HOST" "$HOEDUS_HOST"

echo "[verify] route to Hoedus"
ip route get "$HOEDUS_IP"

echo "[verify] latency to each subnet peer (expect < 2 ms)"
for iface in "${_cx_ifaces[@]}"; do
  addr=$(ip -4 -o addr show "$iface" 2>/dev/null \
    | awk 'NR == 1 {split($4, a, "/"); print a[1]}')
  if [[ -z "$addr" ]]; then
    echo "[verify] no IPv4 address on $iface" >&2
    exit 1
  fi
  # Netplan convention: Diya takes the odd last octet, Hoedus the next even one.
  peer=$(awk -F. 'BEGIN{OFS="."} {if ($4 % 2 == 0) {$4 -= 1} else {$4 += 1} print}' <<<"$addr")
  echo "--- $iface -> $peer ---"
  ping -c 3 "$peer"
done

echo "[verify] byte counters per interface (rerun during a tp2 sweep to confirm traffic)"
IFS=',' read -ra _cx_ifaces <<< "$CX_IFACE"
for iface in "${_cx_ifaces[@]}"; do
  echo "--- $iface ---"
  ip -s link show "$iface" | sed -n '1,6p'
done
