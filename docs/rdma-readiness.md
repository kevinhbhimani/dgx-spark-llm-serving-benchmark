# RDMA paper campaign readiness

A passing software plan validates the campaign definition. Publication readiness
also requires a clean committed checkout, an idle two-node cluster, actual RDMA
channel evidence, and successful hardware measurements.

## Node scope

| Task | Run command on | Nodes involved |
| --- | --- | --- |
| Software checks and `PLAN_ONLY=1` | Diya only | Local checkout and Python environment; no cluster work |
| Firmware, Netplan, host entries, and local ConnectX checks | Both nodes | Each command checks the local node |
| `scripts/verify_connectx.sh` | Diya only | Checks Diya's route and probes each Hoedus ConnectX peer |
| `scripts/start_ray.sh head` / `worker` | Diya / Hoedus, respectively | Starts Ray on both nodes |
| `ray status`, RDMA microbenchmarks, canary, and campaign | Diya only | Ray distributes the two-node work to Hoedus |
| `scripts/hardware_smoke.sh` | Diya only | Single-node diagnostic using Diya's GPU |
| `scripts/start_ray.sh stop` | Both nodes | Stops the local Ray container on each node |

## Software checks

Run on Diya from the repository root with Python 3.12 or newer:

```bash
source .venv/bin/activate
export PYTHONNOUSERSITE=1
python -m pip install -r requirements.lock
python scripts/check_environment.py
python -m unittest discover -s tests -v
python -m compileall -q bench plotting scripts
(for script in scripts/*.sh; do bash -n "$script" || exit 1; done)
export OUT_DIR=results-rdma-paper
PLAN_ONLY=1 scripts/run_job.sh
```

The default plan is 19 checkpoints, 53 model/config curves, 159 independent
server launches, and 1272 measurement windows. Its minimum measurement time is
about 265 sequential hours; warmup and sample-count extensions, canaries,
downloads, and launches add time. Use a fresh output directory after changing
the experiment, dependencies, harness, or material serving policy.

Review and commit the source/configuration changes before hardware collection.
The campaign's source gate rejects uncommitted benchmark changes so each row
refers to a recoverable commit.

## Hardware gates

Configure both machines using [cluster-setup.md](cluster-setup.md). Run the
commands below on the named node. Reserve both nodes for the campaign and stop
unrelated workloads through their normal service controls.
The campaign sets `gpu_util_default: 0.80` for every model and serving config.
`run_models.sh` passes that value through `launch_server.sh` to `vllm serve` as
`--gpu-memory-utilization`. This targets 102.4 GB on a nominal 128 GB node, and
the launcher requires 80% of each node's detected `MemTotal` to be free before
serving starts. vLLM applies the setting per instance. The campaign uses one
serving instance per node at a time; unrelated concurrent processes are outside
this limit. See the [vLLM CacheConfig reference](https://docs.vllm.ai/en/v0.29.0/api/vllm/config/cache/).

Run the local checks and start the Ray head on Diya:

```bash
scripts/verify_connectx.sh
scripts/hardware_smoke.sh                    # Diya-only diagnostic output
SMOKE_LABEL=nemotron3.5-lightning-30b-a3b-nvfp4 SMOKE_GPU_UTIL=0.80 \
  SMOKE_CONCURRENCIES="1 128" OUT_DIR=results-smoke-lightning \
  scripts/hardware_smoke.sh                  # top load; see the 26.09 note below
scripts/start_ray.sh head
```

Start the Ray worker on Hoedus:

```bash
scripts/start_ray.sh worker
```

Return to Diya for the cluster checks and campaign commands:

```bash
docker exec vllm-spark ray status            # checks the two-node cluster
OUT_DIR="$OUT_DIR" scripts/ray_allreduce_bench.sh
OUT_DIR="$OUT_DIR" scripts/interconnect_extra_bench.sh
STOP_AFTER_CANARY=1 scripts/run_job.sh       # every checkpoint/config once
scripts/run_job.sh                           # rerun this command to resume
```

The RDMA microbenchmarks and campaign are launched on Diya and use Ray on both
nodes. The hardware smoke tests run only on Diya. Stop Ray by running
`scripts/start_ray.sh stop` once on Diya and once on Hoedus after collection.

`scripts/verify_connectx.sh` checks name resolution, routing, interface pings,
and active RDMA ports. It does not measure throughput. Run
`scripts/ray_allreduce_bench.sh` from Diya after Ray is active on both nodes
and both GPUs are idle to measure current transport and speed. It requires
`NET/IB` on both ranks, at least 16 GB/s at 1 GiB, and at most 100 microseconds
for the 8-byte collective. `scripts/run_job.sh` runs this fresh gate before
the canary and measurements on every invocation.

Each step must pass. Keep the NCCL logs alongside the microbenchmark CSVs and
the per-launch logs/policies. `NCCL_IB_DISABLE=0` alone allows silent socket
fallback; it does not prove RDMA. The gates require actual channel evidence and
timing checks. A valid RDMA verdict does not establish that all four devices
carry traffic: support that claim with device/channel logs and per-interface
counters or per-device bandwidth measurements.

The pinned NGC 26.09 image has a documented Spark constraint for Nemotron 3
Super NVFP4: `--max-num-seqs 4`. The model matrix records `max_num_seqs: 4` for
that checkpoint and fingerprints this policy across serving configurations.
The client still sweeps up to 128 outstanding requests; above four, server
queueing is part of the measured latency. Report this server sequence limit in
the paper. The FP8 checkpoint is not covered by the note and runs uncapped, so
its comparison with NVFP4 is matched only at concurrency 4 or below. See the
[NVIDIA 26.09 known issues](https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/rel-26-09.html).

The same note names Nemotron Nano V3 NVFP4. Nemotron 3.5 Lightning 30B-A3B
NVFP4 is the same class of checkpoint (30B-A3B Mamba hybrid), and the note does
not say whether it applies. The canary sends one short request, so it cannot
catch a failure that appears only under load. Run the concurrency-128 smoke
check above before the campaign. If it fails, add `max_num_seqs: 4` to that
entry, commit, and start from a fresh output directory.

## Publication checks and interpretation

The campaign verifies the result rectangle before plotting and seals artifacts
in `campaign_manifest.json`. For the full default matrix, repeat the final check
with:

```bash
python scripts/validate_campaign.py verify \
  --models configs/models.yaml --experiment configs/experiment.yaml \
  --out-dir "$OUT_DIR" --concurrencies 1 2 4 8 16 32 64 128 --repeats 3 \
  --manifest "$OUT_DIR/campaign_manifest.json"
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
- RDMA causality needs an otherwise matched TCP ablation with the same image,
  workload, revisions, and server policies. Historical campaigns with different
  software provide context but do not isolate transport.
