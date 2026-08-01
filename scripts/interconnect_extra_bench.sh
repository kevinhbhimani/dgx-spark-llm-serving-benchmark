#!/usr/bin/env bash
# Supplementary two-node interconnect microbench: all-reduce AND send/recv
# (point-to-point) through the Ray cluster, no SSH needed. Companion to
# ray_allreduce_bench.sh; adds the PP-primitive (p2p) measurement and records
# the transport NCCL used, so the paper can contrast TP's all-reduce cost with
# PP's activation hand-off on the same link, and compare TCP sockets vs RoCE.
#
# Run on the head node with Ray up on BOTH nodes and NO model being served.
# The transport is whatever Ray was started with; to compare, restart Ray on
# both nodes under each setting and rerun (each CSV carries its own transport
# column, so runs never overwrite each other):
#
#   scripts/start_ray.sh head            # Hoedus: scripts/start_ray.sh worker
#   scripts/interconnect_extra_bench.sh                      # -> transport=tcp
#   # then, RoCE:
#   NCCL_IB_DISABLE=0 scripts/start_ray.sh head   # and on Hoedus
#   scripts/interconnect_extra_bench.sh                      # -> transport=roce
#
# Writes $OUT_DIR/interconnect_extra_<timestamp>.csv beside the serving results.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"
require_local_connectx_ip "$HEAD_IP" "interconnect benchmark head role" || exit 1
acquire_campaign_lock || exit 1

OUT_DIR="${OUT_DIR:-results}"
PORT="${INTERCONNECT_PORT:-29518}"

if docker exec "$VLLM_CONTAINER" pgrep -f "vllm serve" >/dev/null 2>&1; then
  echo "[microbench] a vLLM server is running in $VLLM_CONTAINER; stop it" \
       "first, the GPU must be idle for a clean interconnect reading" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/interconnect_extra_$(date +%Y%m%d-%H%M%S).csv"
TMP="$OUT.tmp.$$"
RAW="$OUT.raw.$$"
trap 'rm -f "$TMP" "$RAW"' EXIT
# The driver env below is for ray.init on the head; the measured transport is
# the Ray *workers'* NCCL_IB_DISABLE (set by start_ray.sh), which the Python
# reads and writes into the transport column.
docker exec -i \
  -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE" \
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG" \
  "$VLLM_CONTAINER" python3 -u - "$HEAD_IP" "$HOEDUS_IP" "$CX_IFACE" "$PORT" \
  < "$HERE/ray_interconnect_bench.py" | tee "$RAW"

# Ray writes cluster status and log-deduplication notices to stdout, the same
# stream the benchmark prints its CSV on, so the two interleave. Keep the whole
# stream for triage and filter the artifact down to header plus data rows:
# unfiltered, those stray lines are parsed as data and land in the transport
# column, which silently corrupts any plot built from the file.
grep -E '^(collective|allreduce|sendrecv),' "$RAW" > "$TMP" || true
for want in allreduce sendrecv; do
  if ! grep -q "^${want}," "$TMP"; then
    echo "[microbench] no $want rows in the output; keeping the raw stream at" \
         "${OUT%.csv}_raw.log for triage" >&2
    mv "$RAW" "${OUT%.csv}_raw.log"
    exit 1
  fi
done

# Only a complete run becomes an artifact; a failed docker/Ray/NCCL run leaves
# no partial CSV behind.
mv "$TMP" "$OUT"
mv "$RAW" "${OUT%.csv}_raw.log"
trap - EXIT
echo "[microbench] wrote $OUT"
echo "[microbench] rows carry a 'collective' (allreduce|sendrecv) and"
echo "[microbench] 'transport' (tcp|tcp-tuned-xN|roce) column for the paper plot"
