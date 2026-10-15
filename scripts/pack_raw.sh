#!/usr/bin/env bash
# Pack a campaign's raw telemetry into one compressed archive for storage
# outside git (lab NAS, Zenodo, a GitHub release asset).
#
#   scripts/pack_raw.sh results                  # -> ../dgx-spark-raw/<name>.tar.zst
#   DEST=/mnt/archive scripts/pack_raw.sh baselines/pmbs26-tcp
#
# Git keeps only the small artifacts (index.csv, *.summary.json, serving
# policies, interconnect CSVs, figures, the sealed manifest); .gitignore keeps
# raw request records, power samples, vLLM metric samples, and server/job logs
# out of commits. Those files are what `validate_campaign.py verify` hashes, so
# the archive carries a SHA256SUMS file: after extracting it into the result
# directory, `sha256sum -c` and verify both pass again.
#
# Env: DEST (default ../dgx-spark-raw next to the repository).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:?usage: pack_raw.sh RESULT_DIR}"
[[ -d "$SRC" ]] || { echo "[pack] no such directory: $SRC" >&2; exit 1; }
SRC="$(cd "$SRC" && pwd)"
DEST="${DEST:-$(dirname "$HERE")/dgx-spark-raw}"
mkdir -p "$DEST"

name="$(basename "$SRC")-raw-$(date +%Y%m%d)"
list="$(mktemp)"
trap 'rm -f "$list"' EXIT
( cd "$SRC" && find . -maxdepth 1 -type f \
    \( -name '*.raw.jsonl' -o -name '*.power.jsonl' -o -name '*.vllm_metrics.jsonl' \
       -o -name 'server_*.log' -o -name 'run_job_*.log' -o -name '*_raw.log' \) \
    -printf '%P\n' | sort ) > "$list"
count="$(wc -l < "$list")"
if (( count == 0 )); then
  echo "[pack] no raw telemetry in $SRC"
  exit 0
fi

( cd "$SRC" && xargs -d '\n' sha256sum < "$list" ) > "$DEST/$name.SHA256SUMS"
if tar --help 2>/dev/null | grep -q -- '--zstd' && command -v zstd >/dev/null; then
  archive="$DEST/$name.tar.zst"
  tar -C "$SRC" --zstd -cf "$archive" -T "$list"
else
  archive="$DEST/$name.tar.xz"
  tar -C "$SRC" -cJf "$archive" -T "$list"
fi
( cd "$DEST" && sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256" )

raw_bytes="$( cd "$SRC" && xargs -d '\n' du -cb < "$list" | tail -n1 | cut -f1 )"
echo "[pack] $count file(s), $((raw_bytes / 1024 / 1024)) MiB -> $archive" \
     "($(( $(stat -c %s "$archive") / 1024 / 1024 )) MiB)"
echo "[pack] per-file checksums: $DEST/$name.SHA256SUMS"
echo "[pack] restore: tar -C $SRC -xf $archive && (cd $SRC && sha256sum -c $DEST/$name.SHA256SUMS)"
