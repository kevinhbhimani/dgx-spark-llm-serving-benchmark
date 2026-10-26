# Offline bundle

Builds a self-contained bundle for a DGX Spark with no outbound network access.
The bundle carries the container image, the pinned client wheelhouse, and the
repository source. It excludes model weights unless `--include-models` is used.

## Bundle contents

| File | Contents |
| --- | --- |
| `vllm-ray-image.tar.zst` | vLLM image with Ray baked in |
| `wheels/` | `requirements.lock` wheels for the target platform |
| `dgx-spark-benchmark-src.tar.gz` | repository source |
| `model-cache.tar` | Optional Hugging Face cache, included with `--include-models` |
| `install_offline.sh` | installer for the Spark |
| `SHA256SUMS` | checksums for every file above |
| `BUNDLE_INFO` | image tag, base digest, Ray version, target platform |

## Why the image is rebuilt

`scripts/start_ray.sh` installs Ray at container start with
`pip install -q 'ray[default]==2.53.0'`. That step needs a package index. The
derived image bakes Ray in, so the step is a satisfied no-op. The image also
sets `PIP_NO_INDEX=1`, so a pip call that needs an index fails immediately
instead of waiting for a network timeout.

## Build

Run from the repository root on a machine with network access, Docker, Docker
Buildx, Python, and sufficient storage. The script also accepts an absolute
path from another working directory:

```bash
offline/build_bundle.sh
```

Output directory: `offline/offline-bundle/`.

Model weights are excluded by default. To include the Hugging Face cache, pass
`--include-models`. The cache root must contain a non-empty `hub/` directory.
Set `MODEL_CACHE_DIR` to select a cache on an external drive; otherwise the
builder uses `HF_CACHE` from `scripts/connectx_env.sh`.

```bash
MODEL_CACHE_DIR=/media/external-drive/huggingface \
  offline/build_bundle.sh --include-models /media/external-drive/offline-bundle
```

This writes the cache to `model-cache.tar` in the bundle and excludes the
Hugging Face `.locks` directory. The builder archives files already in the
cache and does not check that pinned model snapshots are complete. To carry
model weights separately, leave off `--include-models` and keep the cache
directory on the external drive.

The build machine must be `aarch64` with Python 3.12, or use BuildKit
emulation and cross-platform wheel download. `build_bundle.sh` compares the
build machine to `TARGET_PLATFORM` and `TARGET_PYTHON` and passes `--platform`
and `--python-version` to `pip download` when they differ.

Overrides: `VLLM_IMAGE`, `RAY_VERSION`, `TARGET_PLATFORM`, `TARGET_PYTHON`,
`DERIVED_IMAGE`, `ZSTD_LEVEL`, `MODEL_CACHE_DIR`.

### Build on x86_64

BuildKit executes the arm64 `RUN pip install` step through QEMU. Register the
arm64 handler once:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker run --rm --platform linux/arm64 alpine uname -m
```

The second command prints `aarch64` when the Docker host can run arm64
containers. BuildKit can execute the image build through QEMU. QEMU builds can
run slower than native builds, especially when compiling software.

A configured Docker Build Cloud builder can export the image archive to the
build host. A native ARM64 cloud VM can also run the same bundle script without
emulation. The build host needs registry access for `VLLM_IMAGE` and package
index access for the client wheelhouse.

Docker documents the [multi-platform build methods](https://docs.docker.com/build/building/multi-platform/),
[Buildx image exporters](https://docs.docker.com/build/exporters/), and
[Build Cloud image loading](https://docs.docker.com/build/exporters/). pip
documents target-platform options for [`pip download`](https://pip.pypa.io/en/stable/cli/pip_download/).

## Transfer and install

Copy the generated bundle directory to the offline system:

```bash
BUNDLE_DIR=/absolute/path/to/offline-bundle
rsync -avP --partial "$BUNDLE_DIR/" /media/external-drive/offline-bundle/
```

Run the installer on each target node from the copied bundle directory:

```bash
./install_offline.sh
```

The installer checks checksums, target CPU architecture, and Python major and
minor version. It loads the image, extracts the source, restores the model cache
when present, and installs wheels without an index. It runs
`scripts/check_environment.py` when that script and `requirements.lock` exist.

Set `SRC_DIR` to choose the source installation path. Set `HF_CACHE` to choose
the model cache path when the bundle contains `model-cache.tar`.

## Run

From the bundle directory, export these values on both nodes:

```bash
export VLLM_IMAGE="$(awk -F= '$1 == "derived_image" {print $2}' BUNDLE_INFO)"
export RAY_VERSION="$(awk -F= '$1 == "ray_version" {print $2}' BUNDLE_INFO)"
export RAY_SETUP=:
cd ~/dgx-spark-llm-serving-benchmark
source environments/rdma-dual-cable.env
```

`RAY_SETUP=:` skips the container's Ray installation. The derived image
contains the version from `scripts/ray-runtime-constraints.txt`.

### All-reduce

Head node: `scripts/start_ray.sh head`. Worker node:
`scripts/start_ray.sh worker`. Run this on the head after both nodes report
`Active`:

```bash
bash scripts/ray_allreduce_bench.sh
```

Output: `results/allreduce_ray_<fingerprint>_<timestamp>.csv`.

### Model serving

Model weights are omitted by default. `scripts/predownload.sh` needs network and
does not run on the Spark. Copy the cache from the external drive into the
`HF_CACHE` path on both nodes. If `model-cache.tar` is present, extract it into
the cache root on each node, for example:

```bash
mkdir -p /media/external-drive/huggingface
tar -xf model-cache.tar -C /media/external-drive/huggingface
export HF_CACHE=/media/external-drive/huggingface
```

Follow `rdma-testing/docs/pre-flight-checklist.md` from step 10.

## Files

- `Dockerfile.ray`: derived vLLM image definition.
- `build_bundle.sh`: bundle builder.
- `install_offline.sh`: offline target installer.
