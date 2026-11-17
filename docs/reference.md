# Harness reference

This reference lists configuration fields, environment variables, command
options, output files, and metric definitions. Node setup is in
[cluster-setup.md](cluster-setup.md). The campaign procedure is in
[campaign-runbook.md](campaign-runbook.md). Error messages and fixes are in
[troubleshooting.md](troubleshooting.md).

## Experiment configuration

The main experiment configuration is `configs/experiment.yaml`. Its fields:

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
- `min_window_requests`, `window_extension_step_s`, `max_measure_s`: the
  minimum-sample stopping rule (closed loop only; `0` disables it). With it,
  `measure_s` is the minimum window: while fewer than `min_window_requests`
  requests have started inside it, the window end moves out in
  `window_extension_step_s` steps, never past `max_measure_s`. The rule looks
  only at how many requests started, never at their latency, so it does not
  bias the percentiles. The index records `configured_measure_s` and
  `measure_extension_s` beside the actual `measure_s`.
- `request_timeout_s`: per-request HTTP timeout, including queueing and stream
  drain. Keep it well above the worst-case E2E at the highest load in the
  sweep, or slow-but-successful requests are counted as failures; the shipped
  full-run value is `14400` seconds.
- `ttft_slo_s`, `itl_slo_s`, and `itl_slo_s2`: service-level objectives used for
  goodput. Both ITL tiers are scored against the same TTFT bound: `itl_slo_s` is
  the strict interactive target and `itl_slo_s2` a looser tier, giving two
  goodput columns (`goodput_rps`, `goodput_itl2_rps`). A model can have zero
  strict goodput when its measured TTFT or ITL exceeds the configured bound.
  Compare both goodput columns against the stated SLOs.
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
- Campaign output paths are selected with `OUT_DIR`, not in the experiment
  YAML. New benchmark runs without `OUT_DIR` select the next available
  `results/benchmark-<UTC-date>-001` path. Later runs use the next available
  sequence number.
- `run_note`: free-form note preserved in the resolved configuration block.

Each full result records methodology `steady-exact-v4`, the YAML and harness
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

## Environment variables

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
export CONCURRENCIES="${CONCURRENCIES:-1 4 8 16 32 64}"
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
# Optional shared serving target for campaigns and direct launches:
# export GPU_UTIL=0.80        # default; overrides gpu_util_default in models.yaml
# export PREFIX_CACHING=1     # prefix caching is OFF by default: cache hits on
#                             # repeated synthetic prompts skipped up to a third
#                             # of prefill work in some models but not others
# export GENERATION_CONFIG=vllm  # default: use request sampling settings rather
#                                # than model-repository generation defaults
# export FLASHINFER_AUTOTUNE=1   # autotune is OFF by default in every config:
#                                # vLLM 0.29.0 deadlocks pp2 startup while tuning
#                                # (see troubleshooting.md)
# export QUANT=fp8
# export TOKENIZER=Qwen/Qwen3-8B
# export TOKENIZER_REVISION="$REVISION"
# export TRUST_REMOTE_CODE=1
export VLLM_IMAGE=nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2
export VLLM_CONTAINER=vllm-spark
export HF_TOKEN=...
# Optional HF_CACHE override. The default is "$HOME/.cache/huggingface-$UID".
# export HF_CACHE="/path/to/writable/huggingface-cache"
# Optional DNS passed to Docker launch/Ray containers. Unset/empty uses
# Docker's host-derived resolver, which is the default.
# export CONTAINER_DNS=8.8.8.8
# Optional download-only override; defaults to CONTAINER_DNS.
# export DOWNLOAD_DNS=8.8.8.8
# NCCL selects RDMA devices automatically when NCCL_IB_HCA is empty.
# Set NCCL_IB_HCA separately on each host when device names differ.
# export NCCL_IB_HCA="rocep1s0f0,roceP2p1s0f0"
# export NCCL_IB_DISABLE=1   # TCP-socket fallback
# Per-channel transport evidence in every dual-node server log (the
# connectx_env.sh default; read by run_models.sh and audit_nccl_transport.py).
# export NCCL_DEBUG=INFO
# export NCCL_DEBUG_SUBSYS=INIT,NET
export BASE_URL=http://127.0.0.1:8000
# Optional prefix for auto-generated benchmark campaign directories.
# export CAMPAIGN_NAME=rdma-v4-paper
# Optional. Set the complete campaign output path, such as:
# export OUT_DIR=results/rdma-v4-paper-2026-11-12
export CONFIG_YAML=configs/experiment.yaml
export CONCURRENCIES="${CONCURRENCIES:-1 4 8 16 32 64}"
export REPEATS="${REPEATS:-3}"
# unset defers to the YAML's power_backend; an override must still satisfy
# require_power_data (the full config therefore cannot use "none")
# export POWER_BACKEND=nvidia_smi
export MODELS_YAML=configs/models.yaml
export READY_TIMEOUT=1800
# Seconds of startup with nothing new in the server log but engine heartbeats
# before run_models.sh declares the launch hung (0 disables).
# export READY_STALL_TIMEOUT=900
# Installed into Ray containers by start_ray.sh. Keep it aligned with
# scripts/ray-runtime-constraints.txt and VLLM_IMAGE.
export RAY_VERSION=2.56.1
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
blocked on managed networks. `DOWNLOAD_DNS` is useful when the
model-acquisition container needs a resolver different from the serving/Ray
containers.

Do not put an arbitrary `SERVING_FINGERPRINT` in `.env` to satisfy the full
config. It must be the SHA-256 hash of the server's actual launch policy;
`run_models.sh` computes and forwards it together with the pinned revisions.

## Cluster topology

Topology values come from `CLUSTER_CONFIG`. The loader identifies the local
node by matching `hostname -s` to one YAML entry. It derives the head and worker
addresses, local interface list, socket interface variables, and Ray port.
An inherited topology variable, such as `CX_IFACE` or `HEAD_IP`, that
conflicts with the `CLUSTER_CONFIG` value fails with an error. The canonical
topology digest must match on both systems.
The local YAML paths may differ.

`configs/cluster.yaml` is the default topology file.
`configs/cluster.example.yaml` contains documentation-only hostnames and
reserved IPv4 ranges. Set `CLUSTER_CONFIG` to use another topology file. The
RDMA setup and verification sequence is in
[cluster-setup.md](cluster-setup.md). The per-device RDMA perftest
matrix runs from the standalone `perftest-suite-dgx-spark-cluster` repository.

## Single-node runs

The commands below run on a node listed in `CLUSTER_CONFIG`. On a Spark that
is not listed there, set `SINGLE_NODE_ONLY=1`.

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
when `MemAvailable` is sufficient. `run_models.sh` flushes host page cache after
model files are staged and before each server starts. It records `MemFree` and
`MemAvailable` before and after the flush. The server memory preflight then
checks the same `GPU_UTIL x MemTotal` threshold that vLLM uses. A remaining
shortage stops the launch before measurement. Direct `launch_server.sh` calls
can use `DROP_CACHES=1` for a local proactive flush:

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
CONCURRENCIES="1 4 8 16 32 64" \
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
`--collect-hf-metadata`, and `--no-hf-metadata`.

The benchmark log prints `point=<value>` for the x-axis load value being
measured. In `closed` mode, `point` is the fixed concurrency. In `open` mode,
`point` is the target `qps`.

## Two-node runs

The dual-node scripts use the head and worker roles and per-node interface
assignments from `CLUSTER_CONFIG`.

1. Verify configured addresses, routes, peer reachability, and RDMA mappings on
   both nodes:

```bash
scripts/verify_connectx.sh
```

2. Start Ray on the head node:

```bash
scripts/start_ray.sh head
```

3. Start Ray on the worker node:

```bash
scripts/start_ray.sh worker
```

`DROP_CACHES=1` is an optional page-cache flush on the local node. It requires
sudo. Omit it on systems where the benchmark account does not have sudo access.

Direct `run_models.sh` launches acquire each selected pinned snapshot on the
head before launch and mirror it to the worker for tp2/pp2. Set `MODEL_LABELS`
to select a subset for manual launches.
`run_job.sh` acquires once per model group, mirrors when needed, and applies
the campaign purge policy.

```bash
export MODEL_LABELS="llama3.1-8b"
```

4. Back on the head node, launch and strictly probe each tensor-parallel model,
   without starting the long sweep:

```bash
CANARY_ONLY=1 scripts/run_models.sh tp2
```

5. Run the provenance-complete tensor-parallel sweep:

```bash
CONCURRENCIES="1 4 8 16 32 64" scripts/run_models.sh tp2
```

6. For pipeline parallel serving, keep the Ray cluster running, canary it, then
   run the matching sweep:

```bash
CANARY_ONLY=1 scripts/run_models.sh pp2
CONCURRENCIES="1 4 8 16 32 64" scripts/run_models.sh pp2
```

7. Stop the Ray container on each node when finished:

```bash
scripts/start_ray.sh stop
```

For `tp2` and `pp2` the launcher runs a preflight over the Ray cluster before
starting `vllm serve`: it reads free memory on every node and fails fast if any
node is below `GPU_UTIL x total`, which is the same bound vLLM enforces per
worker at startup (on Grace unified memory, CUDA free memory tracks the host's
`MemFree`). Before a distributed server starts, `run_models.sh` flushes page
cache on the head and worker after model files are staged. The preflight still
checks every node and stops before measurement if free memory remains below
the target. Without the preflight, the same condition surfaces minutes later
as an `EngineCore failed to start` traceback from a remote Ray worker. If free
memory stays tens of GiB below the target, no process owns the allocation,
`nvidia-smi` lists no processes, and `buff/cache` is small, a killed CUDA
process may have left unified memory allocated in the NVIDIA driver. Dropping
page cache does not recover that memory. Stop the Ray container
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

The default acceptance floor is 16 GB/s at 1 GiB and the default 8-byte
collective-latency ceiling is 100 µs. The floor is calibrated to the measured
NCCL RDMA plateau of 19.4-23.6 GB/s at 1 GiB on this two-node cluster
(dual-cable ConnectX, 2026-10-12), set ~13% below the lowest healthy run so a
healthy RDMA link passes while the TCP-socket fallback (2.09 GB/s) and a
misrouted path fail. The command fails without publishing an artifact if
either threshold is missed. A campaign run over the TCP fallback must set
`ALLREDUCE_MIN_1G_BUSBW_GBPS=1.8` and `ALLREDUCE_MAX_8B_LATENCY_S=0.005`
explicitly. Both values are included in the artifact fingerprint.

`scripts/interconnect_extra_bench.sh` adds the point-to-point send/recv
primitive that pipeline parallelism uses, and labels each row with the
transport NCCL actually selected. `plotting/plot_interconnect.py` draws both
primitives by transport.

## Multi-model runs

To characterize several models, list them in `configs/models.yaml` and drive them
with `scripts/run_models.sh`. Each entry names a Hugging Face model, a short
`label` used to tag results and plots, the serving configs to run it under
(`single`, `tp2`, `pp2`), and `revision`, plus optional `max_model_len`,
`quant`, `tokenizer`, `tokenizer_revision`, and `trust_remote_code`. The
shipped paper matrix pins every active model to an
immutable commit. `gpu_util_default` in `configs/models.yaml` sets the run-wide
machine memory target, defaulting to `0.80`. Set `GPU_UTIL` to override that
target for planning, validation, and every serving launch. The resolved value
is recorded in serving fingerprints and implicit campaign identity. Use the
same value and `OUT_DIR` when resuming a campaign. Every model and serving
config receives one shared value. The
launcher passes it to `vllm serve` as `--gpu-memory-utilization` and uses the
same value for its free-memory preflight. A 0.80 setting targets 102.4 GB on a
nominal 128 GB node. The vLLM setting is a per-instance limit, as documented
in the [vLLM CacheConfig reference](https://docs.vllm.ai/en/v0.29.0/api/vllm/config/cache/).
The campaign uses one serving instance per node at a time, so reserve each node
for campaign workloads. This setting does not limit aggregate use by unrelated
concurrent processes. Per-model `gpu_util` settings are rejected. Set `GPU_UTIL`
when a campaign needs a different shared target.
The tokenizer id and revision are passed to both vLLM and the client so exact
token-ID prompts match the served tokenizer. A model whose weights plus KV
cache do not fit on one GB10 must omit `single` and use the dual-node configs,
which pool two nodes over ConnectX for roughly twice the memory (a 70B model in
bf16 is dual-only).

`run_models.sh` takes one serving config at a time. It selects the models that
request that config and checks host cache access before loading the pinned
tokenizer. Single-node launches can download a pinned model revision through
vLLM. Dual-node launches require the pinned snapshot on both nodes and perform
a final cache check before distributed serving. Each path tears down any stale
server before launch. Readiness requires both the expected `/v1/models` id and a strict
streaming probe with delta token IDs, matching final usage,
`finish_reason: length`, and `[DONE]`; an old or merely HTTP-ready server cannot
pass. The wrapper then runs the concurrency sweep tagged with the model label
and tears the server down before the next model. Each launch writes a new,
never-overwritten log named
`OUT_DIR/logs/server_<label>_<config>_<fingerprint-prefix>_<UTC>_pid<PID>.log`
and an immutable
`OUT_DIR/results/serving_policy_<label>_<config>_<fingerprint>.json`. The log
is passed to the benchmark so exact per-worker memory, max-concurrency, and
other capacity figures are captured; the policy makes the launch inputs and
runtime identity independently inspectable.

```bash
# run_models.sh acquires each selected model immediately before launch.

# Launch/probe/tear down every single-node model without starting a sweep.
CANARY_ONLY=1 scripts/run_models.sh single
# Or target an exact space-separated label allowlist.
CANARY_ONLY=1 MODEL_LABELS='llama3.1-8b-fp8' scripts/run_models.sh single

# Single-node models (no Ray). Launches and removes a container per model.
CONCURRENCIES="1 4 8 16 32 64" scripts/run_models.sh single

# Dual-node models. Start Ray on both nodes first; run_models.sh then cycles
# only the vLLM serve process between models and leaves the Ray cluster up.
scripts/start_ray.sh head
scripts/start_ray.sh worker
# Each node has its own HF cache, and the worker cannot reach huggingface.co.
# run_models.sh acquires on the selected head and mirrors to the worker.
CANARY_ONLY=1 scripts/run_models.sh tp2         # validate every distributed launch
CANARY_ONLY=1 scripts/run_models.sh pp2
scripts/run_models.sh tp2
scripts/run_models.sh pp2
# Single-node serving inside the running Ray head container.
SINGLE_CONTAINER=ray scripts/run_models.sh single
# One replicate on a fresh launch, and a status query that launches nothing.
REPEATS=3 ONLY_REPEAT=2 MODEL_LABELS=llama3.1-8b scripts/run_models.sh tp2 --skip-done
STATUS_ONLY=1 REPEATS=3 MODEL_LABELS=llama3.1-8b scripts/run_models.sh tp2
# Free the disk once a model is done (both nodes; DRY_RUN=1 to preview).
MODEL_LABELS=llama3.1-8b scripts/purge_model_cache.sh
scripts/start_ray.sh stop                      # on each node when finished

# Rerun a sweep that failed partway without repeating finished work. Consults
# OUT_DIR/results/index.csv and skips only structurally complete, compatible
# (repeat, concurrency) windows for the same model, config, methodology, YAML,
# harness, client runtime,
# serving policy, and observed live deployment. A fully covered model never
# launches; a partial sweep rechecks the new server before resuming gaps.
scripts/run_models.sh single --skip-done       # or SKIP_DONE=1
```

## Campaign options

Campaign knobs for `run_job.sh`: `OUT_DIR`, `CONCURRENCIES`, `REPEATS`
(default 3), `CANARY_FIRST` (`auto`, `0`, `1`), `STOP_AFTER_CANARY`,
`ALLOW_CANARY_FAILURES`, `PURGE_WEIGHTS`, and the subset selectors
`CAMPAIGN_LABELS` and `CAMPAIGN_CONFIGS`. The old `FULL_REPEATS`,
`REPEAT_CONCS`, and `REPEAT_TOTAL` knobs are rejected rather than silently
ignored.

Key environment variables for the multi-model wrapper: `MODELS_YAML` (default
`configs/models.yaml`), `OUT_DIR`, `BASE_URL`, `POWER_BACKEND`,
`CONCURRENCIES`, `CONFIG_YAML`, `REPEATS` (default `3`), `ONLY_REPEAT`,
`STATUS_ONLY`, `SINGLE_CONTAINER`, `CANARY_ONLY`, `MODEL_LABELS`, and
`READY_TIMEOUT` (seconds to wait for a model to load, default `1800`).
`READY_STALL_TIMEOUT` (default `900`, `0` disables) fails a launch early when
its server log gains nothing but the EngineCore's once-a-minute
`shm_broadcast` heartbeat for that long; workers that fall silent while the
engine keeps waiting are deadlocked, not slow.
`PROBE_TIMEOUT` bounds the two-token streaming canary (default `600`), and
`CACHE_PREFLIGHT=1` is the default; keep the local revision check enabled for
full runs. The wrapper also honors launcher and network variables from the
[Environment variables](#environment-variables) section, including `HF_TOKEN`, `HF_CACHE`, `VLLM_IMAGE`,
`VLLM_CONTAINER`, `DROP_CACHES`, `CLUSTER_CONFIG`, and the NCCL transport
settings. The wrapper clears model-specific launch
overrides before applying each matrix entry, so an unrelated `QUANT`,
`TOKENIZER`, or `TRUST_REMOTE_CODE` value in `.env` cannot leak into the next
model.

An optional model-matrix `max_num_seqs` sets and fingerprints vLLM's server
scheduler limit across that checkpoint's configs. No checkpoint in the current
matrix sets this field. Record a future sequence limit in `configs/models.yaml`
and validate it with the canary before measurement.

`scripts/predownload.sh` is the pinned-snapshot acquisition command used by
`run_job.sh` per model group and by direct `run_models.sh` launches per model.
It uses the same pinned image, cache mount, model ids, and revisions as
serving. It sizes each pinned revision from the Hub's per-file
listing (`scripts/hf_snapshot_size.py`), net of blobs already in the cache, and
exits on an API failure before the first download. `CHECK_ONLY=1` uses Hugging
Face local-only resolution and fails on a missing or partial snapshot; it never
substitutes a newer cached revision. `MODEL_LABELS` restricts acquisition,
mirror, purge, and `run_models.sh` to the listed labels.

`run_job.sh` verifies the exact rectangle before plotting and again afterward,
when it atomically writes the final artifact manifest. To repeat that final
fail-closed check explicitly:

```bash
python scripts/validate_campaign.py verify \
  --models configs/models.yaml \
  --experiment configs/experiment.yaml \
  --out-dir "$OUT_DIR" \
  --concurrencies 1 4 8 16 32 64 \
  --repeats 3 \
  --manifest "$OUT_DIR/results/campaign_manifest.json"
```

New campaigns keep measurement artifacts in `OUT_DIR/results/`, figures and
reports in `OUT_DIR/figs/`, and diagnostic logs in `OUT_DIR/logs/`. Existing
campaigns with a root-level `index.csv` remain readable and resumable.

The verifier requires exactly the planned rows and repeat IDs, validates the
indexed artifact sizes and SHA-256 digests, provenance consistency, request and
observability quality, server logs/policies, and the dual-node all-reduce CSV.
It also seals configs, trace (when used), dependency locks, benchmark/plot
source, client environment, raw data, telemetry, summaries, logs, policies,
all-reduce data, index, and figures into `campaign_manifest.json`. Run it on
the head node: raw telemetry and server logs are not in git (see
[Raw data and archives](#raw-data-and-archives)).

## Replicates and statistics

The PMBS26 campaign measured each point once in a 1500-second window. The
variance subset repeated five points selected after inspection of their
margins. The `steady-exact-v4` methodology uses three independent launches for
every model, config, and concurrency.

- A replicate is one fresh server launch followed by an ascending concurrency
  sweep. The launch includes memory profiling, allocator state, compilation, and
  cluster setup.
- The campaign runs three replicates per model/configuration/concurrency point.
- Config order rotates between replicates: `single`, `tp2`, `pp2`; `tp2`, `pp2`,
  `single`; then `pp2`, `single`, `tp2`.
- Each measurement window lasts at least 600 seconds. The closed-loop window
  extends in 60-second steps until 20 requests have started or 3600 seconds
  have elapsed.
- Figures plot the replicate mean with its min-max band. Tables print mean ±
  the 95% Student-t half-width.
  `scripts/variance_report.py` writes per-point CV/CI and a Welch interval for
  every pairwise config margin at every concurrency to
  `OUT_DIR/figs/stats/`. A claim that one config beats another needs its Welch
  interval to exclude zero. These are per-comparison intervals, not
  family-wise corrected.

## Workloads and traces

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

FlashInfer autotuning is also off by default (`FLASHINFER_AUTOTUNE=1`
restores vLLM's default) and recorded in the serving policy as
`flashinfer_autotune: "0"`. The pinned vLLM 0.29.0 averages autotune timings
over the world process group, which deadlocks pipeline-parallel startup when
the two stages tune different kernels (see
[troubleshooting.md](troubleshooting.md)). One setting for
single, tp2, and pp2 keeps kernel selection comparable across configs; report
it with the results.

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
  power samples used for window integration, with GPU temperature in Celsius
  and GPU/node identity when the sensor reports it. It is written whenever a
  power backend is active.
- `<run_tag>.thermal.log`: timestamped output from `sensors`, sampled once per
  second on the head and, for dual-node runs, the worker. The summary records
  its path, SHA-256, and byte count under `artifacts.thermal_samples`. Canary
  captures use the same format in the campaign `logs/` directory.
- `<run_tag>.vllm_metrics.jsonl`: timestamped raw values and label sets for the
  required vLLM Prometheus series whenever metrics scraping is active.
- `<run_tag>.summary.json`: aggregate metrics, capacity samples, server-side
  token counters, the served model and label, a `server_info` block (capacity
  and launch configuration), optional energy metrics, the full resolved
  configuration, observed workload-bucket counts, and the actual
  `warmup_extension_s`, `load_extension_s`, and `drain_s` phase timings. It also
  carries methodology/config/source/serving/experiment provenance hashes and
  the client environment, live deployment hash, pinned revisions, repeat
  number, and a versioned temperature summary for the request window.
- `index.csv`: one row per measurement point for plotting sweeps. Each row is
  tagged with `model`, `model_label`, and `repeat_id` and carries the provenance,
  server-side counters, and capacity columns below alongside the client-side
  latency/throughput metrics. The benchmark refuses to append if the existing
  header is not exactly the current schema. It records the path, byte count,
  and SHA-256 for the raw request data, summary, raw power, raw vLLM metrics,
  and serving-policy artifact. All point artifacts are fsynced and atomically
  renamed before the index row can make that point resumable.

Campaign result files are under `OUT_DIR/results/`: immutable
`serving_policy_*.json` launch records, the fingerprinted `allreduce_ray_*.csv`,
the `interconnect_extra_*.csv` send/recv benchmark, `client_environment.json`,
and the final `campaign_manifest.json`. Figures and tables are under
`OUT_DIR/figs/`, including replicate statistics, the NCCL transport audit, and
baseline comparisons under `OUT_DIR/figs/stats/`. Diagnostic logs are under
`OUT_DIR/logs/`, including server launch logs and the run log. The manifest is the
paper bundle's integrity inventory; rerun the verifier after any intentional
figure regeneration. Raw telemetry and logs stay out of git; see
[Raw data and archives](#raw-data-and-archives).

When several models were run into one `index.csv` (see [Multi-model runs](#multi-model-runs)), `plot_results.py` writes a cross-model `model_comparison` figure and a
`capacity_table`, then a full per-model figure set under
`OUT_DIR/figs/<model_label>/`.
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
python plotting/plot_results.py --index "$OUT_DIR" --format both
```

Available formats are `pdf`, `png`, and `both`. The plotting script writes
throughput scaling, latency-throughput, tail latency, goodput, capacity,
reliability, and energy figures when the needed columns are present. Series use
the colorblind-safe Okabe-Ito palette and are also distinguished by marker and
line style, so identity never rests on hue alone. Add `--serif` for a Times-like
body font matching an IEEE paper, and `--table` to also emit a paper-ready
per-point summary as `OUT_DIR/figs/summary_table.tex` (booktabs) and
`OUT_DIR/figs/summary_table.md`.

The reliability and energy figures use these definitions:

- The reliability figure is two panels. The left panel is the real reliability
  signal: the share of *finished* requests that returned an error (this is zero
  across the reference sweep). The right panel is the share of requests that both
  started and completed inside the measurement window
  (`requests_started_and_completed_in_window / requests_completed`), using the
  same population of successful requests in numerator and denominator. At high
  concurrency it falls because long successful requests outlive the
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

Repeated loads in `index.csv` can come from `REPEATS>1` or a rerun. The plot
averages compatible rows into one point per load, reports the number of copies,
and shades the min-max spread across copies behind the mean line for the
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

## Metric definitions

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

The client records two accounting sets for each logical window:

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
  `--collect-hf-metadata` is used. These fields describe the checkpoint and
  full-KV estimate. For quantized or repartitioned serving runs, the log-derived
  worker fields describe the launched workers.

The index-backed capacity numbers are collected into `capacity_table.tex`/`.md`
by `plot_results.py --table`.

## Measurement window

Closed-loop measurement starts after the configured warmup and after every
worker completes its first request. The logical window extends in configured
steps until `min_window_requests` is reached or `max_measure_s` is reached.
Output-token throughput counts tokens received during the window across all
requests. Latency percentiles use requests started in the window and tracked to
completion. After the window, replacement load continues until measurement
requests finish and the configured cooldown elapses. The
[`server_vs_client_throughput` figure](#metric-definitions) compares client and
server token rates; the completed-only series reports the earlier accounting.

Guidance:

- Treat `warmup_s` as the minimum stabilization time. In closed-loop mode the
  harness automatically waits longer if any worker's initial request is still
  running, so the shipped `120` seconds cannot open the window in the middle of
  a slow first wave: at high concurrency on the slowest models the first-wave
  rule stretches warmup to the worst request lifetime automatically.
- Treat `measure_s` as the minimum window and let `min_window_requests` size
  the slow points. The shipped `600` seconds with a 20-request floor backs
  p50/p90 in every replicate: a slow c=1 point extends in 60 s steps (up to
  `3600` s) instead of reporting a median from a handful of requests. p99 at
  the slowest low-load points stays below the 100-sample target per replicate;
  the plots flag those points as low-n instead of reporting them as reliable.
  Inspect the plotter's per-copy sample warnings after the run.
- Treat `cooldown_s` as a minimum (shipped `30`). If a measurement-started
  request remains active, the harness keeps replacement load running until it
  finishes, preventing drain-down bias without guessing the worst lifetime, so
  a long minimum cooldown only adds wall time.
- Keep `request_timeout_s` above the worst queue-plus-stream lifetime. The
  shipped `14400` seconds gives measurement-started requests room to finish
  under severe backlog instead of converting slow successes into client
  timeouts.
- Aim for at least 20 started-in-window requests for medians and at least 100
  for p99; these are the plotting defaults. Prefer many more at loads you will
  quote.
- Keep the configured window consistent across a sweep and across configs you
  compare. Points extended by the minimum-sample rule share the configured
  window and are listed in the plotter's data-quality report.
- Replicate every point on separate launches (`REPEATS=3`, the campaign
  default) rather than lengthening one window: see
  [Replicates and statistics](#replicates-and-statistics).

## Power telemetry

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

For dual-node runs, a local sampler only sees the head node's GPU, so
`scripts/run_all.sh` (for `tp2`/`pp2` labels) also passes
`--worker-power-container`/`--worker-power-node-ip` and samples the worker's GPU
through a Ray actor, totaling both nodes into `mean_power_w` with `power_nodes=2`
marking full coverage (see [Metric definitions](#metric-definitions) for the mechanism). The general
harness can describe a head-only result when a worker sampler fails; the
shipped full config is stricter: `require_power_data: true` rejects the point
instead of writing it unless a single run has one node and a tp2/pp2 run has
both nodes with positive, dense samples bracketing the full window. It likewise sets
`require_vllm_metrics_data: true`, so missing request-running/waiting, KV-usage,
prompt/generation-token, or preemption samples; a scrape gap over five seconds;
an incomplete counter span; or a non-advancing generation counter while the
client receives tokens rejects the point. Disable these requirements only for an explicitly
non-paper diagnostic run.

Temperature fields use telemetry schema version 2. Summary values include the
window-only mean, maximum, valid sample count, maximum observed sample gap,
sensor source, units, and head/worker coverage. Missing readings remain null
with an explicit status. GPU temperature is diagnostic and does not gate a
result. The thermal log contains timestamped host `sensors` output, including
available NIC ASIC sensors. A missing `sensors` command is recorded in that
log and does not gate a result.

## Raw data and archives

Raw telemetry (`*.raw.jsonl`, `*.power.jsonl`, `*.vllm_metrics.jsonl`) volume
depends on campaign duration and model output. `.gitignore` keeps raw telemetry
and plain `.log` files out of git. To preserve logs in a campaign commit,
create one sibling ZIP per ignored log:

```bash
scripts/archive_logs.py zip 'results/<campaign-dir>'
```

Each `name.log.zip` contains `name.log` and stays beside it. The source log
remains on disk and stays ignored. Add the ZIP files to the campaign commit.
Restore plain logs from ZIPs with:

```bash
scripts/archive_logs.py unzip 'results/<campaign-dir>'
```

Unzip leaves the ZIP files in place and does not overwrite an existing log.
Commit `OUT_DIR/results/index.csv`, `OUT_DIR/results/*.summary.json`, the
`OUT_DIR/results/serving_policy_*.json` files, the interconnect CSVs,
`OUT_DIR/results/client_environment.json`, `OUT_DIR/results/campaign_manifest.json`,
and `OUT_DIR/figs/`. Archive the raw data outside the repository:

```bash
scripts/pack_raw.sh results/xyz      # -> ../dgx-spark-raw/xyz-raw-<date>.tar.zst
```

The archive scans recursively, including raw telemetry and logs under
`OUT_DIR/preflight/`. It ships a `SHA256SUMS` file; extracting it back into the
result directory restores everything `validate_campaign.py verify` hashes.
Raw files that were committed before this policy (under `baselines/`) remain
tracked.

## Baselines and comparisons

`baselines/` holds the PMBS26 TCP campaign, its variance subset, and the
October RDMA pilot; `baselines/README.md` documents each one, and the git tag
`pre-rdma-campaign-2026-10-15` pins them at their original paths. Two sets of
models overlap under the same labels and revisions, and `run_job.sh` compares
both with `scripts/compare_campaigns.py`:

- the Llama 3.1 8B ladder against the PMBS26 TCP baseline
  (`figs/stats/compare_vs_pmbs26_tcp.md`): transport and software changed together;
- Qwen3-Next 80B (NVFP4 and BF16), the pilot pair kept in the matrix, against
  the pilot (`figs/stats/compare_vs_rdma_pilot.md`): same transport and
  cabling, so the difference isolates the image, window, and replicate
  changes.

The pilot's rows are not merged into the new index. They are single-repeat
`steady-exact-v3` rows, so Qwen3-Next is re-measured with three replicates. The
pilot's Nemotron Super 49B and Nemotron Puzzle 75B pairs are not in the current
matrix.

## Transport evidence and the TCP ablation

`NCCL_IB_DISABLE=0` only permits RDMA; NCCL falls back to sockets silently
when the RoCE handshake fails. With `NCCL_DEBUG=INFO` and
`NCCL_DEBUG_SUBSYS=INIT,NET` on both Ray containers (the `connectx_env.sh` defaults),
NCCL logs the transport of every channel into the server log.
`run_models.sh` fails a dual launch whose log shows the wrong transport, and
`scripts/audit_nccl_transport.py` writes a per-launch table (RDMA devices,
GPUDirect RDMA channels, verdict) to `figs/stats/nccl_transport_audit.md`.

To attribute differences to RDMA alone, rerun a pre-registered subset over TCP
with the same image and harness, into its own directory. The most
transport-sensitive cases are small dense models (all-reduce latency is a
large share of their per-token time) and the overlap with the PMBS26 baseline:

```bash
# On both nodes: restart Ray with the TCP fallback.
scripts/start_ray.sh stop
NCCL_IB_DISABLE=1 scripts/start_ray.sh head
# Run the same worker command on the configured worker.
# On the selected head: same harness, TCP all-reduce thresholds, subset matrix.
NCCL_IB_DISABLE=1 ALLREDUCE_MIN_1G_BUSBW_GBPS=1.8 ALLREDUCE_MAX_8B_LATENCY_S=0.005 \
  OUT_DIR=results/tcp-ablation-2026-11-12 \
  CAMPAIGN_LABELS="llama3.1-8b llama3.1-8b-fp8 llama3.1-8b-nvfp4" \
  CAMPAIGN_CONFIGS="tp2 pp2" \
  scripts/run_job.sh
```

The subset contains 18 launches and 108 windows. Its minimum scheduled phase
time is 22.5 hours before extensions and launches. Finalization runs with
`--completed-only` for subsets.

## Interpreting results

- A precision step can change the KV-cache dtype as well as the weight
  precision. The index column `kv_cache_dtype` records the KV-cache dtype of
  every row.
- `tok_thru_tps`, the exact client token-ID rate, is the throughput result.
  `server_gen_tps` is a counter and scrape diagnostic.
- `figs/stats/replicate_stats.md` gives the mean and 95% Student-t interval of
  each point. An ordering between two configs is supported where its Welch
  interval in the statistics report excludes zero.
- `nvidia_smi` energy is GPU energy (`power_scope=gpu_devices`). It is not
  system or wall energy.
- Cross-node collectives (`tp2`) and pipeline activation transfers (`pp2`) add
  communication. Check the window length, sample count, KV occupancy, and
  request completions before interpreting a throughput change.
