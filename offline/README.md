# Offline bundle

`build_bundle.sh` packages the current checkout for installation on a system
without outbound network access. The bundle contains a platform-specific vLLM
container image, the Python wheelhouse, the source tree, and the Hugging Face
hub cache when that cache exists on the build host.

## Bundle contents

| File | Contents |
| --- | --- |
| `vllm-ray-image.tar.zst` or `.tar.gz` | vLLM base image with the locked Ray runtime installed |
| `wheels/` | Wheels resolved from `requirements.lock` or `requirements.txt` for the target platform |
| `dgx-spark-benchmark-src.tar.gz` | Current repository working tree, excluding Git metadata, result files, local virtual environments, and common local credential paths |
| `model-cache.tar` | Hugging Face `hub/` cache from the build host, when available |
| `install_offline.sh` | Target installer |
| `SHA256SUMS` | SHA-256 checksums for bundle files |
| `BUNDLE_INFO` | Image, platform, Python, requirements, and source revision metadata |

Model weights are included when the configured Hugging Face cache root contains
`hub/`. The default source is `HF_CACHE/hub`. Set `MODEL_CACHE_DIR` when the
cache uses another location. The bundle contains the `hub/` directory and does
not contain the Hugging Face token file. An absent cache produces a bundle
without model weights. The bundle includes files present in the cache and does
not check that a model snapshot is complete.

The bundle contains the application container image and client Python wheels.
The target system still requires Docker, NVIDIA Container Toolkit, a compatible
NVIDIA driver, and the host RDMA packages used by the benchmark.

## Build

Run from the repository root on a machine with network access, Docker, Docker
Buildx, Python, and sufficient storage. The script also accepts an absolute
path from another working directory:

```bash
offline/build_bundle.sh
```

The default output is a new timestamped directory inside `offline/`. Pass an
output path to choose another location. Existing output paths are rejected so
previous bundles are preserved.

The builder reads `VLLM_IMAGE`, `RAY_VERSION`, and `HF_CACHE` from
`scripts/connectx_env.sh` when that file exists. It copies
`scripts/ray-runtime-constraints.txt` when present and verifies that its pinned
Ray version matches `RAY_VERSION`. The builder uses `requirements.lock` when
present and falls back to `requirements.txt`. Set `VLLM_IMAGE` and
`RAY_VERSION` in the environment when the checkout does not define them.

The default target is `linux/arm64`. The default Python version is the
build-host Python major and minor version. Set `TARGET_PYTHON` when the target
system uses another version. Set `TARGET_PLATFORM=linux/amd64` to build for an
x86-64 target. Set `TARGET_MANYLINUX` to the target system's minimum glibc
compatibility tag when it differs from `manylinux_2_28`. `TARGET_ABIS` and
`TARGET_PYTHON_IMPLEMENTATION` override the wheel ABI defaults.

The builder uses the selected Buildx builder and exports a single-platform
Docker image archive. A local Buildx builder can use QEMU emulation for an
arm64 target on x86-64. Docker Desktop includes emulation support. On Docker
Engine, register QEMU when the selected Buildx builder does not provide an
arm64 emulator:

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

The bundle restores cached Hugging Face model snapshots when the build host had
them. If the bundle has no `model-cache.tar`, stage the pinned snapshots into
`HF_CACHE` before serving. Model downloads require network access on the staging
machine.

## Files

- `Dockerfile.ray`: derived vLLM image definition.
- `build_bundle.sh`: bundle builder.
- `install_offline.sh`: offline target installer.
