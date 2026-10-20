# Full campaign runbook

This runbook covers the full RDMA campaign on Diya and Hoedus. It links to
[cluster setup](cluster-setup.md) for firmware, cabling, Netplan, and ConnectX
configuration and to [RDMA readiness](rdma-readiness.md) for result
interpretation and publication checks. Run commands from the repository root
unless a step names a different node or directory.

## Roles and campaign inputs

- Diya (`cisc-diya`) is the Ray head and the benchmark client.
- Hoedus (`cisc-hoedus`) is the Ray worker.
- `scripts/connectx_env.sh` defines the node addresses, ConnectX interfaces,
  pinned vLLM image, and Ray version.
- `configs/models.yaml` defines model revisions, serving configurations, and
  the run-wide `gpu_util_default`. The shipped value is `0.90` for every
  model and serving configuration. The campaign passes `0.90` to each
  `vllm serve` launch as `--gpu-memory-utilization`. On a nominal 128 GB node,
  this targets 115.2 GB. The launcher checks that 90% of each node's detected
  `MemTotal` is free before serving starts. vLLM applies this setting per
  instance, so reserve each node and run one serving instance per node at a
  time. Per-model `gpu_util` entries are rejected.
- `configs/experiment.yaml` defines workload lengths, measurement windows,
  required metrics, and output defaults.
- `scripts/ray-runtime-constraints.txt` pins the Ray dependency versions used
  on both nodes. `scripts/start_ray.sh` checks its Ray pin against
  `RAY_VERSION` before starting a container.

## Where to run commands

| Task | Run command on | Nodes involved |
| --- | --- | --- |
| Firmware, Netplan, host entries, and local ConnectX interface checks | Both nodes | Each command checks the local node |
| Software checks, client environment, and `PLAN_ONLY=1` | Diya only | Local checkout and Python environment; no cluster work |
| `scripts/verify_connectx.sh` | Diya only | Checks Diya's route and probes the Hoedus peer on each ConnectX interface |
| `scripts/start_ray.sh head` / `worker` | Diya / Hoedus, respectively | Starts Ray on both nodes |
| Ray status, RDMA microbenchmarks, canary, and full campaign | Diya only | Ray uses Hoedus for distributed benchmarks and dual-node serving |
| `scripts/hardware_smoke.sh` | Diya only | Single-node diagnostic using Diya's GPU |
| `scripts/predownload.sh` | Diya only | Downloads to Diya's local Hugging Face cache |
| `scripts/purge_model_cache.sh` | Diya only | Purges both caches while Ray is active; otherwise purges only Diya |
| `scripts/start_ray.sh stop` | Both nodes | Stops the local Ray container on each node |
| Perftest suite | Diya client and Hoedus host | Both nodes use separate client and server commands |

Keep the image digest, Ray version, constraints file, model matrix, and
experiment configuration identical across the two checkouts. Commit changes
before running the campaign. `scripts/run_job.sh` stops if benchmark sources
or inputs under its protected paths have uncommitted, staged, or untracked
changes.

## Prepare the two nodes

1. Complete [cluster setup](cluster-setup.md) on both nodes. Confirm the four
   ConnectX interfaces, RoCE devices, routes, and host entries.
2. Check out the same committed repository revision on Diya and Hoedus. Confirm
   that Docker can access the pinned `VLLM_IMAGE` on both nodes.
3. Keep at least 200 GB free in each node's Hugging Face cache. The largest
   shipped snapshot is about 163 GB. `predownload.sh` reserves 10 GiB by
   default; set `PREDOWNLOAD_MIN_FREE_GB` to change that reserve.
4. Put `HF_TOKEN` in a permission-restricted, repo-local `.env` on Diya when
   the campaign includes gated repositories. Run `chmod 600 .env` after
   editing the file. The token is used for downloads from Hugging Face. Hoedus
   receives model files from Diya and does not need Hugging Face access for
   those files.
5. Set `HF_CACHE` in each node's `.env` only when the default cache directory
   lacks space. Each node has its own local cache.

The scripts set `HF_HUB_DISABLE_SHARED_BLOBS=1` for new Hugging Face downloads.
Hugging Face stores downloaded files in the selected repository's cache
instead of its cache-wide shared blob store. Diya downloads each pinned model,
then the mirror sends its snapshot to Hoedus over a Ray-managed TCP stream.
This setting does not disable the local cache or the Diya-to-Hoedus transfer.
The pinned image runtime uses `huggingface_hub==1.32.0`, which supports this
setting. See the Hugging Face
[environment-variable reference](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables)
and [cache guide](https://huggingface.co/docs/huggingface_hub/guides/manage-cache).

Existing caches can contain detached revisions, incomplete downloads, and
unreferenced blobs from earlier downloads. The purge wrapper removes this cache
garbage by default. Start Ray on both nodes and preview the removals on Diya:

```bash
DRY_RUN=1 scripts/purge_model_cache.sh
```

After reviewing the dry-run output, remove the reported cache garbage from both
nodes with this command on Diya:

```bash
scripts/purge_model_cache.sh
```

The command can remove detached revisions, incomplete downloads, and
unreferenced shared blobs. Set `PURGE_CACHE_GARBAGE=0` to skip this pruning
while deleting the snapshots selected by `MODEL_LABELS`. The Python command
accepts `--no-prune-cache-garbage` for the same opt-out.

Do not run `scripts/predownload.sh all` before the full campaign. That command
fetches every selected snapshot to Diya's cache. `scripts/run_job.sh` fetches
one model at a time, mirrors it when a dual-node configuration needs it, and
purges it after its final measurement. The purge also prunes cache garbage by
default. Set `PURGE_CACHE_GARBAGE=0` to disable that pruning.

## Set up the benchmark client on Diya

Create the locked Python environment from the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
export PYTHONNOUSERSITE=1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
python scripts/check_environment.py
```

Run local source and configuration checks before using the cluster:

```bash
python -m compileall -q bench plotting scripts
python -m unittest discover -s tests -v
for script in scripts/*.sh; do bash -n "$script" || exit 1; done
OUT_DIR=results-rdma-v4-paper PLAN_ONLY=1 scripts/run_job.sh
```

`PLAN_ONLY=1` performs no hardware operations. It checks the locked client
environment, campaign options, model and experiment YAML, revision pins,
request bounds, and expected result matrix. The plan prints launch count,
measurement-window count, and minimum measurement time.

## Start Ray and check hardware

Run the ConnectX checks from Diya:

```bash
scripts/verify_connectx.sh
```

Start the Ray containers from the same repository revision. Run the first
command on Diya and the second on Hoedus:

```bash
scripts/start_ray.sh head
```

```bash
scripts/start_ray.sh worker
```

Check the cluster from Diya:

```bash
docker exec vllm-spark ray status
```

Ray must report exactly two active nodes. The runtime fingerprint check in
`run_job.sh` compares the image digest, Python and package versions, normalized
package list, environment, GPU model, driver, and clock limits across both
nodes.

Run the single-node hardware smoke test on Diya when the image, client, power
sampler, or plotting path needs an independent check:

```bash
scripts/hardware_smoke.sh
```

The smoke test writes to `results-smoke`, uses its own container and port, and
defaults to `SMOKE_GPU_UTIL=0.10`. Its generated one-model YAML uses that
value as the diagnostic run's global cap. It does not replace the campaign
canary.

## Run the full canary preflight

Use a new output directory for this campaign:

```bash
OUT_DIR=results-rdma-v4-paper CANARY_FIRST=1 STOP_AFTER_CANARY=1 \
  scripts/run_job.sh
```

This command runs the campaign's hardware preflight and canary, then exits
before measurements. The preflight checks the committed source tree, locked
client environment, campaign definition, active Ray cluster, absence of a
stale vLLM server, and matching two-node runtime. It runs a fresh all-reduce
gate on every invocation and records the send/recv microbenchmark while both
GPUs are idle. The all-reduce gate requires verified RDMA transport on both
ranks, at least 16 GB/s at 1 GiB, and no more than 100 microseconds for the
8-byte collective. A TCP fallback or slow link stops the campaign before the
canary and measurements. The send/recv result is recorded as a non-fatal
measurement.

The canary fetches one model at a time, mirrors models used by `tp2` or `pp2`,
launches every model/configuration pair once, checks the streaming response and
tokenizer, and purges each model afterward. A canary failure stops the job
before measurements. The command writes `results-rdma-v4-paper/.canary_passed`
only when the canary passes.

The marker identifies the commit, model matrix, experiment configuration,
client environment, image, Ray version, two-node runtime, and network policy.
`scripts/run_job.sh` reuses the marker only when those inputs match. The
`CANARY_FIRST=1` setting forces another complete canary.

## Start and resume the full suite

The default matrix requires about 265 hours of measurement before model
downloads, launches, and extensions. Run it in `tmux` on Diya so it continues
if the SSH connection closes. A host reboot stops the session and campaign
process.

Start the campaign session on Diya:

```bash
tmux new-session -s rdma-v4
```

Run the campaign inside that session:

```bash
OUT_DIR=results-rdma-v4-paper scripts/run_job.sh
```

The default matrix contains 19 checkpoints, 53 model/configuration curves,
159 server launches, and 1,272 measurement windows. The minimum measurement
time is about 265 sequential hours before extensions, downloads, and model
loads.

`run_job.sh` fetches and measures one model group at a time. It mirrors pinned
weights to Hoedus for `tp2` and `pp2`, then purges each group's weights from both
caches. Single-node launches run inside the Ray head container so the Ray
cluster remains active. The campaign writes logs, per-launch policies,
`index.csv`, plots, statistics, transport audit, and `campaign_manifest.json`
under `OUT_DIR`.

Detach from the session with `Ctrl-b`, then `d`. Reattach from Diya with
`tmux attach -t rdma-v4`. Follow the campaign log from another Diya shell:

```bash
tail -f results-rdma-v4-paper/run_job_*.log
```

If the process stops or the connection closes, start Ray again if needed and
run the same command with the same `OUT_DIR`. The job skips compatible complete
measurement points and resumes missing work. It rejects result rows whose
configuration or serving policy differs from the current campaign.

After completion, `scripts/run_job.sh` verifies the result grid, generates
figures and statistics, and writes the campaign manifest. Check
`results-rdma-v4-paper/campaign_manifest.json` and
`results-rdma-v4-paper/index.csv`. Stop Ray on each node after the campaign:

```bash
scripts/start_ray.sh stop
```

Use a new `OUT_DIR` after changing the model matrix, experiment configuration,
benchmark code, locked dependencies, image, or serving policy.
