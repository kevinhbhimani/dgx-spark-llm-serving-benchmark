#!/usr/bin/env bash
# Collect read-only evidence after a node went down, froze, or rebooted during
# a campaign. Run on each configured node; nothing is changed on the host.
#
#   sudo scripts/node_postmortem.sh [CAMPAIGN_DIR]
#
# Requires root. Without it, journalctl reads only the caller's own journal,
# which lists different boots, and /sys/fs/pstore is unreadable.
#
# Writes everything to results/node-postmortem-<host>-<UTC timestamp>/ under the
# repository root, owned by the sudo caller, and prints the directory.
# CAMPAIGN_DIR (optional) is a campaign OUT_DIR whose run_job and server logs
# should be summarized as well.
#
# prev_boot_* files cover boot -1. A power-on attempt that stops seconds after
# the journal flush leaves a boot spanning under MIN_FULL_BOOT_SECONDS. When
# boot -1 is shorter than that, last_full_boot_* files cover the most recent
# previous boot spanning at least that long, and after_full_boot_kernel.txt
# covers the boot that followed it when that boot is not boot -1.
#
# Reading a boot's tail (prev_boot_tail.txt, last_full_boot_tail.txt):
#   ends with systemd "Reached target ... Power-Off/Reboot", "systemd-shutdown"
#     or "Powering off"/"Rebooting"  -> an orderly shutdown; look above it for
#     who started it (a user, unattended-upgrades, the DGX Dashboard updater)
#   kernel "Critical temperature", "thermal", or "hw_protection" lines near the
#     end -> the OS saw a thermal or hardware-protection trip
#   "Kernel panic", "Oops", "soft lockup", "hung_task", "rcu ... stall" -> a
#     kernel crash or hang (usually followed by a manual power cycle)
#   "NV_ERR_NO_MEMORY", "Out of memory", "oom-kill" shortly before the end ->
#     unified-memory exhaustion
#   "NVRM: Xid" -> a GPU fault (Xid 119/120 GSP timeouts can wedge a GB10
#     until a cold power cycle)
#   journal simply stops mid-stream with none of the above -> abrupt power loss
#     or a firmware/EC power-off (thermal or power delivery); the OS logs nothing
#   "BERT", "APEI", "GHES" or "Hardware Error" in the kernel lines of the boot
#     after a stop -> the firmware recorded a hardware error for that stop
set -uo pipefail

if [[ "$(id -u)" != "0" ]]; then
  echo "[postmortem] run with sudo: the system journal and /sys/fs/pstore need root" >&2
  exit 1
fi

MIN_FULL_BOOT_SECONDS=60
CAMPAIGN_DIR="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="$(hostname -s 2>/dev/null || echo unknown)"
OUT="$REPO_ROOT/results/node-postmortem-${HOST}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT" || exit 1

# Run one command into OUT/<name>, recording its exit status but never aborting.
collect() {
  local name="$1"; shift
  {
    printf '$ %s\n' "$*"
    "$@"
    printf '\n[exit %s]\n' "$?"
  } >"$OUT/$name" 2>&1
}

KERNEL_PATTERN='oom|out of memory|NV_ERR_NO_MEMORY|NVRM|Xid|thermal|critical|hw_protection|panic|Oops|hung_task|soft lockup|rcu.*stall|watchdog|Power key|power button|mlx5.*power|BERT|APEI|GHES|Hardware Error'
SHUTDOWN_PATTERN='shutdown|reboot|poweroff|power-off|unattended-upgrade|dgx-dashboard|fwupd|systemd-logind'

# Kernel lines matching KERNEL_PATTERN for one boot (an offset or a boot ID).
collect_kernel() {
  collect "$1" bash -c "journalctl -k -b '$2' --no-pager -o short-iso-precise | grep -iE '$KERNEL_PATTERN' | tail -200"
}

# Journal tail, kernel matches, and shutdown actors of one boot.
collect_boot() {
  local prefix="$1" boot="$2"
  collect "${prefix}_tail.txt" journalctl -b "$boot" -n 300 --no-pager -o short-iso-precise
  collect_kernel "${prefix}_kernel.txt" "$boot"
  collect "${prefix}_shutdown_actors.txt" bash -c "journalctl -b '$boot' --no-pager -o short-iso-precise | grep -iE '$SHUTDOWN_PATTERN' | tail -80"
}

# Whole seconds between the first and last journal entries of a boot ID.
boot_span_seconds() {
  local first last
  first="$(journalctl -b "$1" -o short-unix --no-pager -q | head -n 1 | cut -d' ' -f1)"
  last="$(journalctl -b "$1" -o short-unix --no-pager -q -n 1 | cut -d' ' -f1)"
  [[ "$first" =~ ^[0-9]+(\.[0-9]+)?$ && "$last" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 1
  echo $(( ${last%.*} - ${first%.*} ))
}

collect identity.txt bash -c 'hostname; date -u; uname -a; echo "booted at: $(uptime -s)"; cat /proc/uptime'
collect boots.txt journalctl --list-boots --no-pager
collect last_reboots.txt bash -c 'last -x reboot shutdown | head -40'
collect_boot prev_boot -1
collect_kernel this_boot_kernel.txt 0

# Previous boot IDs, newest first.
mapfile -t PREVIOUS_BOOTS < <(journalctl --list-boots --no-pager -q | awk '$1 ~ /^-[0-9]+$/ {print $2}' | tac)
FULL_BOOT_ID=""
for index in "${!PREVIOUS_BOOTS[@]}"; do
  span="$(boot_span_seconds "${PREVIOUS_BOOTS[$index]}")" || continue
  (( span >= MIN_FULL_BOOT_SECONDS )) || continue
  if (( index > 0 )); then
    FULL_BOOT_ID="${PREVIOUS_BOOTS[$index]}"
    collect_boot last_full_boot "$FULL_BOOT_ID"
    if (( index > 1 )); then
      collect_kernel after_full_boot_kernel.txt "${PREVIOUS_BOOTS[$((index - 1))]}"
    fi
  fi
  break
done

collect updates.txt bash -c 'tail -n 60 /var/log/apt/history.log; ls -l /var/log/unattended-upgrades/ 2>/dev/null; tail -n 60 /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null'
collect crash_dumps.txt bash -c 'ls -la /var/crash /sys/fs/pstore 2>&1'
collect gpu.txt nvidia-smi -q -d TEMPERATURE,PERFORMANCE,CLOCK,POWER
command -v sensors >/dev/null && collect sensors.txt sensors
collect memory.txt bash -c 'free -g; head -20 /proc/meminfo'
collect containers.txt bash -c 'docker ps -a; docker exec vllm-spark ray status 2>&1 | head -40'

if [[ -n "$CAMPAIGN_DIR" ]]; then
  collect campaign_logs.txt bash -c "ls -lt --time-style=full-iso '$CAMPAIGN_DIR' '$CAMPAIGN_DIR'/logs 2>/dev/null | head -60"
  collect run_job_tail.txt bash -c "for f in \$(ls -t '$CAMPAIGN_DIR'/run_job_*.log '$CAMPAIGN_DIR'/logs/run_job_*.log 2>/dev/null | head -2); do echo \"== \$f\"; tail -n 120 \"\$f\"; done"
fi

{
  echo "Evidence directory: $OUT"
  echo
  echo "Boots (latest last):"
  tail -n 6 "$OUT/boots.txt"
  echo
  echo "Last 15 lines of the previous boot:"
  grep -v '^\$ ' "$OUT/prev_boot_tail.txt" | grep -v '^\[exit' | tail -n 15
  if [[ -n "$FULL_BOOT_ID" ]]; then
    echo
    echo "Boot -1 spans under ${MIN_FULL_BOOT_SECONDS} s or could not be measured."
    echo "Last 15 lines of the most recent boot spanning at least ${MIN_FULL_BOOT_SECONDS} s ($FULL_BOOT_ID):"
    grep -v '^\$ ' "$OUT/last_full_boot_tail.txt" | grep -v '^\[exit' | tail -n 15
  fi
} | tee "$OUT/SUMMARY.txt"

# sudo writes root-owned files into the caller's checkout; return them so the
# caller can commit or remove them without root.
if [[ -n "${SUDO_UID:-}" ]]; then
  chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$OUT"
fi
