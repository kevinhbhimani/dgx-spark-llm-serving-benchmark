#!/usr/bin/env bash
# Two-node all-reduce microbenchmark through the Ray cluster (no SSH needed).
# Companion to interconnect_microbench.sh, which requires mpirun+SSH this
# cluster does not have. Run on the head node with Ray up on both nodes
# (scripts/start_ray.sh head|worker) and NO model being served.
#
# Writes results to $OUT_DIR/allreduce_ray_<timestamp>.csv so the interconnect
# numbers live beside the serving results they explain.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"
require_local_connectx_ip "$HEAD_IP" "all-reduce benchmark head role" || exit 1
acquire_campaign_lock || exit 1

OUT_DIR="${OUT_DIR:-results}"
PORT="${ALLREDUCE_PORT:-29517}"
# Floor calibrated on this cluster 2026-07-16: NCCL socket transport
# (NCCL_IB_DISABLE=1) over the 200GbE ConnectX link plateaus at ~2.08 GB/s
# from 8 MiB to 1 GiB, so 1.8 accepts the healthy link and still fails a
# degraded one. Keep in sync with ray_allreduce_bench.py/validate_campaign.py.
MIN_1G_BUSBW="${ALLREDUCE_MIN_1G_BUSBW_GBPS:-1.8}"
MAX_8B_LATENCY="${ALLREDUCE_MAX_8B_LATENCY_S:-0.005}"
if ! python3 - "$PORT" "$MIN_1G_BUSBW" "$MAX_8B_LATENCY" <<'PY_VALIDATE'
import math
import sys

try:
    port = int(sys.argv[1])
    values = [float(value) for value in sys.argv[2:]]
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if 1 <= port <= 65535
                 and all(math.isfinite(value) and value > 0 for value in values)
                 else 1)
PY_VALIDATE
then
  echo "[microbench] invalid port or acceptance threshold" >&2
  exit 2
fi

if [[ -z "${CLUSTER_RUNTIME_FINGERPRINT:-}" ]]; then
  cluster_runtime_json="$(docker exec -i "$VLLM_CONTAINER" python3 - \
    "$HEAD_IP" "$HOEDUS_IP" "$VLLM_IMAGE" "$RAY_VERSION" \
    < "$HERE/cluster_runtime_fingerprint.py")"
  CLUSTER_RUNTIME_FINGERPRINT="$(printf '%s' "$cluster_runtime_json" \
    | sha256sum | awk '{print $1}')"
fi
SOURCE_FINGERPRINT="$(sha256sum "$HERE/ray_allreduce_bench.py" \
  "$HERE/ray_allreduce_bench.sh" | sha256sum | awk '{print $1}')"
ARTIFACT_FINGERPRINT="$(printf '%s\n' "$CLUSTER_RUNTIME_FINGERPRINT" \
  "$SOURCE_FINGERPRINT" "$PORT" "$CX_IFACE" "$NCCL_IB_DISABLE" \
  "$MIN_1G_BUSBW" "$MAX_8B_LATENCY" | sha256sum | awk '{print $1}')"
if [[ "${1:-}" == "--fingerprint-only" ]]; then
  printf '%s\n' "$ARTIFACT_FINGERPRINT"
  exit 0
fi

if docker exec "$VLLM_CONTAINER" pgrep -f "vllm serve" >/dev/null 2>&1; then
  echo "[microbench] a vLLM server is running in $VLLM_CONTAINER; stop it" \
       "first, the GPU must be idle for a clean interconnect reading" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/allreduce_ray_${ARTIFACT_FINGERPRINT}_$(date +%Y%m%d-%H%M%S).csv"
TMP="$LOG.tmp.$$"
trap 'rm -f "$TMP"' EXIT
docker exec -i \
  -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE" \
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG" \
  "$VLLM_CONTAINER" python3 -u - "$HEAD_IP" "$HOEDUS_IP" "$CX_IFACE" "$PORT" \
  "$MIN_1G_BUSBW" "$MAX_8B_LATENCY" \
  < "$HERE/ray_allreduce_bench.py" | tee "$TMP"

# Only a successful complete pipeline becomes a resumable campaign artifact.
# A failed docker/Ray/NCCL run leaves no allreduce_ray_*.csv for run_job.sh to
# mistake as completed on its next invocation.
mv "$TMP" "$LOG"
trap - EXIT

echo "[microbench] wrote $LOG"
echo "[microbench] accepted: 1GiB busbw >= $MIN_1G_BUSBW GB/s and 8B min_s <= $MAX_8B_LATENCY s"
echo "[microbench] busbw at 1GiB approximates sustained link bandwidth; 8B min_s is the collective latency floor"
