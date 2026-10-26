#!/usr/bin/env bash
# Collect read-only evidence after a node went down, froze, or rebooted during
# a campaign. Run on EACH node (Diya and Hoedus); nothing is changed on the host.
#
#   scripts/node_postmortem.sh [CAMPAIGN_DIR]
#   sudo scripts/node_postmortem.sh [CAMPAIGN_DIR]   # if the journal is unreadable
#
# Writes everything to ./results/node-postmortem-<host>-<UTC timestamp>/ and prints the
# directory. CAMPAIGN_DIR (optional) is a campaign OUT_DIR whose run_job and
# server logs should be summarized as well.
#
# Reading the previous boot's tail (prev_boot_tail.txt):
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
set -uo pipefail

CAMPAIGN_DIR="${1:-}"
HOST="$(hostname -s 2>/dev/null || echo unknown)"
OUT="results/node-postmortem-${HOST}-$(date -u +%Y%m%dT%H%M%SZ)"
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

KERNEL_PATTERN='oom|out of memory|NV_ERR_NO_MEMORY|NVRM|Xid|thermal|critical|hw_protection|panic|Oops|hung_task|soft lockup|rcu.*stall|watchdog|Power key|power button|mlx5.*power'

collect identity.txt bash -c 'hostname; date -u; uname -a; echo "booted at: $(uptime -s)"; cat /proc/uptime'
collect boots.txt journalctl --list-boots --no-pager
collect last_reboots.txt bash -c 'last -x reboot shutdown | head -40'
collect prev_boot_tail.txt journalctl -b -1 -n 300 --no-pager -o short-iso-precise
collect prev_boot_kernel.txt bash -c "journalctl -k -b -1 --no-pager -o short-iso-precise | grep -iE '$KERNEL_PATTERN' | tail -200"
collect this_boot_kernel.txt bash -c "journalctl -k -b 0 --no-pager -o short-iso-precise | grep -iE '$KERNEL_PATTERN' | tail -200"
collect prev_boot_shutdown_actors.txt bash -c "journalctl -b -1 --no-pager -o short-iso-precise | grep -iE 'shutdown|reboot|poweroff|power-off|unattended-upgrade|dgx-dashboard|fwupd|systemd-logind' | tail -80"
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
} | tee "$OUT/SUMMARY.txt"
if grep -q 'No journal files were found\|insufficient permissions\|not seeing messages' "$OUT"/*.txt 2>/dev/null; then
  echo
  echo "Some journal reads were not permitted; rerun with sudo for the full picture."
fi
