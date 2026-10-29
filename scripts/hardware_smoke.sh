#!/usr/bin/env bash
# End-to-end single-node smoke test of the whole pipeline on the pinned image,
# in about 15 minutes, before committing weeks of cluster time:
#
#   fetch a small model -> strict canary -> 2 replicates on fresh launches
#   (short windows, release gates on) -> resume status -> figures and
#   replicate statistics -> purge
#
#   scripts/hardware_smoke.sh                 # on Diya, with or without Ray up
#
# It uses its own container name, port, and output directory, and the campaign's
# GPU memory target. Nothing it writes is a paper result. On a machine without
# the ConnectX addresses, point CX_IFACE/HEAD_IP at a local interface and its
# address.
#
# Env: OUT_DIR (default results/smoke-<UTC-date>; a bare name prefixes this
#      default), SMOKE_LABEL (default llama3.1-8b-nvfp4,
#      any label in configs/models.yaml that can run single), SMOKE_GPU_UTIL
#      (default 0.80), SMOKE_CONCURRENCIES (default "1 4"), PORT (default
#      8100), SMOKE_VLLM_CONTAINER (default vllm-spark-smoke), PYTHON,
#      DROP_CACHES
#      (passed to the launcher), KEEP_WEIGHTS=1 (skip the final purge).
#
# The canary sends one short request, so it cannot catch a model that fails
# only under load. Stress a suspect checkpoint at the campaign's top load:
#
#   SMOKE_LABEL=nemotron3.5-lightning-30b-a3b-nvfp4 SMOKE_GPU_UTIL=0.80 \
#     SMOKE_CONCURRENCIES="1 64" OUT_DIR=results/smoke-lightning-2026-10-22 \
#     scripts/hardware_smoke.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

source "$HERE/utils/campaign_output.sh"
campaign_resolve_out_dir smoke
campaign_reject_existing_implicit_out_dir || exit 1
CAMPAIGN_FIGURES_DIR="$OUT_DIR/figs"
export PORT="${PORT:-8100}"
export BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
export SINGLE_CONTAINER=standalone
SMOKE_LABEL="${SMOKE_LABEL:-llama3.1-8b-nvfp4}"
SMOKE_GPU_UTIL="${SMOKE_GPU_UTIL:-0.80}"
SMOKE_CONCURRENCIES="${SMOKE_CONCURRENCIES:-1 4}"
if ! [[ "$SMOKE_CONCURRENCIES" =~ ^[1-9][0-9]*( [1-9][0-9]*)*$ ]]; then
  echo "[smoke] SMOKE_CONCURRENCIES must be space-separated positive integers" >&2
  exit 2
fi
KEEP_WEIGHTS="${KEEP_WEIGHTS:-0}"
if [[ -z "${PYTHON:-}" ]]; then
  PYTHON=python3
  [[ -x "$HERE/.venv/bin/python" ]] && PYTHON="$HERE/.venv/bin/python"
fi
export PYTHON PYTHONNOUSERSITE=1

source scripts/connectx_env.sh
CAMPAIGN_VLLM_CONTAINER="$VLLM_CONTAINER"
SMOKE_VLLM_CONTAINER="${SMOKE_VLLM_CONTAINER:-vllm-spark-smoke}"
if [[ "$SMOKE_VLLM_CONTAINER" == "$CAMPAIGN_VLLM_CONTAINER" ]]; then
  echo "[smoke] SMOKE_VLLM_CONTAINER must differ from the Ray container ($CAMPAIGN_VLLM_CONTAINER)" >&2
  exit 2
fi

remove_smoke_container() {
  local container_listing container_id container_name
  if ! container_listing="$(docker ps --all --format '{{.ID}} {{.Names}}')"; then
    echo "[smoke] ERROR: cannot list Docker containers for cleanup" >&2
    return 1
  fi
  while read -r container_id container_name; do
    if [[ "$container_name" == "$SMOKE_VLLM_CONTAINER" ]]; then
      if ! docker rm --force "$container_id" >/dev/null; then
        echo "[smoke] ERROR: could not remove temporary container $SMOKE_VLLM_CONTAINER" >&2
        return 1
      fi
      echo "[smoke] removed temporary container $SMOKE_VLLM_CONTAINER"
    fi
  done <<<"$container_listing"
}

finish_smoke() {
  local original_exit_status=$?
  trap - EXIT
  if ! remove_smoke_container; then
    original_exit_status=1
  fi
  exit "$original_exit_status"
}

acquire_resource_lock || exit 1
remove_smoke_container || exit 1
wait_for_resources_idle auto || exit 1
export VLLM_CONTAINER="$SMOKE_VLLM_CONTAINER"
trap finish_smoke EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "[smoke] temporary serving container: $VLLM_CONTAINER"

mkdir -p "$CAMPAIGN_RESULTS_DIR"
if [[ -e "$CAMPAIGN_RESULTS_DIR/index.csv" ]]; then
  echo "[smoke] $OUT_DIR already holds results; choose a fresh OUT_DIR" >&2
  exit 1
fi
mkdir -p "$OUT_DIR"
export CAMPAIGN_PATH_PREPARED=1
export MODELS_YAML="$CAMPAIGN_RESULTS_DIR/smoke-models.yaml"
export CONFIG_YAML="$CAMPAIGN_RESULTS_DIR/smoke-experiment.yaml"

# One-entry matrix built from the campaign's own entry, single only, with the
# selected GPU memory target and the campaign experiment with short windows.
# The release gates (power, vLLM metrics, provenance, zero failures) stay on.
"$PYTHON" - "$SMOKE_LABEL" "$SMOKE_GPU_UTIL" "$MODELS_YAML" "$CONFIG_YAML" \
  "$SMOKE_CONCURRENCIES" <<'PY'
import re
import sys

import yaml

label, gpu_util, models_out, experiment_out, concurrencies = sys.argv[1:]
models = yaml.safe_load(open("configs/models.yaml"))["models"]
entry = next((m for m in models if m.get("label") == label), None)
if entry is None or "single" not in entry.get("configs", []):
    raise SystemExit(f"[smoke] {label} is not a single-node entry of configs/models.yaml")
entry = {k: v for k, v in entry.items() if k != "meta"}
entry.update(configs=["single"])
yaml.safe_dump({"gpu_util_default": float(gpu_util), "models": [entry]},
               open(models_out, "w"), sort_keys=False)

text = open("configs/experiment.yaml").read()
for key, value in (("warmup_s", "15.0"), ("measure_s", "30.0"),
                   ("min_window_requests", "5"), ("window_extension_step_s", "10.0"),
                   ("max_measure_s", "120.0"), ("cooldown_s", "5.0"),
                   ("request_timeout_s", "900.0"), ("min_successful_requests", "5")):
    text, count = re.subn(rf"^{key}: .*$", f"{key}: {value}", text, flags=re.M)
    if count != 1:
        raise SystemExit(f"[smoke] cannot set {key} in configs/experiment.yaml")
open(experiment_out, "w").write("# Smoke-test windows; not a paper config.\n" + text)
print(f"[smoke] {label}: single, gpu_util {gpu_util}, 30 s windows, "
      f"c = {concurrencies}, 2 replicates")
PY

step() { echo; echo "[smoke] === $* ==="; }
export MODEL_LABELS="$SMOKE_LABEL" CONCURRENCIES="$SMOKE_CONCURRENCIES"
export EXPECTED_CONCURRENCIES="$SMOKE_CONCURRENCIES" REPEATS=2

downloaded=0
step "fetch"
if ! CHECK_ONLY=1 scripts/predownload.sh single >/dev/null 2>&1; then
  scripts/predownload.sh single
  downloaded=1
fi

step "canary: launch, stream contract, client tokenizer"
CANARY_ONLY=1 scripts/run_models.sh single

for rep in 1 2; do
  step "replicate $rep/2 on a fresh launch"
  ONLY_REPEAT="$rep" scripts/run_models.sh single --skip-done
done

step "resume status (must report complete)"
STATUS_ONLY=1 scripts/run_models.sh single

step "figures, tables, replicate statistics"
"$PYTHON" plotting/plot_results.py --index "$CAMPAIGN_RESULTS_DIR/index.csv" \
  --out "$CAMPAIGN_FIGURES_DIR" --format png --table
mkdir -p "$CAMPAIGN_FIGURES_DIR/stats"
"$PYTHON" scripts/variance_report.py --index "$CAMPAIGN_RESULTS_DIR/index.csv" \
  --md "$CAMPAIGN_FIGURES_DIR/stats/replicate_stats.md" >/dev/null
"$PYTHON" scripts/compare_campaigns.py --new "$CAMPAIGN_RESULTS_DIR/index.csv" \
  --baseline baselines/pmbs26-tcp/index.csv \
  --md "$CAMPAIGN_FIGURES_DIR/stats/compare_vs_pmbs26_tcp.md" >/dev/null || true

if [[ "$downloaded" == "1" && "$KEEP_WEIGHTS" != "1" ]]; then
  step "purge the weights this test downloaded"
  PURGE_WORKER=0 scripts/purge_model_cache.sh
fi

rows="$(($(wc -l < "$CAMPAIGN_RESULTS_DIR/index.csv") - 1))"
echo
echo "[smoke] PASSED: $rows measurement rows in $CAMPAIGN_RESULTS_DIR/index.csv"
echo "[smoke] summary table: $CAMPAIGN_FIGURES_DIR/summary_table.md"
echo "[smoke] replicate statistics: $CAMPAIGN_FIGURES_DIR/stats/replicate_stats.md"
