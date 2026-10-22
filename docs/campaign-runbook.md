# Full campaign runbook

This runbook covers the full RDMA campaign on the Diya and Hoedus DGX Spark
GB10 systems. It links to
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
  the run-wide `gpu_util_default`. The shipped value is `0.80` for every
  model and serving configuration. `GPU_UTIL` overrides the shared target for
  planning, validation, and serving. The campaign passes the resolved value to
  each `vllm serve` launch as `--gpu-memory-utilization` and applies the same
  value to its free-memory preflight. On a nominal 128 GB node, the default
  targets 102.4 GB. vLLM applies this setting per
  instance, so reserve each node and run one serving instance per node at a
  time. Per-model `gpu_util` entries are rejected.
- `configs/experiment.yaml` defines workload lengths, measurement windows, and
  required metrics.
- `scripts/ray-runtime-constraints.txt` pins the Ray dependency versions used
  on both nodes. `scripts/start_ray.sh` checks its Ray pin against
  `RAY_VERSION` before starting a container.

## Campaign output path

`OUT_DIR` is the complete campaign output path. Set `GPU_UTIL` to override the
default `0.80` target. Use the same value when resuming so validation expects
the target recorded by each serving policy. Use a new `OUT_DIR` when changing
`GPU_UTIL`. If `OUT_DIR` is unset,
`scripts/run_job.sh` resolves `results/benchmark-<UTC-date>` once and passes
that absolute path to its child processes. `PLAN_ONLY=1` prints the resolved
path without creating it. An existing implicit path resumes only when its
campaign identity matches; otherwise the job stops before hardware work.

The default target is 80%. Set `GPU_UTIL` in Diya's environment to override it
for planning, validation, memory preflight, and every serving launch. Export
the override in the shell that runs the campaign, or include it in each
command. The single-node hardware smoke test uses the separate
`SMOKE_GPU_UTIL` setting.

Set `OUT_DIR` to name a campaign or resume an existing campaign. The path can
be `results/rdma-v4-paper-2026-10-22` for a new campaign or an existing legacy
directory such as `results-rdma-v4-paper`. Do not change it when resuming.
Campaign manifests record the campaign name, date when present in the
directory name, and absolute path. Plotting accepts the campaign directory or
its `index.csv`; default figures and reports go under that campaign's `figs/`.

## Where to run commands

| Task | Run command on | Nodes involved |
| --- | --- | --- |
| Firmware, Netplan, host entries, and local ConnectX interface checks | Both nodes | Each command checks the local node |
| Software checks, client environment, and `PLAN_ONLY=1` | Diya only | Local checkout and Python environment; no cluster work |
| `scripts/verify_connectx.sh` | Diya only | Checks Diya's route and probes the Hoedus peer on each ConnectX interface |
| `scripts/start_ray.sh head` / `worker` | Diya / Hoedus, respectively | Starts Ray on both nodes |
| Ray status, RDMA microbenchmarks, canary, and full campaign | Diya only | Ray uses Hoedus for distributed benchmarks and dual-node serving |
| `scripts/hardware_smoke.sh` | Diya only | Single-node diagnostic using Diya's GPU |
| `scripts/predownload.sh` | Diya only | Acquires pinned snapshots for dual-node serving or offline cache warming |
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
   lacks space. Each node has its own cache. The host cache defaults to
   `$HOME/.cache/huggingface`; scripts derive `HF_HUB_CACHE=$HF_CACHE/hub` and
   mount the cache at `/hf-cache` in containers.

The host user and the pinned image probe create/read/delete access before cache
acquisition. Downloads and single-node serving use the invoking UID/GID. Ray
mirror extraction uses the worker's invoking UID/GID. Single-node serving uses
online Hub access for pinned revisions by default. Set `HF_HUB_OFFLINE=1` only
when the snapshot is already cached. Dual-node serving remains offline after
head-side acquisition and worker mirroring.

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
PLAN_ONLY=1 scripts/run_job.sh
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

The smoke test writes to `results/smoke-<UTC-date>` by default. It uses its own
container and port and defaults to `SMOKE_GPU_UTIL=0.10`. Its generated
one-model YAML uses that value as the diagnostic run's global cap. It does not
replace the campaign canary.

## Run the full canary preflight

Use a new output directory for this campaign:

```bash
OUT_DIR=results/rdma-v4-paper-2026-10-22 CANARY_FIRST=1 STOP_AFTER_CANARY=1 \
  scripts/run_job.sh
```

This command runs the campaign's hardware preflight and canary, then exits
before measurements. The preflight checks the committed source tree, locked
client environment, campaign definition, active Ray cluster, absence of a
stale vLLM server, and matching two-node runtime. It runs a fresh all-reduce
gate on every invocation and records or reuses a valid send/recv result while
both GPUs are idle. The all-reduce gate requires verified RDMA transport on
both ranks, at least 16 GB/s at 1 GiB, and no more than 100 microseconds for
the 8-byte collective. A TCP fallback or slow link stops the campaign before
the canary and measurements. A send/recv failure is logged and does not block
later campaign work.

The canary acquires single-node-only models through vLLM and fetches model
groups that need `tp2` or `pp2` before mirroring them. It attempts one launch
for each model/configuration pair it can fetch, checks the streaming response
and tokenizer, and purges each model afterward. It logs pair failures and
continues through later groups. By default, the canary exits with an error
after those checks and stops before measurements if any pair fails. The
command writes
`results/rdma-v4-paper-2026-10-22/.canary_passed`
only when the canary passes.

The marker identifies the commit, model matrix, experiment configuration,
client environment, `GPU_UTIL` value, image, Ray version, two-node runtime,
and network policy. `scripts/run_job.sh` reuses the marker only when those
inputs match. The `CANARY_FIRST=1` setting forces another complete canary.

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
OUT_DIR=results/rdma-v4-paper-2026-10-22 scripts/run_job.sh
```

The default matrix contains 19 checkpoints, 53 model/configuration curves,
159 server launches, and 1,272 measurement windows. The minimum measurement
time is about 265 sequential hours before extensions, downloads, and model
loads.

`run_job.sh` acquires and measures one model group at a time. Single-node-only
groups use vLLM JIT. Groups that include `tp2` or `pp2` use head-side snapshot
acquisition, worker mirroring, and a two-node cache check before serving. The
job purges each group's weights from both caches. Single-node launches run
inside the Ray head container so the Ray
cluster remains active. The campaign writes logs, per-launch policies,
`index.csv`, plots, statistics, transport audit, and `campaign_manifest.json`
under `OUT_DIR`.

Detach from the session with `Ctrl-b`, then `d`. Reattach from Diya with
`tmux attach -t rdma-v4`. Follow the campaign log from another Diya shell:

```bash
tail -f results/rdma-v4-paper-2026-10-22/run_job_*.log
```

If the process stops or the connection closes, start Ray again if needed and
run the same command with the same `OUT_DIR`. The job skips compatible complete
measurement points and resumes missing work. It rejects result rows whose
configuration or serving policy differs from the current campaign.

Measurement points continue independently when their failure leaves the
serving endpoint available. `run_all.sh` records a failed repeat/concurrency
point and checks `/v1/models`. When `MODEL` is set, it continues only if the
expected model is still served. An unavailable or mismatched endpoint stops
the sweep. `run_job.sh` checks Ray before each model/configuration leg and
continues after an isolated leg failure while shared state remains healthy. It
stops on invalid resume state, resource preflight failure, Ray loss, or cache
purge failure. Failed points remain missing from `index.csv`; the campaign
reports a nonzero status and can be resumed with the same `OUT_DIR`.

After completion, `scripts/run_job.sh` verifies the result grid, generates
figures and statistics, and writes the campaign manifest. Check
`results/rdma-v4-paper-2026-10-22/campaign_manifest.json` and
`results/rdma-v4-paper-2026-10-22/index.csv`. Stop Ray on each node after the campaign:

```bash
scripts/start_ray.sh stop
```

Use a new `OUT_DIR` after changing the model matrix, experiment configuration,
benchmark code, locked dependencies, image, or serving policy.
