# DGX Spark LLM Serving Benchmark

Benchmark harness and measurement data for the paper
[*Benchmarking LLM Inference on Desktop-Scale NVIDIA Grace Blackwell Clusters*](https://arxiv.org/abs/2611.20289)
(Kevin Bhimani, Christian Escobar, Daniel Mihovch, Rudolf Eigenmann, Sunita
Chandrasekaran; arXiv:2611.20289, 2026).

The harness serves open-weight LLMs with vLLM on NVIDIA DGX Spark (GB10)
systems in three deployments:

| Config | Deployment |
| --- | --- |
| `single` | One Spark |
| `tp2` | Two Sparks, tensor parallelism (`--tensor-parallel-size 2`) |
| `pp2` | Two Sparks, pipeline parallelism (`--pipeline-parallel-size 2`) |

A Python client drives closed-loop streaming load against vLLM's
`/v1/completions` endpoint. Each measurement window records time to first
token (TTFT), inter-token latency (ITL), output-token throughput, goodput at
two ITL targets, GPU power on every participating node, and vLLM server
metrics. Each window also records its provenance: model and tokenizer
revisions, container image digest, serving policy, and harness commit.

## Contents

1. [Paper data](#paper-data)
2. [Requirements](#requirements)
3. [Quick start](#quick-start)
4. [Running a campaign](#running-a-campaign)
5. [Methodology](#methodology)
6. [Outputs](#outputs)
7. [Repository layout](#repository-layout)
8. [Documentation](#documentation)
9. [Development checks](#development-checks)
10. [Citation](#citation)
11. [License](#license)

## Paper data

The paper analyzes the campaign in
`results/RDMA-Betelgeuse-Suite-benchmark-2026-10-30-001/`. Its measurement
windows ran from 2026-10-31 to 2026-11-10 on two DGX Sparks (head `diya`,
worker `betelgeuse`) with the four-rail RoCEv2 topology in
`configs/cluster.yaml`.

| Path in the campaign directory | Contents |
| --- | --- |
| `results/index.csv` | One row per measurement window (918 rows) |
| `results/*.summary.json` | Metrics, resolved configuration, and provenance of each window |
| `results/serving_policy_*.json` | Launch policy and runtime identity of each model and config |
| `results/allreduce_ray_*.csv`, `results/interconnect_extra_*.csv` | NCCL all-reduce and send/recv microbenchmarks |
| `results/campaign_manifest.json` | SHA-256 digests of the campaign artifacts |
| `logs/` | Zipped server, thermal, and microbenchmark logs |
| `figs/` | Per-model figures, replicate statistics, baseline comparisons, and the NCCL transport audit |

- The paper analyzes 846 windows of 17 checkpoints. The index also contains the
  72 windows of the two Nemotron-3-Super-120B-A12B checkpoints that the paper
  excludes.
- Every row records `harness_git_commit`
  `9c14fab3a64d416bb5f58e16324663a9b9a9c0f2`. A 2026-11-17 edit of commit
  messages changed that ID to `14a151cadbe091dfa79055bfc982c753f8ed645b`
  without changing any file. [docs/commit-map.txt](docs/commit-map.txt) maps
  every earlier ID to its current ID. That commit holds the harness, model
  matrix, and experiment configuration that produced the data. Commit
  `ae1d188` added the sealed manifest.
- Later commits removed the two excluded checkpoints from `configs/models.yaml`
  and changed harness code. The parsed values of `configs/experiment.yaml` are
  unchanged.
- Raw request-level telemetry and GPU power samples are not in the repository.
  They are available from the corresponding author upon request.
- `baselines/` holds earlier campaigns, including the July 2026 TCP campaign
  that the paper compares against. See [baselines/README.md](baselines/README.md).

Regenerate the per-model figures into a separate directory:

```bash
python plotting/plot_results.py \
  --index results/RDMA-Betelgeuse-Suite-benchmark-2026-10-30-001 \
  --out /tmp/rdma-campaign-figs --format png
```

## Requirements

- One DGX Spark for `single`. Two DGX Sparks for `tp2` and `pp2`, connected by
  ConnectX-7 links with RoCEv2 enabled. See
  [docs/cluster-setup.md](docs/cluster-setup.md).
- Docker with the NVIDIA container runtime. The benchmark account needs access
  to the Docker daemon.
- The vLLM image pinned in `scripts/connectx_env.sh`:
  `nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2`
  (NGC 26.09: vLLM 0.29.0, CUDA 13.4.1, NCCL 2.31.2). `scripts/start_ray.sh`
  installs Ray 2.56.1 into the container.
- Python 3.12 or newer for the benchmark client.
- At least 200 GB free in each node's Hugging Face cache. The default cache is
  `$HOME/.cache/huggingface-$UID`. Set `HF_CACHE` in a repo-local `.env` to use
  another path.
- `HF_TOKEN` in the repo-local `.env` on the head node for gated models such as
  Llama 3.1. Run `chmod 600 .env` after adding the token.
- Noninteractive sudo for the page-cache flush that `scripts/run_models.sh`
  runs before every server launch, on each participating node. Add this rule
  with `visudo` and replace `<user>` with the benchmark account:

  ```text
  <user> ALL=(root) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches
  ```

- For two-node runs: noninteractive SSH from the head to the worker's
  `ssh_target`, with the worker's host key in `known_hosts`.

## Quick start

Run all commands from the repository root.

1. Install the locked client environment:

   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   export PYTHONNOUSERSITE=1
   python -m pip install --upgrade pip
   python -m pip install -r requirements.lock
   python scripts/check_environment.py
   ```

2. Run the unit tests. They use fixtures and mocked commands and need no GPU,
   Docker daemon, or sudo:

   ```bash
   python -m unittest discover -s tests
   ```

3. Print a campaign plan. Plan mode validates the model matrix, the experiment
   configuration, and the expected result grid. It performs no hardware
   operations. The one-node plan runs on any host. The two-node plan runs only
   on a host named in the topology file (`configs/cluster.yaml`, or the file
   in `CLUSTER_CONFIG`).

   ```bash
   SINGLE_NODE_ONLY=1 PLAN_ONLY=1 scripts/run_job.sh  # 14 models, 42 launches, 252 windows
   PLAN_ONLY=1 scripts/run_job.sh                     # 17 models, 141 launches, 846 windows
   ```

4. On a Spark, start a vLLM server in one terminal. `SINGLE_NODE_ONLY=1` skips
   the two-node topology, so the command runs on a Spark that is not listed in
   `configs/cluster.yaml`:

   ```bash
   export SINGLE_NODE_ONLY=1
   export MODEL=Qwen/Qwen3-8B
   export REVISION=b968826d9c46dd6066d109eabc6255188de91218
   HF_HUB_OFFLINE=0 scripts/launch_server.sh single
   ```

5. In a second terminal, run the connectivity smoke test. `configs/smoke.yaml`
   checks connectivity and output files. Its results are not
   publication-grade measurements.

   ```bash
   source .venv/bin/activate
   python -m bench.run_benchmark --config configs/smoke.yaml \
     --config-label smoke --base-url http://127.0.0.1:8000
   ```

## Running a campaign

A campaign acquires each model's pinned snapshot, launches a server for every
config in `configs/models.yaml`, sweeps concurrency 1 to 64 in three
replicates, deletes the weights, and seals the results. `scripts/run_job.sh`
refuses to start while `bench/`, `configs/`, `scripts/`, `plotting/`, or the
requirements files have uncommitted changes. The full procedure, including
resume and verification, is in
[docs/campaign-runbook.md](docs/campaign-runbook.md).

### One Spark

```bash
export SINGLE_NODE_ONLY=1
scripts/run_preflight_then_benchmark.sh
```

The command runs the readiness checks and a canary launch of every selected
model, then the measurement campaign and finalization, in one output
directory under `results/`. Set `CAMPAIGN_LABELS` to select models, for
example `CAMPAIGN_LABELS='llama3.1-8b qwen3.6-27b'`.

### Two Sparks

1. Configure both nodes and describe them in `configs/cluster.yaml` (see
   [docs/cluster-setup.md](docs/cluster-setup.md)). Check out the same commit
   on both nodes.
2. Run `scripts/verify_connectx.sh` on both nodes.
3. Start Ray with `scripts/start_ray.sh head` on the head node and
   `scripts/start_ray.sh worker` on the worker node.
4. On the head node, inside `tmux`, run the preflight and the campaign:

   ```bash
   OUT_DIR=results/my-campaign scripts/run_preflight_then_benchmark.sh
   ```

5. After an interruption, start Ray again if needed and resume with
   `OUT_DIR=results/my-campaign scripts/run_job.sh`. Completed windows are
   skipped.
6. Run `scripts/start_ray.sh stop` on each node when the campaign ends.

The full matrix has 176.25 hours of scheduled warmup, measurement, and
cooldown, before window extensions, downloads, canaries, and model startup.
The paper states that the complete campaign needs exclusive use of both
systems for about eleven days.

### Individual models

`scripts/run_models.sh` launches, probes, sweeps, and tears down each selected
model for one config:

```bash
CANARY_ONLY=1 MODEL_LABELS=llama3.1-8b scripts/run_models.sh tp2  # launch and probe only
MODEL_LABELS=llama3.1-8b scripts/run_models.sh tp2                # three-replicate sweep
MODEL_LABELS=llama3.1-8b scripts/purge_model_cache.sh             # delete the weights
```

On one Spark, set `SINGLE_NODE_ONLY=1` and use the `single` config. Options
are listed in [docs/reference.md](docs/reference.md#multi-model-runs).

## Methodology

The shipped `configs/experiment.yaml` and `configs/models.yaml` define the
paper's measurement protocol.

- Workload: synthetic token-ID prompts in four buckets of input/output tokens:
  128/128, 1024/256, and 4096/256 with weight 1, and 8192/512 with weight 0.5.
  Each prompt begins with a unique tag, and vLLM prefix caching is off.
- Requests: streaming, greedy (`temperature: 0`), with `ignore_eos` and
  `min_tokens == max_tokens`, so every successful request returns exactly its
  requested output length.
- Load: closed loop at concurrency 1, 4, 8, 16, 32, and 64.
- Replicates: three independent server launches per model, config, and
  concurrency. The config order rotates between replicates.
- Window: at least 120 s of warmup; at least 600 s of measurement, extended in
  60 s steps until 20 requests have started, up to 3600 s; at least 30 s of
  cooldown. Requests that start inside the window are tracked to completion.
- Acceptance: a window is recorded only with zero failed requests, at least 20
  successful requests, and complete GPU power and vLLM metric coverage.
- Goodput: requests with TTFT at or below 3 s and mean ITL at or below 100 ms
  (strict tier) or 200 ms (loose tier), per second.
- Serving: `--gpu-memory-utilization 0.80`, `--max-model-len 8704`, prefix
  caching off, FlashInfer autotuning off, and `--generation-config vllm`. Host
  page cache is flushed before every launch.
- Power: `nvidia-smi` GPU power, sampled every 100 ms on each participating
  node. It excludes CPU, network, and other system power.
- Transport: NCCL uses RDMA (`NCCL_IB_DISABLE=0`). A dual-node launch whose
  server log shows NCCL socket transport is refused.

Metric definitions and configuration fields are in
[docs/reference.md](docs/reference.md).

## Outputs

Each campaign writes one directory (`OUT_DIR`):

```text
<OUT_DIR>/
  results/    index.csv, *.summary.json, serving_policy_*.json, microbenchmark CSVs, campaign_manifest.json
  figs/       figures and tables; figs/stats/ holds replicate statistics, the transport audit, and baseline comparisons
  logs/       server, job, thermal, and microbenchmark logs, and the system snapshot
  preflight/  readiness and canary artifacts
```

Per-request telemetry (`*.raw.jsonl`, `*.power.jsonl`,
`*.vllm_metrics.jsonl`) is written beside the summaries and is ignored by git.
`scripts/pack_raw.sh` archives it for storage outside the repository.
`scripts/archive_logs.py zip` writes a ZIP beside each ignored log for
committing. Plot a campaign with
`python plotting/plot_results.py --index "$OUT_DIR" --format both`.

## Repository layout

```text
bench/       benchmark client: workload, streaming requests, load generation, metrics, provenance
configs/     experiment.yaml (workload, windows, SLOs), models.yaml (model matrix),
             cluster.yaml (two-node topology), smoke.yaml (connectivity test)
scripts/     launchers, campaign orchestration, cache management, microbenchmarks, validation
plotting/    campaign figures, cross-model trends, interconnect figures
utils/       campaign output path resolution
tests/       unit tests (no GPU required)
docs/        cluster setup, campaign runbook, reference, troubleshooting
offline/     bundle builder for nodes without internet access
results/     the paper's campaign
baselines/   earlier campaigns
```

| Entry point | Purpose |
| --- | --- |
| `scripts/run_preflight_then_benchmark.sh` | Readiness checks, canary, campaign, and finalization in one output directory |
| `scripts/run_job.sh` | Resumable campaign over the model matrix; `PLAN_ONLY=1` prints the plan |
| `scripts/run_models.sh` | Launch, probe, sweep, and tear down each model for one config |
| `scripts/launch_server.sh` | Start vLLM in `single`, `tp2`, or `pp2` mode |
| `scripts/run_all.sh` | Sweep concurrencies against a running server |
| `scripts/start_ray.sh` | Start or stop the Ray container on a node |
| `scripts/verify_connectx.sh` | Check fabric addresses, routes, peers, and RDMA devices |
| `scripts/ray_allreduce_bench.sh`, `scripts/interconnect_extra_bench.sh` | NCCL all-reduce and send/recv microbenchmarks |
| `scripts/validate_campaign.py` | Plan or verify a campaign result grid and manifest |
| `scripts/variance_report.py` | Replicate statistics and Welch intervals |
| `scripts/audit_nccl_transport.py` | NCCL transport of every dual-node launch |
| `scripts/compare_campaigns.py` | Compare a campaign with a baseline index |
| `scripts/purge_model_cache.sh` | Delete model snapshots from the node caches |
| `plotting/plot_results.py` | Per-campaign and per-model figures and tables |
| `plotting/plot_trends.py` | Cross-model trend figures |
| `plotting/plot_interconnect.py` | Microbenchmark figures by transport |

## Documentation

- [docs/cluster-setup.md](docs/cluster-setup.md): two-node topology file,
  fabric checks, and Ray.
- [docs/campaign-runbook.md](docs/campaign-runbook.md): campaign procedure for
  one and two Sparks, including canary, resume, and verification.
- [docs/rdma-readiness.md](docs/rdma-readiness.md): pre-campaign gates and
  publication checks.
- [docs/model-run-special-cases.md](docs/model-run-special-cases.md):
  per-model launch requirements.
- [docs/reference.md](docs/reference.md): configuration fields, environment
  variables, outputs, and metric definitions.
- [docs/troubleshooting.md](docs/troubleshooting.md): error messages and
  fixes.
- [offline/README.md](offline/README.md): installation on nodes without
  internet access.
- [baselines/README.md](baselines/README.md): earlier campaigns.

## Development checks

```bash
export PYTHONNOUSERSITE=1
python scripts/check_environment.py
python -m compileall -q bench plotting scripts utils tests
python -m unittest discover -s tests
(for script in scripts/*.sh; do bash -n "$script" || exit 1; done)
python -m pip check
```

## Citation

```bibtex
@misc{bhimani2026dgxspark,
  title         = {Benchmarking {LLM} Inference on Desktop-Scale {NVIDIA} Grace Blackwell Clusters},
  author        = {Bhimani, Kevin and Escobar, Christian and Mihovch, Daniel and Eigenmann, Rudolf and Chandrasekaran, Sunita},
  year          = {2026},
  eprint        = {2611.20289},
  archivePrefix = {arXiv},
  url           = {https://arxiv.org/abs/2611.20289}
}
```

## License

MIT. See [LICENSE](LICENSE).
