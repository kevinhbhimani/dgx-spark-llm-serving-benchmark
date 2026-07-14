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
#   TRUST_REMOTE_CODE  non-empty adds --trust-remote-code (models with custom code).
#   GPU_UTIL        override --gpu-memory-utilization (default 0.90 single, 0.80 tp2/pp2).
#                   On Grace unified memory, GPU util shares the same pool as host
#                   memory, so tp2/pp2 default lower to leave headroom for Ray's
#                   node memory monitor (see README troubleshooting).
#   HF_HUB_OFFLINE  1 (default) serves purely from the local HF cache. The worker
#                   node (Hoedus) has no DNS to huggingface.co, so an online launch
#                   burns minutes on doomed metadata retries before falling back to
#                   cache. Set HF_HUB_OFFLINE=0 only to pull a not-yet-cached model
#                   (predownload it first with scripts/predownload.sh).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

CONFIG="${1:-single}"          # single, tp2, pp2
MODEL="${MODEL:-Qwen/Qwen3-8B}"
PORT="${PORT:-8000}"
DTYPE="${DTYPE:-auto}"
QUANT="${QUANT:-}"             # for example fp8; empty means none
TOKENIZER="${TOKENIZER:-}"     # optional tokenizer id/path; empty means model default
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-}"  # non-empty adds --trust-remote-code
# vLLM max model length is prompt + generated tokens. The default experiment's
# largest bucket is 8192 input + 512 output, so use at least 8704 by default.
MAXLEN="${MAXLEN:-8704}"
if [[ "$CONFIG" == "tp2" || "$CONFIG" == "pp2" ]]; then
  GPU_UTIL="${GPU_UTIL:-0.80}"
else
  GPU_UTIL="${GPU_UTIL:-0.90}"
fi

# Serve from the local HF cache by default; the worker node cannot reach
# huggingface.co, so online metadata lookups only add slow, failing retries.
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
hf_offline_env=()
if [[ "$HF_HUB_OFFLINE" == "1" ]]; then
  hf_offline_env=( -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 )
fi

common=( --host 0.0.0.0 --port "$PORT" --dtype "$DTYPE"
         --max-model-len "$MAXLEN" --gpu-memory-utilization "$GPU_UTIL"
         --no-enable-log-requests )
if [[ -n "$QUANT" ]]; then common+=( --quantization "$QUANT" ); fi
if [[ -n "$TOKENIZER" ]]; then common+=( --tokenizer "$TOKENIZER" ); fi
if [[ -n "$TRUST_REMOTE_CODE" ]]; then common+=( --trust-remote-code ); fi

echo "[launch] $CONFIG  model=$MODEL  image=$VLLM_IMAGE  iface=$CX_IFACE  head=$HEAD_IP"

# Optionally flush stale page cache before large model loads.
# Stale cache causes false OOM on Grace unified memory; requires sudo.
if [[ "${DROP_CACHES:-0}" == "1" ]]; then
  echo "[launch] flushing page cache (DROP_CACHES=1)"
  sync
  echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
  echo "[launch] page cache flushed"
fi

# For distributed configs, confirm Ray is healthy before committing to the launch.
_require_ray() {
  if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
    echo "[launch] ERROR: Ray cluster is not active in $VLLM_CONTAINER" >&2
    echo "[launch] Start Ray first: scripts/start_ray.sh head  (Diya)" >&2
    echo "[launch]                  scripts/start_ray.sh worker (Hoedus)" >&2
    echo "[launch] Then verify with: docker exec $VLLM_CONTAINER ray status" >&2
    exit 1
  fi
  echo "[launch] Ray cluster OK:"
  docker exec "$VLLM_CONTAINER" ray status
}

# vLLM refuses to start when any node's free memory is below GPU_UTIL x total
# (on Grace unified memory, CUDA free tracks the host's MemFree, measured ~0.5
# GiB apart). Check every Ray node up front so the failure names the offending
# node, instead of surfacing minutes later as an EngineCore traceback from a
# remote worker. When a node is only low because of page cache (MemAvailable
# high, MemFree low — the false-OOM mode README troubleshooting describes),
# reclaim it automatically: /proc/sys/vm/drop_caches is read-only inside the
# container and the worker has no SSH, so a pinned Ray task allocates and
# touches anonymous memory until the kernel evicts the cache, then releases it.
_check_cluster_memory() {
  echo "[launch] preflight: free memory on all Ray nodes vs GPU_UTIL=$GPU_UTIL"
  if ! docker exec -i -e GPU_UTIL="$GPU_UTIL" "$VLLM_CONTAINER" python3 -u - <<'PY'
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
    # Grow anonymous memory until MemFree plus what we hold covers `need`,
    # then free it; the kernel evicts clean page cache to feed the growth.
    # The 8 GiB reserve leaves room for other tenants of a shared node and
    # keeps usage under Ray's memory monitor kill threshold (95%).
    import gc
    free, avail, total = meminfo()
    cap = avail - (8 << 30)
    goal = need + (2 << 30)
    hold, held, chunk = [], 0, 1 << 30
    while held < cap and free + held < goal:
        buf = bytearray(chunk)
        for i in range(0, chunk, 4096):  # touch every page to force commit
            buf[i] = 1
        hold.append(buf)
        held += chunk
        free, avail, total = meminfo()
    del hold
    gc.collect()

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
[launch] Stale page cache is reclaimed automatically, so a LOW node means the
[launch] memory is genuinely held. On the low node, find the owner:
[launch]   free -g; ps aux --sort=-rss | head              # live process?
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
    echo "[launch] single node TP=1 PP=1, standalone container"
    run_env=( -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
              -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG" )
    if [[ -n "${HF_TOKEN:-}" ]]; then run_env+=( -e HF_TOKEN="$HF_TOKEN" ); fi
    # Name the container so a sweep orchestrator can tear it down between models
    # (scripts/run_models.sh). Single mode does not use the Ray container, so
    # reusing $VLLM_CONTAINER here is safe; clear any stale one first.
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run --rm --name "$VLLM_CONTAINER" --gpus all --network host --ipc=host \
      "${run_env[@]}" "${hf_offline_env[@]}" -v "$HF_CACHE":/root/.cache/huggingface \
      "$VLLM_IMAGE" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 1 --pipeline-parallel-size 1
    ;;
  tp2)
    echo "[launch] dual node tensor parallel TP=2 PP=1 over Ray in $VLLM_CONTAINER"
    _require_ray
    _check_cluster_memory
    docker exec "${hf_offline_env[@]}" "$VLLM_CONTAINER" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 2 --pipeline-parallel-size 1 \
      --distributed-executor-backend ray
    ;;
  pp2)
    echo "[launch] dual node pipeline parallel TP=1 PP=2 over Ray in $VLLM_CONTAINER"
    _require_ray
    _check_cluster_memory
    docker exec "${hf_offline_env[@]}" "$VLLM_CONTAINER" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 1 --pipeline-parallel-size 2 \
      --distributed-executor-backend ray
    ;;
  *)
    echo "unknown config $CONFIG, use single, tp2, or pp2"; exit 1 ;;
esac
