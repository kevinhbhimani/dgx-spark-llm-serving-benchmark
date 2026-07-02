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
  connectx_env.sh             shared ConnectX and container settings
  verify_connectx.sh          route and latency sanity checks
  start_ray.sh                start or stop the Ray container for dual-node runs
  launch_server.sh            start vLLM in single, tp2, or pp2 mode
  interconnect_microbench.sh  nccl-tests all-reduce check
  run_all.sh                  sweep a concurrency list for one config
  run_models.sh               sweep several models for one config, one at a time
  models_for_config.py        select models.yaml entries for a serving config
plot_results.py               generate PDF/PNG figures from results/index.csv
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
- `tokenizer`: Hugging Face tokenizer id or local path. Set to `""` only for
  quick approximate checks.
- `use_token_ids`: when true, send exact token-id prompts built with the
  tokenizer.
- `buckets`: weighted synthetic workload buckets with `input_len`,
  `output_len`, and `weight`.
- `trace_path`: optional JSONL trace that replaces `buckets`.
- `mode`: `closed` for fixed concurrency or `open` for Poisson arrivals.
- `concurrency`: number of closed-loop workers.
- `qps`: offered request rate for open-loop mode.
- `warmup_s` and `measure_s`: warmup and logical measurement window durations.
- `ttft_slo_s` and `itl_slo_s`: service-level objectives used for goodput.
- `power_backend`: `none`, `nvidia_smi`, `sysfs`, or `tegrastats`.
- `scrape_vllm_metrics`: whether to sample vLLM Prometheus metrics.
- `config_label`: label written to outputs, such as `single`, `tp2`, or `pp2`.
- `out_dir`: output directory, default `results`.

Common script environment variables:

The scripts load a repo-local `.env` file automatically if it exists, so these
can be saved there instead of exported in every shell session. `.env` is ignored
by git because it may contain `HF_TOKEN`.

```bash
export MODEL=Qwen/Qwen3-8B
export PORT=8000
export MAXLEN=8704
export VLLM_IMAGE=nvcr.io/nvidia/vllm:26.04-py3
export HF_TOKEN=...
export HF_CACHE=$HOME/.cache/huggingface
export BASE_URL=http://127.0.0.1:8000
export OUT_DIR=results
export CONFIG_YAML=configs/experiment.yaml
export CONCURRENCIES="1 2 4 8 16 32 64 128"
export POWER_BACKEND=none
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
`--model`, `--tokenizer`, `--mode`, `--concurrency`, `--qps`, `--measure-s`,
`--warmup-s`, `--base-url`, `--out-dir`, and `--power-backend`.

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

Run the interconnect microbenchmark from the head node and save the log beside
the serving results:

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
(`single`, `tp2`, `pp2`), and optional `max_model_len`, `gpu_util`, `quant`, and
`tokenizer`. A model whose weights plus KV cache do not fit on one GB10 must omit
`single` and use the dual-node configs, which pool two nodes over ConnectX for
roughly twice the memory (a 70B model in bf16 is dual-only).

`run_models.sh` takes one serving config at a time. It selects the models that
request that config, and for each one launches a server, waits until
`/v1/models` reports it, runs the concurrency sweep tagged with the model label,
then tears the server down before the next model. Server stdout is teed to
`OUT_DIR/server_<label>_<config>.log` and passed to the benchmark so exact
capacity figures are captured.

```bash
# Single-node models (no Ray). Launches and removes a container per model.
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_models.sh single

# Dual-node models. Start Ray on both nodes first; run_models.sh then cycles
# only the vLLM serve process between models and leaves the Ray cluster up.
DROP_CACHES=1 scripts/start_ray.sh head        # on Diya
DROP_CACHES=1 scripts/start_ray.sh worker      # on Hoedus
scripts/run_models.sh tp2
scripts/run_models.sh pp2
scripts/start_ray.sh stop                      # on each node when finished
```

All runs append to one `OUT_DIR/index.csv` tagged by `model_label`, so a single
plotting invocation compares across models (see Outputs). Use a fresh `OUT_DIR`
the first time on an upgraded checkout: the index gained columns, and the
benchmark refuses to append rows to an index whose header does not match, to
avoid silently misaligning columns.

Key environment variables: `MODELS_YAML` (default `configs/models.yaml`),
`OUT_DIR`, `BASE_URL`, `POWER_BACKEND`, `CONCURRENCIES`, `CONFIG_YAML`, and
`READY_TIMEOUT` (seconds to wait for a model to load, default 1200). Trim
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
token-id lists. Synthetic prompts are randomized slices of a larger token buffer
so repeated requests do not all share the same complete prefix. Record the vLLM
prefix-cache setting you used, because it materially affects long-context TTFT
and KV cache behavior.

A JSONL trace can replace buckets by setting `trace_path` in the YAML. Each
line can contain:

```json
{"input_len": 1024, "output_len": 128}
{"input_len": 512, "max_tokens": 64, "prompt": "optional prompt text"}
```

When `use_token_ids` is true, the harness preserves requested lengths by
building synthetic token-id prompts. When `use_token_ids` is false and a prompt
is present, the prompt text is sent directly.

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
  started and completed inside the measurement window; at high concurrency with a
  short window it falls toward zero simply because long requests do not finish
  before the window closes. That is a windowing/backlog effect, not the server
  dropping requests, and the panels are separated so the two are never conflated.
- The energy figure daggers the dual-node (`tp2`, `pp2`) series and prints a
  footnote, because `nvidia_smi` samples only the head node's GB10. Dual-node
  per-token energy is therefore a lower bound and tokens/W an upper bound; do not
  compare a one-node dual number against a true single-node number without
  summing both nodes' power offline.

Before drawing anything, the script prints a short data-quality report: per
configuration it lists the number of points, the measurement window(s) used,
peak observed throughput, and any point with zero completions in the window
(where throughput is "no data", not a real zero). Mixed measurement windows
within one series are called out, since windowed rates stay comparable but the
per-point sample sizes behind the percentiles do not.

If a sweep was rerun so the same `config_label` and load (concurrency or
`qps`) appear twice in `index.csv`, `plot_results.py` keeps only the run with
more completed requests (its percentiles rest on a larger sample) and prints
a `note:` line naming both runs so you can compare them yourself; without
this, two contradictory points at the same x value draw a zigzag through the
figures instead of a curve. Separately, any point backed by fewer than
`--min-samples` completed requests (default 20) is circled in the figures
and listed in a `warning:` line, since its percentiles are small-sample noise
rather than a real reading, not a capacity signal to draw conclusions from.
This shows up at the lowest loads (few requests fit in the window even when
the server keeps up) and at loads beyond a config's capacity (requests queue
faster than they complete, so few finish inside the window at all). A longer
`--measure-s` gives more completions to trust at high concurrency.

## Metric Semantics

The logical measurement window starts after warmup and lasts `measure_s`
seconds. The load generator drains in-flight requests after that point, but
aggregate metrics count only requests that complete inside the logical window.
The drain duration is reported as `drain_s` in the summary.

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

- `request_throughput_rps`: successful completions in the window divided by
  `measure_s`.
- `output_token_throughput_tps`: generated output tokens from those completions
  divided by `measure_s`.
- `goodput_rps`: successful completions per second whose TTFT is at or below
  `ttft_slo_s` and whose per-request mean ITL is at or below `itl_slo_s`.
- Capacity: sampled from vLLM Prometheus metrics. The index uses
  `kv_cache_usage_*`, `requests_running_*`, and `requests_waiting_*` columns.
  The scraper accepts both `vllm:kv_cache_usage_perc` and the older
  `vllm:gpu_cache_usage_perc` metric name.
- Energy: when enabled, power samples over the same logical window produce
  `mean_power_w`, `energy_per_token_j`, and `tokens_per_watt`.

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
once from `/v1/models`, vLLM `cache_config_info`, and the model config):

- `max_model_len`, `kv_cache_dtype`, `gpu_memory_utilization`, `block_size`,
  `gpu_blocks`: the launch-time KV cache configuration.
- `total_kv_tokens`: `gpu_blocks * block_size`, the KV cache capacity in tokens.
- `weight_gb`, `kv_cache_gb`: full-model weight and KV cache footprints. These
  are aggregates across shards; for `tp2`/`pp2` the per-GPU footprint is roughly
  the value divided by the parallel size. `weight_gb` is the checkpoint size and
  approximates the served size unless a different dtype or quantization is used.
  Pass `--server-log` (the multi-model orchestrator does this automatically) to
  also capture the exact `max_concurrency` vLLM logged at load.

These per-model capacity numbers are collected into `capacity_table.tex`/`.md` by
`plot_results.py --table`.

## Choosing The Measurement Window

Because only requests that *complete* inside the window count, the window must be
long enough to collect a trustable number of completions at the load you care
about, or the aggregate rates understate the server and the percentiles are
small-sample noise. This workload is long-context: a single request can take
tens of seconds end to end at low load and well over a minute once the server is
saturated, so a short window collects very few completions per point.

A 60 s window is far too short here. At high concurrency it can report zero
completions while the server is healthy and busy (`requests_failed` is 0, KV
cache is not full, and `requests_running` is high) — the requests simply have
not finished yet. That reads as a throughput *collapse* that is not real: on a
longer window the same point keeps climbing. For example, `tp2` at concurrency 64
measured 0 tok/s over 60 s (0 completions) but 141 tok/s over 240 s (138
completions), and `pp2` at concurrency 128 measured 0 tok/s over 60 s but 135
tok/s over 360 s. Treat any windowed throughput backed by a handful of
completions as a lower bound, not a capacity reading.

Guidance:

- Set `measure_s` to at least several times the E2E p50 you expect at the
  highest load in the sweep. For the default workload that means `measure_s`
  around `300`-`600`; the shipped `configs/experiment.yaml` default is `600`.
- Aim for at least `MIN_SAMPLES` (20, the plotting threshold) completions per
  point, and preferably many more at the loads you will quote in the paper.
- Keep the window consistent across a sweep and across configs you compare.
  `plot_results.py` still plots mixed-window sweeps but flags them, because the
  per-point sample sizes behind the percentiles differ even though the windowed
  rates remain comparable.
- Points below `MIN_SAMPLES` completions are circled in the figures, daggered in
  the summary table, and listed in the data-quality report; rerun them with a
  longer `--measure-s` before quoting their numbers.

## Power Telemetry

Power is off by default. Enable it with `POWER_BACKEND` for sweeps or
`--power-backend` for a single run:

```bash
POWER_BACKEND=nvidia_smi scripts/run_all.sh single
python -m bench.run_benchmark --power-backend sysfs --config configs/experiment.yaml
```

Backends:

- `nvidia_smi`: reads GPU power from `nvidia-smi`.
- `sysfs`: reads a hardware monitor file named by `power_sysfs_path`; values are
  interpreted as microwatts.
- `tegrastats`: parses named rails from `power_rails`, or all matched rails if
  the list is empty.

Validate the backend on DGX Spark before using energy numbers. For dual-node
energy, make sure the sampled rail covers both nodes or combine per-node energy
offline; a local `nvidia_smi` sampler only sees the local GPU.

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
  TTFT or mean ITL did not meet `ttft_slo_s`/`itl_slo_s`. Loosen the SLOs if you
  want a capacity curve for a less strict service target.
- `PyTorch was not found` from Transformers on the benchmark client is harmless
  for this harness because it only loads tokenizers locally. vLLM and PyTorch
  run inside the serving container.
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
- Docker cannot access the GPU: verify `docker run --gpus all ... nvidia-smi`
  works with your container runtime.
- Dual-node launch says Ray is not active: run `scripts/start_ray.sh head` on
  the head node and `scripts/start_ray.sh worker` on the worker node, then check
  `docker exec vllm-spark ray status`.
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
- `plot_results.py` reports `Expected 29 fields`: an older `index.csv` row was
  likely missing a trailing newline before another run appended. New runs guard
  against this; split the concatenated row or start a fresh `OUT_DIR`.

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
