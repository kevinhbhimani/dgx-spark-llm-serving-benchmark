#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

CONFIG="${1:-}"
case "$CONFIG" in
  single|tp2|pp2) ;;
  *) echo "usage: flush_page_cache.sh [single|tp2|pp2]" >&2; exit 1 ;;
esac

show_memory() {
  grep -E '^(MemTotal|MemFree|MemAvailable):' /proc/meminfo
}

flush_local_page_cache() {
  local node_name
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    node_name="$LOCAL_HOSTNAME"
  else
    node_name="$LOCAL_NODE_ID"
  fi
  echo "[memory] page cache before flush on $node_name"
  show_memory
  sync
  if ! printf '3\n' | sudo -n /usr/bin/tee /proc/sys/vm/drop_caches >/dev/null; then
    echo "[memory] cache flush needs noninteractive sudo for /usr/bin/tee /proc/sys/vm/drop_caches on $node_name" >&2
    return 1
  fi
  echo "[memory] page cache after flush on $node_name"
  show_memory
}

flush_worker_page_cache() {
  local remote_command
  remote_command='set -eu
echo "[memory] page cache before flush on worker"
grep -E "^(MemTotal|MemFree|MemAvailable):" /proc/meminfo
sync
if ! printf "3\\n" | sudo -n /usr/bin/tee /proc/sys/vm/drop_caches >/dev/null; then
  echo "[memory] cache flush needs noninteractive sudo for /usr/bin/tee /proc/sys/vm/drop_caches on worker" >&2
  exit 1
fi
echo "[memory] page cache after flush on worker"
grep -E "^(MemTotal|MemFree|MemAvailable):" /proc/meminfo'
  ssh -T \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    -o UpdateHostKeys=no \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    "$WORKER_SSH_TARGET" "$remote_command"
}

flush_local_page_cache
if [[ "$CONFIG" != "single" ]]; then
  echo "[memory] flushing worker page cache before $CONFIG server launch"
  flush_worker_page_cache
fi
