# perftest-suite

RDMA perftest suite for two DGX Sparks joined by one QSFP cable.
`host` = perftest server. `client` = perftest client. Run both concurrently.

## Topology

| dev            | subnet         | server IP (client target) |
|----------------|----------------|---------------------------|
| rocep1s0f0     | 10.10.10.0/24  | 10.10.10.1                |
| roceP2p1s0f0   | 10.10.11.0/24  | 10.10.11.1                |

## Prereqs

```
apt install perftest
ibdev2netdev            # devices present and UP
```
Identical perftest version on both nodes.

## Run

host:
```
./perftest-suite.sh host
```
client:
```
./perftest-suite.sh client
```
Client sleeps `INITIAL_DELAY`, then walks the same test/port sequence as host.

## Tests

Per device (2 devices = 22 runs), sequential, no cross-talk.

```
ib_send_lat   ib_write_lat   ib_read_lat   ib_atomic_lat
ib_send_bw    ib_write_bw    ib_read_bw    ib_atomic_bw
ib_send_bw -b ib_write_bw -b ib_read_bw -b
```

## Env

| var           | default                        | meaning                          |
|---------------|--------------------------------|----------------------------------|
| `DEVS`        | `rocep1s0f0 roceP2p1s0f0`      | local RDMA devices               |
| `SERVER_IPS`  | `10.10.10.1 10.10.11.1`        | client targets, paired to `DEVS` |
| `BASE_PORT`   | `18515`                        | first TCP port; +1 per run       |
| `CONN`        | `RC`                           | `-c` connection type             |
| `LAT_ITERS`   | `1000`                         | `-n` iterations for `*_lat`      |
| `BW_SECS`     | `10`                           | `-D` duration for `*_bw`         |
| `RETRIES`     | `3`                            | client connect retries           |
| `INITIAL_DELAY` | `3`                          | client startup delay (s)         |
| `GAP`         | `1`                            | sleep between runs (s)           |
| `OUTDIR`      | `./perftest-results/<ts>`      | output dir                       |

## Output

- `$OUTDIR/<tag>.log` — raw perftest stdout for each run.
- `$OUTDIR/summary.<role>.csv` — `tag,test,rc,metric,size_bytes,value`.

metric is `bw_avg_MBps_max` (max BW average across sizes) for `*_bw`, or
`t_typical_usec_min` (min typical latency across sizes) for `*_lat`; `size_bytes`
is the message size at which it occurred.

## Notes

- `-c UD` has no read tests; `ib_read_*` skipped when `CONN` is not `RC`/`UC`.
- Both roles must use identical `CONN`/`DEVS`/ordering; the script enforces order.
