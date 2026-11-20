#!/usr/bin/env bash
# Load a repo-local .env file for script entrypoints.
# Supports normal shell syntax such as `export VAR=value` and `$HOME` expansion.

resolve_single_node_mode() {
  if [[ "${SINGLE_NODE_ONLY+x}" != "x" ]]; then
    SINGLE_NODE_ONLY=0
  fi
  case "$SINGLE_NODE_ONLY" in
    0|1) export SINGLE_NODE_ONLY ;;
    *)
      echo "[execution] SINGLE_NODE_ONLY must be exactly 0 or 1, got '$SINGLE_NODE_ONLY'" >&2
      return 2
      ;;
  esac
}

resolve_git_check_mode() {
  SKIP_GIT_CHECK="${SKIP_GIT_CHECK-0}"
  case "$SKIP_GIT_CHECK" in
    0|1) export SKIP_GIT_CHECK ;;
    *)
      echo "[source] SKIP_GIT_CHECK must be exactly 0 or 1, got '$SKIP_GIT_CHECK'" >&2
      return 2
      ;;
  esac
}

if [[ -n "${DGX_SPARK_ENV_LOADED:-}" ]]; then
  resolve_git_check_mode || return 2
  return 0 2>/dev/null || exit 0
fi
export DGX_SPARK_ENV_LOADED=1

_load_env_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_load_env_file="${ENV_FILE:-$_load_env_root/.env}"

if [[ -f "$_load_env_file" ]]; then
  case $- in
    *a*) _load_env_had_allexport=1 ;;
    *) _load_env_had_allexport=0 ;;
  esac
  case $- in
    *u*) _load_env_had_nounset=1; set +u ;;
    *) _load_env_had_nounset=0 ;;
  esac

  # Standard dotenv precedence: values already in the environment win over
  # .env, so documented one-shot overrides such as
  # `CONCURRENCIES="1" scripts/run_all.sh single` keep working when .env sets
  # the same variable. Snapshot the exported env, source .env, then re-apply
  # the snapshot. (`export VAR=...` form so the restore is safe even if this
  # file is ever sourced inside a function.)
  _load_env_snapshot="$(export -p | sed 's/^declare -x /export /')"

  set -a
  # shellcheck disable=SC1090
  source "$_load_env_file"

  if [[ "$_load_env_had_allexport" != "1" ]]; then
    set +a
  fi
  eval "$_load_env_snapshot"
  if [[ "$_load_env_had_nounset" == "1" ]]; then
    set -u
  fi
fi

unset _load_env_root _load_env_file _load_env_had_allexport _load_env_had_nounset \
      _load_env_snapshot
resolve_git_check_mode || return 2
