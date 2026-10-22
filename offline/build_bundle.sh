#!/usr/bin/env bash
# Build a transferable runtime bundle from the current repository checkout.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

if [[ -f "$REPO_ROOT/scripts/connectx_env.sh" ]]; then
  # shellcheck source=../scripts/connectx_env.sh
  source "$REPO_ROOT/scripts/connectx_env.sh"
fi

RAY_CONSTRAINTS="$REPO_ROOT/scripts/ray-runtime-constraints.txt"
REQUIREMENTS_FILE=""
if [[ -f "$REPO_ROOT/requirements.lock" ]]; then
  REQUIREMENTS_FILE="$REPO_ROOT/requirements.lock"
elif [[ -f "$REPO_ROOT/requirements.txt" ]]; then
  REQUIREMENTS_FILE="$REPO_ROOT/requirements.txt"
fi

if [[ -z "${RAY_VERSION:-}" && -f "$RAY_CONSTRAINTS" ]]; then
  RAY_VERSION="$(awk -F '==' 'tolower($1) == "ray" { print $2; exit }' "$RAY_CONSTRAINTS")"
fi
VLLM_IMAGE="${VLLM_IMAGE:-}"
RAY_VERSION="${RAY_VERSION:-}"
if [[ -z "$VLLM_IMAGE" ]]; then
  echo "[offline] set VLLM_IMAGE or define it in scripts/connectx_env.sh" >&2
  exit 1
fi
if [[ -z "$RAY_VERSION" ]]; then
  echo "[offline] set RAY_VERSION or pin ray in scripts/ray-runtime-constraints.txt" >&2
  exit 1
fi
if [[ -z "$REQUIREMENTS_FILE" ]]; then
  echo "[offline] requirements.lock or requirements.txt is required" >&2
  exit 1
fi

if [[ -f "$RAY_CONSTRAINTS" ]]; then
  CONSTRAINED_RAY_VERSION="$(awk -F '==' 'tolower($1) == "ray" { print $2; exit }' "$RAY_CONSTRAINTS")"
  if [[ -n "$CONSTRAINED_RAY_VERSION" && "$RAY_VERSION" != "$CONSTRAINED_RAY_VERSION" ]]; then
    echo "[offline] RAY_VERSION=$RAY_VERSION differs from ray==$CONSTRAINED_RAY_VERSION in $RAY_CONSTRAINTS" >&2
    exit 1
  fi
fi

TARGET_PLATFORM="${TARGET_PLATFORM:-linux/arm64}"
case "$TARGET_PLATFORM" in
  linux/arm64)
    TARGET_ARCH="aarch64"
    TARGET_IMAGE_ARCH="arm64"
    ;;
  linux/amd64)
    TARGET_ARCH="x86_64"
    TARGET_IMAGE_ARCH="amd64"
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

TARGET_PYTHON="${TARGET_PYTHON:-$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')}"
if [[ ! "$TARGET_PYTHON" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "[offline] TARGET_PYTHON must be a major.minor version, got: $TARGET_PYTHON" >&2
  exit 1
fi
TARGET_MANYLINUX="${TARGET_MANYLINUX:-manylinux_2_28}"
TARGET_PYTHON_IMPLEMENTATION="${TARGET_PYTHON_IMPLEMENTATION:-cp}"
TARGET_ABIS="${TARGET_ABIS:-cp${TARGET_PYTHON/./} abi3 none}"
read -r -a TARGET_ABI_LIST <<< "$TARGET_ABIS"
if [[ ${#TARGET_ABI_LIST[@]} -eq 0 ]]; then
  echo "[offline] TARGET_ABIS must contain at least one ABI tag" >&2
  exit 1
fi

DERIVED_IMAGE="${DERIVED_IMAGE:-dgx-spark-vllm-ray:$RAY_VERSION}"
ZSTD_LEVEL="${ZSTD_LEVEL:-3}"
DEFAULT_OUTPUT_DIR="$HERE/offline-bundle-$(date -u +%Y%m%dT%H%M%SZ)-$$"
OUTPUT_DIR="${1:-$DEFAULT_OUTPUT_DIR}"
OUTPUT_DIR="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$OUTPUT_DIR")"
if [[ -e "$OUTPUT_DIR" ]]; then
  echo "[offline] output path already exists: $OUTPUT_DIR" >&2
  echo "[offline] choose a new output path to preserve existing files" >&2
  exit 1
fi
OUTPUT_PARENT="$(dirname "$OUTPUT_DIR")"
mkdir -p "$OUTPUT_PARENT"
STAGING_DIR="$(mktemp -d "$OUTPUT_PARENT/.offline-bundle.XXXXXX")"
trap 'if [[ -n "${STAGING_DIR:-}" && -d "$STAGING_DIR" ]]; then rm -rf -- "$STAGING_DIR"; fi' EXIT

echo "[offline] pulling $VLLM_IMAGE for $TARGET_PLATFORM"
docker pull --platform "$TARGET_PLATFORM" "$VLLM_IMAGE"
BASE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$VLLM_IMAGE")"

BUILD_CONTEXT="$STAGING_DIR/docker-context"
mkdir -p "$BUILD_CONTEXT"
cp "$HERE/Dockerfile.ray" "$BUILD_CONTEXT/Dockerfile.ray"
if [[ -f "$RAY_CONSTRAINTS" ]]; then
  cp "$RAY_CONSTRAINTS" "$BUILD_CONTEXT/ray-runtime-constraints.txt"
else
  : > "$BUILD_CONTEXT/ray-runtime-constraints.txt"
fi

IMAGE_TAR="$STAGING_DIR/vllm-ray-image.tar"
echo "[offline] building $DERIVED_IMAGE for $TARGET_PLATFORM"
docker buildx build --platform "$TARGET_PLATFORM" \
  --build-arg "BASE_IMAGE=$VLLM_IMAGE" \
  --build-arg "RAY_VERSION=$RAY_VERSION" \
  --tag "$DERIVED_IMAGE" \
  --file "$BUILD_CONTEXT/Dockerfile.ray" \
  --output "type=docker,dest=$IMAGE_TAR" \
  "$BUILD_CONTEXT"
rm -rf -- "$BUILD_CONTEXT"

echo "[offline] loading image archive for architecture inspection"
docker load --input "$IMAGE_TAR"
IMAGE_ARCH="$(docker image inspect --format '{{.Architecture}}' "$DERIVED_IMAGE")"
if [[ "$IMAGE_ARCH" != "$TARGET_IMAGE_ARCH" ]]; then
  echo "[offline] built image architecture is $IMAGE_ARCH, expected $TARGET_IMAGE_ARCH" >&2
  exit 1
fi

echo "[offline] verifying Ray in the derived image"
if docker run --rm --platform "$TARGET_PLATFORM" --entrypoint python3 "$DERIVED_IMAGE" \
    -c 'import ray; print("ray", ray.__version__)'; then
  :
else
  echo "[offline] image execution is unavailable on this builder; verify Ray on the target system" >&2
fi

if command -v zstd >/dev/null 2>&1; then
  IMAGE_ARCHIVE="vllm-ray-image.tar.zst"
  zstd -T0 "-$ZSTD_LEVEL" "$IMAGE_TAR" -o "$STAGING_DIR/$IMAGE_ARCHIVE"
else
  IMAGE_ARCHIVE="vllm-ray-image.tar.gz"
  gzip -1 -c "$IMAGE_TAR" > "$STAGING_DIR/$IMAGE_ARCHIVE"
fi
rm -f "$IMAGE_TAR"

echo "[offline] downloading client wheels for $TARGET_ARCH Python $TARGET_PYTHON"
WHEEL_DIR="$STAGING_DIR/wheels"
mkdir -p "$WHEEL_DIR"
PIP_TARGET_ARGS=(
  --platform "${TARGET_MANYLINUX}_${TARGET_ARCH}"
  --python-version "$TARGET_PYTHON"
  --implementation "$TARGET_PYTHON_IMPLEMENTATION"
)
for TARGET_ABI in "${TARGET_ABI_LIST[@]}"; do
  PIP_TARGET_ARGS+=(--abi "$TARGET_ABI")
done
PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip download \
  --requirement "$REQUIREMENTS_FILE" \
  --dest "$WHEEL_DIR" \
  --only-binary=:all: \
  "${PIP_TARGET_ARGS[@]}"

echo "[offline] archiving source"
SOURCE_ARCHIVE="dgx-spark-benchmark-src.tar.gz"
SOURCE_TAR_EXCLUDES=(
  --exclude-vcs
  --exclude='./.venv' --exclude='./venv' --exclude='./env'
  --exclude='./.aws' --exclude='./.ssh' --exclude='./.gnupg' --exclude='./.codex'
  --exclude='./.cache' --exclude='./__pycache__' --exclude='*.pyc'
  --exclude='./.env*' --exclude='*/.env*' --exclude='./.netrc' --exclude='./.pypirc'
  --exclude='./offline-bundle' --exclude='./offline-bundle-*'
  --exclude='./offline/offline-bundle-*' --exclude='./offline/.offline-bundle.*'
  --exclude='results*' --exclude='figs' --exclude='__pycache__'
)
case "$STAGING_DIR/" in
  "$REPO_ROOT/"*)
    STAGING_RELATIVE="${STAGING_DIR#"$REPO_ROOT"/}"
    SOURCE_TAR_EXCLUDES+=(--exclude="./$STAGING_RELATIVE")
    ;;
esac
tar -C "$REPO_ROOT" "${SOURCE_TAR_EXCLUDES[@]}" -czf "$STAGING_DIR/$SOURCE_ARCHIVE" .

MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-${HF_CACHE:-}}"
MODEL_CACHE_ARCHIVE="none"
if [[ -n "$MODEL_CACHE_DIR" && -d "$MODEL_CACHE_DIR/hub" && ! -L "$MODEL_CACHE_DIR/hub" ]] \
    && find "$MODEL_CACHE_DIR/hub" -mindepth 1 -print -quit | grep -q .; then
  MODEL_CACHE_ARCHIVE="model-cache.tar"
  echo "[offline] archiving Hugging Face hub cache from $MODEL_CACHE_DIR"
  tar -C "$MODEL_CACHE_DIR" --exclude='./hub/token' --exclude='./hub/.locks' \
    -cf "$STAGING_DIR/$MODEL_CACHE_ARCHIVE" hub
else
  echo "[offline] no Hugging Face hub cache found; model weights are not included"
fi

cp "$HERE/install_offline.sh" "$STAGING_DIR/install_offline.sh"
chmod +x "$STAGING_DIR/install_offline.sh"
SOURCE_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || printf unknown)"
if [[ "$SOURCE_REVISION" == "unknown" ]]; then
  SOURCE_WORKTREE_STATE="unknown"
else
  SOURCE_STATUS="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)"
  if [[ -n "$SOURCE_STATUS" ]]; then
    SOURCE_WORKTREE_STATE="modified"
  else
    SOURCE_WORKTREE_STATE="clean"
  fi
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
target_manylinux=$TARGET_MANYLINUX
requirements_file=$(basename "$REQUIREMENTS_FILE")
requirements_sha256=$(sha256sum "$REQUIREMENTS_FILE" | awk '{print $1}')
image_archive=$IMAGE_ARCHIVE
source_archive=$SOURCE_ARCHIVE
model_cache_archive=$MODEL_CACHE_ARCHIVE
source_revision=$SOURCE_REVISION
source_worktree_state=$SOURCE_WORKTREE_STATE
image_id=$(docker image inspect --format '{{.Id}}' "$DERIVED_IMAGE")
built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

( cd "$STAGING_DIR" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS )

mv "$STAGING_DIR" "$OUTPUT_DIR"
STAGING_DIR=""
echo "[offline] bundle written to $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
