# perftest-suite

RDMA perftest matrix for two DGX Sparks joined by one or more QSFP cables.
`host` = perftest server (binds). `client` = perftest client. Run both concurrently.

## Topology

Each physical QSFP port carries 2 logical RDMA devices. Devices are discovered at
runtime: every RDMA device that is UP and has an IPv4 address is tested, so adding
a cable automatically adds its devices to the run.

The peer's IP is derived from the local IP by flipping the last octet between the
odd/even pair (`1<->2`, `3<->4`, ...), keeping the third octet. This matches the
current netplan scheme (diya = odd, hoedus = even, same /24):

| subnet         | cisc-diya | cisc-hoedus |
|----------------|-----------|-------------|
| 10.10.10.0/24  | .1        | .2          |
| 10.10.11.0/24  | .1        | .2          |
| 10.10.12.0/24  | .1        | .2          |
| 10.10.13.0/24  | .1        | .2          |

This is a convention, not an invariant: if the addressing scheme changes, the
`derive_peer_ip` rule in the script needs updating.

## Prereqs

```
apt install perftest iproute2 jq
```

Identical perftest version on both nodes. The script uses `ip -j` and `jq` to
discover devices and their addresses, and `ss` for the port preflight.

## Run

host:
```
./perftest-suite.sh host
```
client:
```
./perftest-suite.sh client
```

Both accept `--notes <file>` in any position, which copies a free-form notes
sheet into the run's output dir (see Output). Use it to record anything the
script can't capture — cable used, orientation, physical setup, etc.:

```
./perftest-suite.sh client --notes cable-notes.txt
```

Client sleeps `INITIAL_DELAY`, then walks the same test/port order as host.
Each run gets its own port (`BASE_PORT + n`), identical on both sides. Both nodes
log their discovered `device | local_ip | peer_ip` table at startup — check these
match before trusting a run.

## Tests

Per device (11 tests). Total runs = devices x 11.

```
ib_send_lat   ib_write_lat   ib_read_lat   ib_atomic_lat
ib_send_bw    ib_write_bw    ib_read_bw    ib_atomic_bw
ib_send_bw -b ib_write_bw -b ib_read_bw -b     (-b = bidirectional)
```

## Env

| var             | default                   | meaning                            |
|-----------------|---------------------------|------------------------------------|
| `BASE_PORT`     | `18515`                   | first TCP port; +1 per run         |
| `CONN`          | `RC`                      | `-c` connection type               |
| `LAT_ITERS`     | `15000`                   | `-n` iterations for `*_lat`        |
| `BW_ITERS`      | `15000`                   | `-n` iterations for `*_bw` (≤20000)|
| `RETRIES`       | `3`                       | connect/bind retries (both roles)  |
| `INITIAL_DELAY` | `3`                       | client startup delay (s)           |
| `OUTDIR`        | `./perftest-results/<ts>` | output dir (role subdir appended)  |

## Output

Each role writes to its own subdir, `$OUTDIR/<role>/`:

- `$OUTDIR/<role>/<tag>.log` — header with both roles' exact command, then raw perftest output.
- `$OUTDIR/<role>/commands.txt` — every test's host and client command.
- `$OUTDIR/<role>/summary.csv` — columns below.
- `$OUTDIR/<role>/notes.<name>` — the `--notes` file, if one was given.

```
test,device,role,rc,bw_peak_MBps,bw_avg_MBps,lat_typical_usec,size_bytes
```

`bw_peak_MBps`/`bw_avg_MBps` = max across message sizes; `lat_typical_usec` = min
t_typical across sizes. `size_bytes` is where that value occurred.

Note: perftest prints the results table on the **client**; the host's summary
metrics are typically zero. Read BW/latency from the client summary; the host's
`rc` column tells you whether the server bound and ran.

## BW peak = 0.00

Perftest computes BW peak only in iteration mode with `iters <= 20000`
(`perftest_parameters.c` forces `noPeak` otherwise). In `-D` duration mode peak
is always disabled, so the column prints `0.00`. This suite therefore uses
`-n` (`BW_ITERS`) for bandwidth, not `-D`.

Read/atomic BW measure receive completions on the server; the server-side run
reports `BW peak 0.00` by design — read the peak from the client summary.

## Notes

- `-c UD` has no read verbs; `ib_read_*` skipped when `CONN` is not `RC`/`UC`.
- Both roles must use identical `CONN` and discover the same devices in the same
  order; ports are allocated by position, so any divergence misaligns them.
- Ports are checked free before the run; stale `ib_*` servers are killed between
  tests.
- `-F` suppresses the perftest cpufreq warning.
