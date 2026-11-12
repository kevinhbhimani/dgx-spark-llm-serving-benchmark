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

From the repository root:

```bash
offline/build_bundle.sh [--include-models] [OUTPUT_DIR]
```

The default output directory is `offline/offline-bundle/`. The builder refuses
to overwrite an existing output path. The target defaults to `linux/arm64`
and Python 3.12. Set `TARGET_PLATFORM=linux/amd64` for an x86_64 target.

For an x86_64 build host targeting DGX Spark, register arm64 emulation for the
Docker Buildx image build:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker run --rm --platform linux/arm64 alpine uname -m
```

The second command prints `aarch64` when the host can run arm64 containers.
The builder checks the saved image architecture. Ray import verification runs
when the build host can execute the target image.

### Include model weights

Model weights are excluded by default. To prepare a complete cache on a
networked build machine, set `HF_CACHE` and download the pinned snapshots:

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

The source archive omits `.env` files and Git history. Configure each node's
repo-local `.env` with `CLUSTER_CONFIG` when the default topology file does not
match the local systems. Use the same canonical topology content on both nodes
and configure the declared addresses on the hosts before starting Ray. Follow
[cluster setup](../docs/cluster-setup.md) and
[RDMA readiness](../docs/rdma-readiness.md) for hardware configuration.

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
`run_job.sh` campaign wrapper requires a clean committed Git checkout. The
bundle source archive omits Git history, so use `run_models.sh` for model runs
from an installed bundle.

## Offline troubleshooting

- `weight_gb` or `kv_cache_gb` is blank because model configuration or Hub
  metadata is unavailable: confirm that the required snapshot is in the local
  cache and that gated repositories have valid credentials on the networked
  build host.
- `client tokenizer preflight failed`: confirm the pinned tokenizer snapshot is
  complete and that the model entry's `trust_remote_code` setting is correct.
  Run `CHECK_ONLY=1 scripts/predownload.sh <config>` to validate the local
  snapshots for one serving configuration.
