# DGX Spark LLM Serving Benchmark

This repository contains a client-side benchmarking harness for LLM serving on
DGX Spark. It drives a vLLM OpenAI-compatible server, measures streaming latency
and throughput, optionally samples power and vLLM Prometheus metrics, and writes
CSV/JSON outputs that can be plotted for single-node, dual-node tensor parallel,
and dual-node pipeline parallel experiments.

The default request path is the vLLM completions endpoint (`/v1/completions`).
Requests are streaming, greedy requests with `max_tokens == min_tokens`,
`ignore_eos: true`, and `temperature: 0.0` so output length is controlled by the
workload definition. Output tokens are counted as content-bearing streaming
chunks, which matches the validated vLLM greedy streaming behavior.

## Quick Start

Run these commands from the repository root.

1. Create and activate a Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

2. Check the local client tools:

```bash
python -m bench.run_benchmark --help
python plot_results.py --help
python -m unittest discover -s tests -v
```

3. Start a vLLM server. If you are using the included Docker launcher on one
   Spark node, run this in one terminal:

```bash
export MODEL=Qwen/Qwen3-8B
# export HF_TOKEN=...    # only needed for gated Hugging Face models
scripts/launch_server.sh single
```

The launch command stays in the foreground while vLLM is running. If you already
have a vLLM-compatible server, skip the launcher and use its URL as `BASE_URL` or
`--base-url`.

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
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_all.sh single
```

6. Plot the sweep:

```bash
python plot_results.py --index results/index.csv --out figs --format both
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
  load_env.sh                 source a repo-local .env for script entrypoints
  connectx_env.sh             shared ConnectX and container settings; loads .env
  verify_connectx.sh          route and latency sanity checks
  start_ray.sh                start or stop the Ray container for dual-node runs
  launch_server.sh            start vLLM in single, tp2, or pp2 mode
  interconnect_microbench.sh  nccl-tests all-reduce check
  run_all.sh                  sweep a concurrency list for one config
  run_models.sh               sweep several models for one config, one at a time
  models_for_config.py        select models.yaml entries for a serving config
plot_results.py               generate PDF/PNG figures from results/index.csv
tests/                        standard-library regression tests for release paths
requirements.txt              Python client and plotting dependencies
```

## Prerequisites

Client-side requirements:

- Python 3.10 or newer. The validation checks in this workspace used Python
  3.12, but the code is not tied to that exact version.
- Python packages from `requirements.txt`.
- Network access from the benchmark client to the vLLM server and its `/metrics`
  endpoint.
- A Hugging Face tokenizer matching the served model for exact input lengths.
  Without a tokenizer, prompts fall back to repeated words and input lengths are
  approximate.

Server-side requirements for the included scripts:

- Docker with NVIDIA GPU runtime support.
- Permission to run `docker` as the current user.
- A vLLM container image, defaulting to `nvcr.io/nvidia/vllm:26.04-py3`.
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
- `api_key`: bearer token sent in the `Authorization` header. The vLLM default
  is usually `EMPTY`.
- `tokenizer`: Hugging Face tokenizer id or local path. Set to `""` only for
  quick approximate checks.
- `use_token_ids`: when true, send exact token-id prompts built with the
  tokenizer.
- `buckets`: weighted synthetic workload buckets with `input_len`,
  `output_len`, and `weight`.
- `trace_path`: optional JSONL trace that replaces `buckets`.
- `seed`: random seed for bucket/trace sampling and synthetic prompt slices.
- `mode`: `closed` for fixed concurrency or `open` for Poisson arrivals.
- `concurrency`: number of closed-loop workers.
- `qps`: offered request rate for open-loop mode.
- `max_inflight`: open-loop cap on concurrently in-flight requests.
- `warmup_s`, `measure_s`, and `cooldown_s`: one continuous load phase; the
  logical measurement window is `[warmup_s, warmup_s + measure_s)` within it.
  Load is never dropped at the window boundaries: warmup holds full concurrency
  into the window (a separate warmup loop used to drain completely and reopen
  the window on an idle server), and the cooldown keeps offered load up while
  requests started near the window's end finish, so their latency is measured
  under load. Size `warmup_s` at or above the worst single-request lifetime at
  the highest concurrency, and `cooldown_s` around its p50.
- `request_timeout_s`: per-request HTTP timeout, including queueing and stream
  drain. Keep it well above the worst-case E2E at the highest load in the
  sweep, or slow-but-successful requests are counted as failures; the shipped
  default is `3600`.
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

Common script environment variables:

Every script entrypoint sources `scripts/load_env.sh`, which loads a repo-local
`.env` file if it exists, so these can be saved there instead of exported in
every shell session. `.env` is ignored by git because it may contain `HF_TOKEN`.
It is loaded once per process tree (guarded by `DGX_SPARK_ENV_LOADED`), so a
wrapper that sources it and then launches child scripts does not re-load it in
the children; that is what lets `run_models.sh` pass explicit per-model
`MODEL`, `QUANT`, `GPU_UTIL`, etc. through to the launcher without `.env`
clobbering them.

`.env` is sourced as shell code, so a plain assignment in `.env` overrides a
one-shot inline assignment such as `CONCURRENCIES="1" scripts/run_all.sh single`.
If you want inline values to win, write defaults in `.env` with shell parameter
expansion, for example:

```bash
export CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
```

Set `ENV_FILE=/path/to/another.env` to load a different environment file for a
script invocation.

```bash
export MODEL=Qwen/Qwen3-8B
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
# export QUANT=fp8
# export TOKENIZER=Qwen/Qwen3-8B
# export TRUST_REMOTE_CODE=1
export VLLM_IMAGE=nvcr.io/nvidia/vllm:26.04-py3
export VLLM_CONTAINER=vllm-spark
export HF_TOKEN=...
export HF_CACHE=$HOME/.cache/huggingface
export BASE_URL=http://127.0.0.1:8000
export OUT_DIR=results
export CONFIG_YAML=configs/experiment.yaml
export CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
# unset defers to the YAML's power_backend; set to override (e.g. none)
# export POWER_BACKEND=nvidia_smi
export MODELS_YAML=configs/models.yaml
export READY_TIMEOUT=1200
export RAY_VERSION=2.53.0
# interpreter used by the sweep scripts; default python3 (a venv's python3
# when the venv is active)
# export PYTHON=python3
```

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
MODEL=Qwen/Qwen3-8B scripts/launch_server.sh single
```

`MAXLEN` is passed to vLLM as `--max-model-len`, which counts prompt tokens plus
generated tokens. The default full workload needs at least `8704` because its
largest request is `8192` input tokens and `512` output tokens. The launcher
defaults to `8704`; if you lower it, also lower the largest workload bucket.

On Grace unified memory, stale page cache can cause false out-of-memory errors
when loading large models. Set `DROP_CACHES=1` to flush it before launch. This
requires sudo:

```bash
DROP_CACHES=1 MODEL=Qwen/Qwen3-8B scripts/launch_server.sh single
```

Run one measurement point:

```bash
python -m bench.run_benchmark \
  --config configs/experiment.yaml \
  --config-label single \
  --mode closed \
  --concurrency 16 \
  --base-url http://127.0.0.1:8000
```

Run a full closed-loop sweep:

```bash
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_all.sh single
```

Run against a remote or separately managed vLLM server:

```bash
BASE_URL=http://spark-node.example.com:8000 scripts/run_all.sh single
```

When you already know the served model or want stable plot labels, pass
`MODEL`, `MODEL_LABEL`, `TOKENIZER`, `TRUST_REMOTE_CODE` (for models that ship
custom code), and optionally `SERVER_LOG` through the environment. `run_all.sh`
forwards those to `bench.run_benchmark`; if `MODEL` is unset, the benchmark
resolves the served name from `/v1/models`.

Run an open-loop Poisson point:

```bash
python -m bench.run_benchmark \
  --config configs/experiment.yaml \
  --config-label single-open \
  --mode open \
  --qps 4.0 \
  --base-url http://127.0.0.1:8000
```

Command-line flags override the YAML for that run. Useful overrides include
`--model`, `--model-label`, `--server-log`, `--tokenizer`,
`--trust-remote-code`, `--mode`, `--concurrency`, `--qps`, `--measure-s`,
`--warmup-s`, `--base-url`, `--out-dir`, `--power-backend`,
`--collect-hf-metadata`, and `--no-hf-metadata`.

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

4. Back on the head node, launch tensor parallel serving:

```bash
MODEL=Qwen/Qwen3-8B scripts/launch_server.sh tp2
```

5. Run the matching sweep from the benchmark client:

```bash
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_all.sh tp2
```

6. For pipeline parallel serving, keep the Ray cluster running and launch:

```bash
MODEL=Qwen/Qwen3-8B scripts/launch_server.sh pp2
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_all.sh pp2
```

7. Stop the Ray container on each node when finished:

```bash
scripts/start_ray.sh stop
```

For `tp2` and `pp2` the launcher runs a preflight over the Ray cluster before
starting `vllm serve`: it reads free memory on every node and fails fast if any
node is below `GPU_UTIL x total`, which is the same bound vLLM enforces per
worker at startup (on Grace unified memory, CUDA free memory tracks the host's
`MemFree`). The preflight names the low node; without it the same condition
surfaces minutes later as an opaque `EngineCore failed to start` traceback
from a remote Ray worker. On the low node, check for a stale process
(`ps aux --sort=-rss`) and stale page cache (`DROP_CACHES=1`, above). If free
memory stays tens of GiB short with no owning process, `nvidia-smi` lists no
processes, and `buff/cache` is small, the NVIDIA driver has leaked unified
memory from a killed CUDA process — dropping caches will not recover it. Stop
the Ray container (`scripts/start_ray.sh stop`), reload the UVM module with
`sudo rmmod nvidia_uvm && sudo modprobe nvidia_uvm` (or reboot the node), then
restart Ray.

Run the interconnect microbenchmark from the head node; the log is written to
`OUT_DIR` (default `results/`) beside the serving results:

```bash
HOST0=Diya HOST1=Hoedus scripts/interconnect_microbench.sh
```

If `nccl-tests` is not already built, the script clones
`https://github.com/NVIDIA/nccl-tests` into `$HOME/nccl-tests` and builds it
with `MPI=1`. Set `NCCL_TESTS_DIR` to use an existing checkout.

## Benchmarking Multiple Models

To characterize several models, list them in `configs/models.yaml` and drive them
with `scripts/run_models.sh`. Each entry names a Hugging Face model, a short
`label` used to tag results and plots, the serving configs to run it under
(`single`, `tp2`, `pp2`), and optional `max_model_len`, `gpu_util`, `quant`,
`tokenizer`, and `trust_remote_code`. `max_model_len`, `gpu_util`, `quant`,
`tokenizer`, and `trust_remote_code` are passed to the launcher for that model;
`gpu_util` is either a scalar or a per-config mapping such as
`{single: 0.90, tp2: 0.85}` (a config missing from the mapping uses the
launcher default, 0.90 single / 0.80 dual);
the tokenizer is also passed to the benchmark client so exact token-id prompts
match the served tokenizer. A model whose weights plus KV cache do not fit on
one GB10 must omit `single` and use the dual-node configs, which pool two nodes
over ConnectX for roughly twice the memory (a 70B model in bf16 is dual-only).

`run_models.sh` takes one serving config at a time. It selects the models that
request that config, and for each one launches a server, waits until
`/v1/models` reports it, runs the concurrency sweep tagged with the model label,
then tears the server down before the next model. Server stdout is teed to
`OUT_DIR/server_<label>_<config>.log` and passed to the benchmark so exact
per-worker memory, max-concurrency, and other capacity figures are captured.

```bash
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
scripts/run_models.sh tp2
scripts/run_models.sh pp2
scripts/start_ray.sh stop                      # on each node when finished

# Rerun a sweep that failed partway without repeating finished work. Consults
# OUT_DIR/index.csv and skips every closed-mode point already recorded for a
# model at the current measure_s; a fully covered model never launches its
# server, a partially swept one resumes with just the missing concurrencies.
scripts/run_models.sh single --skip-done       # or SKIP_DONE=1
```

All runs append to one `OUT_DIR/index.csv` tagged by `model_label`, so a single
plotting invocation compares across models (see Outputs). Use a fresh `OUT_DIR`
the first time on an upgraded checkout: the index gained columns, and the
benchmark refuses to append rows to an index whose header does not match, to
avoid silently misaligning columns.

Key environment variables for the multi-model wrapper: `MODELS_YAML` (default
`configs/models.yaml`), `OUT_DIR`, `BASE_URL`, `POWER_BACKEND`,
`CONCURRENCIES`, `CONFIG_YAML`, and `READY_TIMEOUT` (seconds to wait for a model
to load, default 1200). It also honors the launcher and network variables from
the Configuration section, such as `HF_TOKEN`, `HF_CACHE`, `VLLM_IMAGE`,
`VLLM_CONTAINER`, `DROP_CACHES`, and the ConnectX host/interface settings. Trim
`configs/models.yaml` to your target models before running; the shipped file is
an example matrix.

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

When `use_token_ids` is true, the harness preserves requested lengths by
building synthetic token-id prompts. When `use_token_ids` is false and a prompt
is present, the prompt text is sent directly. Trace lines define the request
*mix*, not a schedule: the load generator samples them randomly with
replacement, so they are not replayed in file order or with trace timestamps.

## Outputs

Every measurement point writes these files under `out_dir`:

- `<run_tag>.raw.jsonl`: one JSON record per request, including TTFT, E2E,
  token timestamps, inter-token gaps, success state, and error text.
- `<run_tag>.summary.json`: aggregate metrics, capacity samples, server-side
  token counters, the served model and label, a `server_info` block (capacity
  and launch configuration), optional energy metrics, the full resolved
  configuration, and `drain_s`.
- `index.csv`: one row per measurement point for plotting sweeps. Each row is
  tagged with `model` and `model_label` and carries the server-side counters and
  capacity columns below alongside the client-side latency/throughput metrics.

When several models were run into one `index.csv` (see Benchmarking Multiple
Models), `plot_results.py` writes a cross-model `model_comparison` figure and a
`capacity_table`, then a full per-model figure set under `figs/<model_label>/`.
Restrict to one model with `--model <label>`.

The run tag has this shape:

```text
<config_label>_<mode>_<concurrency-or-qps>_<YYYYmmdd-HHMMSS>
```

When `--model-label` is explicit, as it is in `scripts/run_models.sh`, the run
tag is prefixed with that label:

```text
<model_label>_<config_label>_<mode>_<concurrency-or-qps>_<YYYYmmdd-HHMMSS>
```

Plot all rows in an index:

```bash
python plot_results.py --index results/index.csv --out figs --format both
```

Available formats are `pdf`, `png`, and `both`. The plotting script writes
throughput scaling, latency-throughput, tail latency, goodput, capacity,
reliability, and energy figures when the needed columns are present. Series use
the colorblind-safe Okabe-Ito palette and are also distinguished by marker and
line style, so identity never rests on hue alone. Add `--serif` for a Times-like
body font matching an IEEE paper, and `--table` to also emit a paper-ready
per-point summary as `figs/summary_table.tex` (booktabs) and
`figs/summary_table.md`:

```bash
python plot_results.py --index results/index.csv --out figs --format both \
  --serif --table
```

Two figures need a word on what they do and do not show:

- The reliability figure is two panels. The left panel is the real reliability
  signal: the share of *finished* requests that returned an error (this is zero
  across the reference sweep). The right panel is the share of requests that both
  started and completed inside the measurement window
  (`requests_completed_in_window / requests_started`); at high concurrency it
  falls simply because long requests outlive the window. That is a
  windowing/backlog indicator, not the server dropping requests, and the panels
  are separated so the two are never conflated.
- The energy figure daggers any dual-node (`tp2`, `pp2`) series whose rows
  record `power_nodes < 2` (or predate that column) and prints a footnote:
  those rows' power covers the head node's GB10 only, so per-token energy is a
  lower bound and tokens/W an upper bound. Series with `power_nodes = 2` carry
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
headline metrics; a copy with zero completed requests (a dead-server row) is
dropped rather than averaged when a healthy copy exists. Separately, any point
whose percentiles rest on fewer than `--min-samples` requests (default 20) is
circled in the figures and listed in a `warning:` line, since its percentiles
are small-sample noise rather than a real reading. This shows up at the lowest
loads, where few requests start within the window even when the server keeps
up; a longer `--measure-s` gives more samples to trust.

## Metric Semantics

Warmup, measurement, and cooldown run as one continuous load phase; the logical
measurement window is the `measure_s` seconds starting `warmup_s` into it. The
load generator stops launching new requests when the cooldown ends and drains
in-flight requests to completion; the drain duration is reported as `drain_s`
in the summary. The window's `perf_counter` bounds (`window_start_perf`,
`window_end_perf`) and a wall-clock anchor (`window_start_unix`) are stored in
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
  methodology diagnostic figure only.

Per-request metrics:

- `TTFT`: `t_first_token - t_start`, where the first token is the first
  content-bearing streaming chunk.
- `ITL`: gaps between adjacent content-bearing streaming chunks. The summary
  `itl_s` block reports percentiles over all token gaps completed in the
  measurement window.
- `mean_itl`: average ITL for one request. The summary `mean_itl_s` block
  reports percentiles over per-request means.
- `E2E`: `t_end - t_start`, including stream completion.

Aggregate metrics:

- `request_throughput_rps`: successful requests started in the window (tracked
  to completion) divided by `measure_s`.
- `output_token_throughput_tps`: tokens received during the window (all
  requests) divided by `measure_s`. Index column `tok_thru_tps`.
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
  failed and that row's energy is a head-only lower bound; the plots dagger
  such series automatically).

Server-side counters (from vLLM `/metrics`, differenced over the logical window):

- `server_prompt_tokens`, `server_generation_tokens`,
  `server_prompt_tokens_cached`: prefill, decode, and prefix-cache-hit token
  counts the server actually processed during the window.
- `server_gen_tps`: `server_generation_tokens / measure_s`. Because it counts
  tokens the server produced, not requests that finished on the client, it is a
  throughput reading that stays honest when the measurement window is short
  relative to request duration. The `server_vs_client_throughput` figure plots
  it against the client rate; a gap between them means the window was too short
  (see Choosing The Measurement Window).
- `num_preemptions`: requests vLLM preempted during the window, a capacity
  pressure signal.

Served-model capacity and launch configuration (constant per server, captured
once from `/v1/models`, vLLM `cache_config_info`, the optional server log, and
optionally the Hugging Face model config):

- `max_model_len`, `kv_cache_dtype`, `gpu_memory_utilization`, `block_size`,
  `gpu_blocks`: the launch-time KV cache configuration.
- `total_kv_tokens`: `gpu_blocks * block_size`, the KV cache capacity in tokens.
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
continuous warmup/measure/cooldown phase fix them. The
`server_vs_client_throughput` figure is the regression check: the client and
server curves should now track closely at every load, with the dotted
completed-only curve showing what the old accounting would have read.

Guidance:

- Set `warmup_s` at or above the worst single-request lifetime (queueing +
  full stream) at the highest concurrency in the sweep, so the window opens
  with a steady running/queue mix. The shipped default is `600`.
- Set `measure_s` to a small multiple of that lifetime; the shipped default is
  `1200`. Longer windows buy more percentile samples at the highest loads.
- Set `cooldown_s` around the p50 lifetime at the heaviest point (shipped
  `300`), so requests started late in the window finish under load rather than
  on a draining server.
- Aim for at least `MIN_SAMPLES` (20, the plotting threshold) started-in-window
  requests per point, and preferably many more at the loads you will quote.
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
POWER_BACKEND=nvidia_smi scripts/run_all.sh single
POWER_BACKEND=none scripts/run_all.sh single
python -m bench.run_benchmark --power-backend sysfs --config configs/experiment.yaml
```

Backends:

- `nvidia_smi`: reads GPU power from `nvidia-smi`.
- `sysfs`: reads a hardware monitor file named by `power_sysfs_path`; values are
  interpreted as microwatts.
- `tegrastats`: parses named rails from `power_rails`, or all matched rails if
  the list is empty.

Validate the backend on DGX Spark before using energy numbers. Note that
`nvidia_smi` reports the GPU module's draw, not wall power.

For dual-node runs, a local sampler only sees the head node's GPU. There is no
SSH path from head to worker, so `scripts/run_all.sh` (for `tp2`/`pp2` labels)
also passes `--worker-power-container`/`--worker-power-node-ip`, and the
benchmark samples the worker's GPU through a tiny Ray actor pinned to
`node:<worker ip>` inside the serving container. `mean_power_w` then totals
both nodes and `power_nodes=2` marks the row as fully covered; if the worker
sampler fails, the run continues, warns, and records `power_nodes=1`, and the
plots dagger that series as head-only. Set `WORKER_POWER_NODE_IP=""` to disable
worker sampling explicitly.

## Troubleshooting

- `ModuleNotFoundError` for `pandas`, `aiohttp`, or `yaml`: activate the virtual
  environment and reinstall `requirements.txt`.
- `Connection refused` or model resolution fails: confirm vLLM is running and
  `curl http://127.0.0.1:8000/v1/models` works from the benchmark client.
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
- `run_models.sh` skips a model with a timeout or "launcher exited": that model
  failed to come up (often OOM). Check `OUT_DIR/server_<label>_<config>.log`;
  lower its `gpu_util` in `configs/models.yaml`, add a `quant`, or move it to the
  dual-node configs if it does not fit on one GB10. The orchestrator continues
  to the next model rather than aborting the whole sweep.
- `weight_gb`/`kv_cache_gb` blank in the index: the model config or Hub metadata
  could not be fetched (offline or gated without `HF_TOKEN`). The HTTP-derived
  fields (`gpu_blocks`, `total_kv_tokens`, `block_size`, etc.) still populate.
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
- `PREFLIGHT FAILED: low free memory` before a `tp2`/`pp2` launch: the
  preflight already reclaims stale page cache automatically, so a node
  reported LOW is genuinely out of memory — usually another process on that
  shared node (check `ps aux --sort=-rss`), or a `gpu_util` too high for the
  node's baseline usage (`util x 119.6 GiB` must fit in MemFree).
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
  per-run `vllm_metrics` summaries. The `server_gen_tps` column gives a
  window-robust server-side throughput, and the `weight_gb`, `kv_cache_gb`,
  `total_kv_tokens`, `gpu_blocks`, `block_size`, `kv_cache_dtype`,
  `gpu_memory_utilization`, and `max_model_len` columns give the served-model
  capacity and launch configuration for the platform/model table.
- Energy: enable a validated power backend and report the summary `energy`
  block for the same window as throughput.
- Interconnect model validation: record the `all_reduce_perf` log from
  `scripts/interconnect_microbench.sh` alongside the serving sweep.
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
  not as a throughput cliff. Record the `all_reduce_perf` log from
  `scripts/interconnect_microbench.sh` alongside the sweep to attribute the
  latency component.
