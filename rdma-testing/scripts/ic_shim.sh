#!/usr/bin/env bash

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

HOST0=cisc-diya HOST1=cisc-hoedus NCCL_IB_DISABLE=0 bash "$SCRIPT_DIR"/../scripts/interconnect_microbench.sh

