#!/usr/bin/env bash
# Benchmark several models for ONE serving config, launching and tearing down a
# vLLM server per model. Reads configs/models.yaml (see MODELS_YAML).
#
#   scripts/run_models.sh single        # every model whose configs include single
#   scripts/run_models.sh tp2           # dual-node tensor parallel (Ray must be up)
#   scripts/run_models.sh pp2           # dual-node pipeline parallel (Ray must be up)
#   scripts/run_models.sh single --skip-done   # skip work already in index.csv
#
# --skip-done (or SKIP_DONE=1) consults OUT_DIR/index.csv before each model and
# runs only missing (repeat, concurrency) pairs with matching methodology,
# config, harness, client-runtime, serving-policy, and live-deployment
# fingerprints. A fully covered model is skipped without launching its server.
#
# For tp2/pp2, start Ray on both nodes first (scripts/start_ray.sh head|worker);
# this script cycles only the `vllm serve` process between models and leaves the
# Ray cluster running. For single it launches and removes a standalone container.
#
# Each actual launch writes a new no-clobber server log whose name includes the
# serving-fingerprint prefix and UTC launch timestamp. The log and an immutable
# canonical serving-policy JSON are passed to the benchmark so runtime identity
# and exact capacity figures remain auditable. Results append to
# $OUT_DIR/index.csv tagged with the model label. Use a fresh OUT_DIR when the
# index schema has changed between versions (the benchmark refuses to append to
# a mismatched header).
#
# Env: MODELS_YAML BASE_URL OUT_DIR POWER_BACKEND CONCURRENCIES CONFIG_YAML
#      REPEATS (independent server launches for spread, default 3)
#      READY_TIMEOUT (seconds to wait for a model to come up, default 1800;
#      large models on tp2/pp2 can take >20 min to load weights + capture graphs)
#      PROBE_TIMEOUT (seconds for the strict two-token streaming canary, default 600)
#      SKIP_DONE (1 behaves like --skip-done, default 0)
#      CANARY_ONLY=1 launches each selected model, validates the exact streaming
#      contract, and tears it down without starting the long measurement sweep.
#      CACHE_PREFLIGHT=1 (default) requires every selected pinned snapshot to be
#      locally complete before launching the first server.
#      MODEL_LABELS (optional space-separated label allowlist for targeted
#      canaries or reruns; default is every entry for the serving config).
#      ONLY_REPEAT (optional replicate id: run only that replicate's missing
#      concurrencies on a fresh launch; rows still record repeat_count=REPEATS).
#      STATUS_ONLY=1 reports each model's missing repeat:concurrency pairs
#      without a cache check or launch; exits 3 when anything is missing.
#      SINGLE_CONTAINER (standalone|ray, see launch_server.sh; default
#      standalone). "ray" keeps the Ray cluster alive across single launches.
#      PYTHON (interpreter for the benchmark and helpers, default python3).
# Per-model tokenizer defaults to the model name unless configs/models.yaml
# supplies an explicit tokenizer.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"     # VLLM_CONTAINER, HEAD_IP, and .env

CONFIG="${1:-single}"
case "$CONFIG" in single|tp2|pp2) ;; *)
  echo "usage: run_models.sh [single|tp2|pp2] [--skip-done]" >&2; exit 1 ;;
esac

require_local_connectx_ip "$HEAD_IP" "model benchmark head role" || exit 1
acquire_campaign_lock || exit 1

SKIP_DONE="${SKIP_DONE:-0}"
for arg in "${@:2}"; do
  case "$arg" in
    --skip-done) SKIP_DONE=1 ;;
    *) echo "usage: run_models.sh [single|tp2|pp2] [--skip-done]" >&2; exit 1 ;;
  esac
done

MODELS_YAML="${MODELS_YAML:-configs/models.yaml}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
OUT_DIR="${OUT_DIR:-results}"
# Unset defers to the experiment YAML's power_backend; set it to override.
POWER_BACKEND="${POWER_BACKEND:-}"
CONFIG_YAML="${CONFIG_YAML:-configs/experiment.yaml}"
CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
EXPECTED_CONCURRENCIES="${EXPECTED_CONCURRENCIES:-$CONCURRENCIES}"
REPEATS="${REPEATS:-3}"
READY_TIMEOUT="${READY_TIMEOUT:-1800}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-600}"
CACHE_PREFLIGHT="${CACHE_PREFLIGHT:-1}"
CANARY_ONLY="${CANARY_ONLY:-0}"
MODEL_LABELS="${MODEL_LABELS:-}"
ONLY_REPEAT="${ONLY_REPEAT:-}"
STATUS_ONLY="${STATUS_ONLY:-0}"
SINGLE_CONTAINER="${SINGLE_CONTAINER:-standalone}"
PYTHON="${PYTHON:-python3}"

case "$SINGLE_CONTAINER" in standalone|ray) ;; *)
  echo "[models] SINGLE_CONTAINER must be standalone or ray, got '$SINGLE_CONTAINER'" >&2
  exit 1 ;;
esac
for flag_name in SKIP_DONE CACHE_PREFLIGHT CANARY_ONLY STATUS_ONLY; do
  flag_value="${!flag_name}"
  if [[ "$flag_value" != "0" && "$flag_value" != "1" ]]; then
    echo "[models] $flag_name must be 0 or 1, got '$flag_value'" >&2
    exit 1
  fi
done
# A canary is a real launch/contract check, never a resume query. An inherited
# SKIP_DONE=1 must not turn it into a no-op.
if [[ "$CANARY_ONLY" == "1" ]]; then
  SKIP_DONE=0
fi
if [[ ! "$REPEATS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[models] REPEATS must be a positive integer, got '$REPEATS'" >&2
  exit 1
fi
if [[ -n "$ONLY_REPEAT" ]]; then
  if [[ ! "$ONLY_REPEAT" =~ ^[1-9][0-9]*$ ]] || (( ONLY_REPEAT > REPEATS )); then
    echo "[models] ONLY_REPEAT must be an integer in 1..REPEATS ($REPEATS), got '$ONLY_REPEAT'" >&2
    exit 1
  fi
fi
if [[ "$STATUS_ONLY" == "1" && "$CANARY_ONLY" == "1" ]]; then
  echo "[models] STATUS_ONLY and CANARY_ONLY are mutually exclusive" >&2
  exit 1
fi
for timeout_name in READY_TIMEOUT PROBE_TIMEOUT; do
  timeout_value="${!timeout_name}"
  if [[ ! "$timeout_value" =~ ^[1-9][0-9]*$ ]]; then
    echo "[models] $timeout_name must be a positive integer, got '$timeout_value'" >&2
    exit 1
  fi
done
declare -A EXPECTED_SET=()
for c in $EXPECTED_CONCURRENCIES; do
  if [[ ! "$c" =~ ^[1-9][0-9]*$ || -n "${EXPECTED_SET[$c]:-}" ]]; then
    echo "[models] invalid or duplicate expected concurrency '$c'" >&2
    exit 1
  fi
  EXPECTED_SET[$c]=1
done
if [[ "${#EXPECTED_SET[@]}" -eq 0 ]]; then
  echo "[models] EXPECTED_CONCURRENCIES must not be empty" >&2
  exit 1
fi
declare -A REQUESTED_SET=()
for c in $CONCURRENCIES; do
  if [[ ! "$c" =~ ^[1-9][0-9]*$ || -n "${REQUESTED_SET[$c]:-}" ]]; then
    echo "[models] invalid or duplicate requested concurrency '$c'" >&2
    exit 1
  fi
  if [[ -z "${EXPECTED_SET[$c]:-}" ]]; then
    echo "[models] requested concurrency $c is outside EXPECTED_CONCURRENCIES" >&2
    exit 1
  fi
  REQUESTED_SET[$c]=1
done
if [[ "${#REQUESTED_SET[@]}" -eq 0 ]]; then
  echo "[models] CONCURRENCIES must not be empty" >&2
  exit 1
fi

if [[ ! "$VLLM_IMAGE" =~ @sha256:[0-9a-fA-F]{64}$ ]]; then
  echo "[models] VLLM_IMAGE must be pinned by @sha256 for a resumable sweep: $VLLM_IMAGE" >&2
  exit 1
fi

EFFECTIVE_POWER_BACKEND="$("$PYTHON" - "$CONFIG_YAML" "$POWER_BACKEND" <<'PY'
import sys
from bench.config import ExperimentConfig

cfg = ExperimentConfig.from_yaml(sys.argv[1])
if sys.argv[2]:
    cfg.power_backend = sys.argv[2]
if cfg.require_power_data and cfg.power_backend == "none":
    raise SystemExit("[models] full config requires power data; backend cannot be none")
if cfg.require_vllm_metrics_data and not cfg.scrape_vllm_metrics:
    raise SystemExit("[models] full config requires vLLM metrics scraping")
print(cfg.power_backend)
PY
)"

mkdir -p "$OUT_DIR"

# Refuse an incompatible index before loading a model for many minutes. The
# benchmark repeats this check per point, but orchestration must fail before GPU
# work begins.
"$PYTHON" -c \
  'import sys; from bench.run_benchmark import validate_index_schema; validate_index_schema(sys.argv[1])' \
  "$OUT_DIR"

# Select the models that request this serving config.
entries_text="$("$PYTHON" "$HERE/models_for_config.py" "$MODELS_YAML" "$CONFIG")" \
  || { status=$?; exit "$status"; }
ENTRIES=()
if [[ -n "$entries_text" ]]; then
  mapfile -t ENTRIES <<<"$entries_text"
fi
if [[ "${#ENTRIES[@]}" -eq 0 ]]; then
  echo "[models] no models request config '$CONFIG' in $MODELS_YAML" >&2
  exit 1
fi
if [[ -n "$MODEL_LABELS" ]]; then
  FILTERED=()
  for wanted in $MODEL_LABELS; do
    found=0
    for entry in "${ENTRIES[@]}"; do
      if [[ "${entry%%|*}" == "$wanted" ]]; then
        FILTERED+=("$entry")
        found=1
        break
      fi
    done
    if [[ "$found" != "1" ]]; then
      echo "[models] MODEL_LABELS requested unknown/inactive label '$wanted' for $CONFIG" >&2
      exit 1
    fi
  done
  ENTRIES=("${FILTERED[@]}")
fi
echo "[models] $CONFIG: ${#ENTRIES[@]} model(s) from $MODELS_YAML"

# Cache misses are a deterministic launch failure because serving is offline.
# Validate the complete selected matrix once, before spending time loading a GPU.
# A status query launches nothing, so it must work before weights are fetched.
if [[ "$CACHE_PREFLIGHT" == "1" && "$STATUS_ONLY" != "1" ]]; then
  CHECK_ONLY=1 MODEL_LABELS="$MODEL_LABELS" MODELS_YAML="$MODELS_YAML" \
    PYTHON="$PYTHON" "$HERE/predownload.sh" "$CONFIG"
fi

# Runtime identity is collected before any resume decision. Dual runs execute
# model code on both containers, so their existing canonical two-node snapshot
# remains unchanged. Single runs hash stable host facts instead of the former
# literal "standalone" marker, preventing a curve from spanning GPU, driver,
# clock-policy, or power-limit changes.
RUNTIME_SCOPE="single-host"
RUNTIME_FACTS_JSON=""
CLUSTER_RUNTIME_FINGERPRINT=""
if [[ "$CONFIG" == "single" ]]; then
  if ! RUNTIME_FACTS_JSON="$("$PYTHON" - "$SINGLE_CONTAINER" <<'PY'
import csv
import json
import subprocess
import sys

serving_container = sys.argv[1]

query = (
    ("gpu_name", "name"),
    ("gpu_uuid", "uuid"),
    ("driver", "driver_version"),
    ("power_limit_w", "power.limit"),
    ("compute_capability", "compute_cap"),
    ("max_graphics_clock_mhz", "clocks.max.graphics"),
    ("application_graphics_clock_mhz", "clocks.applications.graphics"),
    (
        "default_application_graphics_clock_mhz",
        "clocks.default_applications.graphics",
    ),
)
command = [
    "nvidia-smi",
    "--query-gpu=" + ",".join(field for _, field in query),
    "--format=csv,noheader,nounits",
]
try:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
    print(f"[models] single-host nvidia-smi identity query failed: {exc}",
          file=sys.stderr)
    raise SystemExit(1)

lines = [line for line in completed.stdout.splitlines() if line.strip()]
if len(lines) != 1:
    print(
        f"[models] expected exactly one host GPU, nvidia-smi returned {len(lines)}",
        file=sys.stderr,
    )
    raise SystemExit(1)
values = [value.strip() for value in next(csv.reader([lines[0]]))]
if len(values) != len(query):
    print(
        f"[models] nvidia-smi returned {len(values)} fields, expected {len(query)}",
        file=sys.stderr,
    )
    raise SystemExit(1)
unavailable = {"", "n/a", "[n/a]", "not supported", "[not supported]", "unknown"}
# GB10 legitimately reports N/A for some policy telemetry (notably the power
# limit). Preserve those exact values in the immutable runtime record while
# still failing closed on the fields needed to identify the GPU and driver.
required = {
    "gpu_name",
    "gpu_uuid",
    "driver",
    "compute_capability",
    "max_graphics_clock_mhz",
}
for (name, _), value in zip(query, values):
    if name in required and value.casefold() in unavailable:
        print(f"[models] required nvidia-smi field {name} is unavailable: {value!r}",
              file=sys.stderr)
        raise SystemExit(1)

facts = {
    "gpus": [dict((name, value) for (name, _), value in zip(query, values))],
    "query_fields": [field for _, field in query],
    # Which container hosts the TP=1 server. Part of the hashed runtime
    # identity, so standalone and in-Ray single rows never resume together.
    "serving_container": serving_container,
    "source": "host-nvidia-smi",
}
print(json.dumps(facts, sort_keys=True, separators=(",", ":")))
PY
)"; then
    echo "[models] single-host runtime identity preflight failed" >&2
    exit 1
  fi
else
  RUNTIME_SCOPE="dual-cluster"
  if ! cluster_runtime_json="$(docker exec -i "$VLLM_CONTAINER" \
      python3 - "$HEAD_IP" "$HOEDUS_IP" "$VLLM_IMAGE" "$RAY_VERSION" \
      < "$HERE/cluster_runtime_fingerprint.py")"; then
    echo "[models] dual-node runtime identity preflight failed" >&2
    exit 1
  fi
  RUNTIME_FACTS_JSON="$cluster_runtime_json"
fi
CLUSTER_RUNTIME_FINGERPRINT="$(printf '%s' "$RUNTIME_FACTS_JSON" \
  | sha256sum | awk '{print $1}')"
echo "[models] $RUNTIME_SCOPE runtime fingerprint: $CLUSTER_RUNTIME_FINGERPRINT"
echo "[models] $RUNTIME_SCOPE runtime facts: $RUNTIME_FACTS_JSON"

LAUNCH_PID=""
ACTIVE_CHILD=""
LAST_SERVER_LOG=""
STATUS_MISSING=0

server_ready() {
  # 200 from /v1/models AND the requested model id is being served
  local name="$1" body
  if ! body="$(curl -sf --max-time 5 "$BASE_URL/v1/models" 2>/dev/null)"; then
    return 1
  fi
  "$PYTHON" -c \
    'import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
want = sys.argv[1]
models = doc.get("data") or []
raise SystemExit(0 if any(m.get("id") == want for m in models) else 1)' \
    "$name" <<<"$body"
}

server_contract() {
  # Exercise the exact response fields used by bench/client.py. Merely seeing
  # /v1/models is insufficient: a stale server or incompatible streaming build
  # can otherwise invalidate an entire multi-day sweep.
  "$PYTHON" - "$BASE_URL" "$1" "$PROBE_TIMEOUT" <<'PY'
import json
import sys
import urllib.request

base_url, model, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
payload = {
    "model": model,
    "prompt": [1],
    "max_tokens": 2,
    "min_tokens": 2,
    "ignore_eos": True,
    "temperature": 0.0,
    "stream": True,
    "return_token_ids": True,
    "stream_options": {"include_usage": True},
}
request = urllib.request.Request(
    base_url.rstrip("/") + "/v1/completions",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
count = 0
saw_ids = False
usage = None
finish = ""
done = False
try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise RuntimeError(f"stream error: {chunk['error']}")
            choices = chunk.get("choices") or []
            choice = choices[0] if choices else {}
            token_ids = choice.get("token_ids")
            if token_ids is None:
                token_ids = (choice.get("delta") or {}).get("token_ids")
            if token_ids is None:
                token_ids = chunk.get("token_ids")
            if isinstance(token_ids, list):
                saw_ids = True
                count += len(token_ids)
            if choice.get("finish_reason") is not None:
                finish = str(choice["finish_reason"])
            if (chunk.get("usage") or {}).get("completion_tokens") is not None:
                usage = int(chunk["usage"]["completion_tokens"])
except Exception as exc:
    print(f"[models] strict streaming probe failed: {type(exc).__name__}: {exc}",
          file=sys.stderr)
    raise SystemExit(1)

problems = []
if not done: problems.append("missing [DONE]")
if not saw_ids: problems.append("missing delta token_ids")
if count != 2: problems.append(f"received {count} token ids, expected 2")
if usage != 2: problems.append(f"final usage={usage!r}, expected 2")
if finish != "length": problems.append(f"finish_reason={finish!r}, expected 'length'")
if problems:
    print("[models] strict streaming probe failed: " + "; ".join(problems),
          file=sys.stderr)
    raise SystemExit(1)
print("[models] strict stream contract OK: exact token_ids, usage, length, [DONE]")
PY
}

wait_ready() {
  local name="$1" waited=0
  echo "[models] waiting for $name (up to ${READY_TIMEOUT}s)"
  while (( waited < READY_TIMEOUT )); do
    if server_ready "$name"; then
      echo "[models] $name endpoint is ready after ${waited}s; probing stream contract"
      server_contract "$name"
      return $?
    fi
    # bail early if the backgrounded launcher already died
    if [[ -n "$LAUNCH_PID" ]] && ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
      echo "[models] launcher for $name exited before the server was ready" >&2
      return 1
    fi
    sleep 5; waited=$((waited + 5))
  done
  echo "[models] timed out after ${READY_TIMEOUT}s waiting for $name" >&2
  return 1
}

teardown() {
  # A status query never launched anything. Above all it must not remove the
  # Ray head container, which a standalone single teardown would do.
  [[ "$STATUS_ONLY" == "1" ]] && return 0
  if [[ "$CONFIG" == "single" && "$SINGLE_CONTAINER" == "standalone" ]]; then
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
  else
    # stop only the serve process; keep the Ray cluster up for the next model
    docker exec "$VLLM_CONTAINER" pkill -f "vllm serve" >/dev/null 2>&1 || true
  fi
  if [[ -n "$LAUNCH_PID" ]]; then
    # A docker exec client can linger after its remote process exits. Bound the
    # cleanup so one broken launcher cannot deadlock the whole model matrix.
    local i
    for i in $(seq 1 30); do
      kill -0 "$LAUNCH_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$LAUNCH_PID" 2>/dev/null; then
      kill -TERM "$LAUNCH_PID" 2>/dev/null || true
      for i in $(seq 1 5); do
        kill -0 "$LAUNCH_PID" 2>/dev/null || break
        sleep 1
      done
    fi
    kill -KILL "$LAUNCH_PID" 2>/dev/null || true
    wait "$LAUNCH_PID" 2>/dev/null || true
    LAUNCH_PID=""
  fi
}
handle_signal() {
  local status="$1" i
  trap - INT TERM
  if [[ -n "$ACTIVE_CHILD" ]] && kill -0 "$ACTIVE_CHILD" 2>/dev/null; then
    kill -TERM "$ACTIVE_CHILD" 2>/dev/null || true
    for i in $(seq 1 30); do
      kill -0 "$ACTIVE_CHILD" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$ACTIVE_CHILD" 2>/dev/null || true
    wait "$ACTIVE_CHILD" 2>/dev/null || true
    ACTIVE_CHILD=""
  fi
  teardown
  exit "$status"
}

wait_endpoint_down() {
  local i
  for i in $(seq 1 30); do
    if ! curl -sf --max-time 1 "$BASE_URL/v1/models" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "[models] endpoint $BASE_URL is still live after teardown; refusing a stale-server launch" >&2
  return 1
}

# Keep only one replicate's items of a "rep:c ..." schedule (ONLY_REPEAT).
restrict_schedule() {
  local item out=()
  if [[ -z "$ONLY_REPEAT" ]]; then
    printf '%s' "$1"
    return 0
  fi
  for item in $1; do
    [[ "${item%%:*}" == "$ONLY_REPEAT" ]] && out+=("$item")
  done
  printf '%s' "${out[*]}"
}

# The client builds exact token-id prompts with the served tokenizer. Load it
# offline from the shared cache before a launch, so a tokenizer the pinned
# client cannot read fails here in seconds rather than at the first point.
client_tokenizer_check() {
  local tokenizer="$1" tokrev="$2" trust="$3"
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_CACHE="$HF_CACHE/hub" \
  HF_MODULES_CACHE="${HF_MODULES_CACHE:-$HOME/.cache/huggingface-client/modules}" \
  TRANSFORMERS_NO_ADVISORY_WARNINGS=1 \
    "$PYTHON" - "$tokenizer" "$tokrev" "$trust" <<'PY'
import sys
from transformers import AutoTokenizer

name, revision, trust = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
try:
    tok = AutoTokenizer.from_pretrained(name, revision=revision or None,
                                        trust_remote_code=trust)
    ids = tok("tokenizer preflight", add_special_tokens=False)["input_ids"]
except Exception as exc:
    raise SystemExit(f"[models] client tokenizer preflight failed for {name}: "
                     f"{type(exc).__name__}: {exc}")
if not ids:
    raise SystemExit(f"[models] client tokenizer for {name} produced no token ids")
print(f"[models] client tokenizer OK: {type(tok).__name__}, vocab {len(tok)}")
PY
}

# A dual launch whose NCCL data path silently fell back to TCP would be
# mislabelled as RDMA (or the reverse in a TCP ablation). With NCCL_DEBUG=INFO
# on both Ray containers, NCCL logs one "via NET/<transport>" line per channel
# into the server log. Fail on positive evidence of the wrong transport; only
# warn when the log carries no channel lines at all.
nccl_transport_check() {
  local log="$1" want="IB" wrong="Socket" n_want n_wrong
  if [[ "$NCCL_IB_DISABLE" == "1" ]]; then want="Socket"; wrong="IB"; fi
  n_want="$(grep -c "via NET/$want" "$log" || true)"
  n_wrong="$(grep -c "via NET/$wrong" "$log" || true)"
  if (( n_wrong > 0 )); then
    echo "[models] NCCL used NET/$wrong on $n_wrong channel line(s) although" \
         "NCCL_IB_DISABLE=$NCCL_IB_DISABLE requests NET/$want; see $log" >&2
    return 1
  fi
  if (( n_want == 0 )); then
    echo "[models] WARNING: no NCCL channel lines in $log; start Ray with" \
         "NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET to record transport evidence" >&2
    return 0
  fi
  echo "[models] NCCL transport verified: $n_want channel line(s) via NET/$want"
}

serving_fingerprint() {
  local name="$1" revision="$2" tokenizer="$3" tokrev="$4"
  local maxlen="$5" gpu_util="$6" quant="$7" trust="$8"
  local max_num_seqs="${9:-}"
  {
    printf 'schema=serving-v1\n'
    printf 'config=%s\n' "$CONFIG"
    printf 'image=%s\n' "$VLLM_IMAGE"
    printf 'model=%s\nrevision=%s\n' "$name" "$revision"
    printf 'tokenizer=%s\ntokenizer_revision=%s\n' "$tokenizer" "$tokrev"
    printf 'dtype=%s\nquant=%s\n' "${DTYPE:-auto}" "$quant"
    printf 'max_model_len=%s\ngpu_util=%s\n' "$maxlen" "$gpu_util"
    printf 'prefix_caching=%s\ngeneration_config=%s\n' \
      "${PREFIX_CACHING:-0}" "${GENERATION_CONFIG:-vllm}"
    printf 'torchinductor_autograd_cache=%s\nhf_hub_offline=%s\n' \
      "${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}" "${HF_HUB_OFFLINE:-1}"
    printf 'trust_remote_code=%s\nray_version=%s\n' "$trust" "$RAY_VERSION"
    printf 'connectx_iface=%s\nnccl_ib_disable=%s\n' "$CX_IFACE" "$NCCL_IB_DISABLE"
    printf 'head_ip=%s\nworker_ip=%s\npower_backend=%s\n' \
      "$HEAD_IP" "$HOEDUS_IP" "$EFFECTIVE_POWER_BACKEND"
    printf 'cluster_runtime=%s\n' "$CLUSTER_RUNTIME_FINGERPRINT"
    [[ -z "$max_num_seqs" ]] || printf 'max_num_seqs=%s\n' "$max_num_seqs"
  } | sha256sum | awk '{print $1}'
}


write_serving_policy() {
  local destination="$1" fingerprint="$2" name="$3" revision="$4"
  local tokenizer="$5" tokrev="$6" maxlen="$7" gpu_util="$8"
  local quant="$9" trust="${10}"
  local max_num_seqs="${11:-}"
  "$PYTHON" - "$destination" "$fingerprint" "serving-v1" "$CONFIG" \
    "$VLLM_IMAGE" "$name" "$revision" "$tokenizer" "$tokrev" \
    "${DTYPE:-auto}" "$quant" "$maxlen" "$gpu_util" \
    "${PREFIX_CACHING:-0}" "${GENERATION_CONFIG:-vllm}" \
    "${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}" "${HF_HUB_OFFLINE:-1}" \
    "$trust" "$RAY_VERSION" "$CX_IFACE" "$NCCL_IB_DISABLE" \
    "$HEAD_IP" "$HOEDUS_IP" "$EFFECTIVE_POWER_BACKEND" \
    "$CLUSTER_RUNTIME_FINGERPRINT" "$RUNTIME_SCOPE" \
    "$RUNTIME_FACTS_JSON" "$max_num_seqs" <<'PY'
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

(
    destination_text,
    fingerprint,
    schema,
    config,
    image,
    model,
    revision,
    tokenizer,
    tokenizer_revision,
    dtype,
    quant,
    max_model_len,
    gpu_util,
    prefix_caching,
    generation_config,
    torchinductor_autograd_cache,
    hf_hub_offline,
    trust_remote_code,
    ray_version,
    connectx_iface,
    nccl_ib_disable,
    head_ip,
    worker_ip,
    power_backend,
    runtime_hash,
    runtime_scope,
    runtime_json,
    max_num_seqs,
) = sys.argv[1:]

try:
    runtime_facts = json.loads(runtime_json)
except (TypeError, ValueError) as exc:
    raise SystemExit(f"[models] invalid canonical runtime JSON: {exc}")
runtime_canonical = json.dumps(
    runtime_facts, sort_keys=True, separators=(",", ":")
)
computed_runtime_hash = hashlib.sha256(runtime_canonical.encode()).hexdigest()
if computed_runtime_hash != runtime_hash:
    raise SystemExit(
        "[models] runtime JSON is not canonical or its hash changed: "
        f"{computed_runtime_hash} != {runtime_hash}"
    )

# This is the existing serving-v1 line serialization. Keeping it here as an
# assertion prevents the human-readable policy and resume fingerprint from
# silently drifting apart; dual-node fingerprints remain backward-compatible.
legacy_input = "".join((
    f"schema={schema}\n",
    f"config={config}\n",
    f"image={image}\n",
    f"model={model}\nrevision={revision}\n",
    f"tokenizer={tokenizer}\ntokenizer_revision={tokenizer_revision}\n",
    f"dtype={dtype}\nquant={quant}\n",
    f"max_model_len={max_model_len}\ngpu_util={gpu_util}\n",
    f"prefix_caching={prefix_caching}\ngeneration_config={generation_config}\n",
    "torchinductor_autograd_cache="
    f"{torchinductor_autograd_cache}\nhf_hub_offline={hf_hub_offline}\n",
    f"trust_remote_code={trust_remote_code}\nray_version={ray_version}\n",
    f"connectx_iface={connectx_iface}\nnccl_ib_disable={nccl_ib_disable}\n",
    f"head_ip={head_ip}\nworker_ip={worker_ip}\npower_backend={power_backend}\n",
    f"cluster_runtime={runtime_hash}\n",
))
if max_num_seqs:
    if not max_num_seqs.isascii() or not max_num_seqs.isdigit() or int(max_num_seqs) <= 0:
        raise SystemExit("[models] max_num_seqs must be a positive integer")
    legacy_input += f"max_num_seqs={max_num_seqs}\n"
computed_fingerprint = hashlib.sha256(legacy_input.encode()).hexdigest()
if computed_fingerprint != fingerprint:
    raise SystemExit(
        "[models] serving policy fields do not reproduce fingerprint: "
        f"{computed_fingerprint} != {fingerprint}"
    )

policy = {
    "cluster_runtime": runtime_hash,
    "config": config,
    "connectx_iface": connectx_iface,
    "dtype": dtype,
    "generation_config": generation_config,
    "gpu_util": gpu_util,
    "head_ip": head_ip,
    "hf_hub_offline": hf_hub_offline,
    "image": image,
    "max_model_len": max_model_len,
    "model": model,
    "nccl_ib_disable": nccl_ib_disable,
    "power_backend": power_backend,
    "prefix_caching": prefix_caching,
    "quant": quant,
    "ray_version": ray_version,
    "revision": revision,
    "runtime_facts": runtime_facts,
    "runtime_facts_sha256": runtime_hash,
    "runtime_scope": runtime_scope,
    "schema": schema,
    "serving_fingerprint": fingerprint,
    "tokenizer": tokenizer,
    "tokenizer_revision": tokenizer_revision,
    "torchinductor_autograd_cache": torchinductor_autograd_cache,
    "trust_remote_code": trust_remote_code,
    "worker_ip": worker_ip,
}
if max_num_seqs:
    policy["max_num_seqs"] = max_num_seqs
encoded = (
    json.dumps(policy, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
).encode()
destination = Path(destination_text).absolute()
if not destination.parent.is_dir():
    raise SystemExit(
        f"[models] serving policy parent does not exist: {destination.parent}"
    )

descriptor, temporary_name = tempfile.mkstemp(
    dir=destination.parent,
    prefix=f".{destination.name}.",
    suffix=".tmp",
)
temporary = Path(temporary_name)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, destination)
    except FileExistsError:
        if destination.is_symlink() or not destination.is_file():
            raise SystemExit(
                f"[models] refusing non-regular serving policy: {destination}"
            )
        if destination.read_bytes() != encoded:
            raise SystemExit(
                f"[models] immutable serving policy conflicts with {destination}"
            )
finally:
    temporary.unlink(missing_ok=True)
PY
}

run_one() {
  local label="$1" name="$2" revision="$3" maxlen="$4" gpu_util="$5"
  local quant="$6" tok="$7" tokrev="$8" trust="$9"
  local max_num_seqs="${10:-}"
  local log="" policy_file="" launch_timestamp="" fingerprint_prefix=""
  local tokenizer="${tok:-$name}"
  local tokenizer_revision="$tokrev"
  local actual_maxlen="${maxlen:-8704}"
  local actual_gpu_util="${gpu_util:-0.80}"
  local fingerprint deployment="" schedule=""
  local status=0
  LAST_SERVER_LOG=""
  echo "[models] === $label ($name) config=$CONFIG ==="

  if [[ ! "$label" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "[models] unsafe model label for provenance filenames: $label" >&2
    return 1
  fi
  if [[ -z "$revision" ]]; then
    echo "[models] $label has no pinned model revision" >&2
    return 1
  fi
  if [[ -z "$tokenizer_revision" && "$tokenizer" == "$name" ]]; then
    tokenizer_revision="$revision"
  elif [[ -z "$tokenizer_revision" && -n "$tok" ]]; then
    echo "[models] $label uses tokenizer $tok without a pinned tokenizer_revision" >&2
    return 1
  fi
  fingerprint="$(serving_fingerprint "$name" "$revision" "$tokenizer" \
                    "$tokenizer_revision" "$actual_maxlen" "$actual_gpu_util" \
                    "$quant" "$trust" "$max_num_seqs")"
  echo "[models] serving fingerprint: $fingerprint"
  fingerprint_prefix="${fingerprint:0:16}"

  policy_file="$OUT_DIR/serving_policy_${label}_${CONFIG}_${fingerprint}.json"
  if ! write_serving_policy "$policy_file" "$fingerprint" "$name" \
      "$revision" "$tokenizer" "$tokenizer_revision" "$actual_maxlen" \
      "$actual_gpu_util" "$quant" "$trust" "$max_num_seqs"; then
    echo "[models] failed to persist serving policy for $label" >&2
    return 1
  fi
  echo "[models] serving policy: $policy_file"

  # Resume at exact (repeat, concurrency) granularity. Only rows produced by
  # the current methodology/config/source/serving-policy identity count as
  # complete; live KV capacity is allowed to differ from launch to launch.
  if [[ "$SKIP_DONE" == "1" || "$STATUS_ONLY" == "1" ]]; then
    if ! schedule="$(POWER_BACKEND="$POWER_BACKEND" \
                       EXPECTED_CONCURRENCIES="$EXPECTED_CONCURRENCIES" \
                       "$PYTHON" "$HERE/swept_concurrencies.py" \
                          "$OUT_DIR/index.csv" "$label" "$CONFIG" "$CONFIG_YAML" \
                          "$fingerprint" "$REPEATS" $CONCURRENCIES)"; then
      echo "[models] resume validation failed; refusing to guess what is complete" >&2
      return 1
    fi
    schedule="$(restrict_schedule "$schedule")"
    if [[ "$STATUS_ONLY" == "1" ]]; then
      local missing=0
      [[ -n "$schedule" ]] && missing=$(wc -w <<<"$schedule")
      echo "[models] status: $label $CONFIG missing=$missing${schedule:+ ($schedule)}"
      (( missing > 0 )) && STATUS_MISSING=$((STATUS_MISSING + 1))
      return 0
    fi
    if [[ -z "$schedule" ]]; then
      echo "[models] $label: ${ONLY_REPEAT:+replicate $ONLY_REPEAT of }$REPEATS" \
           "repeat(s) of $CONCURRENCIES complete; skipping"
      return 0
    fi
    echo "[models] $label missing repeat:concurrency pairs: $schedule"
  elif [[ -n "$ONLY_REPEAT" ]]; then
    local c
    for c in $CONCURRENCIES; do schedule+="${schedule:+ }$ONLY_REPEAT:$c"; done
  fi

  if ! client_tokenizer_check "$tokenizer" "$tokenizer_revision" "$trust"; then
    echo "[models] skipping $label: the client cannot build exact prompts" >&2
    return 1
  fi

  # Explicit empty values prevent unrelated .env values from leaking from one
  # model entry into another.
  local launch_env=(MODEL="$name" REVISION="$revision"
                    MAXLEN="$actual_maxlen" GPU_UTIL="$actual_gpu_util"
                    MAX_NUM_SEQS="$max_num_seqs"
                    QUANT="$quant" TOKENIZER="$tok"
                    TOKENIZER_REVISION="$tokenizer_revision"
                    TRUST_REMOTE_CODE="$trust" DTYPE="${DTYPE:-auto}"
                    PREFIX_CACHING="${PREFIX_CACHING:-0}"
                    GENERATION_CONFIG="${GENERATION_CONFIG:-vllm}"
                    TORCHINDUCTOR_AUTOGRAD_CACHE="${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}"
                    HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
                    SINGLE_CONTAINER="$SINGLE_CONTAINER")
  teardown
  wait_endpoint_down || return 1

  if ! launch_timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"; then
    echo "[models] could not obtain a UTC launch timestamp for $label" >&2
    return 1
  fi
  log="$OUT_DIR/server_${label}_${CONFIG}_${fingerprint_prefix}_${launch_timestamp}_pid$$.log"
  # Exclusive creation makes a collision a hard failure. The launcher appends
  # only to this newly allocated path, so prior server evidence is never
  # truncated or reused by a later launch.
  if ! (set -o noclobber; : >"$log") 2>/dev/null; then
    echo "[models] refusing to overwrite existing server log: $log" >&2
    return 1
  fi
  LAST_SERVER_LOG="$log"

  local sweep_env=(MODEL="$name" MODEL_LABEL="$label" SERVER_LOG="$log"
                   SERVING_POLICY_FILE="$policy_file"
                   MODEL_REVISION="$revision"
                   TOKENIZER="$tokenizer" TRUST_REMOTE_CODE="$trust"
                   TOKENIZER_REVISION="$tokenizer_revision"
                   SERVING_FINGERPRINT="$fingerprint"
                   DEPLOYMENT_FINGERPRINT=""
                   BASE_URL="$BASE_URL" OUT_DIR="$OUT_DIR"
                   POWER_BACKEND="$POWER_BACKEND" CONFIG_YAML="$CONFIG_YAML"
                   CONCURRENCIES="$CONCURRENCIES"
                   EXPECTED_CONCURRENCIES="$EXPECTED_CONCURRENCIES"
                   REPEATS="$REPEATS" SWEEP_SCHEDULE="$schedule"
                   HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
                   HF_HUB_CACHE="$HF_CACHE/hub"
                   HF_MODULES_CACHE="${HF_MODULES_CACHE:-$HOME/.cache/huggingface-client/modules}"
                   PYTHON="$PYTHON")

  echo "[models] immutable server log: $log"
  # launch_server.sh reads MODEL/MAXLEN/GPU_UTIL/QUANT/TOKENIZER from the
  # environment. Append to the exclusively created per-launch log.
  env "${launch_env[@]}" "$HERE/launch_server.sh" "$CONFIG" >>"$log" 2>&1 &
  LAUNCH_PID=$!

  if ! wait_ready "$name"; then
    echo "[models] skipping $label; see $log" >&2
    teardown
    return 1
  fi
  if [[ "$CONFIG" != "single" ]] && ! nccl_transport_check "$log"; then
    echo "[models] skipping $label: wrong NCCL transport for this campaign" >&2
    teardown
    return 1
  fi

  # Hash the runtime/capacity actually observed after startup. Recorded on
  # every row, and re-checked by run_benchmark.py after the window so a server
  # that changes underneath a measurement (different vLLM build, KV capacity,
  # block size) is caught. It is deliberately not part of resume identity:
  # repeats collected across launches never reproduce the same block count.
  if ! deployment="$("$PYTHON" - "$BASE_URL" "$name" "$log" "$fingerprint" <<'PY'
import sys
from bench.run_benchmark import collect_deployment_info, deployment_fingerprint

base_url, model, log, serving = sys.argv[1:5]
info = collect_deployment_info(
    base_url, model, log, compute_from_hf=False,
)
print(deployment_fingerprint(serving, info))
PY
)"; then
    echo "[models] failed to fingerprint the live deployment for $label" >&2
    teardown
    return 1
  fi
  echo "[models] deployment fingerprint: $deployment"

  if [[ "$SKIP_DONE" == "1" ]]; then
    if ! schedule="$(POWER_BACKEND="$POWER_BACKEND" \
                       EXPECTED_CONCURRENCIES="$EXPECTED_CONCURRENCIES" \
                       "$PYTHON" "$HERE/swept_concurrencies.py" \
                          "$OUT_DIR/index.csv" "$label" "$CONFIG" "$CONFIG_YAML" \
                          "$fingerprint" "$REPEATS" $CONCURRENCIES)"; then
      echo "[models] post-launch resume validation failed" >&2
      teardown
      return 1
    fi
    schedule="$(restrict_schedule "$schedule")"
    if [[ -z "$schedule" ]]; then
      echo "[models] $label became complete before measurement; skipping"
      teardown
      wait_endpoint_down || return 1
      return 0
    fi
    echo "[models] live deployment missing repeat:concurrency pairs: $schedule"
  fi
  sweep_env+=(DEPLOYMENT_FINGERPRINT="$deployment" SWEEP_SCHEDULE="$schedule")

  if [[ "$CANARY_ONLY" == "1" ]]; then
    echo "[models] $label canary passed; measurement intentionally skipped"
    teardown
    wait_endpoint_down || return 1
    return 0
  fi

  # Sweep this model. run_all.sh receives the model, label, immutable server log,
  # and canonical serving-policy path so rows can retain the complete launch
  # provenance while recording capacity figures.
  env "${sweep_env[@]}" "$HERE/run_all.sh" "$CONFIG" &
  ACTIVE_CHILD=$!
  if ! wait "$ACTIVE_CHILD"; then
    echo "[models] sweep for $label returned an error" >&2
    status=1
  fi
  ACTIVE_CHILD=""

  teardown
  echo "[models] done $label"
  sleep 5
  return "$status"
}

trap 'teardown' EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

FAILURES=0
REQUESTED_REPEAT="$ONLY_REPEAT"
REPLICATE_IDS=("$REQUESTED_REPEAT")
if [[ -z "$REQUESTED_REPEAT" && "$STATUS_ONLY" != "1" && "$CANARY_ONLY" != "1" ]]; then
  mapfile -t REPLICATE_IDS < <(seq 1 "$REPEATS")
fi
for entry in "${ENTRIES[@]}"; do
  IFS='|' read -r label name revision maxlen gpu_util quant tok tokrev trust max_num_seqs <<<"$entry"
  for replicate in "${REPLICATE_IDS[@]}"; do
    # The direct wrapper must honor the same launch-level replicate design as
    # run_job.sh. A status query still checks the entire requested rectangle,
    # and a canary runs only once per model.
    ONLY_REPEAT="$replicate"
    if ! run_one "$label" "$name" "$revision" "$maxlen" "$gpu_util" \
                 "$quant" "$tok" "$tokrev" "$trust" "$max_num_seqs"; then
      FAILURES=$((FAILURES + 1))
      # A failed launcher preflight means a node is short on memory; every later
      # model would fail the same way, so stop instead of cycling through them.
      if [[ -n "$LAST_SERVER_LOG" ]] && \
          grep -q "PREFLIGHT FAILED" "$LAST_SERVER_LOG" 2>/dev/null; then
        echo "[models] cluster memory preflight failed for $label; aborting the" \
             "sweep. Free memory on the low node (see the log), then rerun" \
             "with --skip-done." >&2
        exit 1
      fi
      echo "[models] $label${replicate:+ replicate $replicate} did not complete; continuing" >&2
    fi
  done
done

if (( FAILURES > 0 )); then
  echo "[models] $FAILURES model launch(es) failed for $CONFIG; see logs in $OUT_DIR" >&2
  exit 1
fi

if [[ "$STATUS_ONLY" == "1" ]]; then
  # Exit 3 (not 1) so callers can tell "work remains" from a real error.
  (( STATUS_MISSING > 0 )) && exit 3
  echo "[models] status: every selected $CONFIG model is complete"
  exit 0
fi

if [[ "$CANARY_ONLY" == "1" ]]; then
  echo "[models] all $CONFIG strict server canaries passed"
else
  echo "[models] all $CONFIG models done, see $OUT_DIR/index.csv"
fi
