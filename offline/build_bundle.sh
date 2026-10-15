#!/usr/bin/env bash
# Assemble an offline bundle for the DGX Spark two-node benchmark.
#
# Run on a machine with internet access and Docker. The bundle carries the
# derived vLLM+Ray image, the pinned client wheelhouse, and the repository
# source. It excludes model weights; stage those separately (see README.md).
#
# Usage:
#   offline/build_bundle.sh [OUTPUT_DIR]
#
# Environment overrides:
#   VLLM_IMAGE       base image (default: pinned digest in scripts/connectx_env.sh)
#   RAY_VERSION      Ray version baked into the derived image (default: 2.53.0)
#   TARGET_PLATFORM  image platform (default: linux/arm64)
#   TARGET_PYTHON    client Python version (default: 3.12)
#   DERIVED_IMAGE    derived image tag (default: dgx-spark-vllm-ray:$RAY_VERSION)
#   ZSTD_LEVEL       zstd compression level (default: 3)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=../scripts/connectx_env.sh
source "$REPO_ROOT/scripts/connectx_env.sh"

TARGET_PLATFORM="${TARGET_PLATFORM:-linux/arm64}"
TARGET_PYTHON="${TARGET_PYTHON:-3.12}"
DERIVED_IMAGE="${DERIVED_IMAGE:-dgx-spark-vllm-ray:${RAY_VERSION}}"
ZSTD_LEVEL="${ZSTD_LEVEL:-3}"
OUTPUT_DIR="${1:-$REPO_ROOT/offline-bundle}"

case "$TARGET_PLATFORM" in
  linux/arm64)
    TARGET_ARCH=aarch64
    TARGET_PLATFORM_TAGS=(manylinux2014_aarch64 manylinux_2_17_aarch64
      manylinux_2_28_aarch64 manylinux_2_34_aarch64 linux_aarch64)
    ;;
  linux/amd64)
    TARGET_ARCH=x86_64
    TARGET_PLATFORM_TAGS=(manylinux2014_x86_64 manylinux_2_17_x86_64
      manylinux_2_28_x86_64 manylinux_2_34_x86_64 linux_x86_64)
    ;;
  *)
    echo "[offline] unsupported TARGET_PLATFORM: $TARGET_PLATFORM" >&2
    exit 1
    ;;
esac

command -v docker >/dev/null 2>&1 || { echo "[offline] docker is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "[offline] python3 is required" >&2; exit 1; }

mkdir -p "$OUTPUT_DIR"

echo "[offline] pulling $VLLM_IMAGE for $TARGET_PLATFORM"
docker pull --platform "$TARGET_PLATFORM" "$VLLM_IMAGE"

echo "[offline] building $DERIVED_IMAGE"
# --load imports the result into the local image store so `docker save` can read
# it. It is the default for the docker driver and required for a
# docker-container buildx builder.
docker build --platform "$TARGET_PLATFORM" --load \
  --build-arg "BASE_IMAGE=$VLLM_IMAGE" \
  --build-arg "RAY_VERSION=$RAY_VERSION" \
  -t "$DERIVED_IMAGE" \
  -f "$HERE/Dockerfile.ray" "$HERE"

echo "[offline] verifying Ray in the derived image"
if docker run --rm --platform "$TARGET_PLATFORM" --entrypoint python3 "$DERIVED_IMAGE" \
     -c 'import ray; print("ray", ray.__version__)'; then
  :
else
  echo "[offline] could not run the derived image locally; verify on the Spark" >&2
fi

echo "[offline] saving image"
if command -v zstd >/dev/null 2>&1; then
  IMAGE_ARCHIVE="vllm-ray-image.tar.zst"
  docker save "$DERIVED_IMAGE" | zstd -T0 "-$ZSTD_LEVEL" -o "$OUTPUT_DIR/$IMAGE_ARCHIVE"
else
  IMAGE_ARCHIVE="vllm-ray-image.tar.gz"
  docker save "$DERIVED_IMAGE" | gzip -1 > "$OUTPUT_DIR/$IMAGE_ARCHIVE"
fi

echo "[offline] downloading client wheels for $TARGET_ARCH python $TARGET_PYTHON"
WHEEL_DIR="$OUTPUT_DIR/wheels"
mkdir -p "$WHEEL_DIR"
host_arch="$(uname -m)"
host_python="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
wheel_platform_args=()
if [[ "$host_arch" != "$TARGET_ARCH" || "$host_python" != "$TARGET_PYTHON" ]]; then
  # pip download fetches wheels for the running interpreter unless the target is
  # named explicitly. --platform requires --only-binary=:all:.
  for tag in "${TARGET_PLATFORM_TAGS[@]}"; do
    wheel_platform_args+=(--platform "$tag")
  done
  wheel_platform_args+=(--python-version "$TARGET_PYTHON" --implementation cp)
fi
PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip download \
  -r "$REPO_ROOT/requirements.lock" \
  --dest "$WHEEL_DIR" \
  --only-binary=:all: \
  "${wheel_platform_args[@]}"

echo "[offline] verifying the wheelhouse covers requirements.lock"
python3 - "$REPO_ROOT/requirements.lock" "$WHEEL_DIR" <<'PY'
import re
import sys
from pathlib import Path

lock_path = Path(sys.argv[1])
wheel_dir = Path(sys.argv[2])
pin_re = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)$")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# Wheel filenames use the normalized distribution name with '-' replaced by '_'.
available = {normalize(path.name.split("-")[0]) for path in wheel_dir.glob("*.whl")}
missing = []
for line in lock_path.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    match = pin_re.match(line)
    if match is None:
        raise SystemExit(f"unexpected lock line: {line}")
    if normalize(match.group(1)) not in available:
        missing.append(line)
if missing:
    raise SystemExit("no wheel for: " + ", ".join(missing))
print(f"wheelhouse covers {len(available)} distributions")
PY

echo "[offline] archiving source"
SOURCE_ARCHIVE="dgx-spark-benchmark-src.tar.gz"
tar -C "$REPO_ROOT" \
  --exclude='.venv' --exclude='.git' --exclude='offline-bundle' \
  --exclude='results*' --exclude='figs' \
  --exclude='__pycache__' --exclude='*.pyc' \
  -czf "$OUTPUT_DIR/$SOURCE_ARCHIVE" .

cp "$HERE/install_offline.sh" "$OUTPUT_DIR/install_offline.sh"
chmod +x "$OUTPUT_DIR/install_offline.sh"

cat > "$OUTPUT_DIR/BUNDLE_INFO" <<EOF
derived_image=$DERIVED_IMAGE
base_image=$VLLM_IMAGE
ray_version=$RAY_VERSION
target_platform=$TARGET_PLATFORM
target_arch=$TARGET_ARCH
target_python=$TARGET_PYTHON
image_archive=$IMAGE_ARCHIVE
source_archive=$SOURCE_ARCHIVE
image_id=$(docker image inspect --format '{{.Id}}' "$DERIVED_IMAGE")
built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

( cd "$OUTPUT_DIR" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS )

echo "[offline] bundle written to $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
