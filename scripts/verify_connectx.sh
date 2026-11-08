#!/usr/bin/env bash
# Verify local interfaces, routes, source addresses, peers, and requested RDMA.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

echo "[verify] local node $LOCAL_NODE_ID, configured head $HEAD_NODE_ID, topology $TOPOLOGY_SHA256"
validate_local_topology_links

IFS=',' read -ra ifaces <<<"$CX_IFACE"
for iface in "${ifaces[@]}"; do
  echo "--- $iface ---"
  ip -s link show "$iface" | sed -n '1,6p'
done

if [[ "$NCCL_IB_DISABLE" == "0" ]]; then
  echo "[verify] active RDMA ports for every configured rail"
  rdma link show
else
  echo "[verify] TCP transport selected; route and peer checks completed without the RDMA gate"
fi
