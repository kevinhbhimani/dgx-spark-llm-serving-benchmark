#!/usr/bin/env bash
<<<<<<< HEAD
# Install a generated offline bundle on a compatible target system.
=======
# Install the offline bundle on a DGX Spark with no internet access.
# Run from the bundle directory on each node.
>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BUNDLE_DIR"

command -v docker >/dev/null 2>&1 || { echo "[offline] docker is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "[offline] python3 is required" >&2; exit 1; }
<<<<<<< HEAD
command -v sha256sum >/dev/null 2>&1 || { echo "[offline] sha256sum is required" >&2; exit 1; }
=======
>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2

echo "[offline] verifying checksums"
sha256sum -c SHA256SUMS

<<<<<<< HEAD
bundle_format=""
derived_image=""
target_platform=""
target_arch=""
target_python=""
requirements_file=""
requirements_sha256=""
image_archive=""
source_archive=""
model_cache_archive=""
ray_version=""
while IFS='=' read -r field value; do
  case "$field" in
    bundle_format) bundle_format="$value" ;;
    derived_image) derived_image="$value" ;;
    target_platform) target_platform="$value" ;;
    target_arch) target_arch="$value" ;;
    target_python) target_python="$value" ;;
    requirements_file) requirements_file="$value" ;;
    requirements_sha256) requirements_sha256="$value" ;;
    image_archive) image_archive="$value" ;;
    source_archive) source_archive="$value" ;;
    model_cache_archive) model_cache_archive="$value" ;;
    ray_version) ray_version="$value" ;;
    base_image|base_image_id|target_manylinux|source_revision|source_worktree_state|image_id|built_utc) ;;
    *) echo "[offline] unknown BUNDLE_INFO field: $field" >&2; exit 1 ;;
  esac
done < BUNDLE_INFO

if [[ "$bundle_format" != "2" ]]; then
  echo "[offline] unsupported bundle format: $bundle_format" >&2
  exit 1
fi
case "$target_platform" in
  linux/arm64)
    EXPECTED_ARCH="aarch64"
    EXPECTED_IMAGE_ARCH="arm64"
    ;;
  linux/amd64)
    EXPECTED_ARCH="x86_64"
    EXPECTED_IMAGE_ARCH="amd64"
    ;;
  *) echo "[offline] unsupported bundle platform: $target_platform" >&2; exit 1 ;;
esac
if [[ "$target_arch" != "$EXPECTED_ARCH" ]]; then
  echo "[offline] target_arch does not match target_platform" >&2
  exit 1
fi
if [[ ! "$derived_image" =~ ^[A-Za-z0-9][A-Za-z0-9._:/@-]*$ ]]; then
  echo "[offline] invalid image name in BUNDLE_INFO" >&2
  exit 1
fi
if [[ ! "$target_python" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "[offline] invalid Python version in BUNDLE_INFO" >&2
  exit 1
fi
if [[ ! "$requirements_file" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "[offline] invalid requirements file name in BUNDLE_INFO" >&2
  exit 1
fi
if [[ ! "$requirements_sha256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "[offline] invalid requirements hash in BUNDLE_INFO" >&2
  exit 1
fi
if [[ "$image_archive" != "vllm-ray-image.tar.zst" && "$image_archive" != "vllm-ray-image.tar.gz" ]]; then
  echo "[offline] invalid image archive name in BUNDLE_INFO" >&2
  exit 1
fi
if [[ "$source_archive" != "dgx-spark-benchmark-src.tar.gz" ]]; then
  echo "[offline] invalid source archive name in BUNDLE_INFO" >&2
  exit 1
fi
if [[ "$model_cache_archive" != "none" && "$model_cache_archive" != "model-cache.tar" ]]; then
  echo "[offline] invalid model cache archive name in BUNDLE_INFO" >&2
  exit 1
fi

case "$(uname -m)" in
  aarch64|arm64) HOST_ARCH="aarch64" ;;
  x86_64|amd64) HOST_ARCH="x86_64" ;;
  *) echo "[offline] unsupported host architecture: $(uname -m)" >&2; exit 1 ;;
esac
if [[ "$HOST_ARCH" != "$target_arch" ]]; then
  echo "[offline] bundle targets $target_arch, host is $HOST_ARCH" >&2
  exit 1
fi
HOST_PYTHON="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$HOST_PYTHON" != "$target_python" ]]; then
  echo "[offline] bundle targets Python $target_python, host Python is $HOST_PYTHON" >&2
  exit 1
fi
=======
# shellcheck disable=SC1091
source ./BUNDLE_INFO
>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2

echo "[offline] loading $derived_image"
case "$image_archive" in
  *.zst)
    command -v zstd >/dev/null 2>&1 || { echo "[offline] zstd required for $image_archive" >&2; exit 1; }
    zstd -dc "$image_archive" | docker load
    ;;
  *.gz)
    gzip -dc "$image_archive" | docker load
    ;;
  *)
    docker load -i "$image_archive"
    ;;
esac
<<<<<<< HEAD
IMAGE_ARCH="$(docker image inspect --format '{{.Architecture}}' "$derived_image")"
if [[ "$IMAGE_ARCH" != "$EXPECTED_IMAGE_ARCH" ]]; then
  echo "[offline] loaded image architecture is $IMAGE_ARCH, expected $EXPECTED_IMAGE_ARCH" >&2
  exit 1
fi
=======
>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2

SRC_DIR="${SRC_DIR:-$HOME/dgx-spark-llm-serving-benchmark}"
mkdir -p "$SRC_DIR"
echo "[offline] extracting source to $SRC_DIR"
tar -xzf "$source_archive" -C "$SRC_DIR"
<<<<<<< HEAD
if [[ ! -f "$SRC_DIR/$requirements_file" ]]; then
  echo "[offline] $requirements_file is missing from the source archive" >&2
  exit 1
fi
SOURCE_REQUIREMENTS_SHA256="$(sha256sum "$SRC_DIR/$requirements_file" | awk '{print $1}')"
if [[ "$SOURCE_REQUIREMENTS_SHA256" != "$requirements_sha256" ]]; then
  echo "[offline] $requirements_file hash differs from BUNDLE_INFO" >&2
  exit 1
fi
[[ -d "$BUNDLE_DIR/wheels" ]] || { echo "[offline] wheels/ missing from the bundle" >&2; exit 1; }

if [[ "$model_cache_archive" != "none" ]]; then
  HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
  mkdir -p "$HF_CACHE"
  echo "[offline] extracting model cache to $HF_CACHE"
  tar -xf "$model_cache_archive" -C "$HF_CACHE"
else
  echo "[offline] this bundle has no model weights"
fi

echo "[offline] creating client virtual environment"
python3 -m venv "$SRC_DIR/.venv"
PYTHONNOUSERSITE=1 PIP_NO_INDEX=1 "$SRC_DIR/.venv/bin/pip" install \
  --no-index --find-links "$BUNDLE_DIR/wheels" \
  -r "$SRC_DIR/$requirements_file"

echo "[offline] validating client environment"
if [[ -f "$SRC_DIR/scripts/check_environment.py" && -f "$SRC_DIR/requirements.lock" ]]; then
  PYTHONNOUSERSITE=1 "$SRC_DIR/.venv/bin/python" "$SRC_DIR/scripts/check_environment.py"
else
  echo "[offline] project environment checker is unavailable; pip installation completed"
fi
=======

[[ -d "$BUNDLE_DIR/wheels" ]] || { echo "[offline] wheels/ missing from the bundle" >&2; exit 1; }

echo "[offline] creating client virtual environment"
python3 -m venv "$SRC_DIR/.venv"
PYTHONNOUSERSITE=1 "$SRC_DIR/.venv/bin/pip" install \
  --no-index --find-links "$BUNDLE_DIR/wheels" \
  -r "$SRC_DIR/requirements.lock"

echo "[offline] validating client environment"
PYTHONNOUSERSITE=1 "$SRC_DIR/.venv/bin/python" "$SRC_DIR/scripts/check_environment.py"
>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2

cat <<EOF

[offline] image loaded: $derived_image
[offline] source extracted: $SRC_DIR
[offline] client environment validated

Export on both nodes before running any script:
  export VLLM_IMAGE="$derived_image"
  export RAY_VERSION="$ray_version"
  export RAY_SETUP=:
  cd "$SRC_DIR"
  source environments/rdma-dual-cable.env
EOF
