# Offline DGX Spark bundle

This directory builds a bundle for DGX Spark nodes without outbound network
access. The bundle contains the pinned vLLM image with the Ray runtime, the
client wheelhouse for the target platform, and the repository source. Model
weights are excluded unless `--include-models` is set.

## Bundle contents

| File | Contents |
| --- | --- |
| `vllm-ray-image.tar.zst` or `vllm-ray-image.tar.gz` | Derived vLLM image with Ray installed |
| `wheels/` | Binary wheels for `requirements.lock` and the target platform |
| `dgx-spark-benchmark-src.tar.gz` | Repository source without Git history, generated results directories, or local secrets |
| `model-cache.tar` | Optional Hugging Face `hub/` cache |
| `install_offline.sh` | Installer for each target node |
| `BUNDLE_INFO` | Image, Ray, platform, Python, and requirements metadata |
| `SHA256SUMS` | Checksums for bundle files except `SHA256SUMS` itself |
| `SOURCE_RELEASE.json` | Source file hashes, modes, symlink targets, source identity, and dependency artifact identities |
| `source_release.py` | Source update builder, validator, installer, and rollback command |
| `commit.md` | Build source commit, branch, timestamp, and working-tree status |

The builder places `install_offline.sh` at the bundle root so it can be run
after transfer.

The derived image installs the Ray version pinned in
`scripts/ray-runtime-constraints.txt` and sets `PIP_NO_INDEX=1`. The
`scripts/start_ray.sh` pip command can use the installed packages and fails if a
package is missing because the image has no package-index access.

## Build

Run the builder on a machine with network access, Docker Buildx, Python 3 with
pip, `tar`, and `sha256sum`. It pulls the base image and downloads the locked
client wheels. `zstd` is optional; the builder uses gzip when zstd is absent.

Native ARM builds require no architecture flag. From the repository root:

```bash
offline/build_bundle.sh [--include-models] [OUTPUT_DIR]
```

The default output directory is `offline/offline-bundle/`. The builder refuses
to overwrite an existing output path. The target defaults to `linux/arm64`
and Python 3.12. Set `TARGET_PLATFORM=linux/amd64` for an x86_64 target.

Git-backed builds require clean committed files within the packaged source
scope. Results, secrets, virtual environments, and bundle outputs are excluded.
The builder snapshots source before pulling the serving image. Image build
inputs and the client lock are read from that snapshot. Git-free source trees
are identified by their file manifest and record an unknown source commit.

For an x86_64 Docker host targeting DGX Spark, pass `--emulated`:

```bash
offline/build_bundle.sh --emulated [--include-models] [OUTPUT_DIR]
```

`--emulated` requires an x86_64 Docker host and `TARGET_PLATFORM=linux/arm64`.
It also requires a Docker client with `docker image inspect --platform` and
daemon API 1.49 or later. Emulated builds explicitly inspect the target
platform's image ID and architecture. Unqualified inspection can return empty
metadata when only the foreign platform is stored. See
[Docker image inspection](https://docs.docker.com/reference/cli/docker/image/inspect/)
and the [containerd inspection fix](https://github.com/moby/moby/pull/54040).
The Docker daemon's architecture determines the build mode, including when a
Docker context points to another machine. An architecture mismatch without
`--emulated` stops before the base image is pulled.

Both modes create or reuse a `docker-container` builder named
`dgx-spark-offline-<endpoint-hash>`. The builder retains its cache across builds
and leaves the selected Buildx builder unchanged. The
[Docker archive exporter](https://docs.docker.com/build/exporters/oci-docker/)
requires a compatible builder driver.

The script checks the builder's advertised target platforms before pulling
vLLM. Official BuildKit images include
[QEMU user-mode emulators](https://docs.docker.com/build/building/multi-platform/).
The script does not install host-wide `binfmt` handlers. A builder without
ARM64 support fails with an error. Emulated installation can take longer than
native installation, especially when a dependency builds from source.

The Dockerfile imports Ray during the image build. Import failure stops both
build modes. Native builds also verify the loaded image with `docker run`.
Emulated builds rely on the Dockerfile check because BuildKit's emulator does
not enable host `docker run`. Both modes check the base and saved image
architectures and download client wheels for the target Python and platform.

### Include model weights

Model weights are excluded by default. To prepare a complete cache on a
networked cluster node, set `HF_CACHE` and download the pinned snapshots:

```bash
HF_CACHE=/media/external-drive/huggingface scripts/predownload.sh all
HF_CACHE=/media/external-drive/huggingface CHECK_ONLY=1 scripts/predownload.sh all
```

The second command checks the snapshots against `configs/models.yaml`. Then
include that cache in the bundle:

```bash
MODEL_CACHE_DIR=/media/external-drive/huggingface \
  offline/build_bundle.sh --include-models /media/external-drive/offline-bundle
```

The cache root must contain a non-empty `hub/` directory. The builder excludes
`hub/token` and `hub/.locks`; it does not validate snapshot completeness.
On an x86_64 builder, add `--emulated` to the bundle command. This option does
not change `scripts/predownload.sh`, which uses cluster topology and the
serving image. An existing complete model cache can be archived on either
architecture.
To move the cache separately, omit `--include-models` and copy the cache to
each target node.

The builder supports `VLLM_IMAGE`, `RAY_VERSION`, `TARGET_PLATFORM`,
`TARGET_PYTHON`, `DERIVED_IMAGE`, `ZSTD_LEVEL`, and `MODEL_CACHE_DIR`
environment overrides. `RAY_VERSION` must match the version in
`scripts/ray-runtime-constraints.txt`.

## Transfer and install

Copy the complete bundle directory to each target node:

```bash
BUNDLE_DIR=/absolute/path/to/offline-bundle
rsync -avP --partial "$BUNDLE_DIR/" /media/external-drive/offline-bundle/
```

Each target node requires Docker, Python 3.12, `tar`, `sha256sum`, and the
compression tool named by the image archive (`zstd` or `gzip`). Run the
installer from the copied bundle directory:

```bash
./install_offline.sh
```

The installer checks checksums, host architecture, and Python major and minor
version. It loads the image, extracts the source to
`$HOME/dgx-spark-llm-serving-benchmark`, creates `.venv`, installs the
wheelhouse without an index, and runs `scripts/check_environment.py`. Set
`SRC_DIR` to choose another source directory. When installing model weights,
the installer extracts them to `$HOME/.cache/huggingface` unless `HF_CACHE` is
set. Set `HF_CACHE` to that path in each node's `.env` before running benchmark
scripts so the source uses the extracted cache.

New bundles register `.offline-release.json` in the installed source directory.
The receipt records the source identity, runtime artifact identities, and
installation time. `.offline-client-environment.json` records the validated
client environment. Local source additions, edits, moves, and deletions do not
block registration or verification. Verification lists local differences from
the recorded release.

The source archive omits `.env` files and Git history. Configure each node's
repo-local `.env` with `CLUSTER_CONFIG` when the default topology file does not
match the local systems. Use the same canonical topology content on both nodes
and configure the declared addresses on the hosts before starting Ray. Follow
[cluster setup](../docs/cluster-setup.md) and
[RDMA readiness](../docs/rdma-readiness.md) for hardware configuration.

## Source-only updates

Source updates reuse the original image, wheelhouse, virtual environment, and
model cache. Each update contains a complete source archive and identifies its
required baseline. Changes to `requirements.txt`, `requirements.lock`,
`offline/Dockerfile.ray`, `scripts/runtime_defaults.sh`, or
`scripts/ray-runtime-constraints.txt` require a runtime rebuild.

On the build host, create an update against the original bundle:

```bash
python3 offline/source_release.py build-patch \
  --base /media/external-drive/offline-bundle \
  /media/external-drive/source-update
```

For a subsequent update, pass the previous source-update directory to `--base`.
The output path must not exist. The command performs no image build, package
download, model download, or hardware operation. Source snapshots include
uncommitted changes and extra files, including backup cluster configurations.
The generated source metadata records the Git commit and working-tree state.
Git-free build trees record an unknown commit.

Copy the update directory to each node. Stop campaigns and serving processes
before applying updates. Patching an active campaign changes its source
fingerprint and prevents a compatible resume.

```bash
PATCH_DIR=/media/external-drive/source-update
SRC_DIR="$HOME/dgx-spark-llm-serving-benchmark"
(cd "$PATCH_DIR" && sha256sum -c SHA256SUMS)
```

For an installation made with an older bundle, register its source against
that original bundle once. This command does not rebuild artifacts:

```bash
python3 "$PATCH_DIR/source_release.py" register \
  --bundle /media/external-drive/offline-bundle --source "$SRC_DIR"
```

Validate the update, apply it, and verify the installed files:

```bash
python3 "$PATCH_DIR/source_release.py" check --source "$SRC_DIR"
python3 "$PATCH_DIR/source_release.py" apply --source "$SRC_DIR"
python3 "$PATCH_DIR/source_release.py" verify --source "$SRC_DIR"
```

`check` validates the release receipt, archive contents, dependency inputs,
file modes, symlinks, and the staged source's client environment. It lists local
differences and the update's added, changed, and deleted paths without changing
the installation. Local source differences do not block updates. Updates replace
paths changed by the release and retain other local files, edits, moves, and
deletions. A changed release path can replace a local file or directory.
`.env*`, results, caches, virtual environments, Git metadata, and `.offline-*`
installation state remain excluded from source snapshots. Model availability
and serving behavior remain subject to the benchmark preflights. A changed model
matrix may require separately transferring its pinned snapshots.
Archive traversal and symlinks that escape the source tree are rejected.

`apply` validates before changing files, saves the actual local source tree and
receipt under `.offline-releases/`, and records the new receipt after
verification. Backups include extra files, local edits, and the absence of moved
or deleted files. A failed application restores that snapshot. Replacement is
atomic per file; stop benchmark processes before applying. A process interruption leaves a
transaction record that blocks further updates until rollback succeeds.

```bash
python3 "$PATCH_DIR/source_release.py" rollback --source "$SRC_DIR"
```

`rollback` restores the previous snapshot's release changes and receipt or
recovers an interrupted update. Other local files remain in place. Snapshot
restoration does not run the client environment check. Restored local files can
differ from the recorded release.
Retained source history contains no wheels, container images, or model weights.
Recovery refuses to overwrite source edits that match neither recorded release.

## Run Ray and all-reduce

On both nodes, set the image and Ray version from the copied bundle metadata.
Set `BUNDLE_DIR` to the bundle directory on each node:

```bash
BUNDLE_DIR=/media/external-drive/offline-bundle
export VLLM_IMAGE="$(awk -F= '$1 == "derived_image" {print $2}' "$BUNDLE_DIR/BUNDLE_INFO")"
export RAY_VERSION="$(awk -F= '$1 == "ray_version" {print $2}' "$BUNDLE_DIR/BUNDLE_INFO")"
cd "$HOME/dgx-spark-llm-serving-benchmark"
source scripts/connectx_env.sh
```

Start the Ray head on one node and the worker on the other:

```bash
scripts/start_ray.sh head
scripts/start_ray.sh worker
```

After both nodes report `Active`, run this on the head:

```bash
scripts/ray_allreduce_bench.sh
```

The all-reduce output is written to
`results/allreduce_ray_<fingerprint>_<timestamp>.csv`.

## Run model benchmarks from the local cache

With the image, client dependencies, and pinned model snapshots installed
locally, the full offline campaign can run from a Git-free bundle:

```bash
SKIP_GIT_CHECK=1 MODELS_ONDISK=1 HF_CACHE="/media/$USER/external-drive/huggingface" \
  scripts/run_job.sh
```

Disk mode validates the pinned head snapshots locally, mirrors dual-node
groups over the cluster network, and retains model snapshots throughout
canaries and measurements. Set the cache path on each node before starting
Ray. `MODELS_ONDISK=0` or an unset variable selects JIT acquisition. The
campaign schedule and measurement methodology are shared by both modes.

`SKIP_GIT_CHECK` accepts `0` or `1` and defaults to `0`. Set it in `.env` or
export it before running `run_preflight_then_benchmark.sh`, `preflight.sh`,
`run_job.sh`, `run_models.sh`, `run_all.sh`, or the Python benchmark CLI. Child
processes inherit the setting. Direct Python commands require an exported
variable because they do not load `.env` themselves.

```bash
export SKIP_GIT_CHECK=1
MODELS_ONDISK=1 scripts/run_preflight_then_benchmark.sh
```

The exception bypasses repository commit and cleanliness requirements. Source,
configuration, environment, artifact, and result checks remain active.
Git-free campaigns use the source fingerprint for resume and canary identity.
Final verification records `git_checks_skipped=true`; missing Git revisions
remain blank. Source changes during the campaign fail its final source check.

Model-serving runs require complete pinned snapshots in the Hugging Face cache.
When the bundle includes `model-cache.tar`, the installer extracts it into
`HF_CACHE`. Otherwise copy a complete `hub/` cache to each node. For a custom
cache location, set the same `HF_CACHE` in each node's `.env`.

Activate the installed client environment and export the image settings in each
shell:

```bash
cd "$HOME/dgx-spark-llm-serving-benchmark"
source .venv/bin/activate
export PYTHON="$PWD/.venv/bin/python"
BUNDLE_DIR=/media/external-drive/offline-bundle
export VLLM_IMAGE="$(awk -F= '$1 == "derived_image" {print $2}' "$BUNDLE_DIR/BUNDLE_INFO")"
export RAY_VERSION="$(awk -F= '$1 == "ray_version" {print $2}' "$BUNDLE_DIR/BUNDLE_INFO")"
source scripts/connectx_env.sh
```

On each node, validate that its cache contains every pinned snapshot required
by the run. This command is local-only and does not contact Hugging Face:

```bash
CHECK_ONLY=1 scripts/predownload.sh all
```

`launch_server.sh` defaults to `HF_HUB_OFFLINE=1`. Direct
`run_models.sh single` defaults to online mode and acquires snapshots before
launch. Set `RUN_JOB_CACHE_READY=1` to skip that acquisition step and
`HF_HUB_OFFLINE=1` to keep serving local-only. Run a single-node sweep on the
head:

```bash
RUN_JOB_CACHE_READY=1 HF_HUB_OFFLINE=1 \
  OUT_DIR=results/offline-single scripts/run_models.sh single
```

For dual-node runs, validate the required cache on both nodes. `RUN_JOB_CACHE_READY=1`
also skips the runner's worker-cache mirror, so each node must have the complete
snapshot cache before launch. Start Ray on both nodes, then run this command on
the head:

```bash
RUN_JOB_CACHE_READY=1 HF_HUB_OFFLINE=1 \
  OUT_DIR=results/offline-tp2 scripts/run_models.sh tp2
```

Use `pp2` in place of `tp2` for pipeline parallel. A missing or incomplete
snapshot stops at the local cache preflight before server launch. The
`run_job.sh` campaign wrapper requires a clean committed Git checkout unless
`SKIP_GIT_CHECK=1` is set.

## Offline troubleshooting

- `weight_gb` or `kv_cache_gb` is blank because model configuration or Hub
  metadata is unavailable: confirm that the required snapshot is in the local
  cache and that gated repositories have valid credentials on the networked
  build host.
- `client tokenizer preflight failed`: confirm the pinned tokenizer snapshot is
  complete and that the model entry's `trust_remote_code` setting is correct.
  Run `CHECK_ONLY=1 scripts/predownload.sh <config>` to validate the local
  snapshots for one serving configuration.
