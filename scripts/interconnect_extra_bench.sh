#!/usr/bin/env bash
# Supplementary two-node interconnect microbench: all-reduce AND send/recv
# (point-to-point) through the Ray cluster, no SSH needed. Companion to
# ray_allreduce_bench.sh; adds the PP-primitive (p2p) measurement and records
# the transport NCCL used, so the paper can contrast TP's all-reduce cost with
# PP's activation hand-off on the same link, and compare TCP sockets vs RoCE.
#
# Run on the head node with Ray up on BOTH nodes and NO model being served.
# The transport is whatever Ray was started with. RDMA is the default
# (NCCL_IB_DISABLE=0 in connectx_env.sh). To measure the TCP-socket fallback,
# restart Ray on both nodes with NCCL_IB_DISABLE=1 and rerun; each CSV
# carries its own transport column, so runs never overwrite each other:
#
#   scripts/start_ray.sh head            # run worker command on the other host
#   scripts/interconnect_extra_bench.sh                      # -> transport=roce
#   # then, the TCP fallback:
#   NCCL_IB_DISABLE=1 scripts/start_ray.sh head   # and on the worker
#   scripts/interconnect_extra_bench.sh                      # -> transport=tcp
#
# Writes result CSVs under $OUT_DIR/results/ and diagnostic streams under
# $OUT_DIR/logs/.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"
reject_solo_execution "interconnect microbenchmarking" || exit 2
source "$HERE/../utils/campaign_output.sh"
campaign_resolve_out_dir
campaign_reject_existing_implicit_out_dir || exit 1
require_local_connectx_ip "$HEAD_IP" "interconnect benchmark head role" || exit 1
if [[ "${1:-}" == "--fingerprint-only" ]]; then
  validate_local_topology_links >/dev/null || exit 1
else
  validate_local_topology_links || exit 1
fi
acquire_campaign_lock || exit 1

PORT="${INTERCONNECT_PORT:-29518}"
if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
  echo "[microbench] INTERCONNECT_PORT must be in [1, 65535]" >&2
  exit 2
fi
if [[ -z "${CLUSTER_RUNTIME_FINGERPRINT:-}" ]]; then
  cluster_runtime_json="$(docker exec -i "$VLLM_CONTAINER" python3 - \
    "$HEAD_IP" "$WORKER_IP" "$VLLM_IMAGE" "$RAY_VERSION" \
    "$TOPOLOGY_SHA256" "$HEAD_NODE_ID" "$WORKER_NODE_ID" \
    "$HEAD_CX_IFACE" "$WORKER_CX_IFACE" "$NO_IFACE_MATCH" \
    < "$HERE/cluster_runtime_fingerprint.py")"
  CLUSTER_RUNTIME_FINGERPRINT="$(printf '%s' "$cluster_runtime_json" \
    | sha256sum | awk '{print $1}')"
fi
SOURCE_FINGERPRINT="$(sha256sum "$HERE/ray_interconnect_bench.py" \
  "$HERE/interconnect_extra_bench.sh" | sha256sum | awk '{print $1}')"
ARTIFACT_FINGERPRINT="$(printf '%s\n' "$CLUSTER_RUNTIME_FINGERPRINT" \
  "$SOURCE_FINGERPRINT" "$PORT" "$TOPOLOGY_SHA256" "$NO_IFACE_MATCH" \
  "$HEAD_CX_IFACE" "$WORKER_CX_IFACE" "$NCCL_IB_DISABLE" \
  "${NCCL_IB_HCA:-}" "${NCCL_IB_GID_INDEX:-}" "${NCCL_NET_PLUGIN:-}" \
  | sha256sum | awk '{print $1}')"
if [[ "${1:-}" == "--fingerprint-only" ]]; then
  printf '%s\n' "$ARTIFACT_FINGERPRINT"
  exit 0
fi

wait_for_resources_idle required || exit 1

mkdir -p "$CAMPAIGN_RESULTS_DIR" "$CAMPAIGN_LOGS_DIR"
OUT="$CAMPAIGN_RESULTS_DIR/interconnect_extra_${ARTIFACT_FINGERPRINT}_$(date +%Y%m%d-%H%M%S).csv"
TMP="$OUT.tmp.$$"
RAW="$CAMPAIGN_LOGS_DIR/$(basename "${OUT%.csv}")_raw.log"
DIAG="$CAMPAIGN_LOGS_DIR/$(basename "${OUT%.csv}")_nccl.log"
trap 'rm -f "$TMP"' EXIT
# The driver env below is for ray.init on the head. The transport column is
# not read from this environment: each Ray worker enables NCCL_DEBUG=INFO for
# its own process and classifies the transport from NCCL's own "via NET/IB" /
# "via NET/Socket" log lines, so a RoCE request that silently falls back to
# sockets is labelled tcp rather than roce.
docker exec -i \
  -e NCCL_SOCKET_IFNAME="$HEAD_CX_IFACE" -e GLOO_SOCKET_IFNAME="$HEAD_CX_IFACE" \
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG" \
  "$VLLM_CONTAINER" python3 -u - "$HEAD_IP" "$WORKER_IP" \
  "$HEAD_CX_IFACE" "$WORKER_CX_IFACE" "$PORT" \
  < "$HERE/ray_interconnect_bench.py" 2> >(tee "$DIAG" >&2) | tee "$RAW"

# Ray writes cluster status and log-deduplication notices to stdout, the same
# stream the benchmark prints its CSV on, so the two interleave. Keep the whole
# stream for triage and filter the artifact down to header plus data rows:
# unfiltered, those stray lines are parsed as data and land in the transport
# column, which silently corrupts any plot built from the file.
grep -E '^(collective|allreduce|sendrecv),' "$RAW" > "$TMP" || true
for want in allreduce sendrecv; do
  if ! grep -q "^${want}," "$TMP"; then
    echo "[microbench] no $want rows in the output; keeping the raw stream at" \
         "$RAW for triage" >&2
    exit 1
  fi
done
expected_transport=roce
if [[ "$NCCL_IB_DISABLE" == "1" ]]; then expected_transport=tcp; fi
validator_python="${PYTHON:-$HERE/../.venv/bin/python}"
if [[ -z "${PYTHON:-}" && ! -x "$validator_python" ]]; then validator_python=python3; fi
"$validator_python" "$HERE/validate_microbench.py" interconnect "$TMP" \
  --expected-transport "$expected_transport"

# Only a complete run becomes an artifact; a failed docker/Ray/NCCL run leaves
# no partial CSV behind.
mv "$TMP" "$OUT"
trap - EXIT
echo "[microbench] wrote $OUT"
echo "[microbench] rows carry a 'collective' (allreduce|sendrecv) and"
echo "[microbench] 'transport' (tcp|tcp-tuned-xN|roce) column for the paper plot"
