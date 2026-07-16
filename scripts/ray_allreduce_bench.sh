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

OUT_DIR="${OUT_DIR:-results}"
PORT="${ALLREDUCE_PORT:-29517}"

if [[ -z "${CLUSTER_RUNTIME_FINGERPRINT:-}" ]]; then
  cluster_runtime_json="$(docker exec -i "$VLLM_CONTAINER" python3 - \
    "$HEAD_IP" "$HOEDUS_IP" "$VLLM_IMAGE" "$RAY_VERSION" \
    < "$HERE/cluster_runtime_fingerprint.py")"
  CLUSTER_RUNTIME_FINGERPRINT="$(printf '%s' "$cluster_runtime_json" \
    | sha256sum | awk '{print $1}')"
fi
SOURCE_FINGERPRINT="$(sha256sum "$HERE/ray_allreduce_bench.py" \
  "$HERE/ray_allreduce_bench.sh" | sha256sum | awk '{print $1}')"
ARTIFACT_FINGERPRINT="$(printf '%s\n%s\n' "$CLUSTER_RUNTIME_FINGERPRINT" \
  "$SOURCE_FINGERPRINT" | sha256sum | awk '{print $1}')"
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
  < "$HERE/ray_allreduce_bench.py" | tee "$TMP"

# Only a successful complete pipeline becomes a resumable campaign artifact.
# A failed docker/Ray/NCCL run leaves no allreduce_ray_*.csv for run_job.sh to
# mistake as completed on its next invocation.
mv "$TMP" "$LOG"
trap - EXIT

echo "[microbench] wrote $LOG"
echo "[microbench] busbw at 1GiB ~ sustained link bandwidth; min_s at 8B ~ per-collective latency floor (feeds the TP2 per-layer cost model)"
