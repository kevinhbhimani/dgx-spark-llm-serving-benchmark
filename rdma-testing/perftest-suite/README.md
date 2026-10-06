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

`ethtool` and `pciutils` (provides `lspci`) are optional but strongly
recommended: the suite captures link speed, PCIe width/speed, and
driver/firmware versions into every run. Without them the environment capture
is incomplete.

Identical perftest version on both nodes. The script uses `ip -j` and `jq` to
discover devices and their addresses, and `ss` for the port preflight.

## Run

### Single paired run

Pass the same `--run-name` on both machines so the two sides land in matching
directories (host/ vs client/):

host:
```
./perftest-suite.sh host --run-name 2026-10-06-cable-a/run-001
```
client:
```
./perftest-suite.sh client --run-name 2026-10-06-cable-a/run-001
```

Without `--run-name` the output lands in a timestamped directory; that is for
ad-hoc runs only, since a host and its client started this way do not share a
directory name.

### Campaign (N repeated runs)

Use `runner.sh`. Start the host side first, then the client side, with the
same `--campaign` on both machines:

```
./runner.sh 5 host --campaign cables-oct06     # cisc-diya
./runner.sh 5 client --campaign cables-oct06   # cisc-hoedus
```

All runs are filed under one campaign directory, and run numbers are
deterministic, so both machines' directories match exactly:

```
perftest-results/cables-oct06/run-001/{host,client}
perftest-results/cables-oct06/run-002/{host,client}
...
```

`runner.sh` stops the whole campaign if any run fails. Reusing a campaign name
re-creates the same `run-00N` names, so pick a new campaign name (or delete
the old one) rather than appending to an existing campaign.

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
| `OUTDIR`        | `./perftest-results`      | results root; runs live under it   |
| `BASE_PORT`     | `18515`                   | first TCP port; +1 per run         |
| `CONN`          | `RC`                      | `-c` connection type               |
| `LAT_ITERS`     | `15000`                   | `-n` iterations for `*_lat`        |
| `BW_ITERS`      | `15000`                   | `-n` iterations for `*_bw` (≤20000)|
| `RETRIES`       | `3`                       | connect/bind retries (both roles)  |
| `INITIAL_DELAY` | `3`                       | client startup delay (s)           |

`BW_ITERS` is validated to be ≤ 20000: perftest disables BW peak computation
above that.

## Output

Each run writes to `<OUTDIR>/<run-name>/<role>/`:

```
cables-oct06/run-001/
  host/
    environment/                    hardware + link-state capture
      system.json                   kernel, distro, perftest/rdma-core versions
      <device>/
        link.json                   parsed link speed, PCIe, MTU, driver
        ethtool.txt                 raw `ethtool <netdev>`
        ethtool-i.txt               raw `ethtool -i <netdev>` (driver/firmware)
        ibv_devinfo.txt             raw `ibv_devinfo -v <device>`
        lspci.txt                   raw `lspci -vv -s <addr>` (PCIe link)
    results.csv                     parsed perftest tables, all message sizes
    commands.txt                    every test's host and client command
    <test>[_bidi]_<device>.log      raw perftest output, never discarded
    notes.<name>                    the --notes file, if one was given
  client/
    ... same layout ...
```

`link.json` fields: `device`, `netdev`, `local_ip`, `peer_ip`, `mtu`,
`link_speed_mbps`, `autoneg`, `link_detected`, `pcie_speed`, `pcie_width`,
`driver`, `driver_version`, `firmware_version`. Empty string means the tool or
field was unavailable on that machine.

The suite also prints a one-line link-state table per device at startup:

```
link state (device | local -> peer | link speed | pcie | mtu):
  rocep1s0f0     | 10.10.10.2 -> 10.10.10.1 | 200000Mb/s | 16GT/s x8 | mtu 4096
```

## results.csv

Tidy long format: one row per (test, message size). Units: bandwidth in
**GB/s** (decimal gigabytes per second), latency in microseconds.

```
test,device,role,bidi,size_bytes,bw_peak_GBps,bw_avg_GBps,msg_rate_Mpps,lat_min_usec,lat_max_usec,lat_typical_usec,lat_avg_usec,lat_stdev_usec,lat_p99_usec,lat_p999_usec,error
```

- Bandwidth rows (`ib_*_bw`) fill the `bw_*` and `msg_rate_*` columns; latency
  rows fill the `lat_*` columns. The other columns are empty.
- `bidi=true` rows are the `-b` tests; perftest reports the aggregate of both
  directions in those rows.
- Values are converted from perftest's `--report_gbits` output:
  `GB/s = Gbit/s / 8`.
- The `error` column is empty on success. On failure it records what happened
  (`perftest rc=N`, `no parseable rows in perftest output`), so a failed test
  is never silently dropped.
- perftest prints its results table on the **client**. Some host-side cells
  are zero by perftest design (host `ib_send_bw` peak, host `ib_read_lat` /
  `ib_atomic_lat`). Read BW/latency from the client's `results.csv`; the
  host's file confirms the server bound and ran.

## Notes

- `-c UD` has no read verbs; `ib_read_*` skipped when `CONN` is not `RC`/`UC`.
- Both roles must use identical `CONN` and discover the same devices in the
  same order; ports are allocated by position, so any divergence misaligns
  them. Check the "discovered devices" tables on both sides match before
  trusting a run.
- Ports are checked free before the run; stale `ib_*` servers are killed
  between tests.
- `-F` suppresses the perftest cpufreq warning.
- `--notes` is still free-form prose; replacing it with structured, validated
  metadata (operator, cable part numbers, purpose, ...) is planned.
