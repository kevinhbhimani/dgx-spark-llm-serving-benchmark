#!/usr/bin/env bash
set -uo pipefail

# ---------------------------------------------------------------------------
# perftest-suite.sh - RDMA perftest matrix for two DGX Sparks joined by one or
# more QSFP cables. Each physical QSFP port carries 2 logical RDMA devices.
# Every RDMA device that is UP with an IPv4 address is tested.
#
#   ./perftest-suite.sh host --run-name <campaign>/run-001
#   ./perftest-suite.sh client --run-name <campaign>/run-001
#
#   --run-name <name>  relative path under the results root; pass the SAME
#                      value on both machines so the two sides land in
#                      matching host/ and client/ subdirectories
#   --notes <file>     copy a free-form notes sheet into the run directory
#
# NOTE!! This script currently targets cisc-diya and cisc-hoedus, whose netplan
# binds 10.10.10.1/10.10.11.1 (diya) and 10.10.10.2/10.10.11.2 (hoedus), with
# further cables following the same pattern (10.10.12.x, 10.10.13.x, ...).
# Devices are discovered at runtime, so any RDMA device that is UP with an IPv4
# address is tested. See the pairing rule in derive_peer_ip below.
#
#
# USB-C USB-C USB-C USB-C HDMI ETHERNET CONNECTX-7 CONNECTX-7
#                                       ^^^^
#                                       THIS PORT!
#
#
#
# Output per run (see README.md for the full schema):
#
#   <results-root>/<run-name>/<role>/
#     environment/                  hardware and link-state capture
#     commands.txt                  every test's host and client command
#     results.csv                   parsed perftest tables, GB/s and usec
#     <test>[_bidi]_<device>.log    raw perftest output, never discarded
#     notes.<name>                  the --notes file, if one was given
# ---------------------------------------------------------------------------

# `set -e` is deliberately not used: environment probes (ethtool, lspci,
# dpkg-query, ...) are best-effort and record what is available instead of
# aborting the run. Failure points that matter (directory creation, CSV
# writes, invalid input) are checked explicitly.

readonly RESULTS_ROOT_DEFAULT="./perftest-results"
readonly BASE_PORT_DEFAULT=18515
readonly CONNECTION_TYPE_DEFAULT="RC"
readonly LATENCY_ITERATIONS_DEFAULT=15000
readonly BANDWIDTH_ITERATIONS_DEFAULT=15000
readonly RETRIES_DEFAULT=3
readonly INITIAL_DELAY_DEFAULT=3
# Run names are relative paths whose segments must start with an alphanumeric
# character and contain only [A-Za-z0-9._-]. This rejects absolute paths, '..'
# traversal, and spaces, so --run-name can never write outside the results root.
readonly RUN_NAME_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$'
# perftest --report_gbits prints decimal Gbit/s; GB/s = Gbit/s / 8.
readonly GBITS_PER_GBYTE=8

readonly LATENCY_TESTS=(ib_send_lat ib_write_lat ib_read_lat ib_atomic_lat)
readonly BANDWIDTH_TESTS=(ib_send_bw ib_write_bw ib_read_bw ib_atomic_bw)
readonly BANDWIDTH_BIDI_TESTS=(ib_send_bw ib_write_bw ib_read_bw)

readonly CSV_HEADER="test,device,role,bidi,size_bytes,bw_peak_GBps,bw_avg_GBps,msg_rate_Mpps,lat_min_usec,lat_max_usec,lat_typical_usec,lat_avg_usec,lat_stdev_usec,lat_p99_usec,lat_p999_usec,error"

usage() {
  echo "usage: $0 {host|client} [--run-name <name>] [--notes <file>]" >&2
}

fail() {
  echo "error: $*" >&2
  exit 1
}

# Print the peer IP for a local IP by flipping the last octet odd<->even.
# Matches the netplan scheme (diya = odd, hoedus = even, same /24). This is a
# convention, not an invariant: if the addressing changes, update this rule.
derive_peer_ip() {
  local address="$1"
  local prefix="${address%.*}"
  local last_octet="${address##*.}"
  if (( last_octet % 2 == 1 )); then
    echo "$prefix.$((last_octet + 1))"
  else
    echo "$prefix.$((last_octet - 1))"
  fi
}

# Fill devices[]/local_ips[]/netdevs[] with every RDMA device that is UP and
# has an IPv4 address. DOWN / address-less devices are logged and skipped.
discover_devices() {
  devices=()
  local_ips=()
  netdevs=()

  local device netdev address state
  for device in /sys/class/infiniband/*; do
    device=$(basename "$device")

    # The RDMA device name (rocep1s0f0) differs from its netdev name
    # (enp1s0f0np0); the sysfs symlink bridges the two.
    netdev=$(find "/sys/class/infiniband/$device/device/net/" -mindepth 1 -maxdepth 1 \
             -printf '%f\n' 2>/dev/null | head -n1)
    if [ -z "$netdev" ]; then
      echo "skipping $device: no netdev found" >&2
      continue
    fi

    address=$(ip -j addr show dev "$netdev" 2>/dev/null \
              | jq -r '.[0].addr_info[]? | select(.family=="inet") | .local' \
              | head -n1)
    state=$(ip -j addr show dev "$netdev" 2>/dev/null \
            | jq -r '.[0].operstate // "UNKNOWN"')

    if [ -z "$address" ]; then
      echo "skipping $device ($netdev): no IPv4 address" >&2
      continue
    fi
    if [ "$state" != "UP" ]; then
      echo "skipping $device ($netdev, $address): state=$state" >&2
      continue
    fi

    devices+=("$device")
    local_ips+=("$address")
    netdevs+=("$netdev")
  done
}

validate_configuration() {
  [[ "$base_port" =~ ^[0-9]+$ ]] && (( 10#$base_port >= 1 && 10#$base_port <= 65535 )) \
    || fail "BASE_PORT must be an integer in 1..65535 (got '$base_port')"
  [[ "$latency_iterations" =~ ^[0-9]+$ ]] && (( 10#$latency_iterations >= 1 )) \
    || fail "LAT_ITERS must be a positive integer (got '$latency_iterations')"
  # perftest disables BW peak computation above 20000 iterations.
  [[ "$bandwidth_iterations" =~ ^[0-9]+$ ]] && (( 10#$bandwidth_iterations >= 1 && 10#$bandwidth_iterations <= 20000 )) \
    || fail "BW_ITERS must be an integer in 1..20000 (got '$bandwidth_iterations')"
  [[ "$retries" =~ ^[0-9]+$ ]] && (( 10#$retries >= 1 )) \
    || fail "RETRIES must be a positive integer (got '$retries')"
  [[ "$initial_delay" =~ ^[0-9]+$ ]] \
    || fail "INITIAL_DELAY must be a non-negative integer (got '$initial_delay')"
  case "$connection_type" in
    RC|UC|UD|XRC|DC) ;;
    *) fail "CONN must be one of RC, UC, UD, XRC, DC (got '$connection_type')" ;;
  esac
}

preflight() {
  local missing=""
  local binary
  for binary in "${LATENCY_TESTS[@]}" "${BANDWIDTH_TESTS[@]}"; do
    command -v "$binary" >/dev/null 2>&1 || missing="$missing $binary"
  done
  local tool
  for tool in ip jq ss; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if [ -n "$missing" ]; then
    fail "missing required tools:$missing"
  fi

  # Optional: their absence degrades the environment capture, not the tests.
  for tool in ethtool lspci ibv_devinfo; do
    command -v "$tool" >/dev/null 2>&1 \
      || echo "warning: $tool not installed; environment capture will be incomplete" >&2
  done

  local device
  for device in "${devices[@]}"; do
    [ -e "/sys/class/infiniband/$device" ] || fail "device disappeared: $device"
  done

  local tests_per_device=$(( ${#LATENCY_TESTS[@]} + ${#BANDWIDTH_TESTS[@]} + ${#BANDWIDTH_BIDI_TESTS[@]} ))
  local port_count=$(( ${#devices[@]} * tests_per_device ))
  local port
  for (( port = base_port; port < base_port + port_count; port++ )); do
    if ss -ltn 2>/dev/null | grep -q ":$port "; then
      fail "port $port already in use; kill stale ib_* servers first"
    fi
  done
}

# ---------------------------------------------------------------------------
# Environment capture: link state and hardware, so every run carries the
# information needed to interpret its numbers (link speed, PCIe width/speed,
# MTU, driver/firmware/software versions). Raw tool output is kept verbatim
# alongside a parsed JSON summary. Missing tools/fields record empty strings.
# ---------------------------------------------------------------------------
capture_environment() {
  local out_dir="$1"
  local environment_dir="$out_dir/environment"
  mkdir -p "$environment_dir" || fail "cannot create $environment_dir"

  capture_system_environment "$environment_dir"

  echo "link state (device | local -> peer | link speed | pcie | mtu):"
  local index
  for index in "${!devices[@]}"; do
    capture_device_environment "$environment_dir" \
      "${devices[$index]}" "${netdevs[$index]}" "${local_ips[$index]}" "${peer_ips[$index]}"
  done
}

capture_system_environment() {
  local environment_dir="$1"

  local hostname_value kernel distro perftest_version rdma_core_version captured_at
  hostname_value=$(hostname)
  kernel=$(uname -r)
  distro=$(lsb_release -ds 2>/dev/null || true)
  perftest_version=$(dpkg-query -W -f='${Version}' perftest 2>/dev/null || true)
  rdma_core_version=$(dpkg-query -W -f='${Version}' rdma-core 2>/dev/null || true)
  captured_at=$(date -Is)

  jq -n \
    --arg hostname "$hostname_value" \
    --arg role "$role" \
    --arg kernel "$kernel" \
    --arg distro "$distro" \
    --arg perftest_version "$perftest_version" \
    --arg rdma_core_version "$rdma_core_version" \
    --arg captured_at "$captured_at" \
    '{hostname: $hostname,
      role: $role,
      kernel: $kernel,
      distro: $distro,
      perftest_version: $perftest_version,
      rdma_core_version: $rdma_core_version,
      captured_at: $captured_at}' \
    > "$environment_dir/system.json" || fail "cannot write $environment_dir/system.json"
}

capture_device_environment() {
  local environment_dir="$1" device="$2" netdev="$3" local_ip="$4" peer_ip="$5"
  local device_dir="$environment_dir/$device"
  mkdir -p "$device_dir" || fail "cannot create $device_dir"

  # Raw tool dumps: what cannot be parsed now can still be read later.
  if command -v ethtool >/dev/null 2>&1; then
    ethtool "$netdev" > "$device_dir/ethtool.txt" 2>&1 || true
    ethtool -i "$netdev" > "$device_dir/ethtool-i.txt" 2>&1 || true
  fi
  if command -v lspci >/dev/null 2>&1; then
    local pci_address
    pci_address=$(basename "$(readlink -f "/sys/class/infiniband/$device/device")" 2>/dev/null || true)
    if [[ "$pci_address" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$ ]]; then
      lspci -vv -s "$pci_address" > "$device_dir/lspci.txt" 2>&1 || true
    fi
  fi
  if command -v ibv_devinfo >/dev/null 2>&1; then
    ibv_devinfo -v "$device" > "$device_dir/ibv_devinfo.txt" 2>&1 || true
  fi

  local link_speed_mbps="" autoneg="" link_detected="" mtu="" pcie_speed="" pcie_width=""
  local driver="" driver_version="" firmware_version=""

  if [ -f "$device_dir/ethtool.txt" ]; then
    link_speed_mbps=$(awk '/^[[:space:]]*Speed:/ { gsub(/[^0-9]/, "", $2); if ($2 ~ /^[0-9]+$/) print $2; exit }' "$device_dir/ethtool.txt")
    autoneg=$(awk '/^[[:space:]]*Auto-negotiation:/ { print $2; exit }' "$device_dir/ethtool.txt")
    link_detected=$(awk '/^[[:space:]]*Link detected:/ { print $3; exit }' "$device_dir/ethtool.txt")
  fi
  if [ -f "$device_dir/ethtool-i.txt" ]; then
    driver=$(awk '/^driver:/ { sub(/^driver:[[:space:]]*/, ""); print; exit }' "$device_dir/ethtool-i.txt")
    driver_version=$(awk '/^version:/ { sub(/^version:[[:space:]]*/, ""); print; exit }' "$device_dir/ethtool-i.txt")
    firmware_version=$(awk '/^firmware-version:/ { sub(/^firmware-version:[[:space:]]*/, ""); print; exit }' "$device_dir/ethtool-i.txt")
  fi
  if command -v ip >/dev/null 2>&1; then
    mtu=$(ip -j link show dev "$netdev" 2>/dev/null | jq -r '.[0].mtu // empty' 2>/dev/null || true)
  fi
  if [ -f "$device_dir/lspci.txt" ]; then
    local pcie_line
    pcie_line=$(awk '
      /^[[:space:]]*LnkSta:/ {
        if (match($0, /Speed [0-9.]+GT\/s/)) {
          pcie_speed = substr($0, RSTART, RLENGTH)
          sub(/^Speed /, "", pcie_speed)
        }
        if (match($0, /Width x[0-9]+/)) {
          pcie_width = substr($0, RSTART, RLENGTH)
          sub(/^Width /, "", pcie_width)
        }
        print pcie_speed "|" pcie_width
        exit
      }' "$device_dir/lspci.txt")
    pcie_speed=${pcie_line%%|*}
    pcie_width=${pcie_line##*|}
  fi

  jq -n \
    --arg device "$device" \
    --arg netdev "$netdev" \
    --arg local_ip "$local_ip" \
    --arg peer_ip "$peer_ip" \
    --argjson mtu "${mtu:-null}" \
    --argjson link_speed_mbps "${link_speed_mbps:-null}" \
    --arg autoneg "$autoneg" \
    --arg link_detected "$link_detected" \
    --arg pcie_speed "$pcie_speed" \
    --arg pcie_width "$pcie_width" \
    --arg driver "$driver" \
    --arg driver_version "$driver_version" \
    --arg firmware_version "$firmware_version" \
    '{device: $device,
      netdev: $netdev,
      local_ip: $local_ip,
      peer_ip: $peer_ip,
      mtu: $mtu,
      link_speed_mbps: $link_speed_mbps,
      autoneg: $autoneg,
      link_detected: $link_detected,
      pcie_speed: $pcie_speed,
      pcie_width: $pcie_width,
      driver: $driver,
      driver_version: $driver_version,
      firmware_version: $firmware_version}' \
    > "$device_dir/link.json" || fail "cannot write $device_dir/link.json"

  local speed_display="${link_speed_mbps:+${link_speed_mbps}Mb/s}"
  printf '  %-15s | %s -> %s | %s | %s | mtu %s\n' \
    "$device" "$local_ip" "$peer_ip" "${speed_display:-unknown}" \
    "${pcie_speed:-unknown} ${pcie_width:-unknown}" "${mtu:-unknown}"
}

# ---------------------------------------------------------------------------
# perftest table parsing. perftest prints (with --report_gbits):
#   *_bw : size, iters, BW peak[Gb/sec], BW average[Gb/sec], MsgRate[Mpps]
#   *_lat: size, iters, t_min, t_max, t_typical, [t_avg, t_stdev, p99, p999]
# Only rows whose numeric columns all parse as numbers are emitted; header and
# trailer lines are skipped by construction. An empty result is reported by
# the caller as a failure row, so bad output is never silently absorbed.
# ---------------------------------------------------------------------------
parse_perftest_log() {
  local log_file="$1" kind="$2" binary="$3" device="$4" bidi="$5"

  if [ "$kind" = "bw" ]; then
    awk -v test="$binary" -v device="$device" -v role="$role" -v bidi="$bidi" \
        -v divisor="$GBITS_PER_GBYTE" '
      function is_numeric(value) {
        return value ~ /^[0-9]+(\.[0-9]+)?$/
      }
      $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ &&
        is_numeric($3) && is_numeric($4) && is_numeric($5) {
        printf "%s,%s,%s,%s,%s,%.3f,%.3f,%.3f,,,,,,,,\n",
          test, device, role, bidi, $1, $3 / divisor, $4 / divisor, $5
      }' "$log_file"
  else
    awk -v test="$binary" -v device="$device" -v role="$role" -v bidi="$bidi" '
      function is_numeric(value) {
        return value ~ /^[0-9]+(\.[0-9]+)?$/
      }
      $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ &&
        is_numeric($3) && is_numeric($4) && is_numeric($5) {
        average = (NF >= 6 && is_numeric($6)) ? $6 : ""
        stdev = (NF >= 7 && is_numeric($7)) ? $7 : ""
        p99 = (NF >= 8 && is_numeric($8)) ? $8 : ""
        p999 = (NF >= 9 && is_numeric($9)) ? $9 : ""
        printf "%s,%s,%s,%s,%s,,,,%s,%s,%s,%s,%s,%s,%s,\n",
          test, device, role, bidi, $1, $3, $4, $5, average, stdev, p99, p999
      }' "$log_file"
  fi
}

append_error_row() {
  local binary="$1" device="$2" bidi="$3" message="$4"
  local sanitized_message=${message//,/;}
  sanitized_message=${sanitized_message//$'\n'/ }

  # Columns 1..4 identify the test, 5..15 hold no measurement, 16 is the error.
  local -a row_fields=("$binary" "$device" "$role" "$bidi"
                      "" "" "" "" "" "" "" "" "" "" ""
                      "$sanitized_message")
  local row="" separator="" field
  for field in "${row_fields[@]}"; do
    row+="$separator$field"
    separator=","
  done
  printf '%s\n' "$row" >> "$csv_file"
}

run_test() {
  local binary="$1" extra_flag="$2" device="$3" server_ip="$4" port="$5"

  local kind iterations
  if [[ "$binary" == *_lat ]]; then
    kind="lat"
    iterations="$latency_iterations"
  else
    kind="bw"
    iterations="$bandwidth_iterations"
  fi

  local -a command_arguments=(-d "$device" -i 1 -F -p "$port" -c "$connection_type" -n "$iterations" --report_gbits)
  local bidi="false"
  if [ -n "$extra_flag" ]; then
    command_arguments+=("$extra_flag")
    bidi="true"
  fi

  local host_command="$binary ${command_arguments[*]}"
  local client_command="$binary ${command_arguments[*]} $server_ip"
  local local_command="$host_command"
  [ "$role" = client ] && local_command="$client_command"

  local tag="${binary}${extra_flag:+_bidi}_${device}"
  local log_file="$out_dir/$tag.log"

  # Header makes the log self-describing and records both sides' commands.
  {
    echo "# test   : ${binary}${extra_flag:+ ($extra_flag)}"
    echo "# device : $device"
    echo "# port   : $port"
    echo "# host   : $host_command"
    echo "# client : $client_command"
    echo "# ---"
  } > "$log_file" || fail "cannot write $log_file"

  echo ">>> [$role] $local_command"

  local exit_code=1 attempt
  for attempt in $(seq 1 "$retries"); do
    if [ "$role" = client ]; then
      "$binary" "${command_arguments[@]}" "$server_ip" >> "$log_file" 2>&1
      exit_code=$?
    else
      "$binary" "${command_arguments[@]}" >> "$log_file" 2>&1
      exit_code=$?
    fi
    [ "$exit_code" -eq 0 ] && break
    sleep 1
  done

  if [ "$exit_code" -ne 0 ]; then
    echo "!! $tag failed rc=$exit_code" >&2
    append_error_row "$binary" "$device" "$bidi" "perftest rc=$exit_code"
  else
    parse_perftest_log "$log_file" "$kind" "$binary" "$device" "$bidi" > "$parse_tmp_file"
    if [ -s "$parse_tmp_file" ]; then
      cat "$parse_tmp_file" >> "$csv_file"
      rm -f "$parse_tmp_file"
    else
      echo "!! $tag produced no parseable rows" >&2
      append_error_row "$binary" "$device" "$bidi" "no parseable rows in perftest output"
      rm -f "$parse_tmp_file"
    fi
  fi

  {
    echo "$tag"
    echo "  host   : $host_command"
    echo "  client : $client_command"
  } >> "$commands_file"

  # Make sure this test's server is gone before the next test reuses a port.
  pkill -f "^$binary " 2>/dev/null || true
  sleep 1
}

print_results_summary() {
  echo
  echo "== results ($role) =="
  echo "metric,test,device,value,at_size_bytes" | column -t -s','
  awk -F',' '
    NR == 1 { next }
    $16 != "" { next }
    {
      key = $1 "," $2
      if ($6 != "") {
        if ($6 + 0 > peak[key]) {
          peak[key] = $6 + 0
          peak_size[key] = $5
        }
      } else if ($11 != "") {
        if (typical[key] == "" || $11 + 0 < typical[key]) {
          typical[key] = $11 + 0
          typical_size[key] = $5
        }
      }
    }
    END {
      for (key in peak) {
        printf "BW,%s,%.3f,%s\n", key, peak[key], peak_size[key]
      }
      for (key in typical) {
        printf "LAT,%s,%.3f,%s\n", key, typical[key], typical_size[key]
      }
    }' "$csv_file" \
  | sort -t',' -k2,2 -k1,1 \
  | column -t -s','
  echo "results: $out_dir/results.csv"
}

# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------
role=""
run_name=""
notes_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    host|client)
      role="$1"
      ;;
    --run-name)
      [ "$#" -ge 2 ] || fail "--run-name requires an argument"
      run_name="$2"
      shift
      ;;
    --notes)
      [ "$#" -ge 2 ] || fail "--notes requires an argument"
      notes_file="$2"
      shift
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

if [ -z "$role" ]; then
  usage
  exit 2
fi
if [ -n "$run_name" ] && [[ ! "$run_name" =~ $RUN_NAME_PATTERN ]]; then
  fail "invalid run name '$run_name': use relative path segments of letters, digits, '.', '_' and '-' (no leading dot, no '..', no spaces)"
fi

results_root=${OUTDIR:-$RESULTS_ROOT_DEFAULT}
base_port=${BASE_PORT:-$BASE_PORT_DEFAULT}
connection_type=${CONN:-$CONNECTION_TYPE_DEFAULT}
latency_iterations=${LAT_ITERS:-$LATENCY_ITERATIONS_DEFAULT}
bandwidth_iterations=${BW_ITERS:-$BANDWIDTH_ITERATIONS_DEFAULT}   # keep <= 20000, else perftest disables BW peak
retries=${RETRIES:-$RETRIES_DEFAULT}
initial_delay=${INITIAL_DELAY:-$INITIAL_DELAY_DEFAULT}
validate_configuration

discover_devices
if [ "${#devices[@]}" -eq 0 ]; then
  fail "no UP RDMA devices with an IPv4 address were found"
fi

peer_ips=()
for index in "${!local_ips[@]}"; do
  peer_ips+=("$(derive_peer_ip "${local_ips[$index]}")")
done

if [ -n "$run_name" ]; then
  out_dir="$results_root/$run_name/$role"
else
  # Ad-hoc run without --run-name: timestamped so repeated runs never collide.
  out_dir="$results_root/$(date +%Y%m%d-%H%M%S)/$role"
fi
if [ -e "$out_dir" ]; then
  fail "output directory already exists: $out_dir (choose a new --run-name; existing data is never overwritten)"
fi

preflight
mkdir -p "$out_dir" || fail "cannot create $out_dir"
csv_file="$out_dir/results.csv"
commands_file="$out_dir/commands.txt"
parse_tmp_file="$out_dir/.parsing.tmp"
trap 'rm -f "$parse_tmp_file"' EXIT

echo "$CSV_HEADER" > "$csv_file" || fail "cannot write $csv_file"
: > "$commands_file" || fail "cannot create $commands_file"

if [ -n "$notes_file" ]; then
  if [ ! -f "$notes_file" ]; then
    fail "notes file not found: $notes_file"
  fi
  cp "$notes_file" "$out_dir/notes.$(basename "$notes_file")" || fail "cannot copy notes file"
  echo "notes: $notes_file -> $out_dir/notes.$(basename "$notes_file")"
fi

echo "role=$role conn=$connection_type out=$out_dir"
echo "discovered devices (device | local_ip | peer_ip):"
for index in "${!devices[@]}"; do
  echo "  ${devices[$index]} | ${local_ips[$index]} | ${peer_ips[$index]}"
done

capture_environment "$out_dir"

if [ "$role" = client ]; then
  echo "targets=${peer_ips[*]}; starting in ${initial_delay}s"
  sleep "$initial_delay"
fi

port="$base_port"
for index in "${!devices[@]}"; do
  device="${devices[$index]}"
  server_ip="${peer_ips[$index]}"

  for test in "${LATENCY_TESTS[@]}"; do
    # UD has no read verbs.
    if [[ "$connection_type" != "RC" && "$connection_type" != "UC" && "$test" == ib_read_* ]]; then
      continue
    fi
    run_test "$test" "" "$device" "$server_ip" "$port"
    port=$((port + 1))
  done
  for test in "${BANDWIDTH_TESTS[@]}"; do
    run_test "$test" "" "$device" "$server_ip" "$port"
    port=$((port + 1))
  done
  for test in "${BANDWIDTH_BIDI_TESTS[@]}"; do
    if [[ "$connection_type" != "RC" && "$connection_type" != "UC" && "$test" == ib_read_* ]]; then
      continue
    fi
    run_test "$test" "-b" "$device" "$server_ip" "$port"
    port=$((port + 1))
  done
done

print_results_summary
