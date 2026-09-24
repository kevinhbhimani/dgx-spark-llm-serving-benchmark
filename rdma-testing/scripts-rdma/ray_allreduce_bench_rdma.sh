#!/usr/bin/env bash
# =============================================================================
# scripts-rdma/ray_allreduce_bench_rdma.sh
#
# MINIMALLY MODIFIED COPY of scripts/ray_allreduce_bench.sh
# Changes from original:
#   - Sources connectx_env_rdma.sh instead of connectx_env.sh
#   - Added RDMA NCCL env vars passed to docker exec
#   - Defaults NCCL_IB_DISABLE=0 (instead of 1)
# =============================================================================
set -euo pipefail

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHANGE: Source RDMA env
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env_rdma.sh"
require_local_connectx_ip "$HEAD_IP" "all-reduce benchmark head role" || exit 1
acquire_campaign_lock || exit 1

OUT_DIR="${OUT_DIR:-results}"
PORT="${ALLREDUCE_PORT:-29517}"
MIN_1G_BUSBW="${ALLREDUCE_MIN_1G_BUSBW_GBPS:-1.8}"
MAX_8B_LATENCY="${ALLREDUCE_MAX_8B_LATENCY_S:-0.005}"

if ! python3 - "$PORT" "$MIN_1G_BUSBW" "$MAX_8B_LATENCY" <<'PY_VALIDATE'
import math, sys
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
    < "$HERE/../../scripts/cluster_runtime_fingerprint.py")"
  CLUSTER_RUNTIME_FINGERPRINT="$(printf '%s' "$cluster_runtime_json" \
    | sha256sum | awk '{print $1}')"
fi
SOURCE_FINGERPRINT="$(sha256sum "$HERE/../../scripts/ray_allreduce_bench.py" \
  "$HERE/../../scripts/ray_allreduce_bench.sh" | sha256sum | awk '{print $1}')"
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHANGE: Added RDMA NCCL env vars to docker exec
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
docker exec -i \
  -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE" \
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG" \
  -e NCCL_IB_HCA="$NCCL_IB_HCA" -e NCCL_IB_GID_INDEX="$NCCL_IB_GID_INDEX" \
  -e NCCL_IB_TC="$NCCL_IB_TC" -e NCCL_IB_QPS_PER_CONNECTION="$NCCL_IB_QPS_PER_CONNECTION" \
  -e NCCL_IB_TIMEOUT="$NCCL_IB_TIMEOUT" -e NCCL_NET_GDR_LEVEL="$NCCL_NET_GDR_LEVEL" \
  "$VLLM_CONTAINER" python3 -u - "$HEAD_IP" "$HOEDUS_IP" "$CX_IFACE" "$PORT" \
  "$MIN_1G_BUSBW" "$MAX_8B_LATENCY" \
  < "$HERE/../../scripts/ray_allreduce_bench.py" | tee "$TMP"

mv "$TMP" "$LOG"
trap - EXIT

echo "[microbench] wrote $LOG"
echo "[microbench] accepted: 1GiB busbw >= $MIN_1G_BUSBW GB/s and 8B min_s <= $MAX_8B_LATENCY s"
echo "[microbench] transport is determined by NCCL_IB_DISABLE=$NCCL_IB_DISABLE (0=RoCE, 1=TCP)"