#!/usr/bin/env bash
# Run raw RDMA bandwidth/latency benchmarks from this node to WORKER_IP.
# Start the server side FIRST on the other node (see comments).
set -euo pipefail

WORKER="${WORKER_IP:-10.10.10.2}"
DEV="${RDMA_DEV:-mlx5_0}"
DUR="${TEST_DURATION:-10}"   # seconds per test
SIZE="${MSG_SIZE:-1048576}"  # 1 MiB

usage() {
    echo "usage: $0 server    (run on worker node FIRST)"
    echo "       $0 client    (run on head node after server is up)"
    exit 1
}

case "${1:-}" in
    server)
        echo "=== RDMA listeners on $DEV ==="
        echo "Keep this running, then run '$0 client' on the other node."
        # Start all four listeners; they exit after one client run each
        ib_write_bw -d "$DEV" -a -F --report_gbits -D "$DUR" &
        ib_read_bw  -d "$DEV" -a -F --report_gbits -D "$DUR" &
        ib_send_bw  -d "$DEV" -a -F --report_gbits -D "$DUR" &
        ib_send_lat -d "$DEV" -a -F &
        wait
        ;;
    client)
        echo "=== RDMA benchmarks → $WORKER ==="
        for bench in ib_write_bw ib_read_bw ib_send_bw ib_send_lat; do
            echo "--- $bench ---"
            "$bench" -d "$DEV" -a -F --report_gbits -D "$DUR" "$WORKER" 2>&1 | tail -5
            echo ""
        done
        ;;
    *) usage ;;
esac