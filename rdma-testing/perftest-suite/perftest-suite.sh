#!/usr/bin/env bash
set -uo pipefail


# NOTE!! This script will currently only work on cisc-diya and cisc-hoedus, with their current
# network configuration (cisc-diya 10.10.10.1 & 10.10.11.1) (cisc-hoedus 10.10.10.2 & 10.10.11.2)
# It also assumes the rocep1s0f0 and roceP2p1s0f0 devices are being used. These map to the 
# LEFT ConnectX-7 port, when looking at the back IO panel on the spark
#
#
# USB-C USB-C USB-C USB-C HDMI ETHERNET CONNECTX-7 CONNECTX-7 
#                                       ^^^^
#                                       THIS PORT!
#
#
#

# ---------------------------------------------------------------------------
# perftest-suite.sh - RDMA perftest matrix for two DGX Sparks joined by one
# QSFP cable (two RDMA devices).
#
#   ./perftest-suite.sh host      # perftest server (binds)
#   ./perftest-suite.sh client    # perftest client (connects to host_ips)
#
# Run both roles concurrently, one on each node. Both walk the same
# test/port order; ports are allocated identically on both sides.
# ---------------------------------------------------------------------------

role="${1:-}"
case "$role" in
  host|client) ;;
  *) echo "usage: $0 {host|client}" >&2; exit 2 ;;
esac

# --- configuration (SEE ABOVE NOTE!!) -------------------------------
devs=(rocep1s0f0 roceP2p1s0f0)     # one RDMA device per QSFP port

diya_dev0_ips=(10.10.10.1 10.10.11.1)
hoedus_dev0_ips=(10.10.10.2 10.10.11.2)


if [[ $role == "client" ]]; then
  case $(hostname) in
    "cisc-diya")
        host_ips=("${hoedus_dev0_ips[@]}") ;;
    "cisc-hoedus")
        host_ips=("${diya_dev0_ips[@]}") ;;
  esac
fi

if [[ $role == "host" ]]; then
  case $(hostname) in
    "cisc-diya")
        host_ips=("${diya_dev0_ips[@]}") ;;
    "cisc-hoedus")
        host_ips=("${hoedus_dev0_ips[@]}") ;;
  esac
fi

#THIS ASSUMES ONLY 2 DEVICES, THIS WILL BREAK WHEN MOVING TO MORE DEVICES AT THE SAME TIME!
if [[ ${#host_ips[@]} -eq 0 || ${#host_ips[@]} -ne 2 ]]; then 
  echo "[ERROR] Role is $role, but host_ips are broken: host_ips:[${host_ips[@]}]"
  exit 3
fi




base_port=${BASE_PORT:-18515}
conn=${CONN:-RC}
lat_iters=${LAT_ITERS:-15000}
bw_iters=${BW_ITERS:-15000}         # keep <=20000, else perftest disables BW peak
initial_delay=${INITIAL_DELAY:-3}
retries=${RETRIES:-3}
outdir=${OUTDIR:-./perftest-results/$(date +%Y%m%d-%H%M%S)}

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
for dev in "${devs[@]}"; do
  [ -e "/sys/class/infiniband/$dev" ] || { echo "missing device: $dev" >&2; missing=1; }
done
[ "$missing" -eq 1 ] && exit 1

mkdir -p "$outdir" || exit 1
commands_file="$outdir/commands.txt"
summary="$outdir/summary.$role.csv"
: > "$commands_file"
echo "test,device,role,rc,bw_peak_MBps,bw_avg_MBps,lat_typical_usec,size_bytes" > "$summary"

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
    "$name" $flags >> "$log" 2>&1
    rc=$?
  fi
  [ "$rc" -ne 0 ] && echo "!! $tag failed rc=$rc" >&2

  {
    echo "$tag"
    echo "  host   : $host_cmd"
    echo "  client : $client_cmd"
  } >> "$commands_file"

  echo "$tag,$dev,$role,$rc,$(parse_metrics "$log" "$kind")" >> "$summary"
}

# --- run --------------------------------------------------------------------
echo "role=$role conn=$conn devs=${devs[*]} out=$outdir"
if [ "$role" = client ]; then
  echo "targets=${host_ips[*]}; starting in ${initial_delay}s"
  sleep "$initial_delay"
fi

port="$base_port"
for i in "${!devs[@]}"; do
  dev="${devs[$i]}"
  server_ip="${host_ips[$i]}"
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
