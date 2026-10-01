#!/usr/bin/env bash
#
# perftest-suite.sh
# RDMA perftest suite for two DGX Sparks on one QSFP cable.
#
#   host   = perftest server (binds)
#   client = perftest client (connects to SERVER_IPS)
#
# Both roles walk the same test/port sequence.
#
# Usage:
#   ./perftest-suite.sh host
#   ./perftest-suite.sh client
#
set -uo pipefail

ROLE_IN="${1:-}"
case "$ROLE_IN" in
  host|server) ROLE=server ;;
  client)      ROLE=client ;;
  *) echo "usage: $0 {host|client}" >&2; exit 2 ;;
esac

DEVS=(${DEVS:-rocep1s0f0 roceP2p1s0f0})
SERVER_IPS=(${SERVER_IPS:-10.10.10.1 10.10.11.1})
BASE_PORT="${BASE_PORT:-18515}"
CONN="${CONN:-RC}"
LAT_ITERS="${LAT_ITERS:-1000}"
BW_SECS="${BW_SECS:-10}"
RETRIES="${RETRIES:-3}"
INITIAL_DELAY="${INITIAL_DELAY:-3}"
GAP="${GAP:-1}"
OUTDIR="${OUTDIR:-./perftest-results/$(date +%Y%m%d-%H%M%S)}"

# "name [extra args]" — identical order on both roles.
TESTS=(
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

mkdir -p "$OUTDIR" || exit 1
SUM="$OUTDIR/summary.$ROLE.csv"
echo "tag,test,rc,metric,size_bytes,value" >"$SUM"

# --- preflight ---------------------------------------------------------------
fail=0
for spec in "${TESTS[@]}"; do
  read -r name _ <<<"$spec"
  command -v "$name" >/dev/null 2>&1 || { echo "missing binary: $name" >&2; fail=1; }
done
for dev in "${DEVS[@]}"; do
  [ -e "/sys/class/infiniband/$dev" ] || { echo "missing rdma device: $dev" >&2; fail=1; }
done
[ "$fail" -eq 1 ] && exit 1

server_ip_for() {
  local want="$1" i
  for i in "${!DEVS[@]}"; do
    [ "${DEVS[$i]}" = "$want" ] && { printf '%s' "${SERVER_IPS[$i]:-}"; return 0; }
  done
  return 1
}

# Parse perftest table -> summary row.
#   bw : max BW average across sizes
#   lat: min t_typical across sizes
metrics() {
  local logf="$1" name="$2" tag="$3" rc="$4"
  awk -v name="$name" -v tag="$tag" -v rc="$rc" '
    $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {
      size=$1+0
      if (name ~ /_bw$/) { if ($4+0 > bw) { bw=$4+0; bs=size } }
      else { if (tl=="" || ($5+0 > 0 && $5+0 < tl)) { tl=$5+0; ts=size } }
    }
    END {
      if (name ~ /_bw$/) printf "%s,%s,%s,bw_avg_MBps_max,%d,%.2f\n", tag, name, rc, bs, bw
      else               printf "%s,%s,%s,t_typical_usec_min,%d,%.3f\n", tag, name, rc, ts, tl
    }' "$logf" >>"$SUM"
}

run_one() {
  local dev="$1" name="$2" extra="$3" port="$4"
  local tag="${name}${extra:+_bidi}_${dev}"
  local logf="$OUTDIR/$tag.log"

  local -a args=( -d "$dev" -i 1 -F -p "$port" -c "$CONN" )
  case "$name" in
    *_bw)  args+=( -D "$BW_SECS" ) ;;
    *_lat) args+=( -n "$LAT_ITERS" ) ;;
  esac
  [ -n "$extra" ] && args+=( $extra )

  local -a cmd=( "$name" "${args[@]}" )
  if [ "$ROLE" = client ]; then
    local sip
    sip="$(server_ip_for "$dev")" || { echo "no server ip for $dev" >&2; return; }
    cmd+=( "$sip" )
  fi

  echo ">>> $tag port=$port ${cmd[*]}"
  local rc=0
  if [ "$ROLE" = client ]; then
    rc=1
    local attempt
    for attempt in $(seq 1 "$RETRIES"); do
      "${cmd[@]}" >"$logf" 2>&1 && { rc=0; break; }
      rc=$?; sleep 1
    done
  else
    "${cmd[@]}" >"$logf" 2>&1; rc=$?
  fi
  [ "$rc" -ne 0 ] && echo "!! $tag failed rc=$rc" >&2
  metrics "$logf" "$name" "$tag" "$rc"
}

# --- run ---------------------------------------------------------------------
echo "role=$ROLE conn=$CONN devs=${DEVS[*]} out=$OUTDIR"
if [ "$ROLE" = client ]; then
  echo "server=${SERVER_IPS[*]} start in ${INITIAL_DELAY}s"
  sleep "$INITIAL_DELAY"
fi

idx=0
for dev in "${DEVS[@]}"; do
  for spec in "${TESTS[@]}"; do
    read -r name extra <<<"$spec"
    # UD/other transports have no read tests.
    if [ "$CONN" != RC ] && [ "$CONN" != UC ] && [[ "$name" == ib_read_* ]]; then
      continue
    fi
    port=$((BASE_PORT + idx)); idx=$((idx+1))
    run_one "$dev" "$name" "${extra:-}" "$port"
    sleep "$GAP"
  done
done

# --- report ------------------------------------------------------------------
echo
echo "== results ($ROLE) =="
printf '%-30s %-14s %3s %-20s %10s %10s\n' TAG TEST RC METRIC SIZE VALUE
tail -n +2 "$SUM" | awk -F, '{printf "%-30s %-14s %3s %-20s %10s %10s\n",$1,$2,$3,$4,$5,$6}'
echo "logs: $OUTDIR"
