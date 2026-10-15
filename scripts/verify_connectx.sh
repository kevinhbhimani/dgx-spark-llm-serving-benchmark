#!/usr/bin/env bash
# Confirm cross node traffic is on ConnectX and not the campus path. Run on Diya.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"
require_local_connectx_ip "$HEAD_IP" "ConnectX verification head role" || exit 1
IFS=',' read -ra ifaces <<< "$CX_IFACE"

echo "[verify] names must resolve to their configured ConnectX IPv4 addresses"
for spec in "$DIYA_HOST=$DIYA_IP" "$HOEDUS_HOST=$HOEDUS_IP"; do
  host="${spec%%=*}" expected="${spec#*=}"
  resolved="$(getent ahostsv4 "$host" | awk '{print $1}' | sort -u)"
  if [[ "$resolved" != "$expected" ]]; then
    echo "[verify] $host resolved to '${resolved:-nothing}', expected $expected" >&2
    echo "[verify] map this rendezvous name only to its primary ConnectX IP; remove secondary or campus aliases" >&2
    exit 1
  fi
  echo "[verify] $host -> $resolved"
done

echo "[verify] route to Hoedus"
route="$(ip -4 route get "$HOEDUS_IP")"
printf '%s\n' "$route"
route_iface="$(awk '{for(i=1;i<NF;i++) if($i=="dev") {print $(i+1); exit}}' <<< "$route")"
if [[ ",$CX_IFACE," != *",$route_iface,"* || -z "$route_iface" ]]; then
  echo "[verify] route to $HOEDUS_IP uses '${route_iface:-unknown}', outside CX_IFACE ($CX_IFACE)" >&2
  exit 1
fi

echo "[verify] latency to each subnet peer (expect < 2 ms)"
for iface in "${ifaces[@]}"; do
  addr=$(ip -4 -o addr show "$iface" 2>/dev/null \
    | awk 'NR == 1 {split($4, a, "/"); print a[1]}')
  if [[ -z "$addr" ]]; then
    echo "[verify] no IPv4 address on $iface" >&2
    exit 1
  fi
  # Netplan convention: Diya takes the odd last octet, Hoedus the next even one.
  peer=$(awk -F. 'BEGIN{OFS="."} {if ($4 % 2 == 0) {$4 -= 1} else {$4 += 1} print}' <<<"$addr")
  echo "--- $iface -> $peer ---"
  # Bind the probe to this port; otherwise a working alternate route can hide
  # a broken rail in a multi-interface configuration.
  ping -I "$iface" -W 2 -c 3 "$peer"
done

echo "[verify] byte counters per interface (rerun during a tp2 sweep to confirm traffic)"
for iface in "${ifaces[@]}"; do
  echo "--- $iface ---"
  ip -s link show "$iface" | sed -n '1,6p'
done

if [[ "$NCCL_IB_DISABLE" == "0" ]]; then
  echo "[verify] active RDMA ports for every configured ConnectX interface"
  require_connectx_rdma || exit 1
  rdma link show
fi
