#!/usr/bin/env bash
# Shared shell entry points for the campaign path contract.
CAMPAIGN_OUTPUT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

campaign_resolve_out_dir() {
  local default_name="${1:-benchmark}"
  if [[ -z "${CAMPAIGN_OUT_DIR_IMPLICIT+x}" ]]; then
    if [[ -n "${OUT_DIR:-}" ]]; then
      CAMPAIGN_OUT_DIR_IMPLICIT=0
    else
      CAMPAIGN_OUT_DIR_IMPLICIT=1
    fi
  fi
  export CAMPAIGN_OUT_DIR_IMPLICIT
  if [[ "${CAMPAIGN_PATH_PREPARED:-0}" == "1" && -n "${OUT_DIR:-}" ]]; then
    OUT_DIR="$(python3 "$CAMPAIGN_OUTPUT_ROOT/campaign_output.py" resolve \
      --out-dir "$OUT_DIR")" || return 1
  elif [[ "$CAMPAIGN_OUT_DIR_IMPLICIT" == "1" ]]; then
    OUT_DIR="$(python3 "$CAMPAIGN_OUTPUT_ROOT/campaign_output.py" resolve \
      --default-name "$default_name")" || return 1
  else
    OUT_DIR="$(python3 "$CAMPAIGN_OUTPUT_ROOT/campaign_output.py" resolve \
      --out-dir "$OUT_DIR")" || return 1
  fi
  export OUT_DIR
  CAMPAIGN_RESULTS_DIR="$(python3 "$CAMPAIGN_OUTPUT_ROOT/campaign_output.py" \
    results-dir --out-dir "$OUT_DIR")" || return 1
  CAMPAIGN_LOGS_DIR="$(python3 "$CAMPAIGN_OUTPUT_ROOT/campaign_output.py" \
    logs-dir --out-dir "$OUT_DIR")" || return 1
  export CAMPAIGN_RESULTS_DIR CAMPAIGN_LOGS_DIR
}

campaign_reject_existing_implicit_out_dir() {
  if [[ "${CAMPAIGN_OUT_DIR_IMPLICIT:-0}" == "1" \
      && "${CAMPAIGN_PATH_PREPARED:-0}" != "1" \
      && -e "$OUT_DIR" ]]; then
    echo "[campaign] implicit output path already exists: $OUT_DIR; set OUT_DIR to resume or choose a fresh path" >&2
    return 1
  fi
}
