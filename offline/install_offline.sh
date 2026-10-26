#!/usr/bin/env bash
# Install the offline bundle on a DGX Spark with no internet access.
# Run from the bundle directory on each node.
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BUNDLE_DIR"

command -v docker >/dev/null 2>&1 || { echo "[offline] docker is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "[offline] python3 is required" >&2; exit 1; }

echo "[offline] verifying checksums"
sha256sum -c SHA256SUMS

# shellcheck disable=SC1091
source ./BUNDLE_INFO

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

SRC_DIR="${SRC_DIR:-$HOME/dgx-spark-llm-serving-benchmark}"
mkdir -p "$SRC_DIR"
echo "[offline] extracting source to $SRC_DIR"
tar -xzf "$source_archive" -C "$SRC_DIR"

[[ -d "$BUNDLE_DIR/wheels" ]] || { echo "[offline] wheels/ missing from the bundle" >&2; exit 1; }

echo "[offline] creating client virtual environment"
python3 -m venv "$SRC_DIR/.venv"
PYTHONNOUSERSITE=1 "$SRC_DIR/.venv/bin/pip" install \
  --no-index --find-links "$BUNDLE_DIR/wheels" \
  -r "$SRC_DIR/requirements.lock"

echo "[offline] validating client environment"
PYTHONNOUSERSITE=1 "$SRC_DIR/.venv/bin/python" "$SRC_DIR/scripts/check_environment.py"

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
