# Full campaign runbook

This runbook covers the full RDMA campaign on two configured DGX Spark GB10
systems. It links to
[cluster setup](cluster-setup.md) for firmware, cabling, Netplan, and ConnectX
configuration and to [RDMA readiness](rdma-readiness.md) for result
interpretation and publication checks. Run commands from the repository root
unless a step names a different node or directory.

## Roles and campaign inputs

- The node selected by `head_node` is the Ray head and benchmark client.
- The other configured node is the Ray worker.
- `CLUSTER_CONFIG` defines both nodes, their IPv4 addresses, interface names,
  the Ray port, and the selected head. `scripts/connectx_env.sh` loads the
  pinned vLLM image and Ray version.
- `configs/cluster.yaml` is the default topology file.
  `configs/cluster.example.yaml` uses documentation-only addresses and
  hostnames. Set `CLUSTER_CONFIG` to use a different topology file.
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

## Solo campaign

Set `SINGLE_NODE_ONLY=1` for local readiness and campaign execution on one
host. The solo path does not read `CLUSTER_CONFIG`, check a peer, or query Ray.
The serving container uses the `-solo` suffix. The client connects to
`127.0.0.1`. Local GPU readiness, cache access, canaries, power sampling,
metrics, result checks, and local cache cleanup remain active. ConnectX,
interconnect, Ray, all-reduce, send/receive, and worker-cache stages report
`SKIP`.

```bash
# Validate options and print a local-only campaign plan.
SINGLE_NODE_ONLY=1 PLAN_ONLY=1 scripts/run_job.sh

# Run local preflight followed by the campaign.
SINGLE_NODE_ONLY=1 scripts/run_preflight_then_benchmark.sh

# Set vLLM's per-instance GPU memory target to 70% for both stages.
SINGLE_NODE_ONLY=1 GPU_UTIL=0.70 scripts/run_preflight_then_benchmark.sh

# Run selected labels. Dual-only labels are listed as excluded.
SINGLE_NODE_ONLY=1 CAMPAIGN_LABELS='llama3.1-8b qwen3.6-27b' \
  scripts/run_job.sh
```

Solo mode resolves selected model configurations to `single`. An explicit
`CAMPAIGN_CONFIGS=tp2` or `CAMPAIGN_CONFIGS=pp2` fails before acquisition or
cleanup. `CAMPAIGN_CONFIGS=single` is accepted. Selection uses the same typed
model/config rule in scheduling and final verification. Verification requires
every selected concurrency and repeat row.

`GPU_UTIL` sets vLLM's `--gpu-memory-utilization` fraction and the matching
free-memory preflight target. It does not cap GPU compute utilization. The
default is `0.80`. The serial entrypoint selects one output directory and uses
it for preflight and campaign execution.

Model acquisition uses the local Hugging Face cache and online Hub access by
default. `HF_HUB_OFFLINE=1` requires the selected pinned revisions to be cached.
Cache purge runs locally. `PURGE_WORKER=1` fails in solo mode. The host-scoped
resource lock and local busy-GPU checks remain active.

Solo campaigns save `results/inputs/local_execution.json`, record a local
hostname and one-GPU runtime identity, and write a schema-version-3 manifest.
Resume the same `OUT_DIR` on the same host with matching workload, source,
runtime, serving, and model-selection identity. A solo campaign cannot resume a
dual campaign directory.

Topology entrypoints default `CLUSTER_CONFIG` to
`<repo-root>/configs/cluster.yaml` on both systems. Set an override only when
using a different file. The canonical topology digests must match, and local
paths may differ:

```bash
# Optional override when the default file does not describe this cluster.
# export CLUSTER_CONFIG=/path/to/site/cluster.yaml
```

## Campaign output path

`OUT_DIR` selects a campaign prefix or a complete campaign output path. Set
`GPU_UTIL` to override the default `0.80` target. Use the same value when
resuming so validation expects the target recorded by each serving policy. Use
a new `OUT_DIR` when changing `GPU_UTIL`. If `OUT_DIR` is unset,
`scripts/run_job.sh` resolves the next unused path beginning with
`results/benchmark-<UTC-date>-NNN` and passes that absolute path to its child
processes. A bare name such as `OUT_DIR=rdma-v4-paper` selects the next unused
`results/rdma-v4-paper-benchmark-<UTC-date>-NNN` path. Sequence numbers begin
at `001`. `CAMPAIGN_NAME` adds the same prefix when `OUT_DIR` is unset.
`PLAN_ONLY=1` prints the selected path without creating it. A bare name ending
in a valid campaign date, with an optional sequence number, identifies that
directory under `results/`. A relative path with a directory component or an
absolute path is used as given. Set `OUT_DIR` to an existing complete path
with a matching `results/campaign_run.json` to resume a campaign.
`run_job.sh` rejects a nonempty markerless path and a path whose recorded
campaign identity differs from the current inputs. A new campaign can claim
an existing root that contains only preflight artifacts and empty
`results/` and `logs/` directories.

The campaign identity includes the raw cluster YAML file hash and canonical
topology digest. New campaigns copy `cluster.yaml` and its raw and canonical
digests to `OUT_DIR/results/inputs/`. A topology change requires a new `OUT_DIR`.

The default target is 80%. Set `GPU_UTIL` in the selected head's environment
to override it for planning, validation, memory preflight, and every serving
launch. Export the override in the campaign shell or include it in each
command. The single-node hardware smoke test defaults to the same 0.80 target
and accepts `SMOKE_GPU_UTIL` when run on its own. The master preflight passes
the campaign's resolved `GPU_UTIL` value to the smoke test.

Set `OUT_DIR` to a bare campaign name or a complete output path. For
`run_job.sh`, a bare name such as `rdma-v4-paper` selects
`results/rdma-v4-paper-benchmark-<UTC-date>-001` or the next unused sequence.
Other entrypoints use their command-specific default campaign name. A relative
path with a directory component or an absolute path selects its exact location.
Use a complete path such as
`results/rdma-v4-paper-benchmark-2026-11-12-001` when resuming an existing
campaign. Campaign manifests record the campaign name, date when present in
the directory name, and absolute path. New campaigns put result artifacts
under `OUT_DIR/results/`, figures and reports under `OUT_DIR/figs/`, logs
under `OUT_DIR/logs/`, and preflight artifacts under
`OUT_DIR/preflight/<run-id>/`. Plotting accepts the campaign directory or its
`results/index.csv`; default figures and reports go under `OUT_DIR/figs/`.

## Where to run commands

| Task | Run command on | Nodes involved |
| --- | --- | --- |
| Firmware, Netplan, configured hostnames, and local ConnectX checks | Both nodes | Each command checks the local node |
| Software checks, client environment, and `PLAN_ONLY=1` | The selected head | Local checkout and Python environment; no cluster work |
| `scripts/verify_connectx.sh` | Both nodes | Checks local addresses, routes, and peers for every configured rail |
| `scripts/start_ray.sh head` / `worker` | The selected head / worker | Starts Ray on both nodes |
| Ray status, RDMA microbenchmarks, canary, and full campaign | The selected head | Ray uses the worker for distributed benchmarks and dual-node serving |
| `scripts/hardware_smoke.sh` | The selected head | Single-node diagnostic using the selected head's GPU |
| `scripts/predownload.sh` | The selected head | Acquires selected pinned snapshots before single- or dual-node runs |
| `scripts/purge_model_cache.sh` | The selected head | Purges both caches while Ray is active; otherwise purges only the head |
| `scripts/start_ray.sh stop` | Both nodes | Stops the local Ray container on each node |
| Perftest suite | The selected head client and worker host | Both nodes use separate client and server commands |

Firmware, Netplan, and `/etc/hosts` changes require an administrator. Researchers
using preconfigured nodes can run the client setup, cache checks, unit suite,
and benchmark commands as their own account. The account must have access to
the Docker daemon, provisioned by the sysadmin before repository setup.
`DROP_CACHES=1` is unsupported; remove it from the environment and `.env`.

Keep the image digest, Ray version, constraints file, model matrix, and
experiment configuration identical across the two checkouts. Commit changes
before running the campaign. `scripts/run_job.sh` stops if benchmark sources
or inputs under its protected paths have uncommitted, staged, or untracked
changes.

The benchmark source fingerprint covers Python modules under `bench/`, the
planning and manifest modules in `scripts/campaign/`, the dependency-lock
module in `scripts/contracts/`, the topology loader, and the launch and
microbenchmark scripts. The campaign manifest lists Python modules recursively
under `scripts/` and the saved topology inputs.
New measurements after a source refactor require a fresh `OUT_DIR`. Existing
partial campaigns retain their recorded fingerprint and must be completed or
verified with the source revision that produced their rows.

## Prepare the two nodes

1. Complete [cluster setup](cluster-setup.md) on both nodes. Confirm every
   configured fabric interface, peer route, and requested RDMA mapping.
2. Check out the same committed repository revision on the selected head and
   worker. Confirm
   that Docker can access the pinned `VLLM_IMAGE` on both nodes.
3. Keep at least 200 GB free in each node's Hugging Face cache. The largest
   shipped snapshot is about 163 GB. `predownload.sh` reserves 10 GiB by
   default; set `PREDOWNLOAD_MIN_FREE_GB` to change that reserve.
4. Put `HF_TOKEN` in a permission-restricted, repo-local `.env` on the selected
   head when the campaign includes gated repositories. Run `chmod 600 .env`
   after editing the file. The token is used for downloads from Hugging Face.
   The worker receives model files from the selected head and does not need
   Hugging Face access for those files.
5. Install PyYAML from the locked environment on both hosts before Ray startup.
   The selected head also needs the full locked client environment. Each node
   has its own cache. Cache-using script entry points source
   `scripts/connectx_env.sh`, which loads the repo-local `.env`, defaults
   `CLUSTER_CONFIG` to the repo's `configs/cluster.yaml`, and defaults
   `HF_CACHE` to `$HOME/.cache/huggingface-$UID`. Scripts derive
   `HF_HUB_CACHE=$HF_CACHE/hub` and mount the cache at `/hf-cache` in
   containers. No cache variable needs to be set for the default location. The
   cache directory must be writable by the account running the scripts. If the
   default directory is owned by another account or has less than 200 GB free,
   set `HF_CACHE` in the repo-local `.env` to a user-writable local scratch
   path on that node.

The host and pinned image probe cache create/read/delete access before use. A
failure reports the effective UID:GID and, when available, the cache directory
owner and mode. The scripts do not change ownership. Cache setup and regular
benchmark commands do not require sudo. Launch preflights attempt clean HF
file-page reclamation without elevated access. Host-wide cache flushing is
disabled. Use a fresh campaign directory after migrating from a revision that
flushed host cache; retain source revisions and logs for comparisons.

Downloads and single-node serving use the invoking UID/GID. Ray mirror
extraction uses the worker's invoking UID/GID. Single-node serving uses online
Hub access for pinned revisions by default. Set `HF_HUB_OFFLINE=1` only when
the snapshot is already cached. Dual-node serving remains offline after
head-side acquisition and worker mirroring.

The scripts set `HF_HUB_DISABLE_SHARED_BLOBS=1` for new Hugging Face downloads.
Hugging Face stores downloaded files in the selected repository's cache
instead of its cache-wide shared blob store. The selected head downloads each
pinned model, then the mirror sends its snapshot to the worker over a
Ray-managed TCP stream. This setting does not disable the local cache or the
head-to-worker transfer.
The pinned image runtime uses `huggingface_hub==1.32.0`, which supports this
setting. See the Hugging Face
[environment-variable reference](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables)
and [cache guide](https://huggingface.co/docs/huggingface_hub/guides/manage-cache).

Existing caches can contain detached revisions, incomplete downloads, and
unreferenced blobs from earlier downloads. The purge wrapper removes this cache
garbage by default. Start Ray on both nodes and preview the removals on the
selected head:

```bash
DRY_RUN=1 scripts/purge_model_cache.sh
```

After reviewing the dry-run output, remove the reported cache garbage from both
nodes with this command on the selected head:

```bash
scripts/purge_model_cache.sh
```

The command can remove detached revisions, incomplete downloads, and
unreferenced shared blobs. Set `PURGE_CACHE_GARBAGE=0` to skip this pruning
while deleting the snapshots selected by `MODEL_LABELS`. The Python command
accepts `--no-prune-cache-garbage` for the same opt-out.

Do not run `scripts/predownload.sh all` before the full campaign. That command
fetches every selected snapshot to the selected head's cache.
`scripts/run_job.sh` acquires
one model group immediately before its canary or measurement runs, mirrors it
when a dual-node configuration needs it, and applies the campaign's existing
purge behavior after the group's scheduled runs. The canary always purges each
group; the measurement pass purges groups when `PURGE_WEIGHTS=1`. The purge
also prunes cache garbage by default. Set `PURGE_CACHE_GARBAGE=0` to disable
that pruning.

## Set up the benchmark client on the selected head

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
measurement-window count, and minimum scheduled phase time.

The unit suite uses local fixtures and mocked external commands. It does not
start benchmark containers or require a GPU or sudo. Hardware benchmark
commands require permission to access the Docker daemon.

## Start Ray and check hardware

Run the ConnectX check on both nodes:

```bash
scripts/verify_connectx.sh
```

Start the Ray containers from the same repository revision. Run the first
command on the selected head and the second on the worker:

```bash
scripts/start_ray.sh head
```

```bash
scripts/start_ray.sh worker
```

Check the cluster from the selected head:

```bash
docker exec vllm-spark ray status
```

Ray must report exactly two active nodes. The runtime fingerprint check in
`run_job.sh` compares the image digest, Python and package versions, normalized
package list, environment, GPU model, driver, and clock limits across both
nodes.

Run the single-node hardware smoke test on the selected head when the image,
client, power sampler, or plotting path needs an independent check:

```bash
scripts/hardware_smoke.sh
```

The smoke test writes to `results/smoke-<UTC-date>-001` or the next unused
sequence by default. It uses its own container and port and defaults to
`SMOKE_GPU_UTIL=0.80`. Its generated one-model YAML uses that value as the
diagnostic run's global cap. It does not replace the campaign canary.

## Resource locking and idle checks

Campaign, launcher, cache, and microbenchmark scripts take a host resource lock
at `/tmp/dgx-spark-resources_<hostname>.lock`. The lock serializes these
scripts on that host across output directories and container names. Set
`RESOURCE_LOCK_FILE` or `CAMPAIGN_LOCK_FILE` to the same path on a filesystem
mounted by both nodes and supporting `flock` to use one lock across hosts. The
default `/tmp` lock is local to each host.

Resource checks inspect host processes and running Docker containers. When Ray
is active, checks also inspect every active Ray node. Dual-node launch and
microbenchmark commands require both configured nodes to be available. A
resource check waits up to `RESOURCE_WAIT_TIMEOUT_SECONDS` seconds, default
1800. A lock wait lasts up to `RESOURCE_LOCK_WAIT_SECONDS` seconds, default
3600. Set `RESOURCE_WAIT_INTERVAL_SECONDS` to change the idle-check interval;
the default is 5 seconds.

An experiment failure can continue to the next independent experiment after
the serving processes stop and the endpoint is unavailable. A cleanup failure
or an unconfirmed resource check stops later launches.

## Run the full canary preflight

For one end-to-end readiness check, set the campaign output name and run this
on the selected head after starting Ray on both nodes:

```bash
OUT_DIR=xyz scripts/preflight.sh
```

The bare name resolves to `results/xyz-benchmark-<UTC-date>-001` or the next
unused sequence number. Campaign results
go under `OUT_DIR/results/`; campaign logs go under `OUT_DIR/logs/`. Each
preflight run gets a unique top-level subdirectory:

```text
results/xyz-benchmark-<UTC-date>-001/
  results/                  campaign measurement outputs
  figs/                     campaign figures, tables, and reports
  logs/                     server and job logs
  preflight/<run-id>/       all artifacts from one preflight run
    logs/                    preflight stage logs
      system_snapshot.json   read-only head and worker host/runtime inventory
    hardware-smoke/          smoke results and logs
    campaign/                campaign canary results and logs
```

Each preflight run and campaign invocation records a JSON system snapshot in
its `logs/` directory. The snapshot includes host OS, kernel, package versions,
selected firmware identifiers, GPU and network interface details, and serving
container versions on both nodes. Collection uses the current account without
`sudo`. Worker collection uses noninteractive SSH; unreachable fields are
recorded as unavailable and do not stop the benchmark.

The preflight checks that the campaign results and logs directories can create,
write, read, and remove a file before running the hardware stages. The smoke
uses the campaign's resolved `GPU_UTIL` value, which defaults to `0.80`.

The script runs the plan-only client and campaign checks, ConnectX verification,
the single-node serving smoke, and the campaign preflight with a forced canary.
The campaign preflight includes the live all-reduce RDMA gate and send/recv
check. Each stage prints PASS or FAIL and saves its output under a unique
preflight directory. The script continues through independent
stages, reports a final summary, and exits nonzero if any stage fails.

At exit, it purges every model and tokenizer repo listed in
`configs/models.yaml` from the Hugging Face caches on both nodes, including
cache garbage. If the two-node purge fails, it retries the selected head's
cache and still reports overall failure because worker cleanup could not be
confirmed. The
script does not delete unrelated Hugging Face model repos. The campaign
preflight requires benchmark sources and inputs to be committed; the smoke
stage also requires an available single-node model and GPU.

Use a new output directory for this campaign:

```bash
OUT_DIR=results/rdma-v4-paper-2026-11-12 CANARY_FIRST=1 STOP_AFTER_CANARY=1 \
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

The canary acquires every selected model group on the head using the same
pinned-snapshot step. It mirrors groups that need `tp2` or `pp2` to the worker.
It attempts one launch for each model/configuration pair it can acquire, checks
the streaming response and tokenizer, and purges each model afterward. It logs
pair failures and continues through later groups. By default, the canary exits
with an error after those checks and stops before measurements if any pair
fails. The command writes
`results/rdma-v4-paper-2026-11-12/.canary_passed`
only when the canary passes.

The marker identifies the commit, model matrix, experiment configuration,
client environment, `GPU_UTIL` value, image, Ray version, two-node runtime,
and network policy. `scripts/run_job.sh` reuses the marker only when those
inputs match. The `CANARY_FIRST=1` setting forces another complete canary.

## Start and resume the full suite

The default matrix has a 176.25-hour minimum scheduled phase time before
window extensions, downloads, canaries, and model startup. Each 750-second
phase includes 120 seconds of warmup, 600 seconds of measurement, and 30
seconds of cooldown. Run it in `tmux` on the selected head so it continues if
the SSH connection closes. A host reboot stops the session and campaign
process.

Start the campaign session on the selected head:

```bash
tmux new-session -s rdma-v4
```

Run the campaign inside that session:

```bash
OUT_DIR=results/rdma-v4-paper-2026-11-12 scripts/run_job.sh
```

The default matrix contains 17 checkpoints, 47 model/configuration pairs,
141 server launches, and 846 measurement windows (concurrencies 1 4 8 16 32
64). Each scheduled phase has a 750-second minimum, for 176.25 sequential
hours before extensions, downloads, and model startup.

`run_job.sh` acquires and measures one model group at a time. Every group uses
the same head-side snapshot acquisition; groups that include `tp2` or `pp2`
also mirror to the worker and check both caches before serving. The job purges
each group's weights from both caches. Single-node launches run
inside the Ray head container so the Ray
cluster remains active. The campaign writes per-launch policies, `index.csv`,
plots, statistics, transport audit, and `campaign_manifest.json` under
`OUT_DIR/results/`. Server and job logs are under `OUT_DIR/logs/`.

Detach from the session with `Ctrl-b`, then `d`. Reattach from the selected
head with `tmux attach -t rdma-v4`. Follow the campaign log from another head
shell:

```bash
tail -f results/rdma-v4-paper-2026-11-12/logs/run_job_*.log
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
`results/rdma-v4-paper-2026-11-12/results/campaign_manifest.json` and
`results/rdma-v4-paper-2026-11-12/results/index.csv`. Stop Ray on each node
after the campaign:

```bash
scripts/start_ray.sh stop
```

Use a new `OUT_DIR` after changing the model matrix, experiment configuration,
benchmark code, locked dependencies, image, or serving policy.
