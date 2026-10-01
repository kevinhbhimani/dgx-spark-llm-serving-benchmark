#!/usr/bin/env bash
set -uo pipefail


# NOTE!! This script currently targets cisc-diya and cisc-hoedus, whose netplan
# binds 10.10.10.1/10.10.11.1 (diya) and 10.10.10.2/10.10.11.2 (hoedus), with
# further cables following the same pattern (10.10.12.x, 10.10.13.x, ...).
# Devices are discovered at runtime, so any RDMA device that is UP with an IPv4
# address is tested. See the pairing rule in the configuration section below.
#
#
# USB-C USB-C USB-C USB-C HDMI ETHERNET CONNECTX-7 CONNECTX-7 
#                                       ^^^^
#                                       THIS PORT!
#
#
#

# ---------------------------------------------------------------------------
# perftest-suite.sh - RDMA perftest matrix for two DGX Sparks joined by one or
# more QSFP cables. Each physical QSFP port carries 2 logical RDMA devices.
# Every RDMA device that is UP with an IPv4 address is tested.
#
#   ./perftest-suite.sh host      # perftest server (binds)
#   ./perftest-suite.sh client    # perftest client (connects to the peer IPs)
#
#   --notes <file>   copy a free-form notes sheet into the run's output dir
#
# Run both roles concurrently, one on each node. Both walk the same
# test/port order; ports are allocated identically on both sides.
# ---------------------------------------------------------------------------

role=""
notes_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    host|client)
      role="$1"
      ;;
    --notes)
      shift
      notes_file="${1:-}"
      if [ -z "$notes_file" ]; then
        echo "--notes requires a file argument" >&2
        exit 2
      fi
      ;;
    *)
      echo "usage: $0 {host|client} [--notes <file>]" >&2
      exit 2
      ;;
  esac
  shift
done

if [ -z "$role" ]; then
  echo "usage: $0 {host|client} [--notes <file>]" >&2
  exit 2
fi

# --- configuration (SEE ABOVE NOTE!!) -------------------------------
# Devices and their IPs are discovered at runtime: every RDMA device that is
# UP and has an IPv4 address is tested. The peer's IP is derived from ours by
# flipping the last octet between the odd/even pair (1<->2, 3<->4, ...), which
# matches the current netplan scheme (diya = odd, hoedus = even, same /24).
# This is a convention, not an invariant: if the addressing scheme changes,
# this rule needs updating.

# Print the peer IP for a local IP by flipping the last octet odd<->even.
derive_peer_ip() {
  local ip="$1"
  local prefix="${ip%.*}"
  local last="${ip##*.}"
  if (( last % 2 == 1 )); then
    echo "$prefix.$((last + 1))"
  else
    echo "$prefix.$((last - 1))"
  fi
}

# Fill devs[] and local_ips[] with every RDMA device that is UP and has an
# IPv4 address. DOWN / address-less devices are logged and skipped.
discover_devices() {
  devs=()
  local_ips=()

  local dev netdev ip state
  for dev in /sys/class/infiniband/*; do
    dev=$(basename "$dev")

    # The RDMA device name (rocep1s0f0) differs from its netdev name
    # (enp1s0f0np0); the sysfs symlink bridges the two.
    netdev=$(find /sys/class/infiniband/"$dev"/device/net/ -mindepth 1 -maxdepth 1 \
             -printf '%f\n' 2>/dev/null | head -n1)
    if [ -z "$netdev" ]; then
      echo "skipping $dev: no netdev found" >&2
      continue
    fi

    ip=$(ip -j addr show dev "$netdev" 2>/dev/null \
         | jq -r '.[0].addr_info[]? | select(.family=="inet") | .local' \
         | head -n1)
    state=$(ip -j addr show dev "$netdev" 2>/dev/null \
            | jq -r '.[0].operstate // "UNKNOWN"')

    if [ -z "$ip" ]; then
      echo "skipping $dev ($netdev): no IPv4 address" >&2
      continue
    fi
    if [ "$state" != "UP" ]; then
      echo "skipping $dev ($netdev, $ip): state=$state" >&2
      continue
    fi

    devs+=("$dev")
    local_ips+=("$ip")
  done
}

discover_devices

if [ "${#devs[@]}" -eq 0 ]; then
  echo "[ERROR] no UP RDMA devices with an IPv4 address were found" >&2
  exit 3
fi

# Derive the peer IP for each discovered device.
peer_ips=()
for ip in "${local_ips[@]}"; do
  peer_ips+=("$(derive_peer_ip "$ip")")
done

base_port=${BASE_PORT:-18515}
conn=${CONN:-RC}
lat_iters=${LAT_ITERS:-15000}
bw_iters=${BW_ITERS:-15000}         # keep <=20000, else perftest disables BW peak
initial_delay=${INITIAL_DELAY:-3}
retries=${RETRIES:-3}
outdir=${OUTDIR:-./perftest-results/$(date +%Y%m%d-%H%M%S)}
# Each role writes to its own subdir so the two sides' logs don't collide.
outdir="$outdir/$role"

# <binary> [extra flag]. "-b" = bidirectional.
tests=(
  ib_send_lat
  ib_write_lat
  ib_read_lat
  ib_atomic_lat
  ib_send_bw
  ib_write_bw
  ib_read_bw
  ib_atomic_bw
  "ib_send_bw -b"
  "ib_write_bw -b"
  "ib_read_bw -b"
)

# --- preflight -------------------------------------------------------------
missing=0
for spec in "${tests[@]}"; do
  binary=${spec%% *}
  command -v "$binary" >/dev/null 2>&1 || { echo "missing binary: $binary" >&2; missing=1; }
done
for tool in ip jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing tool: $tool" >&2; missing=1; }
done
for dev in "${devs[@]}"; do
  [ -e "/sys/class/infiniband/$dev" ] || { echo "missing device: $dev" >&2; missing=1; }
done
[ "$missing" -eq 1 ] && exit 1

# Ports must be free before we start, else the host fails to bind and the
# client may attach to a stale server from a previous run.
port_count=$(( ${#devs[@]} * ${#tests[@]} ))
for ((p = base_port; p < base_port + port_count; p++)); do
  if ss -ltn 2>/dev/null | grep -q ":$p "; then
    echo "port $p already in use; kill stale ib_* servers first" >&2
    exit 1
  fi
done

mkdir -p "$outdir" || exit 1
commands_file="$outdir/commands.txt"
summary="$outdir/summary.csv"
: > "$commands_file"
echo "test,device,role,rc,bw_peak_MBps,bw_avg_MBps,lat_typical_usec,size_bytes" > "$summary"

# Attach the researcher's notes sheet to this run, if one was given.
if [ -n "$notes_file" ]; then
  if [ ! -f "$notes_file" ]; then
    echo "notes file not found: $notes_file" >&2
    exit 1
  fi
  cp "$notes_file" "$outdir/notes.$(basename "$notes_file")" || exit 1
  echo "notes: $notes_file -> $outdir/notes.$(basename "$notes_file")"
fi

# --- perftest table parsing -------------------------------------------------
# Data rows look like: " 65536     5000     12680.83     12636.10     0.202"
#   *_bw  : 1 size, 2 iters, 3 BW peak[Mb/s], 4 BW average[Mb/s], 5 MsgRate
#   *_lat : 1 size, 2 iters, 3 t_min, 4 t_max, 5 t_typical[usec], ...
# Reported value: max BW peak/average across sizes, or min t_typical.
parse_metrics() {
  local log="$1" kind="$2"
  if [ "$kind" = bw ]; then
    awk '$1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {
           if ($3+0 > peak) peak = $3+0
           if ($4+0 > avg)  { avg = $4+0; size = $1 }
         }
         END { printf "%.2f,%.2f,,%d", peak+0, avg+0, size+0 }' "$log"
  else
    awk '$1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {
           if (typ == 0 || ($5+0 > 0 && $5+0 < typ)) { typ = $5+0; size = $1 }
         }
         END { printf ",,%.3f,%d", typ+0, size+0 }' "$log"
  fi
}

# --- one test ---------------------------------------------------------------
# $1 name  $2 extra flag  $3 device  $4 server ip  $5 port
run_test() {
  local name="$1" extra="$2" dev="$3" server_ip="$4" port="$5"

  local kind iters
  case "$name" in
    *_lat) kind=lat; iters="$lat_iters" ;;
    *)     kind=bw;  iters="$bw_iters"  ;;
  esac

  local flags="-d $dev -i 1 -F -p $port -c $conn -n $iters --report_gbits"
  [ -n "$extra" ] && flags="$flags $extra"

  local host_cmd="$name $flags"
  local client_cmd="$name $flags $server_ip"
  local local_cmd="$host_cmd"
  [ "$role" = client ] && local_cmd="$client_cmd"

  local tag="$name${extra:+_bidi}_$dev"
  local log="$outdir/$tag.log"

  # Header makes the log self-describing and records both sides' commands.
  {
    echo "# test   : $name${extra:+ ($extra)}"
    echo "# device : $dev"
    echo "# port   : $port"
    echo "# host   : $host_cmd"
    echo "# client : $client_cmd"
    echo "# ---"
  } > "$log"

  echo ">>> [$role] $local_cmd"

  local rc=0 attempt
  if [ "$role" = client ]; then
    rc=1
    for attempt in $(seq 1 "$retries"); do
      "$name" $flags "$server_ip" >> "$log" 2>&1 && { rc=0; break; }
      rc=$?
      sleep 1
    done
  else
    # Retry the bind too: a previous server may still be releasing the port.
    rc=1
    for attempt in $(seq 1 "$retries"); do
      "$name" $flags >> "$log" 2>&1 && { rc=0; break; }
      rc=$?
      sleep 1
    done
  fi
  [ "$rc" -ne 0 ] && echo "!! $tag failed rc=$rc" >&2

  {
    echo "$tag"
    echo "  host   : $host_cmd"
    echo "  client : $client_cmd"
  } >> "$commands_file"

  echo "$tag,$dev,$role,$rc,$(parse_metrics "$log" "$kind")" >> "$summary"

  # Make sure this test's server is gone before the next test reuses a port.
  pkill -f "^$name " 2>/dev/null
  sleep 1
}

# --- run --------------------------------------------------------------------
echo "role=$role conn=$conn out=$outdir"
echo "discovered devices (device | local_ip | peer_ip):"
for i in "${!devs[@]}"; do
  echo "  ${devs[$i]} | ${local_ips[$i]} | ${peer_ips[$i]}"
done
if [ "$role" = client ]; then
  echo "targets=${peer_ips[*]}; starting in ${initial_delay}s"
  sleep "$initial_delay"
fi

port="$base_port"
for i in "${!devs[@]}"; do
  dev="${devs[$i]}"
  server_ip="${peer_ips[$i]}"
  for spec in "${tests[@]}"; do
    name="${spec%% *}"
    extra="${spec#* }"
    [ "$extra" = "$spec" ] && extra=""

    # UD has no read verbs.
    if [ "$conn" != RC ] && [ "$conn" != UC ] && [[ "$name" == ib_read_* ]]; then
      continue
    fi

    run_test "$name" "$extra" "$dev" "$server_ip" "$port"
    port=$((port + 1))
  done
done

# --- report -----------------------------------------------------------------
echo
echo "== results ($role) =="
column -t -s, "$summary" 2>/dev/null || cat "$summary"
echo "logs: $outdir"
