# RDMA Verification, Speed Testing, and TCP Comparison Guide

This guide covers everything needed to:
1. Confirm RDMA/RoCE is functional on your ConnectX NICs
2. Measure raw RDMA bandwidth and latency
3. Compare RDMA vs TCP performance
4. Verify NCCL is actually using RDMA

## Prerequisites

Install required tools on BOTH nodes:
```bash
# RDMA/InfiniBand diagnostics
sudo apt update && sudo apt install -y \
  infiniband-diags \
  perftest \
  ibverbs-utils \
  librdmacm-utils \
  rdma-core \
  iproute2

# Verify RDMA kernel modules are loaded
lsmod | grep -E 'rdma|ib_core|mlx5'
```

## Part 1: Hardware Verification

### 1.1 Check NIC RDMA Capabilities

```bash
# On both nodes, check the ConnectX NIC
# The device is likely mlx5_0 or similar
ibstat                    # List all InfiniBand/RDMA devices
ibstat <device>           # Detailed device status
ibv_devinfo               # Verbose device capabilities
ibv_devinfo -d mlx5_0 -v  # Full verbose for specific device

# Check link layer (should show Ethernet for RoCE)
ibstat | grep -A5 "CA '"

# Check that the NIC supports RoCE
rdma link show
```

### 1.2 Verify Link Status

```bash
# Physical link
ethtool enp1s0f0np0 | grep -E 'Speed|Link|Duplex'
# Expected: Speed: 200000Mb/s, Link detected: yes

# RDMA link
rdma link show
# Should show: link mlx5_0/1 state ACTIVE ...
```

### 1.3 Check RoCE Mode

RoCEv2 (routable) is preferred over RoCEv1:
```bash
# Check current RoCE mode
cibadmin -Q

# Or via sysfs
cat /sys/class/infiniband/mlx5_0/ports/1/gid_attrs/types/*/gid_type 2>/dev/null
```

## Part 2: RDMA Transport Verification

### 2.1 Verify RDMA CM (Connection Manager)

```bash
# Start a server on Hoedus (worker) — run this FIRST on Hoedus
# Using rping (RDMA ping)
rping -s -a 10.10.10.2 -p 9999 -v

# Then on Diya (head), connect:
rping -c -a 10.10.10.1 -s 10.10.10.2 -p 9999 -v -C 10

# Expected: successful RDMA connection with latency measurements
```

If rping fails, RDMA CM is not working. Check:
- `rdma link show` — both nodes should show ACTIVE
- Firewall rules (iptables/nftables)
- ARP/ND resolution between nodes (they're directly connected)

### 2.2 Verify ib_write_bw (RDMA Write Bandwidth)

This is the gold-standard RDMA bandwidth test:

```bash
# On Hoedus (start server first):
ib_write_bw -d mlx5_0 -a -F --report_gbits

# On Diya (client):
ib_write_bw -d mlx5_0 -a -F --report_gbits 10.10.10.2

# Expected: near line-rate bandwidth (~200 Gbps for 200GbE)
```

### 2.3 Verify ib_read_bw (RDMA Read Bandwidth)

```bash
# On Hoedus:
ib_read_bw -d mlx5_0 -a -F --report_gbits

# On Diya:
ib_read_bw -d mlx5_0 -a -F --report_gbits 10.10.10.2
```

### 2.4 Verify ib_send_bw (RDMA Send Bandwidth)

Similar to NCCL point-to-point:
```bash
# On Hoedus:
ib_send_bw -d mlx5_0 -a -F --report_gbits

# On Diya:
ib_send_bw -d mlx5_0 -a -F --report_gbits 10.10.10.2
```

### 2.5 Verify ib_send_lat (RDMA Send Latency)

```bash
# On Hoedus:
ib_send_lat -d mlx5_0 -a -F

# On Diya:
ib_send_lat -d mlx5_0 -a -F 10.10.10.2

# Expected: < 5 µs for small messages, much better than TCP's ~50-100 µs
```

## Part 3: TCP vs RDMA Bandwidth Comparison

### 3.1 TCP Baseline Bandwidth

Using iperf3 or simple netcat+dd:

```bash
# --- TCP bandwidth test ---
# On Hoedus (server):
iperf3 -s -p 5201

# On Diya (client):
iperf3 -c 10.10.10.2 -p 5201 -t 30 -P 4
# Record: TCP bandwidth in Gbps

# Or use qperf for more control:
# On Hoedus:
qperf

# On Diya:
qperf 10.10.10.2 tcp_bw tcp_lat
```

### 3.2 RDMA Baseline Bandwidth

```bash
# On Hoedus:
ib_write_bw -d mlx5_0 -a -F --report_gbits -D 10 -s 1048576

# On Diya:
ib_write_bw -d mlx5_0 -a -F --report_gbits -D 10 -s 1048576 10.10.10.2

# Compare the "GB/s" or "Gbps" column between TCP and RDMA.
# RDMA should be significantly higher (close to line rate: ~200 Gbps)
# and have much lower CPU utilization.
```

## Part 4: NCCL-Specific RDMA Verification

### 4.1 Check if NCCL Uses IB/RDMA

Run with NCCL debug output:
```bash
# Set these in the Docker container or on the host:
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=INIT,NET
export NCCL_IB_DISABLE=0

# Then run any NCCL operation (e.g., all-reduce benchmark)
# Look for lines like:
#   "NCCL INFO NET/IB : Using [0]mlx5_0:1/RoCE ..."
#   "NCCL INFO NET/IB : GPU Direct RDMA ..."
# vs TCP:
#   "NCCL INFO NET/Socket : Using [0]enp1s0f0np0 ..."
```

### 4.2 Run NCCL All-Reduce with RDMA

Using the existing interconnect benchmarks:

```bash
# 1. Stop any running Ray/vLLM
scripts/start_ray.sh stop  # on both nodes

# 2. Start Ray with RDMA enabled:
NCCL_IB_DISABLE=0 scripts/start_ray.sh head     # on Diya
NCCL_IB_DISABLE=0 scripts/start_ray.sh worker   # on Hoedus

# 3. Run the all-reduce benchmark:
NCCL_IB_DISABLE=0 scripts/ray_allreduce_bench.sh

# 4. Check output — look for transport=roce or transport=tcp in the logs
```

### 4.3 Compare TCP vs RDMA with interconnect_extra_bench

The `interconnect_extra_bench.sh` script is designed for exactly this comparison:

```bash
# --- TCP mode ---
NCCL_IB_DISABLE=1 scripts/start_ray.sh head     # Diya
NCCL_IB_DISABLE=1 scripts/start_ray.sh worker   # Hoedus
scripts/interconnect_extra_bench.sh             # → transport=tcp

# Save results
cp results/interconnect_extra_*.csv /tmp/tcp_results.csv

# --- RDMA mode ---
scripts/start_ray.sh stop                       # both nodes
NCCL_IB_DISABLE=0 scripts/start_ray.sh head     # Diya
NCCL_IB_DISABLE=0 scripts/start_ray.sh worker   # Hoedus
scripts/interconnect_extra_bench.sh             # → transport=roce

# Compare:
# diff <(sort /tmp/tcp_results.csv) <(sort results/interconnect_extra_*.csv)
```

## Part 5: CPU/GPU Utilization Comparison

RDMA should save significant CPU cycles:

```bash
# --- Measure CPU use during TCP all-reduce ---
# Terminal 1: Start monitoring
mpstat -P ALL 1 > /tmp/cpu_tcp.log &

# Terminal 2: Run TCP benchmark
NCCL_IB_DISABLE=1 scripts/ray_allreduce_bench.sh

kill %1  # stop monitoring

# --- Measure CPU use during RDMA all-reduce ---
# Terminal 1:
mpstat -P ALL 1 > /tmp/cpu_rdma.log &

# Terminal 2: Run RDMA benchmark
NCCL_IB_DISABLE=0 scripts/ray_allreduce_bench.sh

kill %1

# Compare:
echo "=== TCP CPU Utilization ==="
grep -v "^Linux\|^$" /tmp/cpu_tcp.log | awk '{sum+=$3} END {print "Avg %usr:", sum/NR}'
echo "=== RDMA CPU Utilization ==="
grep -v "^Linux\|^$" /tmp/cpu_rdma.log | awk '{sum+=$3} END {print "Avg %usr:", sum/NR}'
```

## Part 6: GPU Direct RDMA (GDR) Check

GDR allows the NIC to DMA directly from GPU memory, bypassing CPU:

```bash
# Check if GDR is supported
ibv_devinfo -d mlx5_0 | grep -i "gdr\|peer_direct\|nvidia"

# NCCL should negotiate GDR automatically — check with:
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET \
  nvidia-smi topo -m 2>/dev/null
```

## Expected Results: TCP vs RDMA

On a 200GbE ConnectX-7 link between two DGX Spark nodes, expect approximately:

| Metric | TCP Sockets | RDMA/RoCE | Improvement |
|--------|-------------|-----------|-------------|
| Peak bandwidth | ~15-20 GB/s (limited by TCP overhead) | ~23-25 GB/s (near line rate) | 1.2-1.6× |
| 8-byte latency | ~50-100 µs | ~2-5 µs | 10-50× |
| CPU utilization | 30-50% per core | <5% (offloaded to NIC) | 6-10× less CPU |
| GPU Direct | Not available | Available (if supported) | Bypasses CPU entirely |

## Troubleshooting RDMA

### Common Issues:

1. **`NCCL INFO NET/IB : No device found`**
   - RoCE not configured on NIC
   - Check: `rdma link show` — should show ACTIVE
   - Fix: Configure RoCE on the ConnectX interface

2. **RDMA_CM connection timeout**
   - Network/firewall blocking RDMA CM port ranges
   - Check ARP: `arp -n | grep 10.10.10`
   - Fix: Disable firewall on ConnectX interface or allow RDMA_CM ports

3. **`ibv_create_qp failed`**
   - Too many queue pairs or memory registration issues
   - Fix: Increase `mlx5_core` module limits

4. **NCCL falls back to TCP silently**
   - `NCCL_IB_DISABLE` is still set to 1 somewhere
   - Check env in container: `docker exec vllm-spark env | grep NCCL`
   - Remember: Ray workers inherit env from `start_ray.sh`

5. **Bandwidth far below line rate**
   - MTU too small — set to 4096 or 9000 on both nodes
   - PCIe bottleneck — check PCIe link width with `lspci -vv`
   - RoCE congestion control not configured (especially with a switch)