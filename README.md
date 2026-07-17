# DGX Spark LLM Serving Benchmark

This repository contains a client-side benchmarking harness for LLM serving on
DGX Spark. It drives a vLLM OpenAI-compatible server, measures streaming latency
and throughput, optionally samples power and vLLM Prometheus metrics, and writes
CSV/JSON outputs that can be plotted for single-node, dual-node tensor parallel,
and dual-node pipeline parallel experiments.

The default request path is the vLLM completions endpoint (`/v1/completions`).
Requests are streaming, greedy requests with `max_tokens == min_tokens`,
`ignore_eos: true`, and `temperature: 0.0` so output length is controlled by the
workload definition. The client asks vLLM for delta token IDs and final usage,
counts exact output tokens rather than HTTP/SSE chunks, and accepts a request as
successful only when the stream ends cleanly with the requested token count.

## Quick Start

Run these commands from the repository root.

1. Create and activate an isolated Python 3.12-or-newer environment, then
   install the exact release dependency closure:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
export PYTHONNOUSERSITE=1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
```

`requirements.txt` lists the direct client dependencies; `requirements.lock`
is the authoritative, fully pinned transitive environment for final runs.
Keep `PYTHONNOUSERSITE=1` set so packages from a user-site installation cannot
silently enter the benchmark process.

2. Check the locked client environment and local tools:

```bash
python scripts/check_environment.py
python -m bench.run_benchmark --help
python plot_results.py --help
python -m unittest discover -s tests -v
```

Before touching the GPUs, validate the complete default campaign definition:

```bash
OUT_DIR=results-steady-exact-v3 PLAN_ONLY=1 scripts/run_job.sh
```

Plan mode performs no hardware operations. It checks the isolated environment,
campaign options, model/config matrix, revisions, workload bounds, and expected
result rectangle. The shipped matrix is 23 model/config combinations times 8
concurrencies times `FULL_REPEATS` (default 1), or exactly 184 measurement rows
per complete pass; `FULL_REPEATS=3` yields the full 552-row spread matrix.

3. Start a vLLM server. If you are using the included Docker launcher on one
   Spark node, run this in one terminal:

```bash
export MODEL=Qwen/Qwen3-8B
export REVISION=b968826d9c46dd6066d109eabc6255188de91218
# export HF_TOKEN=...    # only needed for gated Hugging Face models
# HF_HUB_OFFLINE=0 permits this first exploratory launch to fetch that exact
# revision. Full sweeps use predownload + CHECK_ONLY instead.
HF_HUB_OFFLINE=0 scripts/launch_server.sh single
```

The launch command stays in the foreground while vLLM is running. If you already
have a vLLM-compatible server, skip the launcher and use its URL as `BASE_URL` or
`--base-url`.

The launcher defaults to `--generation-config vllm`, so model-repository
`generation_config.json` defaults cannot silently change the explicit greedy
request settings. Override `GENERATION_CONFIG` only when that difference is
intentional.

Serving otherwise defaults to `HF_HUB_OFFLINE=1`. Before a full sweep, use the
pinned predownload, local-only cache check, and canary workflow in
Benchmarking Multiple Models rather than downloading during server startup.

4. In another terminal, run a short smoke test:

```bash
source .venv/bin/activate
python -m bench.run_benchmark \
  --config configs/smoke.yaml \
  --config-label smoke \
  --base-url http://127.0.0.1:8000
```

`configs/smoke.yaml` is intentionally small and tokenizer-free. It is for
checking connectivity and output files, not for paper-quality measurements.

5. Run a real single-node sweep:

```bash
scripts/predownload.sh single
CHECK_ONLY=1 scripts/predownload.sh single
CANARY_ONLY=1 scripts/run_models.sh single
CONCURRENCIES="1 2 4 8 16 32 64 128" \
scripts/run_models.sh single
```

The full config requires cryptographic launch provenance. Use
`run_models.sh`, which computes it from the actual pinned launch policy;
`run_all.sh` alone is intended for an already identified server and cannot
invent that provenance. The wrapper safely replaces the exploratory server
from the smoke step.

6. Plot the sweep:

```bash
OUT_DIR="${OUT_DIR:-results}"
python plot_results.py --index "$OUT_DIR/index.csv" --out "$OUT_DIR/figs" --format both
```

## Repository Layout

```text
bench/
  config.py        experiment configuration and YAML loading
  workload.py      synthetic token-length prompts and JSONL trace loading
  client.py        one async streaming request with timing capture
  monitors.py      background power sampler and vLLM metrics scraper
  server_info.py   served-model capacity and launch configuration capture
  runner.py        closed-loop and open-loop load generation
  metrics.py       percentile aggregation, throughput, goodput, energy
  run_benchmark.py command line entry for one measurement point
configs/
  smoke.yaml       short connectivity test
  experiment.yaml  default full benchmark settings
  models.yaml      model matrix for the multi-model sweep
scripts/
  check_environment.py         validate the isolated, fully locked client
  validate_campaign.py         plan/verify the exact publication result grid
  load_env.sh                 source a repo-local .env for script entrypoints
  connectx_env.sh             shared ConnectX and container settings; loads .env
  verify_connectx.sh          route and latency sanity checks
  start_ray.sh                start or stop the Ray container for dual-node runs
  cluster_runtime_fingerprint.py verify identical dual-node software/hardware
  launch_server.sh            start vLLM in single, tp2, or pp2 mode
  interconnect_microbench.sh  nccl-tests all-reduce check
  ray_allreduce_bench.sh      no-SSH Ray/NCCL all-reduce check
  run_all.sh                  sweep a concurrency list for one config
  run_models.sh               pinned multi-model launch/canary/sweep wrapper
  run_job.sh                  resumable unattended full campaign
  models_for_config.py        select models.yaml entries for a serving config
plot_results.py               generate PDF/PNG figures from results/index.csv
tests/                        standard-library regression tests for release paths
requirements.txt              Python client and plotting dependencies
requirements.lock             exact direct and transitive release environment
```

## Prerequisites

Client-side requirements:

- Python 3.12 or newer in a real virtual environment, with user-site packages
  disabled.
- The exact direct and transitive packages from `requirements.lock` for final
  results. Use `requirements.txt` only when intentionally developing or
  refreshing the lock.
- Network access from the benchmark client to the vLLM server and its `/metrics`
  endpoint.
- A Hugging Face tokenizer matching the served model for exact input lengths.
  Without a tokenizer, prompts fall back to repeated words and input lengths are
  approximate.

Server-side requirements for the included scripts:

- Docker with NVIDIA GPU runtime support.
- Permission to run `docker` as the current user.
- The validated vLLM container image. The launcher defaults to the immutable
  `26.04-py3` digest recorded in `scripts/connectx_env.sh`; do not replace it
  with a mutable monthly tag during a comparable sweep.
- Hugging Face access for the selected model, usually through `HF_TOKEN`.
- For dual-node runs: working name resolution or IP configuration for both
  nodes, a ConnectX interface, and matching access to the vLLM image on both
  nodes.
- For `scripts/interconnect_microbench.sh`: `git`, `make`, MPI tooling
  providing `mpirun`, and NCCL build dependencies.

If Docker reports a socket permission error, add the user to the `docker` group
and start a new shell:

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

## Configuration

The main experiment configuration is `configs/experiment.yaml`. Important
fields:

- `base_url`: vLLM server URL, for example `http://127.0.0.1:8000`.
- `endpoint`: default `/v1/completions`.
- `model`: served model name. Leave empty to query `/v1/models`.
- `model_revision`: exact 40-hex Hugging Face commit used by the server. The
  multi-model wrapper fills this from `configs/models.yaml`.
- `api_key`: bearer token sent in the `Authorization` header. The vLLM default
  is usually `EMPTY`. Its value is redacted from resolved configurations,
  summaries, fingerprints, and publication artifacts.
- `tokenizer`: Hugging Face tokenizer id or local path. Set to `""` only for
  quick approximate checks.
- `tokenizer_revision`: exact 40-hex tokenizer commit. It defaults to
  `model_revision` when the tokenizer and model repo are the same.
- `serving_fingerprint`: 64-hex SHA-256 of the material serving policy. Do not
  invent this value; `run_models.sh` derives it from the selected config,
  pinned image/revisions, and launch/network policy.
- `use_token_ids`: when true, send exact token-id prompts built with the
  tokenizer.
- `buckets`: weighted synthetic workload buckets with `input_len`,
  `output_len`, and `weight`.
- `trace_path`: optional JSONL trace that replaces `buckets`. A relative path
  is resolved from the experiment YAML's directory, and the file's SHA-256 is
  recorded as `trace_sha256`; changing trace contents therefore changes the
  experiment identity even when the filename is unchanged.
- `seed`: random seed for workload ordering and synthetic prompt slices.
- `balanced_workload`: when true (the default), use smooth weighted scheduling
  for buckets and shuffled without-replacement epochs for traces. This keeps
  short measurement slices close to the configured request mix.
- `mode`: `closed` for fixed concurrency or `open` for Poisson arrivals.
- `concurrency`: number of closed-loop workers.
- `qps`: offered request rate for open-loop mode.
- `max_inflight`: hard open-loop cap on concurrently in-flight client tasks.
  An arrival that finds the cap full is recorded as a client overload failure
  instead of waiting in a hidden client-side queue and creating coordinated
  omission.
- `warmup_s`, `measure_s`, and `cooldown_s`: one continuous load phase.
  `warmup_s` and `cooldown_s` are minimums. In closed-loop mode the measurement
  window does not open until the configured warmup has elapsed and every worker
  has completed its initial request. After the nominal cooldown, replacement
  load continues until every request started in the measurement window has
  finished; only the remaining replacement requests then drain. The summary
  records the actual boundary extensions.
- `request_timeout_s`: per-request HTTP timeout, including queueing and stream
  drain. Keep it well above the worst-case E2E at the highest load in the
  sweep, or slow-but-successful requests are counted as failures; the shipped
  full-run value is `14400` seconds.
- `ttft_slo_s`, `itl_slo_s`, and `itl_slo_s2`: service-level objectives used for
  goodput. Both ITL tiers are scored against the same TTFT bound: `itl_slo_s` is
  the strict interactive target and `itl_slo_s2` a looser tier, giving two
  goodput columns (`goodput_rps`, `goodput_itl2_rps`). On bandwidth-bound GB10
  the dense 32B/70B models floor above the strict ITL even unloaded, so their
  strict goodput is ~0; the looser tier separates them under load.
- `power_backend`: `none`, `nvidia_smi`, `sysfs`, or `tegrastats`.
- `power_sysfs_path`, `power_rails`, and `power_interval_ms`: backend-specific
  power sampling settings.
- `scrape_vllm_metrics`: whether to sample vLLM Prometheus metrics.
- `require_power_data` and `require_vllm_metrics_data`: fail a paper run rather
  than write a result when the requested GPU-power coverage or the required
  request-running/waiting, KV-usage, prompt/generation-token, or preemption
  metrics are missing or too sparse. They are enabled in
  `configs/experiment.yaml` and disabled for the smoke config.
- `require_zero_request_failures` and `min_successful_requests`: reject a
  measurement point before publication when any request fails or when fewer
  than the required number of successful requests were collected. The shipped
  paper config requires zero failures and at least 20 successes per point;
  the smoke config keeps these gates diagnostic-friendly.
- `require_provenance`: require exact 40-hex model/tokenizer revisions and a
  64-hex serving-policy fingerprint before any measurement begins. It is
  enabled in `configs/experiment.yaml`, false by default, and intentionally
  absent from the smoke config. `run_models.sh` supplies and validates all
  three values for full sweeps.
- `config_label`: label written to outputs, such as `single`, `tp2`, or `pp2`.
- `model_label`: short model slug used in indexes, plots, and explicit
  multi-model run tags. If empty, it is derived from the served model name.
- `server_log`: optional captured vLLM stdout log. When present, the benchmark
  parses deployment facts such as per-worker weight/KV memory and vLLM's logged
  maximum concurrency.
- `collect_hf_metadata`: when true, query Hugging Face after the measurement for
  optional full-checkpoint weight/KV estimates. It is false by default so smoke
  and release runs do not depend on post-measurement network metadata lookups.
- `out_dir`: output directory, default `results`.
- `run_note`: free-form note preserved in the resolved configuration block.

Each full result records methodology `steady-exact-v3`, the YAML and harness
hashes, the git commit, model/tokenizer revisions, a serving-policy
fingerprint, an experiment fingerprint, and the independent `repeat_id`.
These fields prevent rows produced by different code, launch settings, or
measurement definitions from being mistaken for replicates.
The wrapper also writes the canonical launch policy as an immutable
`serving_policy_*.json` artifact. Its serving fingerprint covers the immutable
image and model/tokenizer revisions plus material launch choices such as
dtype/quantization, context and KV budget, prefix/generation policy,
compilation-cache policy, and distributed layout, as well as the recorded
runtime identity. The benchmark validates that policy against the requested
point before measurement and links its hash from the summary and index.

Common script environment variables:

Every script entrypoint sources `scripts/load_env.sh`, which loads a repo-local
`.env` file if it exists, so these can be saved there instead of exported in
every shell session. `.env` is ignored by git because it may contain `HF_TOKEN`.
Standard dotenv precedence applies: values already exported by the caller,
such as `export CONCURRENCIES="1"`, and one-shot command assignments win over
`.env`; `.env` only supplies values the environment did not already set. It is
loaded once per process tree (guarded by `DGX_SPARK_ENV_LOADED`), so child
scripts keep the explicit per-model values selected by `run_models.sh`.

Defaults in `.env` may still use shell parameter expansion, for example:

```bash
export CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
```

Set `ENV_FILE=/path/to/another.env` to load a different environment file for a
script invocation.

```bash
export MODEL=Qwen/Qwen3-8B
export REVISION=b968826d9c46dd6066d109eabc6255188de91218
# For a manually driven run_all.sh, use the corresponding benchmark name:
export MODEL_REVISION="$REVISION"
export PORT=8000
export DTYPE=auto
export MAXLEN=8704
# Optional serving overrides:
# export GPU_UTIL=0.80        # default is 0.80 for every config: keep it
#                             # identical across single/tp2/pp2 for one model,
#                             # or the KV budget confounds the comparison
# export PREFIX_CACHING=1     # prefix caching is OFF by default: cache hits on
#                             # repeated synthetic prompts skipped up to a third
#                             # of prefill work in some models but not others
# export GENERATION_CONFIG=vllm  # default: use request sampling settings rather
#                                # than model-repository generation defaults
# export QUANT=fp8
# export TOKENIZER=Qwen/Qwen3-8B
# export TOKENIZER_REVISION="$REVISION"
# export TRUST_REMOTE_CODE=1
export VLLM_IMAGE=nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27
export VLLM_CONTAINER=vllm-spark
export HF_TOKEN=...
export HF_CACHE=$HOME/.cache/huggingface
# Optional DNS passed to Docker launch/Ray containers. Unset/empty uses
# Docker's host-derived resolver, which is the default.
# export CONTAINER_DNS=8.8.8.8
# Optional download-only override; defaults to CONTAINER_DNS.
# export DOWNLOAD_DNS=8.8.8.8
# Offline serving is the default; predownload exact revisions before launching.
export HF_HUB_OFFLINE=1
export BASE_URL=http://127.0.0.1:8000
export OUT_DIR=results
export CONFIG_YAML=configs/experiment.yaml
export CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
export REPEATS="${REPEATS:-3}"
# unset defers to the YAML's power_backend; an override must still satisfy
# require_power_data (the full config therefore cannot use "none")
# export POWER_BACKEND=nvidia_smi
export MODELS_YAML=configs/models.yaml
export READY_TIMEOUT=1800
export RAY_VERSION=2.53.0
# Work around a serialization failure in the pinned PyTorch build's
# AOTAutograd disk cache while leaving graph compilation itself enabled.
export TORCHINDUCTOR_AUTOGRAD_CACHE=0
# Final campaigns automatically prefer the repo's .venv. Set these explicitly
# for manually driven commands as well.
export PYTHON="$PWD/.venv/bin/python"
export PYTHONNOUSERSITE=1
```

`CONTAINER_DNS` affects container DNS only, not host resolution. Leave it empty
when Docker's host-derived resolver works; an explicit public resolver may be
blocked on managed networks. Offline serving still performs no Hub lookup.
`DOWNLOAD_DNS` is useful when the predownload container needs a resolver
different from the serving/Ray containers.

Do not put an arbitrary `SERVING_FINGERPRINT` in `.env` to satisfy the full
config. It must be the SHA-256 hash of the server's actual launch policy;
`run_models.sh` computes and forwards it together with the pinned revisions.

Dual-node network defaults live in `scripts/connectx_env.sh`. Override them
before running the scripts if your node names, IPs, or interface differ:

```bash
export CX_IFACE=enp1s0f0np0
export DIYA_HOST=Diya
export HOEDUS_HOST=Hoedus
export DIYA_IP=10.10.10.1
export HOEDUS_IP=10.10.10.2
export HEAD_IP=$DIYA_IP
export RAY_PORT=6379
```

## Running Single-Node Benchmarks

Start vLLM on the serving node:

```bash
MODEL=Qwen/Qwen3-8B \
REVISION=b968826d9c46dd6066d109eabc6255188de91218 \
scripts/launch_server.sh single
```

For an exploratory launch `REVISION` may be omitted. For results you intend to
compare, pin the exact model commit as above (and `TOKENIZER_REVISION` when an
explicit tokenizer repo differs), but revisions alone are not sufficient
provenance. `scripts/run_models.sh` supplies them from `configs/models.yaml`
and hashes the complete launch policy automatically; it is the recommended
paper workflow.

`MAXLEN` is passed to vLLM as `--max-model-len`, which counts prompt tokens plus
generated tokens. The default full workload needs at least `8704` because its
largest request is `8192` input tokens and `512` output tokens. The launcher
defaults to `8704`; if you lower it, also lower the largest workload bucket.

On Grace unified memory, clean page cache can make `MemFree` look too small even
when `MemAvailable` is sufficient. The single-node launcher now checks both and
uses `posix_fadvise` to drop only clean file pages in the local Hugging Face
cache before vLLM starts. It never allocates anonymous memory to
pressure the host cache. If free memory remains below the target, launch fails
and asks the operator to use `DROP_CACHES=1` or free the owning process.
`DROP_CACHES=1` remains an optional proactive flush and requires sudo:

```bash
DROP_CACHES=1 \
MODEL=Qwen/Qwen3-8B \
REVISION=b968826d9c46dd6066d109eabc6255188de91218 \
scripts/launch_server.sh single
```

Run one manual diagnostic point. The smoke config deliberately does not claim
paper provenance:

```bash
python -m bench.run_benchmark \
  --config configs/smoke.yaml \
  --config-label single \
  --mode closed \
  --concurrency 1 \
  --base-url http://127.0.0.1:8000
```

Run the provenance-complete full closed-loop sweep through the model wrapper:

```bash
CONCURRENCIES="1 2 4 8 16 32 64 128" \
scripts/run_models.sh single
```

Run a diagnostic against a remote or separately managed vLLM server:

```bash
BASE_URL=http://spark-node.example.com:8000 \
CONFIG_YAML=configs/smoke.yaml CONCURRENCIES=1 REPEATS=1 \
scripts/run_all.sh single
```

When you already know the served model or want stable plot labels, pass
`MODEL`, `MODEL_REVISION`, `MODEL_LABEL`, `TOKENIZER`, `TOKENIZER_REVISION`,
`TRUST_REMOTE_CODE` (for models that ship custom code), and optionally
`SERVER_LOG` and a genuine `SERVING_FINGERPRINT` through the environment.
`run_all.sh` forwards those to
`bench.run_benchmark`; if `MODEL` is unset, the benchmark resolves the served
name from `/v1/models`. A manually managed full run must use the same exact
revisions as the server and a 64-hex fingerprint derived from its complete
launch policy. Prefer `run_models.sh`; never use a placeholder hash merely to
pass validation.

Run an open-loop Poisson diagnostic:

```bash
python -m bench.run_benchmark \
  --config configs/smoke.yaml \
  --config-label single-open \
  --mode open \
  --qps 4.0 \
  --base-url http://127.0.0.1:8000
```

Command-line flags override the YAML for that run. Useful overrides include
`--model`, `--model-revision`, `--model-label`, `--server-log`, `--tokenizer`,
`--tokenizer-revision`, `--serving-fingerprint`, `--repeat-id`,
`--trust-remote-code`, `--mode`, `--concurrency`, `--qps`, `--measure-s`,
`--warmup-s`, `--cooldown-s`, `--base-url`, `--out-dir`, `--power-backend`,
`--collect-hf-metadata`, and `--no-hf-metadata`. The multi-model wrapper
computes the serving fingerprint from the complete launch policy; for a manual
full-config run, supply that genuine 64-hex policy hash rather than leaving it
blank or fabricating one.

The benchmark log prints `point=<value>` for the x-axis load value being
measured. In `closed` mode, `point` is the fixed concurrency. In `open` mode,
`point` is the target `qps`.

## Running Dual-Node Benchmarks

The dual-node scripts assume a head node and a worker node connected through the
ConnectX interface configured in `scripts/connectx_env.sh`.

1. Verify host resolution, routing, latency, and interface counters from the
   head node:

```bash
scripts/verify_connectx.sh
```

2. Start Ray on the head node. As with `single` mode, `DROP_CACHES=1` flushes
   stale page cache first to avoid a false OOM on Grace unified memory (see
   below); it only affects the node it runs on, so set it on both head and
   worker:

```bash
DROP_CACHES=1 scripts/start_ray.sh head
```

3. Start Ray on the worker node:

```bash
DROP_CACHES=1 scripts/start_ray.sh worker
```

Before the first dual launch, fetch and validate the pinned snapshots on the
head and mirror them to the worker's independent cache:

```bash
scripts/predownload.sh all
CHECK_ONLY=1 scripts/predownload.sh all
scripts/mirror_worker_cache.sh all
```

4. Back on the head node, launch and strictly probe each tensor-parallel model,
   without starting the long sweep:

```bash
CANARY_ONLY=1 scripts/run_models.sh tp2
```

5. Run the provenance-complete tensor-parallel sweep:

```bash
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_models.sh tp2
```

6. For pipeline parallel serving, keep the Ray cluster running, canary it, then
   run the matching sweep:

```bash
CANARY_ONLY=1 scripts/run_models.sh pp2
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_models.sh pp2
```

7. Stop the Ray container on each node when finished:

```bash
scripts/start_ray.sh stop
```

For `tp2` and `pp2` the launcher runs a preflight over the Ray cluster before
starting `vllm serve`: it reads free memory on every node and fails fast if any
node is below `GPU_UTIL x total`, which is the same bound vLLM enforces per
worker at startup (on Grace unified memory, CUDA free memory tracks the host's
`MemFree`). As in single-node mode, a task pinned to each Ray node drops only
clean Hugging Face checkpoint pages; it never induces general memory pressure.
The preflight names any node that remains low and asks for an explicit
`DROP_CACHES=1` restart or removal of a live owner. Without the preflight, the
same condition surfaces minutes later as an opaque `EngineCore failed to
start` traceback from a remote Ray worker. If free memory stays tens of GiB
short with no owning process, `nvidia-smi` lists no processes, and `buff/cache`
is small, the NVIDIA driver has leaked unified memory from a killed CUDA
process — dropping caches will not recover it. Stop the Ray container
(`scripts/start_ray.sh stop`), reload the UVM module with
`sudo rmmod nvidia_uvm && sudo modprobe nvidia_uvm` (or reboot the node), then
restart Ray.

Before any dual model is loaded, `run_models.sh` pins one inspection task to
each Ray node with a 90-second deadline. It requires identical declared image
digests; Python, Ray, vLLM, PyTorch, and CUDA versions; normalized `pip freeze`
hashes; NCCL/Gloo/offline/compile environment; GPU model, compute capability,
driver; and GPU clock caps. The NVIDIA GB10 reports its software power limit as
`N/A`, so the preflight records that value and compares the exposed maximum
and application graphics clocks instead of claiming a power-limit check that
the platform cannot provide. The canonical facts are hashed into every dual
serving fingerprint, preventing a restarted cluster with different transitive
packages or worker policy from being resumed into an older curve.

Run the no-SSH Ray/NCCL interconnect microbenchmark from the head while both
GPUs are idle. Its completed CSV is written atomically beside the serving
results and is keyed to the exact cluster runtime and benchmark source:

```bash
scripts/ray_allreduce_bench.sh
```

The default acceptance floor is 1.8 GB/s at 1 GiB and the default 8-byte
collective-latency ceiling is 5 ms. The floor is calibrated to this cluster's
measured NCCL socket-transport plateau of ~2.08 GB/s (`NCCL_IB_DISABLE=1`,
MTU 1500, 200 GbE ConnectX, 2026-07-16), set ~13% below it so a healthy link
passes while a degraded or misrouted one fails. The command fails without
publishing an artifact if either threshold is missed. Override
`ALLREDUCE_MIN_1G_BUSBW_GBPS` or `ALLREDUCE_MAX_8B_LATENCY_S` only when the
paper explicitly defines and records a different acceptance policy; those
values are included in the artifact fingerprint.

`scripts/interconnect_microbench.sh` is an optional MPI/nccl-tests alternative
only for clusters where head-to-worker SSH is configured. It is not the default
for this no-SSH topology.

## Benchmarking Multiple Models

To characterize several models, list them in `configs/models.yaml` and drive them
with `scripts/run_models.sh`. Each entry names a Hugging Face model, a short
`label` used to tag results and plots, the serving configs to run it under
(`single`, `tp2`, `pp2`), and `revision`, plus optional `max_model_len`,
`gpu_util`, `quant`, `tokenizer`, `tokenizer_revision`, and
`trust_remote_code`. The shipped paper matrix pins every active model to an
immutable commit. `gpu_util` is either a scalar or a per-config mapping; a
missing value uses the uniform launcher default `0.80`. Keep it identical
across a model's configs so the KV budget does not confound single/tp2/pp2.
The tokenizer id and revision are passed to both vLLM and the client so exact
token-ID prompts match the served tokenizer. A model whose weights plus KV
cache do not fit on one GB10 must omit `single` and use the dual-node configs,
which pool two nodes over ConnectX for roughly twice the memory (a 70B model in
bf16 is dual-only).

`run_models.sh` takes one serving config at a time. It selects the models that
request that config, verifies all pinned revisions are already complete in the
local cache before spending GPU time, and tears down any stale server before
each launch. Readiness requires both the expected `/v1/models` id and a strict
streaming probe with delta token IDs, matching final usage,
`finish_reason: length`, and `[DONE]`; an old or merely HTTP-ready server cannot
pass. The wrapper then runs the concurrency sweep tagged with the model label
and tears the server down before the next model. Each launch writes a new,
never-overwritten log named
`OUT_DIR/server_<label>_<config>_<fingerprint-prefix>_<UTC>_pid<PID>.log` and an
immutable `OUT_DIR/serving_policy_<label>_<config>_<fingerprint>.json`. The log
is passed to the benchmark so exact per-worker memory, max-concurrency, and
other capacity figures are captured; the policy makes the launch inputs and
runtime identity independently inspectable.

```bash
# Fetch the exact model/tokenizer revisions before allocating a server. The
# local-only check is also run automatically by run_models.sh.
scripts/predownload.sh all
CHECK_ONLY=1 scripts/predownload.sh all

# Launch/probe/tear down every single-node model without starting a sweep.
CANARY_ONLY=1 scripts/run_models.sh single
# Or target an exact space-separated label allowlist. The automatic local-only
# cache check honors the same allowlist.
CANARY_ONLY=1 MODEL_LABELS='llama3.3-70b-fp8' scripts/run_models.sh single

# Single-node models (no Ray). Launches and removes a container per model.
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_models.sh single

# Dual-node models. Start Ray on both nodes first; run_models.sh then cycles
# only the vLLM serve process between models and leaves the Ray cluster up.
DROP_CACHES=1 scripts/start_ray.sh head        # on Diya
DROP_CACHES=1 scripts/start_ray.sh worker      # on Hoedus
# Each node has its OWN HF cache, and the worker can neither reach
# huggingface.co nor be SSH'd into. Mirror the weights to it over the
# cluster network first, or the launch dies with HFValidationError from
# the remote Ray worker. Idempotent; repos already on the worker are skipped.
scripts/mirror_worker_cache.sh all             # on Diya, after predownload.sh
CANARY_ONLY=1 scripts/run_models.sh tp2         # validate every distributed launch
CANARY_ONLY=1 scripts/run_models.sh pp2
scripts/run_models.sh tp2
scripts/run_models.sh pp2
scripts/start_ray.sh stop                      # on each node when finished

# Rerun a sweep that failed partway without repeating finished work. Consults
# OUT_DIR/index.csv and skips only structurally complete, compatible
# (repeat, concurrency) windows for the same model, config, methodology, YAML,
# harness, client runtime,
# serving policy, and observed live deployment. A fully covered model never
# launches; a partial sweep rechecks the new server before resuming gaps.
scripts/run_models.sh single --skip-done       # or SKIP_DONE=1
```

### Final unattended campaign

Finalize and commit the benchmark code and configuration before allocating the
GPUs. The campaign refuses tracked, staged, or untracked changes under the
fingerprinted benchmark paths (`bench`, `configs`, `scripts`,
`plot_results.py`, and the requirements files). Choose a fresh output directory
for methodology `steady-exact-v3`; do not copy rows from an earlier
methodology or checkout into it.

With the locked virtual environment active, run the hardware-free plan first:

```bash
export PYTHONNOUSERSITE=1
export OUT_DIR=results-steady-exact-v3
PLAN_ONLY=1 scripts/run_job.sh
```

The default plan must report 23 model/config combinations, 8 concurrencies,
and 1 repeat: 184 rows. At the configured 30-minute minimum phase duration,
that is at least 92 sequential point-hours (about 4 days), before model
launches and automatic boundary extensions. With 7 single-node and 16 dual-node
model/config combinations, it represents at least 156 GB10-hours of accelerator
use per pass; this is not “92 GPU-hours.” Extending to the full three-repeat
matrix (`FULL_REPEATS=3`, 552 rows) triples those figures to 276 sequential
point-hours (11.5 days) and 468 GB10-hours. Budget additional time for
canaries, model loads, cache work, all-reduce validation, and slow requests
that extend phase boundaries. Then start Ray on Diya and Hoedus as shown above
and run this on Diya inside `tmux`, using the same `OUT_DIR`:

```bash
scripts/run_job.sh
```

The job revalidates the clean commit and locked Python environment, records
`client_environment.json`, validates both caches and the two-node runtime,
records a successful no-SSH Ray all-reduce microbenchmark atomically, runs every
TP2 and PP2 launch canary, performs the dual sweeps at `FULL_REPEATS`, and runs
single-node last (because single mode replaces the Ray head container). A paper
point is published only with zero failed requests, at least 20 successful
requests, complete required power coverage, and complete unambiguous vLLM
metric series. Power and metric samplers must also pass a live preflight before
the long measurement window starts.

Every sweep leg uses `--skip-done`, so rerunning the same command fills only
compatible gaps. Resume is deliberately strict: if an existing row for the
same model/config comes from a different methodology, config, source, client
environment, or serving policy, the sweep fails before another expensive point
and tells you to choose a fresh `OUT_DIR`.

`REPEAT_CONCS` is empty by default. Publication results may only add complete
rectangles: when requesting more repeats, set `REPEAT_CONCS` to the same full
set as `CONCURRENCIES` and set `REPEAT_TOTAL` greater than `FULL_REPEATS`.
Partial-concurrency extra repeats are rejected because they would create an
unbalanced paper matrix.

Run only one campaign against this physical head/container pair at a time. The
orchestrators take a nonblocking campaign lock and fail if another sweep or
all-reduce job is already active. Keep the committed source and YAML unchanged
until the campaign is complete.

`scripts/predownload.sh` uses the same pinned image, cache mount, model ids, and
revisions as serving. `CHECK_ONLY=1` uses Hugging Face local-only resolution and
fails on a missing or partial snapshot; it never substitutes a newer cached
revision. Because each
dual node has its own cache, mirror the checked head cache before the dual
canary. `CANARY_ONLY=1` performs the full launch and strict SSE contract probe
for every selected model, including the on-the-fly FP8 and distributed paths,
but writes no benchmark points.

All runs append to one `OUT_DIR/index.csv` tagged by `model_label`, so a single
plotting invocation compares across models (see Outputs). The benchmark also
refuses an index whose header does not exactly match the current schema. Keep
pre-`steady-exact-v3` results in separate directories and never copy their rows
into the new index. Set `OUT_DIR=results-steady-exact-v3` consistently for
canaries, policies, server logs, all three serving configs, validation, and
plotting.

`run_job.sh` verifies the exact rectangle before plotting and again afterward,
when it atomically writes the final artifact manifest. To repeat that final
fail-closed check explicitly, pass the campaign's final repeat count
(`FULL_REPEATS`, or `REPEAT_TOTAL` when a repeat rectangle was added):

```bash
python scripts/validate_campaign.py verify \
  --models configs/models.yaml \
  --experiment configs/experiment.yaml \
  --out-dir "$OUT_DIR" \
  --concurrencies 1 2 4 8 16 32 64 128 \
  --repeats 1 \
  --manifest "$OUT_DIR/campaign_manifest.json"
```

The verifier requires exactly the planned rows and repeat IDs, validates the
indexed artifact sizes and SHA-256 digests, provenance consistency, request and
observability quality, server logs/policies, and the dual-node all-reduce CSV.
It also seals configs, trace (when used), dependency locks, benchmark/plot
source, client environment, raw data, telemetry, summaries, logs, policies,
all-reduce data, index, and figures into `campaign_manifest.json`.

Key environment variables for the multi-model wrapper: `MODELS_YAML` (default
`configs/models.yaml`), `OUT_DIR`, `BASE_URL`, `POWER_BACKEND`,
`CONCURRENCIES`, `CONFIG_YAML`, `REPEATS` (default `3`), `CANARY_ONLY`, and
`READY_TIMEOUT` (seconds to wait for a model to load, default `1800`).
`PROBE_TIMEOUT` bounds the two-token streaming canary (default `600`), and
`CACHE_PREFLIGHT=1` is the default; do not disable that local revision check for
a full run. The wrapper also
honors the launcher and network variables from the Configuration section, such
as `HF_TOKEN`, `HF_CACHE`, `VLLM_IMAGE`, `VLLM_CONTAINER`, `DROP_CACHES`, and
the ConnectX host/interface settings. The wrapper clears model-specific launch
overrides before applying each matrix entry, so an unrelated `QUANT`,
`TOKENIZER`, or `TRUST_REMOTE_CODE` value in `.env` cannot leak into the next
model.

## Workloads And Traces

The default `configs/experiment.yaml` workload spans short prompts and
long-context prompts:

```yaml
buckets:
  - {input_len: 128,  output_len: 128, weight: 1.0}
  - {input_len: 1024, output_len: 256, weight: 1.0}
  - {input_len: 4096, output_len: 256, weight: 1.0}
  - {input_len: 8192, output_len: 512, weight: 0.5}
```

With `use_token_ids: true` and a tokenizer configured, prompts are exact
token-id lists: randomized slices of a larger token buffer whose first tokens
are overwritten with a per-request counter tag, so every prompt is unique from
its first KV block and no request can hit another's cached prefix. The launcher
also disables vLLM prefix caching by default (`PREFIX_CACHING=1` re-enables):
random slices alone repeated often enough that cache hits skipped up to a third
of prefill work in some models while models whose launch had caching disabled
did all of it, which contaminated cross-model comparisons.

A JSONL trace can replace buckets by setting `trace_path` in the YAML. Each
line can contain:

```json
{"input_len": 1024, "output_len": 128}
{"input_len": 512, "max_tokens": 64, "prompt": "optional prompt text"}
```

Relative trace paths are resolved relative to the experiment YAML. The harness
hashes the trace bytes before a run and records `trace_sha256` in the resolved
configuration, summary, and index, so editing the trace invalidates resume even
when its path stays the same.

When `use_token_ids` is true, the harness preserves requested lengths by
building synthetic token-id prompts. When `use_token_ids` is false and a prompt
is present, the prompt text is sent directly. Trace lines define the request
*mix*, not a timestamped schedule. With `balanced_workload: true`, each trace
entry is used once per seeded, shuffled epoch; with it false, entries are
sampled randomly with replacement. Weighted bucket workloads use a smooth
weighted scheduler in balanced mode, so even a short contiguous request slice
closely follows the configured proportions.

## Outputs

Every measurement point writes these files under `out_dir`:

- `<run_tag>.raw.jsonl`: one JSON record per request, including TTFT, E2E,
  exact delta-token timestamps, final usage count, finish reason, stream-end
  state, inter-token gaps, success state, and error text.
- `<run_tag>.power.jsonl`: the timestamped raw head and, for dual runs, worker
  power samples used for window integration. It is written whenever a power
  backend is active.
- `<run_tag>.vllm_metrics.jsonl`: timestamped raw values and label sets for the
  required vLLM Prometheus series whenever metrics scraping is active.
- `<run_tag>.summary.json`: aggregate metrics, capacity samples, server-side
  token counters, the served model and label, a `server_info` block (capacity
  and launch configuration), optional energy metrics, the full resolved
  configuration, observed workload-bucket counts, and the actual
  `warmup_extension_s`, `load_extension_s`, and `drain_s` phase timings. It also
  carries methodology/config/source/serving/experiment provenance hashes and
  the client environment, live deployment hash, pinned revisions, and repeat
  number.
- `index.csv`: one row per measurement point for plotting sweeps. Each row is
  tagged with `model`, `model_label`, and `repeat_id` and carries the provenance,
  server-side counters, and capacity columns below alongside the client-side
  latency/throughput metrics. The benchmark refuses to append if the existing
  header is not exactly the current schema. It records the path, byte count,
  and SHA-256 for the raw request data, summary, raw power, raw vLLM metrics,
  and serving-policy artifact. All point artifacts are fsynced and atomically
  renamed before the index row can make that point resumable.

Campaign-level files in `OUT_DIR` include immutable `serving_policy_*.json`
launch records, unique `server_*.log` files, the fingerprinted
`allreduce_ray_*.csv`, `client_environment.json`, figures and tables under
`OUT_DIR/figs/`, and the final `campaign_manifest.json`. The manifest is the
paper bundle's integrity inventory; rerun the verifier after any intentional
figure regeneration.

When several models were run into one `index.csv` (see Benchmarking Multiple
Models), `plot_results.py` writes a cross-model `model_comparison` figure and a
`capacity_table`, then a full per-model figure set under `figs/<model_label>/`.
Restrict to one model with `--model <label>`.

The run tag has this shape:

```text
<config_label>_<mode>_<concurrency-or-qps>_r<repeat>_<YYYYmmdd-HHMMSS>-<nanoseconds>
```

When `--model-label` is explicit, as it is in `scripts/run_models.sh`, the run
tag is prefixed with that label:

```text
<model_label>_<config_label>_<mode>_<concurrency-or-qps>_r<repeat>_<YYYYmmdd-HHMMSS>-<nanoseconds>
```

Plot all rows in an index:

```bash
OUT_DIR="${OUT_DIR:-results-steady-exact-v3}"
python plot_results.py --index "$OUT_DIR/index.csv" --out "$OUT_DIR/figs" --format both
```

Available formats are `pdf`, `png`, and `both`. The plotting script writes
throughput scaling, latency-throughput, tail latency, goodput, capacity,
reliability, and energy figures when the needed columns are present. Series use
the colorblind-safe Okabe-Ito palette and are also distinguished by marker and
line style, so identity never rests on hue alone. Add `--serif` for a Times-like
body font matching an IEEE paper, and `--table` to also emit a paper-ready
per-point summary as `OUT_DIR/figs/summary_table.tex` (booktabs) and
`OUT_DIR/figs/summary_table.md`:

```bash
OUT_DIR="${OUT_DIR:-results-steady-exact-v3}"
python plot_results.py --index "$OUT_DIR/index.csv" --out "$OUT_DIR/figs" --format both \
  --serif --table
```

Two figures need a word on what they do and do not show:

- The reliability figure is two panels. The left panel is the real reliability
  signal: the share of *finished* requests that returned an error (this is zero
  across the reference sweep). The right panel is the share of requests that both
  started and completed inside the measurement window
  (`requests_started_and_completed_in_window / requests_completed`), using the
  same population of successful requests in numerator and denominator. At high
  concurrency it falls simply because long successful requests outlive the
  window. Failed requests appear only in the left panel. The
  conventional completion count, `requests_completed_in_window`, also includes
  requests that began during warmup and is therefore not used as that numerator.
  This is a windowing/backlog indicator, not the server dropping requests, and
  the panels are separated so the two are never conflated.
- The energy figure marks each dual-node (`tp2`, `pp2`) point whose row records
  `power_nodes < 2` (or predates that column) with an `x` and prints a footnote:
  that point's power covers the head node's GB10 only, so per-token energy is a
  lower bound and tokens/W an upper bound. Points with `power_nodes = 2` carry
  both nodes' draw and compare cleanly against single-node numbers.

Before drawing anything, the script prints a short data-quality report: per
configuration it lists the number of points, the measurement window(s) used,
peak observed throughput, and any point with zero completions in the window.
Mixed measurement windows within one series are called out, since windowed
rates stay comparable but the per-point sample sizes behind the percentiles do
not.

If the same `config_label` and load (concurrency or `qps`) appear more than
once in `index.csv` — a sweep run with `REPEATS>1`, or a rerun — the copies are
averaged into one point per load, a `note:` line reports how many copies back
it, and the min-max spread across copies is shaded behind the mean line for the
headline metrics. Failed or zero-completion copies remain in the aggregation;
dropping them would create survivorship bias. The plotter rejects duplicate
repeat IDs and refuses to combine rows when methodology, config/source/client
hashes, serving policy, live deployment, server capacity, power scope, or
measurement boundaries differ anywhere across the curve. Paper plots also fail
closed when the recorded intended concurrency set or repeat-ID rectangle is
incomplete. `--allow-incomplete` converts those failures to warnings only for
diagnostic plots; do not use it for publication figures. Separately, any point
whose *least populated
copy* has fewer than `--min-samples` successful requests (default 20) is circled
and listed in a warning. Tail latency uses the stricter `--min-p99-samples`
threshold (default 100), also per-copy. This shows up at the lowest loads, where
few requests start within the window even when the server keeps up; a longer
`--measure-s` gives more samples to trust.

## Metric Semantics

Warmup, measurement, and cooldown run as one continuous load phase. In
closed-loop mode, the logical measurement window opens only after `warmup_s`
has elapsed and every worker has completed its first request. The configured
cooldown is also a minimum: replacement load continues until all requests that
started in the measurement window finish, then the remaining replacements
drain. The summary reports `configured_warmup_s`, actual `warmup_s`,
`warmup_extension_s`, `load_extension_s`, and `drain_s`. The window's
`perf_counter` bounds (`window_start_perf`, `window_end_perf`) and a wall-clock
anchor (`window_start_unix`) are stored in
the summary so `raw.jsonl` token timestamps can be re-aggregated post hoc.

Two accounting sets share the window (this split is what fixed the 60s/600s
windowing artifacts, where completed-in-window throughput read near zero at
high concurrency while the server was busy):

- Throughput counts tokens *received during the window* across all requests,
  including requests started in warmup and requests still streaming when the
  window closes. It should track `server_gen_tps` closely at every load.
- Latency percentiles, goodput, and `requests_completed` cover requests
  *started in* the window and tracked to completion, even when they finish
  after it closes. Completed-in-window counts survive as
  `requests_completed_in_window` and `tok_thru_completed_tps` for the
  methodology diagnostic figure only. The stricter
  `requests_started_and_completed_in_window` is the intersection used for the
  reliability window-containment ratio.

Per-request metrics:

- `TTFT`: `t_first_token - t_start`, where the first token is the first
  returned delta token ID.
- `ITL`: gaps between adjacent returned token IDs. If one SSE event contains
  several IDs, they receive the same arrival timestamp and therefore have zero
  within-burst gaps. The summary `itl_s` block reports percentiles over all
  gaps from successful requests started in the measurement window.
- `mean_itl`: average ITL for one request. The summary `mean_itl_s` block
  reports percentiles over per-request means.
- `E2E`: `t_end - t_start`, including stream completion.

The vLLM-specific streaming contract is checked strictly: a successful record
must contain exact delta token IDs, a clean `[DONE]`, final usage matching the
ID count, the requested output length, and `finish_reason: length`. A truncated
or otherwise ambiguous stream remains in the raw data as a failed request
rather than silently becoming a shorter successful sample.

Aggregate metrics:

- `request_throughput_rps`: successful requests started in the window (tracked
  to completion) divided by `measure_s`.
- `output_token_throughput_tps`: tokens received during the window (all
  requests) divided by `measure_s`. Index column `tok_thru_tps`; this exact
  client token-ID rate is the headline throughput in every plot and table.
- `goodput_rps`: started-in-window successes per second whose TTFT is at or
  below `ttft_slo_s` and whose per-request mean ITL is at or below `itl_slo_s`.
- `goodput_itl2_rps`: the same, but against the looser `itl_slo_s2` ITL tier.
- Capacity: sampled from vLLM Prometheus metrics. The index uses
  `kv_cache_usage_*`, `requests_running_*`, and `requests_waiting_*` columns.
  The scraper accepts both `vllm:kv_cache_usage_perc` and the older
  `vllm:gpu_cache_usage_perc` metric name.
- Energy: when enabled, power samples over the same logical window produce
  `mean_power_w`, `energy_per_token_j`, and `tokens_per_watt`. On dual-node
  runs, `scripts/run_all.sh` additionally samples the worker's GPU through a
  Ray actor pinned to `node:<worker ip>` inside the serving container (there is
  no SSH path head to worker); `mean_power_w` then totals both nodes,
  `mean_power_w_head`/`mean_power_w_worker` carry the split, and `power_nodes`
  records coverage (a tp2/pp2 row with `power_nodes=1` means the worker sampler
  failed and that row's energy is a head-only lower bound; the plots mark that
  point automatically). `power_scope` records what the backend covers;
  for `nvidia_smi` it is `gpu_devices`, not whole-node or wall power. Power is
  linearly interpolated at both window boundaries and trapezoidally integrated;
  `mean_power_w` is that window energy divided by its duration.

Server-side counters (from vLLM `/metrics`, differenced over the logical window):

- `server_prompt_tokens`, `server_generation_tokens`,
  `server_prompt_tokens_cached`: prefill, decode, and prefix-cache-hit token
  counts the server actually processed during the window.
- `server_gen_tps`: the Prometheus generation-counter delta divided by the
  scraper's actual first-to-last sample span. It is a diagnostic, not the
  headline rate: scrape samples do not land exactly on the logical boundaries.
  The `server_vs_client_throughput` figure plots it against the exact client
  token-ID rate; a material gap is a diagnostic for stream/accounting or
  scrape-boundary differences.
- `num_preemptions`: requests vLLM preempted during the window, a capacity
  pressure signal.

Served-model capacity and launch configuration (constant per server, captured
once from `/v1/models`, vLLM `cache_config_info`, the optional server log, and
optionally the Hugging Face model config):

- `max_model_len`, `kv_cache_dtype`, `gpu_memory_utilization`, `block_size`,
  `gpu_blocks`: the launch-time KV cache configuration.
- `total_kv_tokens`: group-aware vLLM `kv_cache_size_tokens`, or the startup
  log's `GPU KV cache size` when supplied. `gpu_blocks * block_size` is used
  only for ordinary attention caches. Hybrid attention/Mamba models have
  physical blocks shared across cache groups, so that multiplication is not a
  valid token capacity; `hybrid_cache` and `total_kv_tokens_source` make the
  distinction explicit.
- `worker_weight_gb`, `worker_kv_cache_gb`, `max_concurrency`: exact per-worker
  vLLM startup-log facts when `--server-log` is provided. The multi-model
  orchestrator captures and passes this log automatically.
- `weight_gb`, `kv_cache_gb`: optional full-checkpoint/full-KV estimates from
  Hugging Face metadata when `collect_hf_metadata: true` or
  `--collect-hf-metadata` is used. These are useful as model descriptors, but
  for quantized or repartitioned serving runs the log-derived worker fields are
  the safer deployment facts.

The index-backed capacity numbers are collected into `capacity_table.tex`/`.md`
by `plot_results.py --table`.

## Choosing The Measurement Window

Throughput no longer depends on how many requests finish inside the window
(tokens are counted as they arrive), so the window-sizing question is about two
things instead: opening the window in steady state, and collecting enough
started-in-window requests to back the latency percentiles.

The history matters because it bit twice. With completed-in-window accounting,
a 60 s window reported literal zero throughput at high concurrency while the
server was healthy and busy, and a 600 s window still understated large dense
models by 5-10x at concurrency 64-128 (client/server throughput ratios of
0.1-0.7), because a single request's end-to-end time reached 500+ s. On top of
that, warmup used to run as a separate loop that drained completely, so the
window opened on an idle server hit by a thundering herd of cold prefills.
Both are structural, not tuning, problems; token-in-window accounting and the
continuous phase plus automatic boundary extensions fix them. The
`server_vs_client_throughput` figure is the regression check: the client and
server curves should now track closely at every load, with the dotted
completed-only curve showing what the old accounting would have read.

Guidance:

- Treat `warmup_s` as the minimum stabilization time. In closed-loop mode the
  harness automatically waits longer if any worker's initial request is still
  running, so the shipped `120` seconds cannot open the window in the middle of
  a slow first wave: at high concurrency on the slowest models the first-wave
  rule stretches warmup to the worst request lifetime automatically.
- Set `measure_s` long enough for each *repeat* to back the percentile you will
  report. The shipped full-run window is `1500` seconds (25 minutes), a budget
  compromise that backs p50/p90 at every point but leaves the slowest c=1
  points with ~30-50 completions, below the 100-sample p99 target; the plots
  flag those p99 points as low-n instead of reporting them as reliable.
  Inspect the plotter's per-copy sample warnings after the run.
- Treat `cooldown_s` as a minimum (shipped `180`). If a measurement-started
  request remains active, the harness keeps replacement load running until it
  finishes, preventing drain-down bias without guessing the worst lifetime.
- Keep `request_timeout_s` above the worst queue-plus-stream lifetime. The
  shipped `14400` seconds gives measurement-started requests room to finish
  under severe backlog instead of converting slow successes into client
  timeouts.
- Aim for at least 20 started-in-window requests for medians and at least 100
  for p99; these are the plotting defaults. Prefer many more at loads you will
  quote.
- Keep the window consistent across a sweep and across configs you compare.
  `plot_results.py` still plots mixed-window sweeps but flags them.
- Run `REPEATS=3` (or rerun the sweep) at least for the points you will quote,
  so the figures carry run-to-run spread instead of a single sample.

## Power Telemetry

The lightweight smoke config sets `power_backend: none`; the full
`configs/experiment.yaml` enables `nvidia_smi`. The script wrappers defer to
whichever YAML they run (they only pass `--power-backend` when the
`POWER_BACKEND` environment variable is set), so sweeps sample power by default
with `configs/experiment.yaml`. Override per run with `POWER_BACKEND` for
sweeps or `--power-backend` for a single point:

```bash
POWER_BACKEND=nvidia_smi scripts/run_models.sh single
# Smoke/diagnostic config: power is deliberately optional.
CONFIG_YAML=configs/smoke.yaml POWER_BACKEND=none CONCURRENCIES=1 REPEATS=1 \
  scripts/run_all.sh single
python -m bench.run_benchmark --power-backend sysfs --config configs/smoke.yaml
```

Changing the backend does not weaken the full config's coverage requirement.
To make power optional for a non-paper experiment, use a separate YAML with
`require_power_data: false`; merely setting `POWER_BACKEND=none` against the
full config correctly fails validation.

Backends:

- `nvidia_smi`: reads GPU power from `nvidia-smi`.
- `sysfs`: reads a hardware monitor file named by `power_sysfs_path`; values are
  interpreted as microwatts.
- `tegrastats`: parses named rails from `power_rails`, or all matched rails if
  the list is empty.

Validate the backend on DGX Spark before using energy numbers. `nvidia_smi`
reports the GPU device/board telemetry domain, not whole-GB10-module,
whole-node, system, or wall power. The index records this as
`power_scope=gpu_devices`, and plots label it as GPU energy.

For dual-node runs, a local sampler only sees the head node's GPU. There is no
SSH path from head to worker, so `scripts/run_all.sh` (for `tp2`/`pp2` labels)
also passes `--worker-power-container`/`--worker-power-node-ip`, and the
benchmark samples the worker's GPU through a tiny Ray actor pinned to
`node:<worker ip>` inside the serving container. `mean_power_w` then totals
both nodes and `power_nodes=2` marks the row as fully covered. The general
harness can describe a head-only result when a worker sampler fails. The
shipped full config is stricter: `require_power_data: true` rejects the point
instead of writing it unless a single run has one node and a tp2/pp2 run has
both nodes with positive, dense samples bracketing the full window. It likewise sets
`require_vllm_metrics_data: true`, so missing request-running/waiting, KV-usage,
prompt/generation-token, or preemption samples; a scrape gap over five seconds;
an incomplete counter span; or a non-advancing generation counter while the
client receives tokens rejects the point. Disable these requirements only for an explicitly
non-paper diagnostic run.

## Troubleshooting

- `ModuleNotFoundError` for `pandas`, `aiohttp`, or `yaml`: activate the Python
  3.12+ virtual environment, reinstall `requirements.lock`, and rerun
  `python scripts/check_environment.py` with `PYTHONNOUSERSITE=1`.
- `Connection refused` or model resolution fails: confirm vLLM is running and
  `curl http://127.0.0.1:8000/v1/models` works from the benchmark client.
- `CHECK_ONLY=1` or the automatic model-cache preflight reports a missing
  revision: run `scripts/predownload.sh <config>` while Hub access is available,
  then repeat the check. Do not drop the revision or enable online serving to
  make the error disappear; that would change or leave ambiguous the model
  being measured. For tp2/pp2, mirror the checked snapshot to the worker too.
- `/v1/models` works but the readiness probe fails: inspect the server log and
  try the same two-token streaming completion manually. The benchmark requires
  vLLM's delta `token_ids`, final usage, `finish_reason: length`, and `[DONE]`;
  a proxy or server that strips those fields is not compatible with exact-token
  accounting.
- Smoke test works but real runs are slow or fail: the real config uses much
  longer prompts and outputs. Reduce buckets temporarily or increase
  `request_timeout_s`.
- vLLM returns `400 Bad Request` with a maximum context length message: make
  sure `MAXLEN`/`--max-model-len` is at least the largest `input_len +
  output_len` in the workload. The default experiment needs `8704`.
- `goodput=0.00` with successful requests: the run completed, but the measured
  TTFT or mean ITL did not meet `ttft_slo_s`/`itl_slo_s`. Check `goodput_itl2_rps`
  (the looser `itl_slo_s2` tier) for a less strict service target, or loosen the
  SLOs. On bandwidth-bound hardware, dense models can floor above a strict ITL
  even unloaded, so a strict `goodput_rps` of 0 across the sweep is expected, not
  a bug.
- `PyTorch was not found` from Transformers on the benchmark client is expected
  and harmless: `requirements.txt` intentionally does not install PyTorch
  because the harness only loads tokenizers and model configs locally. vLLM and
  PyTorch run inside the serving container.
- `index schema mismatch` on a run or `Expected N fields` in plotting: the index
  columns changed between versions and a new run tried to append to an old
  `index.csv`. Write to a fresh `OUT_DIR` (or move the old index aside) and plot
  the old and new indexes separately.
- `required provenance is incomplete` under `configs/experiment.yaml`: use
  `run_models.sh` so the exact 40-hex revisions and genuine 64-hex launch-policy
  fingerprint are forwarded together. A manual server needs the same evidence;
  do not use an all-zero, random, or otherwise fabricated hash. For plumbing
  diagnostics that make no paper claim, use `configs/smoke.yaml`.
- `required power coverage is incomplete` or required vLLM metrics are missing:
  the full config deliberately did not write a paper row. Check `nvidia-smi`,
  `/metrics`, both Ray nodes, and the worker-power actor, then rerun the point.
  Use a separate diagnostic YAML with the corresponding `require_*` field false
  only when incomplete observability is intentional.
- An on-the-fly FP8 launch fails while serializing an AOTAutograd graph cache
  (`PicklingError`): confirm `TORCHINDUCTOR_AUTOGRAD_CACHE=0` reaches the vLLM
  and Ray containers. This disables the problematic disk cache, not graph
  compilation, and is the uniform default for all configs.
- `run_models.sh` skips a model with a timeout or "launcher exited": that model
  failed to come up (often OOM). Check the newest matching
  `OUT_DIR/server_<label>_<config>_*.log` (logs are never overwritten);
  lower its `gpu_util` in `configs/models.yaml`, add a `quant`, or move it to the
  dual-node configs if it does not fit on one GB10. The orchestrator continues
  to the next model rather than aborting the whole sweep.
- `weight_gb`/`kv_cache_gb` blank in the index: the model config or Hub metadata
  could not be fetched (offline or gated without `HF_TOKEN`). HTTP-derived
  launch fields such as `gpu_blocks` and `block_size` still populate. For a
  hybrid attention/Mamba cache, `total_kv_tokens` is intentionally left blank
  unless vLLM exposes a group-aware token capacity or `server_log` supplies its
  startup value; reporting `gpu_blocks * block_size` there would be wrong.
- Tokenizer download fails: set `HF_TOKEN`, log in with the Hugging Face CLI, or
  set `tokenizer` to a local tokenizer path.
- Tokenizer or config download fails with `PermissionError` under
  `~/.cache/huggingface`: the serving container downloads models into the
  mounted cache as root, which leaves it root-owned on the host. The benchmark
  detects this and automatically switches its own small downloads to
  `~/.cache/huggingface-client` (printing a `[bench]` note). To share one cache
  again, `sudo chown -R "$USER" ~/.cache/huggingface` — but expect the container
  to re-create root-owned directories for newly downloaded models.
- Docker cannot access the GPU: verify `docker run --gpus all ... nvidia-smi`
  works with your container runtime.
- Dual-node launch says Ray is not active: run `scripts/start_ray.sh head` on
  the head node and `scripts/start_ray.sh worker` on the worker node, then check
  `docker exec vllm-spark ray status`.
- `tp2`/`pp2` launch dies with `HFValidationError: Repo id must be in the form
  ...` quoting a `/root/.cache/huggingface/...` path, raised by a
  `RayWorkerWrapper` at the worker's IP: the model's weights are not in the
  worker node's HF cache (each node has its own; `predownload.sh` only warms
  the node it runs on). Run `scripts/mirror_worker_cache.sh tp2|pp2|all` from
  the head and relaunch.
- `PREFLIGHT FAILED` for local memory, or `low free memory` before a tp2/pp2
  launch: both preflights already drop clean file pages from the Hugging Face
  cache without applying anonymous-memory pressure. A remaining
  shortage may be unrelated clean cache (retry with `DROP_CACHES=1`), another
  process on that node (check `ps aux --sort=-rss`), or a `gpu_util` too high
  for its baseline usage (`util x 119.6 GiB` must fit in `MemFree`).
- `tp2`/`pp2` launch fails with `ray.exceptions.OutOfMemoryError: Task was
  killed due to the node running low on memory` naming one node's IP: Ray's
  node memory monitor killed a worker, almost always stale page cache on
  Grace unified memory rather than a real leak (the reported "top memory
  users" list won't account for most of the usage). Restart Ray on both nodes
  with `DROP_CACHES=1` (see step 2 above) before relaunching.
- Same `ray.exceptions.OutOfMemoryError`, but it happens later, right after
  `Available KV cache memory: ...` logs during model load rather than at Ray
  startup: this is a real overcommit, not stale cache. On Grace unified
  memory, `--gpu-memory-utilization` reserves from the same pool the OS,
  Docker, and Ray need, so `0.90` (fine on a discrete-GPU box) can leave too
  little headroom for Ray's 95% node memory threshold once weights and KV
  cache are both resident. `scripts/launch_server.sh` defaults `GPU_UTIL` to
  `0.80` for `tp2`/`pp2` for this reason; lower it further with `GPU_UTIL=0.7
  scripts/launch_server.sh tp2` if it recurs.
- Dual-node performance looks like Wi-Fi or management-network traffic: rerun
  `scripts/verify_connectx.sh`, check `CX_IFACE`, and watch interface byte
  counters during a `tp2` run.
- `interconnect_microbench.sh` build fails: install MPI/NCCL build
  dependencies, verify network access to GitHub, or set `NCCL_TESTS_DIR` to an
  existing MPI-enabled nccl-tests checkout.
- No energy figure is generated: rerun with a non-`none` power backend. The
  plotting script skips energy when the index has no power columns.
- `plot_results.py` reports `Expected N fields` (and the schema versions match):
  an older `index.csv` row was likely missing a trailing newline before another
  run appended, concatenating two rows. New runs guard against this; split the
  concatenated row or start a fresh `OUT_DIR`.

## Development Checks

Useful checks after editing the repository:

```bash
export PYTHONNOUSERSITE=1
python scripts/check_environment.py
OUT_DIR=results-steady-exact-v3 PLAN_ONLY=1 scripts/run_job.sh
python -m compileall bench plot_results.py scripts/models_for_config.py
bash -n scripts/*.sh
python -m pip check
python scripts/models_for_config.py configs/models.yaml single
python plot_results.py --index results/index.csv --out /tmp/dgx-spark-figs --format png --table
```

`shellcheck` and `ruff` are not required by the repository, but they are good
extra checks if installed locally.

## PMBS26 Mapping Checklist

- Section 5.1 single Spark: run the `single` closed-loop concurrency sweep.
- Sections 5.2 and 5.3 dual Spark: run `tp2` and `pp2` sweeps with the same
  model, tokenizer, workload, SLO, and measurement window.
- Multiple models: list them in `configs/models.yaml` and drive them with
  `scripts/run_models.sh <config>` (see Benchmarking Multiple Models). Report the
  cross-model `model_comparison` figure and `capacity_table` from
  `plot_results.py --table`.
- Capacity: use `kv_cache_usage_*` and `requests_waiting_*` columns plus the
  per-run `vllm_metrics` summaries. Use exact client `tok_thru_tps` as the
  throughput result and `server_gen_tps` as the counter/scrape diagnostic. The
  `weight_gb`, `kv_cache_gb`, `total_kv_tokens`, `gpu_blocks`, `block_size`,
  `kv_cache_dtype`, `gpu_memory_utilization`, and `max_model_len` columns give
  the served-model capacity and launch configuration for the platform/model
  table.
- Energy: enable a validated power backend and report the summary `energy`
  block for the same window as throughput. For `nvidia_smi`, describe it as GPU
  energy (`power_scope=gpu_devices`), not system or wall energy.
- Interconnect model validation: record the fingerprinted
  `allreduce_ray_*.csv` from `scripts/ray_allreduce_bench.sh` alongside the
  serving sweep.
- `tp2`/`pp2` apparent capacity ceiling: on a ConnectX Ethernet link (not
  NVLink), cross-node all-reduce (`tp2`) or activation passing (`pp2`) does add
  per-token overhead, so dual-node ITL is higher than `single` at matched load.
  But an apparent *collapse* — completions per window falling toward zero at high
  concurrency while KV cache occupancy stays low — is usually a measurement
  artifact of too short a window, not the interconnect. Confirm before reporting
  it: rerun the point with a longer `--measure-s` (see "Choosing The Measurement
  Window"). In the reference sweep the collapse did not survive a longer window
  (`tp2` c=64 went 0 -> 141 tok/s from 60 s to 240 s; `pp2` c=128 went 0 -> 135
  tok/s from 60 s to 360 s), so the interconnect cost shows up as higher latency,
  not as a throughput cliff. Record the fingerprinted Ray/NCCL all-reduce CSV
  alongside the sweep to attribute the latency component.
