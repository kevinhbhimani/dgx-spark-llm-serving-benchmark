#!/usr/bin/env bash
# Launch the vLLM OpenAI compatible server in single, tp2, or pp2 mode on the two
# node DGX Spark cluster, using the NVIDIA vLLM container. Run on the head node.
# For tp2 and pp2, start Ray on both nodes first with scripts/start_ray.sh, which
# brings up the shared container; this script then execs the server into it.
#
# Optional env vars:
#   DROP_CACHES=1   flush the page cache before launch (requires sudo); recommended
#                   before large model loads on Grace unified memory to avoid false OOM.
#   TOKENIZER       override vLLM --tokenizer (defaults to the model tokenizer).
#   REVISION        pinned Hugging Face commit for model files.
#   TOKENIZER_REVISION pinned commit for tokenizer files (defaults to REVISION).
#   TRUST_REMOTE_CODE  non-empty adds --trust-remote-code (models with custom code).
#   GPU_UTIL        shared --gpu-memory-utilization target. Campaigns default
#                   to models.yaml gpu_util_default (0.80) and pass this
#                   environment override through planning and serving. The
#                   preflight checks the same fraction of each node's MemTotal.
#                   vLLM applies this limit per instance, so reserve each node
#                   and run one serving instance per node at a time.
#   MAX_NUM_SEQS    optional positive scheduler sequence limit. A model-specific
#                   value applies consistently to single, tp2, and pp2.
#   PREFIX_CACHING  1 re-enables vLLM prefix caching. Default off: the synthetic
#                   workload can repeat prompt slices, and cache hits skipped up
#                   to a third of prefill work in some models but not others,
#                   contaminating cross-model comparisons.
#   GENERATION_CONFIG  vLLM generation-config policy (default "vllm", which
#                   ignores model-repository generation_config.json defaults so
#                   every model sees the same request-controlled sampling setup).
#   TORCHINDUCTOR_AUTOGRAD_CACHE  defaults to 0. The pinned torch build can
#                   crash while serializing AOT graphs for on-the-fly FP8;
#                   disabling only that disk cache leaves compilation enabled.
#   FLASHINFER_AUTOTUNE  0 (default) passes --no-enable-flashinfer-autotune in
#                   every mode; 1 keeps vLLM's default (autotune on). vLLM
#                   0.29.0 averages autotune timings over the world group, so
#                   pp2 deadlocks at startup when its two stages tune different
#                   kernels (vllm#57197, fixed in 0.32.0). One value for all
#                   configs keeps single/tp2/pp2 kernel selection comparable.
#   HF_HUB_OFFLINE  1 (default) serves from the local HF cache. The worker cache
#                   receives pinned model files from the head-side mirror. Set
#                   HF_HUB_OFFLINE=0 only after a model is available locally.
#   SINGLE_CONTAINER  where `single` runs. "standalone" (default) starts its own
#                   container named $VLLM_CONTAINER, which replaces a running Ray
#                   head. "ray" execs the TP=1 PP=1 server into the running Ray
#                   head container instead, so the cluster survives and a
#                   campaign can interleave single with tp2/pp2 launches (the
#                   worker cannot be restarted from the head).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

CONFIG="${1:-single}"          # single, tp2, pp2
MODEL="${MODEL:-Qwen/Qwen3-8B}"
REVISION="${REVISION:-}"
PORT="${PORT:-8000}"
BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
# The benchmark client always runs on the head, so the API listens on loopback
# only; HOST=0.0.0.0 exposes it for a remote client.
HOST="${HOST:-127.0.0.1}"
DTYPE="${DTYPE:-auto}"
QUANT="${QUANT:-}"             # for example fp8; empty means none
TOKENIZER="${TOKENIZER:-}"     # optional tokenizer id/path; empty means model default
TOKENIZER_REVISION="${TOKENIZER_REVISION:-}"
if [[ -z "$TOKENIZER_REVISION" \
      && ( -z "$TOKENIZER" || "$TOKENIZER" == "$MODEL" ) ]]; then
  TOKENIZER_REVISION="$REVISION"
fi
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-}"  # non-empty adds --trust-remote-code
# vLLM max model length is prompt + generated tokens. The default experiment's
# largest bucket is 8192 input + 512 output, so use at least 8704 by default.
MAXLEN="${MAXLEN:-8704}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-}"
if [[ -n "$MAX_NUM_SEQS" && ! "$MAX_NUM_SEQS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[launch] MAX_NUM_SEQS must be a positive integer" >&2
  exit 2
fi
GPU_UTIL="${GPU_UTIL:-0.80}"
if ! python3 - "$GPU_UTIL" <<'PY'
import math
import sys

try:
    value = float(sys.argv[1])
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if math.isfinite(value) and 0 < value <= 1 else 1)
PY
then
  echo "[launch] GPU_UTIL must satisfy 0 < value <= 1" >&2
  exit 2
fi
PREFIX_CACHING="${PREFIX_CACHING:-0}"
GENERATION_CONFIG="${GENERATION_CONFIG:-vllm}"
TORCHINDUCTOR_AUTOGRAD_CACHE="${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}"
FLASHINFER_AUTOTUNE="${FLASHINFER_AUTOTUNE:-0}"
case "$FLASHINFER_AUTOTUNE" in 0|1) ;; *)
  echo "[launch] FLASHINFER_AUTOTUNE must be 0 or 1, got '$FLASHINFER_AUTOTUNE'" >&2
  exit 2 ;;
esac
SINGLE_CONTAINER="${SINGLE_CONTAINER:-standalone}"
case "$SINGLE_CONTAINER" in standalone|ray) ;; *)
  echo "[launch] SINGLE_CONTAINER must be standalone or ray, got '$SINGLE_CONTAINER'" >&2
  exit 1 ;;
esac

# Serve from the local HF cache by default; the worker node cannot reach
# huggingface.co, so online metadata lookups only add slow, failing retries.
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
hf_cache_env=( -e HF_HOME=/hf-cache -e HF_HUB_CACHE=/hf-cache/hub
  -e HF_MODULES_CACHE=/hf-cache/modules
  -e HF_HUB_DISABLE_SHARED_BLOBS="$HF_HUB_DISABLE_SHARED_BLOBS" )
hf_offline_env=( -e HF_HUB_OFFLINE="$HF_HUB_OFFLINE"
  -e TRANSFORMERS_OFFLINE="$HF_HUB_OFFLINE" )
container_dns_args=()
if [[ -n "$CONTAINER_DNS" ]]; then
  container_dns_args=( --dns "$CONTAINER_DNS" )
fi

# --language-model-only: the workload is text-only. Vision-language checkpoints
# (Qwen3.6, Gemma 4, Mistral Medium 3.5) would otherwise reserve memory for
# their vision encoder and its profiling, shrinking their KV cache relative to
# text-only models. vLLM ignores the flag for text-only architectures.
common=( --host "$HOST" --port "$PORT" --dtype "$DTYPE"
         --max-model-len "$MAXLEN" --gpu-memory-utilization "$GPU_UTIL"
         --generation-config "$GENERATION_CONFIG"
         --language-model-only
         --no-enable-log-requests )
if [[ "$PREFIX_CACHING" != "1" ]]; then
  common+=( --no-enable-prefix-caching )
fi
if [[ "$FLASHINFER_AUTOTUNE" != "1" ]]; then
  common+=( --no-enable-flashinfer-autotune )
fi
if [[ -n "$QUANT" ]]; then common+=( --quantization "$QUANT" ); fi
if [[ -n "$MAX_NUM_SEQS" ]]; then common+=( --max-num-seqs "$MAX_NUM_SEQS" ); fi
if [[ -n "$TOKENIZER" ]]; then common+=( --tokenizer "$TOKENIZER" ); fi
if [[ -n "$REVISION" ]]; then common+=( --revision "$REVISION" ); fi
if [[ -n "$TOKENIZER_REVISION" ]]; then
  common+=( --tokenizer-revision "$TOKENIZER_REVISION" )
fi
if [[ -n "$TRUST_REMOTE_CODE" ]]; then common+=( --trust-remote-code ); fi

require_local_connectx_ip "$HEAD_IP" "serving head role" || exit 1
validate_local_topology_links || exit 1
acquire_resource_lock || exit 1
if [[ "$CONFIG" == "single" ]]; then
  wait_for_resources_idle auto || exit 1
else
  wait_for_resources_idle required || exit 1
fi
echo "[launch] $CONFIG  model=$MODEL  image=$VLLM_IMAGE  iface=$CX_IFACE  head=$HEAD_IP"

# A direct launcher holds the shared lock through startup, then releases it
# once the API is ready so a separate benchmark client can use the server. A
# caller such as run_models.sh already owns the lock and keeps it through the
# full experiment.
run_server_command() {
  if [[ "${RESOURCE_LOCK_INHERITED:-0}" == "1" ]]; then
    "$@"
    return $?
  fi

  local timeout="${LAUNCH_READY_TIMEOUT:-${READY_TIMEOUT:-1800}}"
  local waited=0 server_pid=0 server_status=0
  if [[ ! "$timeout" =~ ^[1-9][0-9]*$ ]]; then
    echo "[launch] LAUNCH_READY_TIMEOUT must be a positive integer" >&2
    return 2
  fi
  "$@" &
  server_pid=$!
  echo "[launch] waiting for $BASE_URL/v1/models before releasing the resource lock"
  while (( waited < timeout )); do
    if curl -sf --max-time 1 "$BASE_URL/v1/models" >/dev/null 2>&1; then
      flock -u "$CAMPAIGN_LOCK_FD"
      export CAMPAIGN_LOCK_HELD=0
      echo "[launch] API ready; released the resource lock for benchmark clients"
      wait "$server_pid" || server_status=$?
      return "$server_status"
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
      wait "$server_pid" || server_status=$?
      if (( server_status == 0 )); then
        echo "[launch] server command exited before the API became ready" >&2
        return 1
      fi
      return "$server_status"
    fi
    sleep 5
    waited=$((waited + 5))
  done

  echo "[launch] timed out after ${timeout}s waiting for the API; stopping the launch" >&2
  if [[ "$CONFIG" == "single" && "$SINGLE_CONTAINER" == "standalone" ]]; then
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
  else
    docker exec "$VLLM_CONTAINER" pkill -f "vllm serve" >/dev/null 2>&1 || true
  fi
  kill -TERM "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  local worker_scope=auto
  [[ "$CONFIG" != "single" ]] && worker_scope=required
  wait_for_resources_idle "$worker_scope" serving || true
  return 1
}

# Optionally flush stale page cache before large model loads.
# Stale cache causes false OOM on Grace unified memory; requires sudo.
if [[ "${DROP_CACHES:-0}" == "1" ]]; then
  echo "[launch] flushing page cache (DROP_CACHES=1)"
  sync
  echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
  echo "[launch] page cache flushed"
fi

# A TP=1 server inside the Ray head container needs only that container and
# its Ray head; it does not need the worker node.
_require_ray_head() {
  if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
    echo "[launch] ERROR: Ray is not active in $VLLM_CONTAINER" >&2
    echo "[launch] Start it first: scripts/start_ray.sh head" >&2
    exit 1
  fi
  if ! validate_local_ray_container; then
    echo "[launch] local Ray head does not match the selected topology" >&2
    exit 1
  fi
}

# For distributed configs, confirm Ray is healthy before committing to the launch.
_require_ray() {
  if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
    echo "[launch] ERROR: Ray cluster is not active in $VLLM_CONTAINER" >&2
    echo "[launch] Start Ray on the configured head and worker first." >&2
    echo "[launch] Then verify with: docker exec $VLLM_CONTAINER ray status" >&2
    exit 1
  fi
  echo "[launch] Ray cluster OK:"
  docker exec "$VLLM_CONTAINER" ray status
  docker exec -i -e EXPECTED_HEAD="$HEAD_IP" -e EXPECTED_WORKER="$WORKER_IP" \
    "$VLLM_CONTAINER" python3 - <<'PY'
import os
import ray

ray.init(address="auto", log_to_driver=False)
alive = {n["NodeManagerAddress"]: n for n in ray.nodes() if n.get("Alive")}
expected = [os.environ["EXPECTED_HEAD"], os.environ["EXPECTED_WORKER"]]
problems = []
extra = sorted(set(alive) - set(expected))
if extra:
    problems.append("unexpected live Ray node(s): " + ", ".join(extra))
for ip in expected:
    node = alive.get(ip)
    if node is None:
        problems.append(f"missing Ray node {ip}")
    elif float((node.get("Resources") or {}).get("GPU", 0)) < 1:
        problems.append(f"Ray node {ip} advertises no GPU")
if problems:
    raise SystemExit("[launch] ERROR: " + "; ".join(problems))
print("[launch] expected two Ray GPU nodes present: " + ", ".join(expected))
PY
  validate_cluster_runtime || exit 1
}

# The head and worker mount separate local Hugging Face caches. Serving runs
# offline, so validate the exact pinned snapshot on both Ray nodes before vLLM
# spends minutes initializing distributed workers.
_check_cluster_cache() {
  echo "[launch] preflight: pinned model snapshot on both Ray nodes"
  docker exec -i \
    "${hf_cache_env[@]}" \
    -e HF_HUB_DISABLE_SHARED_BLOBS="$HF_HUB_DISABLE_SHARED_BLOBS" \
    -e EXPECTED_HEAD="$HEAD_IP" -e EXPECTED_WORKER="$WORKER_IP" \
    -e MODEL="$MODEL" -e REVISION="$REVISION" \
    -e TOKENIZER="$TOKENIZER" -e TOKENIZER_REVISION="$TOKENIZER_REVISION" \
    "$VLLM_CONTAINER" python3 - <<'PY'
import json
import os
import ray

ray.init(address="auto", log_to_driver=False)

def validate(repo, revision, require_weights):
    from pathlib import Path
    from huggingface_hub import snapshot_download
    path = Path(snapshot_download(
        repo, revision=revision or None, local_files_only=True,
        ignore_patterns=["original/*", "metal/*", "*.pth", "*.gguf"],
    ))
    if revision and path.name != revision:
        raise RuntimeError(f"resolved snapshot is {path.name}, expected {revision}")
    broken = [str(p.relative_to(path)) for p in path.rglob("*")
              if p.is_symlink() and not p.exists()]
    partial = list((path.parents[1] / "blobs").glob("*.incomplete"))
    missing = []
    if require_weights:
        indexes = list(path.glob("*.safetensors.index.json"))
        indexes += list(path.glob("*.bin.index.json"))
        for index in indexes:
            weights = json.loads(index.read_text()).get("weight_map", {}).values()
            missing.extend(name for name in set(weights)
                           if not (path / name).is_file())
        if not indexes and not (list(path.glob("*.safetensors"))
                                or list(path.glob("*.bin"))):
            missing.append("no model weight files found")
    if broken or partial or missing:
        raise RuntimeError(
            f"{repo}@{revision or 'main'} incomplete: {len(broken)} broken "
            f"link(s), {len(partial)} partial blob(s), missing={missing[:5]}")

def node_check(model, revision, tokenizer, tokenizer_revision):
    validate(model, revision, True)
    if tokenizer and tokenizer != model:
        validate(tokenizer, tokenizer_revision, False)
    return "ok"

alive = {n["NodeManagerAddress"]: n for n in ray.nodes() if n.get("Alive")}
for ip in (os.environ["EXPECTED_HEAD"], os.environ["EXPECTED_WORKER"]):
    if ip not in alive:
        raise SystemExit(f"[launch] CACHE PREFLIGHT FAILED: missing Ray node {ip}")
    options = {"resources": {f"node:{ip}": 0.001}, "num_cpus": 0}
    task = ray.remote(**options)(node_check)
    try:
        ray.get(task.remote(os.environ["MODEL"], os.environ["REVISION"],
                            os.environ["TOKENIZER"],
                            os.environ["TOKENIZER_REVISION"]), timeout=120)
    except Exception as exc:
        raise SystemExit(f"[launch] CACHE PREFLIGHT FAILED on {ip}: {exc}")
    print(f"[launch]   node {ip}: pinned cache OK")
PY
}

# Grace exposes one unified CPU/GPU memory pool. run_models.sh flushes host
# page cache after staging model files and before each server start. This guard
# also drops clean Hugging Face file pages for direct launcher calls.
_check_local_memory() {
  echo "[launch] preflight: local free memory vs GPU_UTIL=$GPU_UTIL"
  GPU_UTIL="$GPU_UTIL" HF_CACHE="$HF_CACHE" python3 - <<'PY'
import os

def meminfo():
    fields = {}
    with open("/proc/meminfo") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            fields[key] = int(value.split()[0]) * 1024
    return fields["MemFree"], fields["MemAvailable"], fields["MemTotal"]

util = float(os.environ["GPU_UTIL"])
free, available, total = meminfo()
need = util * total
if free < need <= available - (2 << 30):
    print(f"[launch]   free {free / 2**30:.1f} GiB, available "
          f"{available / 2**30:.1f} GiB: dropping clean HF file cache")
    cache = os.environ.get("HF_CACHE", "")
    for root, _dirs, files in os.walk(cache):
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                finally:
                    os.close(fd)
            except OSError:
                pass
    free, available, total = meminfo()
ok = free >= need
print(f"[launch]   free {free / 2**30:.1f} (available "
      f"{available / 2**30:.1f}) of {total / 2**30:.1f} GiB, "
      f"need {need / 2**30:.1f} GiB: {'ok' if ok else 'LOW'}")
if not ok:
    raise SystemExit(
        "[launch] PREFLIGHT FAILED: local memory remains low; rerun with "
        "DROP_CACHES=1 or free the owning process")
PY
}

# vLLM refuses to start when any node's free memory is below GPU_UTIL x total
# (on Grace unified memory, CUDA free tracks the host's MemFree, measured ~0.5
# GiB apart). Check every Ray node up front so the failure names the offending
# node, instead of surfacing minutes later as an EngineCore traceback from a
# remote worker. When a node is only low because of page cache (MemAvailable
# high, MemFree low — the false-OOM mode README troubleshooting describes),
# reclaim checkpoint pages directly on a pinned Ray task. If unrelated cache
# remains, fail rather than creating memory pressure across the shared node.
_check_cluster_memory() {
  echo "[launch] preflight: free memory on all Ray nodes vs GPU_UTIL=$GPU_UTIL"
  if ! docker exec -i "${hf_cache_env[@]}" -e GPU_UTIL="$GPU_UTIL" \
    "$VLLM_CONTAINER" python3 -u - <<'PY'
import os, sys
import ray

util = float(os.environ["GPU_UTIL"])
ray.init(address="auto", log_to_driver=False)

def meminfo():
    fields = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            fields[key] = int(value.split()[0]) * 1024
    return fields["MemFree"], fields["MemAvailable"], fields["MemTotal"]

def reclaim_cache(need):
    # Drop only clean checkpoint pages. Never allocate against the node just to
    # force general cache eviction: doing so can swap unrelated user processes.
    cache_root = os.environ.get("HF_HUB_CACHE", "/hf-cache/hub")
    for root, _dirs, files in os.walk(cache_root):
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                finally:
                    os.close(fd)
            except OSError:
                pass

low = []
for node in ray.nodes():
    if not node.get("Alive"):
        continue
    ip = node["NodeManagerAddress"]
    pin = {"resources": {f"node:{ip}": 0.001}, "num_cpus": 0}
    probe = ray.remote(**pin)(meminfo)
    free, avail, total = ray.get(probe.remote(), timeout=60)
    need = util * total
    if free < need <= avail - (2 << 30):
        print(f"[launch]   node {ip}: free {free / 2**30:.1f} GiB but available "
              f"{avail / 2**30:.1f} GiB is page cache; reclaiming")
        ray.get(ray.remote(**pin)(reclaim_cache).remote(int(need)), timeout=600)
        free, avail, total = ray.get(probe.remote(), timeout=60)
    ok = free >= need
    print(f"[launch]   node {ip}: free {free / 2**30:.1f} (available "
          f"{avail / 2**30:.1f}) of {total / 2**30:.1f} GiB, "
          f"need {need / 2**30:.1f} GiB: {'ok' if ok else 'LOW'}")
    if not ok:
        low.append(ip)
if low:
    print(f"[launch] PREFLIGHT FAILED: low free memory on {', '.join(low)}", file=sys.stderr)
    sys.exit(1)
PY
  then
    cat >&2 <<'MSG'
[launch] Clean HF checkpoint pages were dropped automatically, but memory is
[launch] still LOW. run_models.sh flushes host page cache before server launch.
[launch] For direct launches, restart the low node with DROP_CACHES=1 before retrying.
[launch] Find any live memory owner:
[launch]   free -g; ps aux --sort=-rss | head
[launch] If free stays low with no owning process and nvidia-smi shows no
[launch] processes, the driver leaked unified memory from a killed CUDA
[launch] process. Stop Ray there (scripts/start_ray.sh stop), then
[launch]   sudo rmmod nvidia_uvm && sudo modprobe nvidia_uvm
[launch] (or reboot the node), and restart Ray: scripts/start_ray.sh worker
MSG
    exit 1
  fi
}

case "$CONFIG" in
  single)
    check_hf_cache_access
    check_hf_cache_access_in_image
    if [[ "$SINGLE_CONTAINER" == "ray" ]]; then
      # Same image, mounts, and offline policy as the dual configs. With
      # TP=PP=1 vLLM uses its in-process executor and never touches Ray.
      echo "[launch] single node TP=1 PP=1 inside the Ray head container $VLLM_CONTAINER"
      _require_ray_head
      _check_local_memory
      if [[ "$HF_HUB_OFFLINE" != "1" && -n "${HF_TOKEN:-}" ]]; then
        hf_offline_env+=( -e HF_TOKEN )
      fi
      run_server_command docker exec --user "$HF_CACHE_UID:$HF_CACHE_GID" -e HOME=/tmp \
        "${hf_cache_env[@]}" "${hf_offline_env[@]}" \
        -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE" \
        "$VLLM_CONTAINER" /bin/bash -lc \
        'umask 0002; exec vllm serve "$@"' -- \
        "$MODEL" "${common[@]}" --tensor-parallel-size 1 --pipeline-parallel-size 1
      exit $?
    fi
    echo "[launch] single node TP=1 PP=1, standalone container"
    _check_local_memory
    run_env=( -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
              -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG"
              -e NCCL_IB_HCA="$NCCL_IB_HCA" -e NCCL_NET_PLUGIN="$NCCL_NET_PLUGIN"
              -e NCCL_IB_GID_INDEX="$NCCL_IB_GID_INDEX"
              -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE" )
    # Production serving is offline against a pinned, preflighted snapshot, so
    # keep registry credentials out of that container.  Preserve the documented
    # exploratory online mode, forwarding the variable by name (never its value
    # in the docker CLI argv) when the user explicitly opts into network access.
    if [[ "$HF_HUB_OFFLINE" != "1" && -n "${HF_TOKEN:-}" ]]; then
      run_env+=( -e HF_TOKEN )
    fi
    # Name the container so a sweep orchestrator can tear it down between models
    # (scripts/run_models.sh). Single mode does not use the Ray container, so
    # reusing $VLLM_CONTAINER here is safe; clear any stale one first.
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    run_server_command docker run --rm --name "$VLLM_CONTAINER" --user "$HF_CACHE_UID:$HF_CACHE_GID" \
      --gpus all --network host --ipc=host \
      "${container_dns_args[@]}" \
      -e HOME=/tmp -v "$HF_CACHE":/hf-cache \
      "${run_env[@]}" "${hf_cache_env[@]}" "${hf_offline_env[@]}" \
      "$VLLM_IMAGE" /bin/bash -lc \
      'umask 0002; exec vllm serve "$@"' -- \
      "$MODEL" "${common[@]}" --tensor-parallel-size 1 --pipeline-parallel-size 1
    ;;
  tp2)
    echo "[launch] dual node tensor parallel TP=2 PP=1 over Ray in $VLLM_CONTAINER"
    _require_ray
    _check_cluster_cache
    _check_cluster_memory
    run_server_command docker exec "${hf_cache_env[@]}" "${hf_offline_env[@]}" \
      -e HF_HUB_DISABLE_SHARED_BLOBS="$HF_HUB_DISABLE_SHARED_BLOBS" \
      -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE" \
      "$VLLM_CONTAINER" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 2 --pipeline-parallel-size 1 \
      --distributed-executor-backend ray
    ;;
  pp2)
    echo "[launch] dual node pipeline parallel TP=1 PP=2 over Ray in $VLLM_CONTAINER"
    _require_ray
    _check_cluster_cache
    _check_cluster_memory
    run_server_command docker exec "${hf_cache_env[@]}" "${hf_offline_env[@]}" \
      -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE" \
      "$VLLM_CONTAINER" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 1 --pipeline-parallel-size 2 \
      --distributed-executor-backend ray
    ;;
  *)
    echo "unknown config $CONFIG, use single, tp2, or pp2"; exit 1 ;;
esac
