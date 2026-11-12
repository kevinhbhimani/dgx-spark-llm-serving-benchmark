# RDMA paper campaign readiness

A passing software plan validates the campaign definition. Publication readiness
also requires a clean committed checkout, an idle two-node cluster, actual RDMA
channel evidence, and successful hardware measurements.

## Node scope

| Task | Run command on | Nodes involved |
| --- | --- | --- |
| Software checks and `PLAN_ONLY=1` | The selected head | Local checkout and Python environment; no cluster work |
| Firmware, Netplan, hostnames, SSH, and local ConnectX checks | Both nodes | Each command checks the local node |
| `scripts/verify_connectx.sh` | Both nodes | Checks local routes and probes each configured peer |
| `scripts/start_ray.sh head` / `worker` | The selected head / worker | Starts Ray on both nodes |
| `ray status`, RDMA microbenchmarks, canary, and campaign | The selected head | Ray uses the worker for distributed work |
| `scripts/hardware_smoke.sh` | The selected head | Single-node diagnostic using the selected head's GPU |
| `scripts/start_ray.sh stop` | Both nodes | Stops the local Ray container on each node |

## Software checks

Run on the selected head from the repository root with Python 3.12 or newer.
Topology entrypoints default `CLUSTER_CONFIG` to
`<repo-root>/configs/cluster.yaml` on both systems. Set it only for a different
topology file. Different local paths are allowed when the canonical topology
digests match.

```bash
source .venv/bin/activate
export PYTHONNOUSERSITE=1
# Optional override when the repo default does not describe this cluster.
# export CLUSTER_CONFIG=/path/to/site/cluster.yaml
python -m pip install -r requirements.lock
python scripts/check_environment.py
python -m unittest discover -s tests -v
python -m compileall -q bench plotting scripts
(for script in scripts/*.sh; do bash -n "$script" || exit 1; done)
export OUT_DIR=results/rdma-v4-paper-2026-11-12
PLAN_ONLY=1 scripts/run_job.sh
```

The default plan has 17 checkpoints, 47 model/configuration pairs, 141 server
launches, and 846 measurement windows at concurrencies 1 4 8 16 32 64. Each
scheduled phase has a 750-second minimum: 120 seconds of warmup, 600 seconds
of measurement, and 30 seconds of cooldown. The total is 176.25 sequential
hours before extensions, canaries, downloads, and model startup.
Use a fresh output directory after changing
the experiment, dependencies, harness, material serving policy, or `GPU_UTIL`
target. The campaign GPU-memory target defaults to `0.80`. Set `GPU_UTIL` on
the selected head to override it; planning, validation, memory preflight, and
serving launches use the resolved value. Keep that value unchanged when resuming.

Review and commit the source/configuration changes before hardware collection.
The campaign's source gate rejects uncommitted benchmark changes so each row
refers to a recoverable commit.

## Hardware gates

Configure both machines using [cluster-setup.md](cluster-setup.md). Run the
commands below on the named node. Reserve both nodes for the campaign and stop
unrelated workloads through their normal service controls.
The campaign defaults to `gpu_util_default: 0.80` for every model and serving
config. Set `GPU_UTIL` to override the shared target for planning, validation,
and serving. `run_models.sh` passes the resolved value through
`launch_server.sh` to `vllm serve` as `--gpu-memory-utilization` and uses the
same value in its free-memory preflight. The default targets 102.4 GB on a
nominal 128 GB node. vLLM applies the setting per instance. The campaign uses
one serving instance per node at a time; unrelated concurrent processes are
outside this limit. See the [vLLM CacheConfig reference](https://docs.vllm.ai/en/v0.29.0/api/vllm/config/cache/).

Run the local fabric checks on both systems. Run the hardware smoke test and
start the Ray head on the selected system:

```bash
scripts/verify_connectx.sh
scripts/hardware_smoke.sh                    # selected-head diagnostic output
SMOKE_LABEL=nemotron3.5-lightning-30b-a3b-nvfp4 SMOKE_GPU_UTIL=0.80 \
  SMOKE_CONCURRENCIES="1 64" OUT_DIR=results/smoke-lightning-2026-11-12 \
  scripts/hardware_smoke.sh                  # concurrency-64 smoke check
scripts/start_ray.sh head
```

Start the Ray worker on the other configured system:

```bash
scripts/start_ray.sh worker
```

Return to the selected head for the cluster checks and campaign commands:

```bash
export OUT_DIR=results/rdma-v4-paper-2026-11-12
docker exec vllm-spark ray status
CANARY_FIRST=1 STOP_AFTER_CANARY=1 scripts/run_job.sh
scripts/run_job.sh
```

The canary command runs the campaign preflight and one launch for every
model/configuration pair, then exits before measurements. The next command
starts the campaign in the same output directory. The campaign runs on the
selected head and uses Ray on both nodes. The hardware smoke test runs only on
the selected head. Stop Ray by running `scripts/start_ray.sh stop` once on
each system after collection.

`scripts/verify_connectx.sh` checks configured addresses, routing, interface pings,
and active RDMA ports. It does not measure throughput. After Ray is active on
both nodes and both GPUs are idle, `scripts/run_job.sh` runs a fresh all-reduce
gate before the canary and measurement work on every invocation. The gate
requires `NET/IB` on both ranks, at least 16 GB/s at 1 GiB, and at most 100
microseconds for the 8-byte collective.

Run the microbenchmarks separately for diagnostics when needed:

```bash
OUT_DIR="$OUT_DIR" scripts/ray_allreduce_bench.sh
OUT_DIR="$OUT_DIR" scripts/interconnect_extra_bench.sh
```

The separate all-reduce run does not replace the fresh campaign gate. During
`run_job.sh`, a send/recv failure is logged and does not block later campaign
work. The campaign can reuse a valid send/recv result for the current runtime
and source. Keep NCCL logs alongside microbenchmark CSVs and per-launch
logs/policies. `NCCL_IB_DISABLE=0` alone allows silent socket fallback; it does
not prove RDMA. The gates require actual channel evidence and timing checks. A
valid RDMA verdict does not establish that all four devices carry traffic:
support that claim with device/channel logs and per-interface counters or
per-device bandwidth measurements.

The pinned image's vLLM 0.29.0 deadlocked `pp2` startup during FlashInfer
autotuning when pipeline stages tuned different kernels. The October 2026
canary observed this behavior on a Nemotron 3 Super NVFP4 checkpoint, which is
no longer in the current model matrix. The upstream fix is in v0.32.0.
Launches default to `FLASHINFER_AUTOTUNE=0`
(`--no-enable-flashinfer-autotune`) for every config. Each serving policy
records this setting. Report the setting in the paper.

Nemotron 3.5 Lightning 30B-A3B NVFP4 is in the current matrix with
`max_num_seqs` unset. Run the concurrency-64 smoke check above and retain its
startup and memory logs. Change its scheduler limit only after a measured
failure and a passing canary with the revised policy.

## Publication checks and interpretation

The campaign verifies the result rectangle before plotting and seals artifacts
in `campaign_manifest.json`. For the full default matrix, repeat the final check
with:

```bash
python scripts/validate_campaign.py verify \
  --models configs/models.yaml --experiment configs/experiment.yaml \
  --out-dir "$OUT_DIR" --concurrencies 1 4 8 16 32 64 --repeats 3 \
  --manifest "$OUT_DIR/results/campaign_manifest.json"
```

- Use the three fresh-launch replicates for uncertainty. The plotted min/max
  band is a spread, not a confidence interval. The statistics report supplies
  Student-t and Welch intervals; its many pairwise intervals have no automatic
  family-wise correction.
- Twenty successful requests is an acceptance floor. It cannot support a stable
  p99 estimate by itself; inspect sample counts and plotting warnings, and
  preselect longer windows or additional samples for tail-latency claims.
- TTFT and ITL measure token arrival at the client. Multiple tokens in one SSE
  delta share a timestamp, producing zero gaps within that burst. These are
  observed serving latencies, not internal GPU decode timings.
- Throughput counts tokens arriving inside the measurement window, including
  requests crossing its boundaries. Latency and request goodput use requests
  started inside that window and followed through completion under continuing
  load. These populations differ intentionally.
- `nvidia_smi` energy covers the sampled GPU devices. It does not measure
  whole-system energy. Check that dual-node rows cover both nodes.
- RDMA causality requires a TCP ablation with the same image, workload,
  revisions, and server policies. Historical campaigns with different software
  cannot isolate transport.
