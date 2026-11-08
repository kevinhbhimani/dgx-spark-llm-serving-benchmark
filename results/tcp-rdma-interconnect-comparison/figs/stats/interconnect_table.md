| primitive | transport | runs | latency @ smallest (µs) | bandwidth @ largest (GB/s) |
|---|---|--:|--:|--:|
| allreduce | RoCE / RDMA | 1 | 44.0 | 21.18 |
| allreduce | TCP sockets | 2 | 253.0 | 2.12 |
| sendrecv | RoCE / RDMA | 1 | 113.7 | 24.09 |
| sendrecv | TCP sockets | 2 | 91.7 | 3.05 |
