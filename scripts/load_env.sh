#!/usr/bin/env bash
# Load a repo-local .env file for script entrypoints.
# Supports normal shell syntax such as `export VAR=value` and `$HOME` expansion.

if [[ -n "${DGX_SPARK_ENV_LOADED:-}" ]]; then
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

  set -a
  # shellcheck disable=SC1090
  source "$_load_env_file"

  if [[ "$_load_env_had_allexport" != "1" ]]; then
    set +a
  fi
  if [[ "$_load_env_had_nounset" == "1" ]]; then
    set -u
  fi
fi

unset _load_env_root _load_env_file _load_env_had_allexport _load_env_had_nounset
