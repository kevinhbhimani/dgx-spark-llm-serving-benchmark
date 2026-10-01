# perftest-suite

RDMA perftest matrix for two DGX Sparks joined by one QSFP cable.
`host` = perftest server (binds). `client` = perftest client. Run both concurrently.

## Topology

| dev            | subnet         | client target IP |
|----------------|----------------|------------------|
| rocep1s0f0     | 10.10.10.0/24  | 10.10.10.1       |
| roceP2p1s0f0   | 10.10.11.0/24  | 10.10.11.1       |

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
Client sleeps `INITIAL_DELAY`, then walks the same test/port order as host.
Each run gets its own port (`BASE_PORT + n`), identical on both sides.

## Tests

Per device (2 devices = 22 runs).

```
ib_send_lat   ib_write_lat   ib_read_lat   ib_atomic_lat
ib_send_bw    ib_write_bw    ib_read_bw    ib_atomic_bw
ib_send_bw -b ib_write_bw -b ib_read_bw -b     (-b = bidirectional)
```

## Env

| var             | default                   | meaning                            |
|-----------------|---------------------------|------------------------------------|
| `DEVS`          | `rocep1s0f0 roceP2p1s0f0` | local RDMA devices                 |
| `SERVER_IPS`    | `10.10.10.1 10.10.11.1`   | client targets, paired to `DEVS`   |
| `BASE_PORT`     | `18515`                   | first TCP port; +1 per run         |
| `CONN`          | `RC`                      | `-c` connection type               |
| `LAT_ITERS`     | `1000`                    | `-n` iterations for `*_lat`        |
| `BW_ITERS`      | `5000`                    | `-n` iterations for `*_bw` (≤20000)|
| `RETRIES`       | `3`                       | client connect retries             |
| `INITIAL_DELAY` | `3`                       | client startup delay (s)           |
| `OUTDIR`        | `./perftest-results/<ts>` | output dir                         |

## Output

- `$OUTDIR/<tag>.log` — header with both roles' exact command, then raw perftest output.
- `$OUTDIR/commands.txt` — every test's host and client command.
- `$OUTDIR/summary.<role>.csv` — columns below.

```
test,device,role,rc,bw_peak_MBps,bw_avg_MBps,lat_typical_usec,size_bytes
```

`bw_peak_MBps`/`bw_avg_MBps` = max across message sizes; `lat_typical_usec` = min
t_typical across sizes. `size_bytes` is where that value occurred.

## BW peak = 0.00

Perftest computes BW peak only in iteration mode with `iters <= 20000`
(`perftest_parameters.c` forces `noPeak` otherwise). In `-D` duration mode peak
is always disabled, so the column prints `0.00`. This suite therefore uses
`-n` (`BW_ITERS`) for bandwidth, not `-D`.

Read/atomic BW measure receive completions on the server; the server-side run
reports `BW peak 0.00` by design — read the peak from the client summary.

## Notes

- `-c UD` has no read verbs; `ib_read_*` skipped when `CONN` is not `RC`/`UC`.
- Both roles must use identical `DEVS`/`CONN`/ordering; the script enforces order.
- `-F` suppresses the perftest cpufreq warning.
