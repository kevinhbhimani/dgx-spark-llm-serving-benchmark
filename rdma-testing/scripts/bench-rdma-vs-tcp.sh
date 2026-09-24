#!/usr/bin/env bash
# =============================================================================
# rdma-testing/scripts/bench-rdma-vs-tcp.sh
#
# Run on the HEAD node (Diya). Requires the companion listener running on the
# WORKER node (Hoedus) — see instructions below.
#
# This script runs a battery of raw RDMA benchmarks (ib_write_bw, ib_read_bw,
# ib_send_bw, ib_send_lat) AND compares them to TCP equivalents (iperf3, qperf).
#
# USAGE:
#   # On Hoedus FIRST (separate terminal/SSH):
#     cd rdma-testing/scripts
#     bash bench-rdma-vs-tcp.sh server
#
#   # Then on Diya:
#     cd rdma-testing/scripts
#     bash bench-rdma-vs-tcp.sh client
#
#   # Or run both comparison modes:
#     bash bench-rdma-vs-tcp.sh compare   # runs TCP then RDMA, prints summary
#
# Results are written to rdma-testing/results/ with timestamps.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${HERE}/../results"
mkdir -p "$OUT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

# ---- Configuration ----
# These should match your cluster. Override via env or edit here.
WORKER_IP="${WORKER_IP:-10.10.10.2}"
HEAD_IP="${HEAD_IP:-10.10.10.1}"
RDMA_DEV="${RDMA_DEV:-mlx5_0}"
TEST_DURATION="${TEST_DURATION:-10}"
MSG_SIZE="${MSG_SIZE:-1048576}"  # 1 MiB for bandwidth tests

# ---- Determine role ----
ROLE="${1:-}"
if [[ "$ROLE" != "server" && "$ROLE" != "client" && "$ROLE" != "compare" ]]; then
    echo "Usage: $0 [server|client|compare]"
    echo ""
    echo "  server  — Start RDMA/TCP listeners (run this FIRST on Hoedus)"
    echo "  client  — Run benchmarks from Diya talking to Hoedus"
    echo "  compare — Run TCP then RDMA benchmarks and print a comparison"
    echo ""
    echo "Env vars: WORKER_IP=$WORKER_IP HEAD_IP=$HEAD_IP RDMA_DEV=$RDMA_DEV"
    exit 1
fi

check_tool() {
    if ! command -v "$1" &>/dev/null; then
        echo -e "${RED}[ERROR]${NC} '$1' not found. Install it:"
        case "$1" in
            ib_write_bw|ib_read_bw|ib_send_bw|ib_send_lat)
                echo "  sudo apt install perftest" ;;
            iperf3)
                echo "  sudo apt install iperf3" ;;
            qperf)
                echo "  sudo apt install qperf" ;;
        esac
        exit 1
    fi
}

server_mode() {
    echo "=== RDMA + TCP Listener (Hoedus) ==="
    echo "Starting listeners — keep this terminal open."
    echo ""

    check_tool ib_write_bw

    echo -e "${CYAN}Starting RDMA bandwidth listener on $RDMA_DEV...${NC}"
    echo "(run client on Diya; press Ctrl+C when done)"
    echo ""

    # Start all RDMA listeners in background
    ib_write_bw -d "$RDMA_DEV" -a -F --report_gbits -D "$TEST_DURATION" &
    pid_wbw=$!
    ib_read_bw  -d "$RDMA_DEV" -a -F --report_gbits -D "$TEST_DURATION" &
    pid_rbw=$!
    ib_send_bw  -d "$RDMA_DEV" -a -F --report_gbits -D "$TEST_DURATION" &
    pid_sbw=$!
    ib_send_lat -d "$RDMA_DEV" -a -F &
    pid_slat=$!

    # TCP listener for comparison
    if command -v iperf3 &>/dev/null; then
        iperf3 -s -p 5201 &
        pid_iperf=$!
        echo -e "${GREEN}iperf3 TCP listener on port 5201${NC}"
    fi

    echo ""
    echo "All listeners started. Press Enter to stop all..."
    read -r

    kill $pid_wbw $pid_rbw $pid_sbw $pid_slat ${pid_iperf:-} 2>/dev/null || true
    wait 2>/dev/null || true
    echo "Listeners stopped."
}

run_rdma_bench() {
    local bench="$1" desc="$2"
    local out="$OUT_DIR/rdma_${bench}_$(date +%Y%m%d-%H%M%S).log"
    echo -e "${CYAN}--- RDMA $desc ($bench) ---${NC}"
    if "$bench" -d "$RDMA_DEV" -a -F --report_gbits -D "$TEST_DURATION" \
        "$WORKER_IP" 2>&1 | tee "$out"; then
        echo -e "${GREEN}  OK → $out${NC}"
    else
        echo -e "${RED}  FAILED${NC}"
    fi
    echo ""
}

run_tcp_bench() {
    local out="$OUT_DIR/tcp_iperf3_$(date +%Y%m%d-%H%M%S).log"
    echo -e "${CYAN}--- TCP Bandwidth (iperf3) ---${NC}"
    if command -v iperf3 &>/dev/null; then
        if iperf3 -c "$WORKER_IP" -p 5201 -t "$TEST_DURATION" -P 4 2>&1 | tee "$out"; then
            echo -e "${GREEN}  OK → $out${NC}"
        else
            echo -e "${RED}  FAILED — is iperf3 -s running on Hoedus?${NC}"
        fi
    else
        echo -e "${YELLOW}  SKIP — iperf3 not installed${NC}"
    fi
    echo ""

    echo -e "${CYAN}--- TCP Latency (ping) ---${NC}"
    ping -c 10 "$WORKER_IP" | tail -1
    echo ""
}

client_mode() {
    echo "=== RDMA + TCP Client (Diya) ==="
    echo "Target: $WORKER_IP  Device: $RDMA_DEV"
    echo ""

    # Check tools
    for t in ib_write_bw ib_read_bw ib_send_bw ib_send_lat; do
        check_tool "$t"
    done

    run_rdma_bench ib_write_bw "RDMA Write BW"
    run_rdma_bench ib_read_bw  "RDMA Read BW"
    run_rdma_bench ib_send_bw  "RDMA Send BW"
    run_rdma_bench ib_send_lat "RDMA Send Latency"
    run_tcp_bench

    echo -e "${GREEN}All benchmarks complete. Results in $OUT_DIR/${NC}"
}

compare_mode() {
    local ts
    ts=$(date +%Y%m%d-%H%M%S)
    local report="$OUT_DIR/rdma-vs-tcp-comparison_${ts}.md"

    echo "=== RDMA vs TCP Comparison ==="
    echo "Ensure listeners are running on Hoedus: bash $0 server"
    echo ""

    # ---- TCP Baseline ----
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  PHASE 1: TCP BASELINE${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local tcp_bw="N/A"
    if command -v iperf3 &>/dev/null; then
        tcp_bw=$(iperf3 -c "$WORKER_IP" -p 5201 -t "$TEST_DURATION" -P 4 -f m 2>&1 \
            | grep -oP 'sender.*\K[0-9.]+ Mbits/sec' | tail -1 || echo "N/A")
    fi
    local tcp_lat
    tcp_lat=$(ping -c 5 "$WORKER_IP" 2>/dev/null | grep rtt | awk -F'/' '{print $5}' || echo "N/A")

    echo "TCP bandwidth: $tcp_bw"
    echo "TCP latency (ping): $tcp_lat ms"
    echo ""

    # ---- RDMA Benchmarks ----
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  PHASE 2: RDMA/RoCE${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local rdma_write_bw="N/A" rdma_read_bw="N/A"
    local rdma_send_bw="N/A" rdma_send_lat="N/A"

    for bench in ib_write_bw ib_read_bw ib_send_bw ib_send_lat; do
        if ! command -v "$bench" &>/dev/null; then
            echo -e "${YELLOW}$bench not found — skipping${NC}"
            continue
        fi
        local desc="${bench#ib_}"
        local raw
        raw=$("$bench" -d "$RDMA_DEV" -a -F --report_gbits -D "$TEST_DURATION" \
            "$WORKER_IP" 2>&1) || true
        # Extract the last line's bandwidth or latency
        case "$bench" in
            ib_send_lat)
                rdma_send_lat=$(echo "$raw" | grep -E '^\s+[0-9]' | tail -1 | awk '{print $2}' || echo "N/A")
                echo "RDMA send latency: ${rdma_send_lat} µs"
                ;;
            ib_write_bw)
                rdma_write_bw=$(echo "$raw" | grep -E '^\s+[0-9]' | tail -1 | awk '{print $4}' || echo "N/A")
                echo "RDMA write BW: ${rdma_write_bw} Gbps"
                ;;
            ib_read_bw)
                rdma_read_bw=$(echo "$raw" | grep -E '^\s+[0-9]' | tail -1 | awk '{print $4}' || echo "N/A")
                echo "RDMA read BW: ${rdma_read_bw} Gbps"
                ;;
            ib_send_bw)
                rdma_send_bw=$(echo "$raw" | grep -E '^\s+[0-9]' | tail -1 | awk '{print $4}' || echo "N/A")
                echo "RDMA send BW: ${rdma_send_bw} Gbps"
                ;;
        esac
    done
    echo ""

    # ---- Generate Report ----
    {
        echo "# RDMA vs TCP Comparison Report"
        echo ""
        echo "**Date**: $(date)"
        echo "**Nodes**: $HEAD_IP ↔ $WORKER_IP"
        echo "**NIC**: $RDMA_DEV (ConnectX-7, 200GbE)"
        echo "**Test duration**: ${TEST_DURATION}s per benchmark"
        echo ""
        echo "## Summary"
        echo ""
        echo "| Metric | TCP | RDMA/RoCE | Improvement |"
        echo "|--------|-----|-----------|-------------|"
        echo "| Bandwidth | ${tcp_bw} | ${rdma_write_bw} | — |"
        echo "| Latency | ${tcp_lat} ms | ${rdma_send_lat} µs | — |"
        echo ""
        echo "## Raw RDMA Results"
        echo ""
        echo "- RDMA Write BW: ${rdma_write_bw}"
        echo "- RDMA Read BW: ${rdma_read_bw}"
        echo "- RDMA Send BW: ${rdma_send_bw}"
        echo "- RDMA Send Latency: ${rdma_send_lat} µs"
        echo ""
        echo "## Raw TCP Results"
        echo ""
        echo "- iperf3 BW: ${tcp_bw}"
        echo "- ping RTT: ${tcp_lat} ms"
    } > "$report"

    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  Report: $report${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

case "$ROLE" in
    server)  server_mode ;;
    client)  client_mode ;;
    compare) compare_mode ;;
esac