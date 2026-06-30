# DGX Spark LLM Serving Benchmark

This repository contains the benchmarking harness used for the PMBS26 serving
methodology. It drives a vLLM OpenAI-compatible server and records the same
client-side metrics for single Spark, dual Spark tensor parallel, and dual Spark
pipeline parallel deployments.

The default request path is the vLLM completions endpoint (`/v1/completions`).
The harness sends streaming greedy requests with `max_tokens == min_tokens`,
`ignore_eos: true`, and `temperature: 0.0` so output length is controlled by the
workload definition. Output tokens are counted as content-bearing streaming
chunks, which matches the validated vLLM greedy streaming behavior.

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
- Capacity: sampled from vLLM Prometheus metrics:
  `vllm:gpu_cache_usage_perc`, `vllm:num_requests_running`, and
  `vllm:num_requests_waiting`.
- Energy: when enabled, power samples over the same logical window produce
  `mean_power_w`, `energy_per_token_j`, and `tokens_per_watt`.

## Repository Layout

```text
bench/
  config.py        experiment configuration and YAML loading
  workload.py      synthetic token-length prompts and JSONL trace loading
  client.py        one async streaming request with timing capture
  monitors.py      background power sampler and vLLM metrics scraper
  runner.py        closed-loop and open-loop load generation
  metrics.py       percentile aggregation, throughput, goodput, energy
  run_benchmark.py command line entry for a single measurement point
scripts/
  connectx_env.sh             shared ConnectX and container settings
  verify_connectx.sh          route and latency sanity checks
  start_ray.sh                start or stop the Ray container for dual node runs
  launch_server.sh            start vLLM in single, tp2, or pp2 mode
  interconnect_microbench.sh  nccl-tests all-reduce check
  run_all.sh                  sweep a concurrency list for one config
configs/
  experiment.yaml             default experiment settings
```

## Prerequisites

Install client-side dependencies:

```bash
pip install -r requirements.txt
```

The scripts call `docker` directly, so the user running them must be in the
`docker` group.  If you see a permission-denied error on the Docker socket, add
yourself and start a new shell:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

vLLM runs separately on the serving node or nodes. The client needs network
access to the vLLM server, access to `/metrics`, and a Hugging Face tokenizer
matching the served model if you want exact input token lengths.

Without a tokenizer, synthetic prompts fall back to repeated words and input
lengths are approximate. Do not use tokenizer-free mode for paper numbers.

## Running The Benchmark

Run one serving configuration at a time. For each configuration, launch vLLM,
run the sweep with the matching label, then tear it down before moving to the
next configuration.

On Grace unified memory, stale page cache can cause false out-of-memory errors
when loading large models.  Set `DROP_CACHES=1` to flush it before launch
(requires sudo; skipped by default):

```bash
DROP_CACHES=1 MODEL=Qwen/Qwen3-8B scripts/launch_server.sh single
```

Single node:

```bash
MODEL=Qwen/Qwen3-8B scripts/launch_server.sh single
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_all.sh single
```

Dual node tensor parallel:

```bash
# on the head node
scripts/start_ray.sh head

# on the worker node
scripts/start_ray.sh worker

# back on the head node
MODEL=Qwen/Qwen3-8B scripts/launch_server.sh tp2
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_all.sh tp2
```

Dual node pipeline parallel:

```bash
# with the Ray cluster already running
MODEL=Qwen/Qwen3-8B scripts/launch_server.sh pp2
CONCURRENCIES="1 2 4 8 16 32 64 128" scripts/run_all.sh pp2
```

Run one measurement point directly:

```bash
python -m bench.run_benchmark \
  --config configs/experiment.yaml \
  --config-label single \
  --mode closed \
  --concurrency 16 \
  --base-url http://127.0.0.1:8000
```

`--model` and `--tokenizer` override the values in the YAML without editing the
file.  Pass `--tokenizer ""` to skip the tokenizer download and fall back to
approximate prompt lengths (useful for quick checks, not paper numbers):

```bash
python -m bench.run_benchmark \
  --config configs/experiment.yaml \
  --config-label single \
  --mode closed \
  --concurrency 16 \
  --model Qwen/Qwen3-8B \
  --tokenizer ""
```

Open-loop Poisson arrivals are also supported:

```bash
python -m bench.run_benchmark \
  --config configs/experiment.yaml \
  --config-label single \
  --mode open \
  --qps 4.0
```

## Outputs

Every measurement point writes three files under `out_dir`:

- `<run_tag>.raw.jsonl`: one JSON record per request, including TTFT, E2E,
  token timestamps, inter-token gaps, success state, and error text.
- `<run_tag>.summary.json`: aggregate metrics, capacity samples, optional
  energy metrics, the full resolved configuration, and `drain_s`.
- `index.csv`: one row per measurement point for plotting sweeps, including
  throughput, latency percentiles, goodput, KV cache occupancy, waiting/running
  requests, and optional energy columns.

## Workload Configuration

`configs/experiment.yaml` defines the default bucketed workload. Each bucket has
an `input_len`, an `output_len`, and a sampling `weight`.

With `use_token_ids: true` and a tokenizer configured, prompts are sent as exact
token-id lists. Synthetic prompts are randomized slices of a larger token buffer
so repeated requests do not all share the same complete prefix. Record the vLLM
prefix-cache setting you used, because it materially affects long-context TTFT
and KV cache behavior.

A JSONL trace can replace buckets by setting `trace_path`. Each line may contain
`input_len`, `output_len` or `max_tokens`, and optionally `prompt`. When
`use_token_ids` is true, the harness preserves the requested lengths by building
a synthetic token-id prompt.

## Capacity Analysis

Use `index.csv` for the sweep curve and the summary JSON files for the full
vLLM metric blocks. As concurrency rises, the useful capacity limit is where
token throughput stops scaling, `vllm:gpu_cache_usage_perc` approaches
saturation, and `vllm:num_requests_waiting` begins to grow.

Before dual-node runs, verify traffic is on the ConnectX path:

```bash
scripts/verify_connectx.sh
HOST0=Diya HOST1=Hoedus scripts/interconnect_microbench.sh
```

If `interconnect_microbench.sh` has to build `nccl-tests`, it clones
`https://github.com/NVIDIA/nccl-tests` into `$HOME/nccl-tests`. If the clone
fails, install or fix `git` and network access, or set `NCCL_TESTS_DIR` to an
existing nccl-tests checkout.

If `scripts/start_ray.sh head` or `worker` exits because the container stopped,
the script prints the last container logs. The most common causes are an
unavailable `VLLM_IMAGE`, missing Docker GPU runtime support, or a vLLM image
without Ray installed.

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

## PMBS26 Mapping Checklist

- Section 5.1 single Spark: run the `single` closed-loop concurrency sweep.
- Sections 5.2 and 5.3 dual Spark: run `tp2` and `pp2` sweeps with the same
  model, tokenizer, workload, SLO, and measurement window.
- Capacity: use `kv_cache_usage_*` and `requests_waiting_*` columns plus the
  per-run `vllm_metrics` summaries.
- Energy: enable a validated power backend and report the summary `energy`
  block for the same window as throughput.
- Interconnect model validation: record the `all_reduce_perf` log from
  `scripts/interconnect_microbench.sh` alongside the serving sweep.
