#!/usr/bin/env bash
# Measure all reduce bandwidth and latency between the two DGX Spark nodes with
# NVIDIA nccl-tests. Run from the head node. Requires MPI and a built nccl-tests.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

HOST0="${HOST0:-$DIYA_HOST}"
HOST1="${HOST1:-$HOEDUS_HOST}"
NCCL_TESTS_DIR="${NCCL_TESTS_DIR:-$HOME/nccl-tests}"
IFACE="${NCCL_SOCKET_IFNAME:-$CX_IFACE}"

if [[ ! -x "$NCCL_TESTS_DIR/build/all_reduce_perf" ]]; then
  echo "[microbench] building nccl-tests in $NCCL_TESTS_DIR"
  if [[ ! -d "$NCCL_TESTS_DIR" ]]; then
    git clone https://github.com/NVIDIA/nccl-tests "$NCCL_TESTS_DIR"
  fi
  if [[ ! -d "$NCCL_TESTS_DIR" ]]; then
    echo "[microbench] failed to create $NCCL_TESTS_DIR" >&2
    exit 1
  fi
  make -C "$NCCL_TESTS_DIR" MPI=1
fi

export NCCL_SOCKET_IFNAME="$IFACE"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"

# two ranks, one GPU per node, sizes from 8 bytes to 1 GiB doubling
mpirun --allow-run-as-root -np 2 -H "${HOST0}:1,${HOST1}:1" \
  -x NCCL_SOCKET_IFNAME -x NCCL_IB_DISABLE -x NCCL_DEBUG \
  "$NCCL_TESTS_DIR/build/all_reduce_perf" -b 8 -e 1G -f 2 -g 1 \
  | tee "allreduce_$(date +%Y%m%d-%H%M%S).log"

echo "[microbench] the busbw column is the effective interconnect bandwidth; record the peak and the latency at 8 bytes"
