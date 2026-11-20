#!/usr/bin/env bash
# Assemble an offline bundle for the DGX Spark benchmark.
#
# Usage:
#   offline/build_bundle.sh [--emulated] [--include-models] [OUTPUT_DIR]
#
# Environment overrides:
#   VLLM_IMAGE       base image from scripts/connectx_env.sh by default
#   RAY_VERSION      version pinned in scripts/ray-runtime-constraints.txt
#   TARGET_PLATFORM  linux/arm64 by default
#   TARGET_PYTHON    3.12 by default
#   DERIVED_IMAGE    dgx-spark-vllm-ray:$RAY_VERSION by default
#   ZSTD_LEVEL       3 by default
#   MODEL_CACHE_DIR  Hugging Face cache root for --include-models
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

INCLUDE_MODELS=0
EMULATED=0
OUTPUT_DIR=""
while (($#)); do
  case "$1" in
    --emulated)
      EMULATED=1
      ;;
    --include-models)
      INCLUDE_MODELS=1
      ;;
    --help|-h)
      cat <<EOF
Usage: $(basename "$0") [--emulated] [--include-models] [OUTPUT_DIR]

Native builds require no architecture flag. Pass --emulated to build an ARM64
bundle on an x86_64 Docker host using BuildKit's bundled QEMU emulator.

Model weights are excluded unless --include-models is set. The Hugging Face
hub cache under MODEL_CACHE_DIR or HF_CACHE is added when that option is used.
EOF
      exit 0
      ;;
    --*)
      echo "[offline] unknown option: $1" >&2
      exit 2
      ;;
    *)
      if [[ -n "$OUTPUT_DIR" ]]; then
        echo "[offline] only one OUTPUT_DIR may be specified" >&2
        exit 2
      fi
      OUTPUT_DIR="$1"
      ;;
  esac
  shift
done

# shellcheck source=../scripts/load_env.sh
source "$REPO_ROOT/scripts/load_env.sh"
# shellcheck source=../scripts/runtime_defaults.sh
source "$REPO_ROOT/scripts/runtime_defaults.sh"

TARGET_PLATFORM="${TARGET_PLATFORM:-linux/arm64}"
TARGET_PYTHON="${TARGET_PYTHON:-3.12}"
DERIVED_IMAGE="${DERIVED_IMAGE:-dgx-spark-vllm-ray:${RAY_VERSION}}"
ZSTD_LEVEL="${ZSTD_LEVEL:-3}"
REQUIREMENTS_FILE="$REPO_ROOT/requirements.lock"
RAY_CONSTRAINTS="$REPO_ROOT/scripts/ray-runtime-constraints.txt"
OUTPUT_DIR="${OUTPUT_DIR:-$HERE/offline-bundle}"

if [[ ! -f "$REQUIREMENTS_FILE" || ! -f "$RAY_CONSTRAINTS" ]]; then
  echo "[offline] requirements.lock and Ray constraints are required" >&2
  exit 1
fi
LOCKED_RAY_VERSION="$(awk -F '==' '$1 == "ray" { print $2; exit }' "$RAY_CONSTRAINTS")"
if [[ ! "$RAY_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || "$RAY_VERSION" != "$LOCKED_RAY_VERSION" ]]; then
  echo "[offline] RAY_VERSION=$RAY_VERSION does not match $RAY_CONSTRAINTS (ray==$LOCKED_RAY_VERSION)" >&2
  exit 1
fi
if [[ ! "$TARGET_PYTHON" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "[offline] TARGET_PYTHON must be a major.minor version, got: $TARGET_PYTHON" >&2
  exit 1
fi
if [[ ! "$DERIVED_IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._:/@-]*$ ]]; then
  echo "[offline] invalid DERIVED_IMAGE: $DERIVED_IMAGE" >&2
  exit 1
fi

case "$TARGET_PLATFORM" in
  linux/arm64)
    TARGET_ARCH=aarch64
    TARGET_IMAGE_ARCH=arm64
    TARGET_PLATFORM_TAGS=(manylinux_2_28_aarch64 manylinux_2_17_aarch64 manylinux2014_aarch64 linux_aarch64)
    ;;
  linux/amd64)
    TARGET_ARCH=x86_64
    TARGET_IMAGE_ARCH=amd64
    TARGET_PLATFORM_TAGS=(manylinux_2_28_x86_64 manylinux_2_17_x86_64 manylinux2014_x86_64 linux_x86_64)
    ;;
  *)
    echo "[offline] unsupported TARGET_PLATFORM: $TARGET_PLATFORM" >&2
    exit 1
    ;;
esac

command -v docker >/dev/null 2>&1 || { echo "[offline] docker is required" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "[offline] Docker Buildx is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "[offline] python3 is required" >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "[offline] tar is required" >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { echo "[offline] sha256sum is required" >&2; exit 1; }
if ! command -v zstd >/dev/null 2>&1; then
  command -v gzip >/dev/null 2>&1 || { echo "[offline] gzip is required when zstd is unavailable" >&2; exit 1; }
fi

if (( INCLUDE_MODELS )); then
  MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-${HF_CACHE:-}}"
  if [[ -z "$MODEL_CACHE_DIR" || ! -d "$MODEL_CACHE_DIR/hub" || -L "$MODEL_CACHE_DIR/hub" ]] \
      || ! find "$MODEL_CACHE_DIR/hub" -mindepth 1 -print -quit | grep -q .; then
    echo "[offline] --include-models requires a non-empty hub/ directory under MODEL_CACHE_DIR or HF_CACHE" >&2
    exit 1
  fi
fi

TARGET_ABI_LIST=("cp${TARGET_PYTHON/./}" abi3 none)

OUTPUT_DIR="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$OUTPUT_DIR")"
if [[ -e "$OUTPUT_DIR" ]]; then
  echo "[offline] output path already exists: $OUTPUT_DIR" >&2
  echo "[offline] choose a new output path to preserve existing files" >&2
  exit 1
fi
OUTPUT_PARENT="$(dirname "$OUTPUT_DIR")"

if ! DOCKER_HOST_PLATFORM="$(docker info --format '{{.OSType}}/{{.Architecture}}')"; then
  echo "[offline] cannot access the Docker daemon" >&2
  exit 1
fi
case "$DOCKER_HOST_PLATFORM" in
  linux/aarch64|linux/arm64) DOCKER_HOST_ARCH=arm64 ;;
  linux/x86_64|linux/amd64) DOCKER_HOST_ARCH=amd64 ;;
  *)
    echo "[offline] unsupported Docker host platform: $DOCKER_HOST_PLATFORM" >&2
    exit 1
    ;;
esac
IMAGE_INSPECT_ARGS=()
if (( EMULATED )); then
  if [[ "$DOCKER_HOST_ARCH" != amd64 || "$TARGET_PLATFORM" != linux/arm64 ]]; then
    echo "[offline] --emulated requires an x86_64 Docker host and TARGET_PLATFORM=linux/arm64" >&2
    exit 1
  fi
  IMAGE_INSPECT_ARGS=(--platform "$TARGET_PLATFORM")
elif [[ "$DOCKER_HOST_ARCH" != "$TARGET_IMAGE_ARCH" ]]; then
  echo "[offline] native build targets $TARGET_PLATFORM, Docker host is $DOCKER_HOST_PLATFORM" >&2
  echo "[offline] pass --emulated for an x86_64 Docker host building an ARM64 bundle" >&2
  exit 1
fi

# The Docker archive exporter requires a container builder. Keep a cache per
# Docker endpoint without changing the user's selected Buildx builder.
BUILDER_ENDPOINT="${DOCKER_CONTEXT:-${DOCKER_HOST:-$(docker context show)}}"
BUILDER_ENDPOINT_HASH="$(printf '%s' "$BUILDER_ENDPOINT" | sha256sum | cut -c 1-16)"
BUILDER_NAME="dgx-spark-offline-$BUILDER_ENDPOINT_HASH"
if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
  docker buildx create --name "$BUILDER_NAME" --driver docker-container >/dev/null
fi
echo "[offline] checking builder $BUILDER_NAME"
BUILDER_DETAILS="$(docker buildx inspect --bootstrap "$BUILDER_NAME")"
printf '%s\n' "$BUILDER_DETAILS"
if ! grep -Eq '^Driver:[[:space:]]+docker-container[[:space:]]*$' <<<"$BUILDER_DETAILS"; then
  echo "[offline] builder $BUILDER_NAME must use the docker-container driver" >&2
  exit 1
fi
if ! grep -Eq "Platforms:.*[[:space:],]${TARGET_PLATFORM}([/,*[:space:]]|$)" <<<"$BUILDER_DETAILS"; then
  echo "[offline] builder $BUILDER_NAME cannot execute $TARGET_PLATFORM" >&2
  echo "[offline] ARM64 emulation requires an official BuildKit image with bundled QEMU support" >&2
  exit 1
fi

SOURCE_COMMIT=unknown
SOURCE_BRANCH=unknown
SOURCE_WORKTREE_STATE=unknown
if command -v git >/dev/null 2>&1 \
    && git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  SOURCE_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify HEAD 2>/dev/null || printf 'unknown')"
  SOURCE_BRANCH="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [[ -z "$SOURCE_BRANCH" ]]; then
    SOURCE_BRANCH="detached HEAD"
  fi
  if SOURCE_GIT_STATUS="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all 2>/dev/null)"; then
    if [[ -z "$SOURCE_GIT_STATUS" ]]; then
      SOURCE_WORKTREE_STATE=clean
    else
      SOURCE_WORKTREE_STATE=modified
    fi
  fi
fi

mkdir -p "$OUTPUT_PARENT"
STAGING_DIR="$(mktemp -d "$OUTPUT_PARENT/.offline-bundle.XXXXXX")"
trap 'if [[ -n "${STAGING_DIR:-}" && -d "$STAGING_DIR" ]]; then rm -rf -- "$STAGING_DIR"; fi' EXIT

echo "[offline] archiving source"
SOURCE_ARCHIVE=dgx-spark-benchmark-src.tar.gz
SOURCE_BUNDLED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$STAGING_DIR/commit.md" <<EOF
# Bundle source metadata

- Commit: $SOURCE_COMMIT
- Branch: $SOURCE_BRANCH
- Source bundled at (UTC): $SOURCE_BUNDLED_UTC
- Git working tree: $SOURCE_WORKTREE_STATE
EOF
python3 "$HERE/source_release.py" snapshot --source "$REPO_ROOT" \
  --metadata "$STAGING_DIR/commit.md" --output "$STAGING_DIR/$SOURCE_ARCHIVE"
BUILD_SOURCE_DIR="$STAGING_DIR/source"
mkdir "$BUILD_SOURCE_DIR"
tar --no-same-owner -xzf "$STAGING_DIR/$SOURCE_ARCHIVE" -C "$BUILD_SOURCE_DIR"
REQUIREMENTS_FILE="$BUILD_SOURCE_DIR/requirements.lock"
RAY_CONSTRAINTS="$BUILD_SOURCE_DIR/scripts/ray-runtime-constraints.txt"
cp "$BUILD_SOURCE_DIR/offline/install_offline.sh" "$STAGING_DIR/install_offline.sh"
cp "$BUILD_SOURCE_DIR/offline/source_release.py" "$STAGING_DIR/source_release.py"
chmod +x "$STAGING_DIR/install_offline.sh"

echo "[offline] pulling $VLLM_IMAGE for $TARGET_PLATFORM"
docker pull --platform "$TARGET_PLATFORM" "$VLLM_IMAGE"
BASE_IMAGE_ID="$(docker image inspect "${IMAGE_INSPECT_ARGS[@]}" --format '{{.Id}}' "$VLLM_IMAGE")"
BASE_IMAGE_ARCH="$(docker image inspect "${IMAGE_INSPECT_ARGS[@]}" --format '{{.Architecture}}' "$VLLM_IMAGE")"
if [[ "$BASE_IMAGE_ARCH" != "$TARGET_IMAGE_ARCH" ]]; then
  echo "[offline] base image architecture is $BASE_IMAGE_ARCH, expected $TARGET_IMAGE_ARCH" >&2
  exit 1
fi

BUILD_CONTEXT="$STAGING_DIR/docker-context"
mkdir -p "$BUILD_CONTEXT"
cp "$BUILD_SOURCE_DIR/offline/Dockerfile.ray" "$BUILD_CONTEXT/Dockerfile.ray"
cp "$RAY_CONSTRAINTS" "$BUILD_CONTEXT/ray-runtime-constraints.txt"

IMAGE_TAR="$STAGING_DIR/vllm-ray-image.tar"
echo "[offline] building $DERIVED_IMAGE for $TARGET_PLATFORM"
docker buildx build --builder "$BUILDER_NAME" --platform "$TARGET_PLATFORM" \
  --build-arg "BASE_IMAGE=$VLLM_IMAGE" \
  --build-arg "RAY_VERSION=$RAY_VERSION" \
  --tag "$DERIVED_IMAGE" \
  --file "$BUILD_CONTEXT/Dockerfile.ray" \
  --output "type=docker,dest=$IMAGE_TAR" \
  "$BUILD_CONTEXT"
rm -rf -- "$BUILD_CONTEXT"

echo "[offline] loading image archive for architecture inspection"
docker load --input "$IMAGE_TAR"
IMAGE_ARCH="$(docker image inspect "${IMAGE_INSPECT_ARGS[@]}" --format '{{.Architecture}}' "$DERIVED_IMAGE")"
if [[ "$IMAGE_ARCH" != "$TARGET_IMAGE_ARCH" ]]; then
  echo "[offline] built image architecture is $IMAGE_ARCH, expected $TARGET_IMAGE_ARCH" >&2
  exit 1
fi

if (( EMULATED )); then
  # BuildKit's emulator is scoped to build RUN instructions, not docker run.
  echo "[offline] Ray import verified during the emulated image build"
else
  echo "[offline] verifying Ray in the derived image"
  docker run --rm --platform "$TARGET_PLATFORM" --entrypoint python3 "$DERIVED_IMAGE" \
    -c 'import ray; print("ray", ray.__version__)'
fi

if command -v zstd >/dev/null 2>&1; then
  IMAGE_ARCHIVE=vllm-ray-image.tar.zst
  zstd -T0 "-$ZSTD_LEVEL" "$IMAGE_TAR" -o "$STAGING_DIR/$IMAGE_ARCHIVE"
else
  IMAGE_ARCHIVE=vllm-ray-image.tar.gz
  gzip -1 -c "$IMAGE_TAR" > "$STAGING_DIR/$IMAGE_ARCHIVE"
fi
rm -f "$IMAGE_TAR"

echo "[offline] downloading client wheels for $TARGET_ARCH Python $TARGET_PYTHON"
WHEEL_DIR="$STAGING_DIR/wheels"
mkdir -p "$WHEEL_DIR"
PIP_TARGET_ARGS=(--python-version "$TARGET_PYTHON" --implementation cp)
for TARGET_PLATFORM_TAG in "${TARGET_PLATFORM_TAGS[@]}"; do
  PIP_TARGET_ARGS+=(--platform "$TARGET_PLATFORM_TAG")
done
for TARGET_ABI in "${TARGET_ABI_LIST[@]}"; do
  PIP_TARGET_ARGS+=(--abi "$TARGET_ABI")
done
PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip download \
  --requirement "$REQUIREMENTS_FILE" \
  --dest "$WHEEL_DIR" \
  --only-binary=:all: \
  "${PIP_TARGET_ARGS[@]}"

MODEL_CACHE_ARCHIVE=none
if (( INCLUDE_MODELS )); then
  MODEL_CACHE_ARCHIVE=model-cache.tar
  echo "[offline] archiving Hugging Face hub cache from $MODEL_CACHE_DIR"
  tar -C "$MODEL_CACHE_DIR" --exclude='hub/token' --exclude='hub/.locks' \
    -cf "$STAGING_DIR/$MODEL_CACHE_ARCHIVE" hub
else
  echo "[offline] model weights excluded; pass --include-models to add the Hugging Face hub cache"
fi

cat > "$STAGING_DIR/BUNDLE_INFO" <<EOF
bundle_format=2
derived_image=$DERIVED_IMAGE
base_image=$VLLM_IMAGE
base_image_id=$BASE_IMAGE_ID
ray_version=$RAY_VERSION
target_platform=$TARGET_PLATFORM
target_arch=$TARGET_ARCH
target_python=$TARGET_PYTHON
requirements_file=$(basename "$REQUIREMENTS_FILE")
requirements_sha256=$(sha256sum "$REQUIREMENTS_FILE" | awk '{print $1}')
image_archive=$IMAGE_ARCHIVE
source_archive=$SOURCE_ARCHIVE
model_cache_archive=$MODEL_CACHE_ARCHIVE
built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

rm -rf -- "$BUILD_SOURCE_DIR"
( cd "$STAGING_DIR" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS )
python3 "$STAGING_DIR/source_release.py" record-bundle --bundle "$STAGING_DIR"
( cd "$STAGING_DIR" && sha256sum SOURCE_RELEASE.json >> SHA256SUMS )

mv "$STAGING_DIR" "$OUTPUT_DIR"
STAGING_DIR=""
echo "[offline] bundle written to $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
