# NCCL RDMA/RoCE Configuration Guide

This guide covers configuring NCCL to use RDMA (RoCEv2) on the DGX Spark cluster, replacing the current TCP socket transport. It includes **modified copies** of the key scripts from the parent repo, with **minimal changes** to enable RDMA.

## Quick Start

### 1. Ensure RDMA Hardware/Firmware is Ready

On **both nodes**, run:
```bash
cd rdma-testing
bash scripts/check-rdma-ready.sh
```
Resolve all failures before proceeding.

### 2. Set MTU to Jumbo Frames (One-Time)

RDMA performs best with jumbo frames. Set MTU once on both nodes:
```bash
# On both Diya and Hoedus:
sudo ip link set dev enp1s0f0np0 mtu 4096
# Or for maximum performance: mtu 9000
# Make persistent:
# Add to /etc/network/interfaces or your netplan config
```

### 3. Start Ray with RDMA

On **Diya** (head):
```bash
cd rdma-testing
NCCL_IB_DISABLE=0 bash scripts-rdma/start_ray_rdma.sh head
```

On **Hoedus** (worker):
```bash
cd rdma-testing
NCCL_IB_DISABLE=0 bash scripts-rdma/start_ray_rdma.sh worker
```

### 4. Verify NCCL Is Using RDMA

```bash
cd rdma-testing
bash scripts/verify-nccl-rdma.sh
```

You should see `NCCL INFO NET/IB : Using [0]mlx5_0:1/RoCE` in the output.

### 5. Run Benchmarks

```bash
# Interconnect microbenchmark to confirm RDMA bandwidth
cd rdma-testing
bash scripts/bench-rdma-vs-tcp.sh compare

# Then run the full interconnect extra bench:
cd ..
NCCL_IB_DISABLE=0 scripts/interconnect_extra_bench.sh

# Or run a full model benchmark:
NCCL_IB_DISABLE=0 scripts/run_models.sh tp2 --skip-done
```

## NCCL Environment Variables for RDMA

The following variables control NCCL's RDMA transport. The critical ones are set in `scripts-rdma/connectx_env_rdma.sh`:

| Variable | Value | Purpose |
|----------|-------|---------|
| `NCCL_IB_DISABLE` | `0` | **Enable** InfiniBand/RDMA transport |
| `NCCL_SOCKET_IFNAME` | `enp1s0f0np0` | Bind TCP fallback/control to ConnectX |
| `NCCL_IB_HCA` | `=mlx5_0` | Restrict RDMA to the ConnectX HCA only |
| `NCCL_IB_GID_INDEX` | `3` | Use RoCEv2 GID (usually index 3) |
| `NCCL_IB_TC` | `106` | Traffic class for RoCE QoS |
| `NCCL_IB_QPS_PER_CONNECTION` | `4` | Queue pairs per connection (tuning) |
| `NCCL_NET_GDR_LEVEL` | `5` | GPU Direct RDMA level (if supported) |
| `NCCL_IB_TIMEOUT` | `22` | IB timeout (increased for stability) |
| `NCCL_DEBUG` | `WARN` | Debug level (use `INFO` to verify transport) |

### GID Index Explanation

RoCEv2 uses the GID table for addressing. To find the correct GID index:
```bash
# On each node, in the container:
show_gids   # Shows all GIDs and their types
# Or:
ibv_devinfo -d mlx5_0 -v | grep -A3 GID
# Look for a GID with "RoCE v2" type, typically index 3
```

### GPU Direct RDMA (GDR)

GDR allows the NIC to DMA directly from/to GPU memory, bypassing CPU entirely. It requires:
1. `nvidia-peermem` kernel module loaded
2. PCIe peer-to-peer between GPU and NIC
3. `NCCL_NET_GDR_LEVEL` set appropriately

Check GDR support:
```bash
# On each node:
lsmod | grep nvidia_peermem
nvidia-smi topo -m | grep -i mlx5
```

## What Changed: Scripts Overview

The files in `scripts-rdma/` are **minimally modified copies** of the parent repo's scripts. Here's what changed:

### `scripts-rdma/connectx_env_rdma.sh`
Added RDMA-specific NCCL variables:
```bash
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-0}"     # 0 = enable RDMA (changed from 1)
export NCCL_IB_HCA="${NCCL_IB_HCA:-=mlx5_0}"       # Restrict to ConnectX HCA
export NCCL_IB_GID_INDEX="${NCCL_IB_GID_INDEX:-3}" # RoCEv2 GID index
export NCCL_IB_TC="${NCCL_IB_TC:-106}"              # Traffic class
export NCCL_IB_QPS_PER_CONNECTION="${NCCL_IB_QPS_PER_CONNECTION:-4}"
export NCCL_IB_TIMEOUT="${NCCL_IB_TIMEOUT:-22}"
export NCCL_NET_GDR_LEVEL="${NCCL_NET_GDR_LEVEL:-5}"
```

### `scripts-rdma/start_ray_rdma.sh`
Added RDMA NCCL env vars to the Docker `-e` flags:
```bash
-e NCCL_IB_HCA="$NCCL_IB_HCA" \
-e NCCL_IB_GID_INDEX="$NCCL_IB_GID_INDEX" \
-e NCCL_IB_TC="$NCCL_IB_TC" \
-e NCCL_IB_QPS_PER_CONNECTION="$NCCL_IB_QPS_PER_CONNECTION" \
-e NCCL_IB_TIMEOUT="$NCCL_IB_TIMEOUT" \
-e NCCL_NET_GDR_LEVEL="$NCCL_NET_GDR_LEVEL"
```

### `scripts-rdma/launch_server_rdma.sh`
Same added RDMA env vars passed to the vLLM server container.

### `scripts-rdma/ray_allreduce_bench_rdma.sh`
Same RDMA env vars; records `transport=roce` in the CSV.

## 4-Node Configuration (Future)

When moving to 4 nodes with a switch, additional considerations apply:

### RoCE Congestion Control
Without PFC (Priority Flow Control), RoCE can suffer from congestion. Options:
1. **DCQCN** (Data Center Quantized Congestion Notification) — requires switch support
2. **ECN** (Explicit Congestion Notification) — simpler, works with most switches
3. Let NCCL handle it — NCCL has built-in congestion detection

### Network Topology
For 4 nodes, you may want a fat-tree or ring topology. NCCL handles the communication pattern automatically, but optimal bandwidth requires:
- Non-blocking switch (or at minimum 2:1 oversubscription)
- All links at same speed
- No inter-switch bottlenecks if using multiple switches

### NCCL Tuning for 4+ Nodes
```bash
# Additional variables for multi-node:
export NCCL_IB_SL=0                        # Service level
export NCCL_IB_CUDA_SUPPORT=1              # CUDA IPC support
export NCCL_NET_GDR_READ=1                 # GDR read support
export NCCL_CROSS_NIC=1                    # Use multiple NICs if available
export NCCL_SOCKET_NTHREADS=4              # Socket threads (fallback only)
export NCCL_NSOCKS_PERTHREAD=4             # Sockets per thread (fallback only)
```

## Troubleshooting

### NCCL falls back to TCP even with NCCL_IB_DISABLE=0

Check inside the container:
```bash
docker exec vllm-spark env | grep NCCL
```

Common causes:
- `NCCL_IB_HCA` points to wrong device — check `ibv_devinfo` inside container
- RoCE not configured on NIC — check `rdma link show` on host
- `/dev/infiniband` not mounted in container — check Docker args include `--network host --ipc=host`

### "ibv_create_qp failed" or "Cannot allocate memory"

```bash
# Increase locked memory limit:
echo "* hard memlock unlimited" | sudo tee -a /etc/security/limits.conf
echo "* soft memlock unlimited" | sudo tee -a /etc/security/limits.conf
```

### FDQN/NVCC SHARP / Fabric Manager

NVIDIA's DGX systems typically include NVSwitch and Fabric Manager. The DGX Spark (GB10) does NOT have NVSwitch, so this is not a concern. The ConnectX-7 NIC handles RDMA directly.

### Slow RDMA Performance

1. Check MTU: `ip link show enp1s0f0np0 | grep mtu`
2. Check PCIe bandwidth: `sudo lspci -vvv -s <pcie_addr> | grep -E "LnkSta|Width"`
3. Use `NCCL_DEBUG=INFO` to see negotiated speeds
4. Try `NCCL_IB_QPS_PER_CONNECTION=8` for more parallelism
5. Check for IRQ affinity issues with `NCCL_IB_AR_THRESHOLD`

## NVIDIA ConnectX Firmware Considerations

### Current Known Issues

The ConnectX-7 firmware in DGX Spark systems may need updates for proper RDMA/RoCE operation:

1. **RoCE disabled by default**: Some firmware versions ship with RoCE disabled. Check:
   ```bash
   sudo mlxconfig -d /dev/mst/mt4125_pciconf0 query | grep ROCE
   ```
   If it shows `ROCE_ENABLE=False`, enable it:
   ```bash
   sudo mlxconfig -d /dev/mst/mt4125_pciconf0 set ROCE_ENABLE=1
   # Reboot required
   ```

2. **MFT (Mellanox Firmware Tools)**: You may need to install MFT to update firmware:
   ```bash
   # Download from NVIDIA:
   wget https://www.mellanox.com/downloads/MFT/mft-4.30.0-44-x86_64-deb.tgz
   tar xzf mft-*.tgz && cd mft-*
   sudo ./install.sh
   ```

3. **Firmware version**: Check current firmware:
   ```bash
   sudo mlxfwmanager --query
   # Or:
   sudo ethtool -i enp1s0f0np0 | grep firmware
   ```
   If firmware is outdated, download the latest from NVIDIA Enterprise Support.

4. **RoCE mode**: Ensure RoCEv2 is active:
   ```bash
   # Check current mode:
   cat /sys/kernel/debug/mlx5/0000\:01\:00.0/roce_state 2>/dev/null
   # Or:
   sudo mlxconfig -d /dev/mst/mt4125_pciconf0 query | grep -i roce
   ```

5. **DCQCN/ECN configuration**: When using a switch, configure congestion control:
   ```bash
   # Enable DCQCN (if switch supports it):
   echo 1 | sudo tee /sys/kernel/debug/mlx5/0000\:01\:00.0/cc_params/np_enable
   echo 106 | sudo tee /sys/kernel/debug/mlx5/0000\:01\:00.0/cc_params/np_cnp_dscp
   ```

### Firmware Update Procedure

```bash
# 1. Install MFT
sudo apt install ./mft-*.deb

# 2. Start MST
sudo mst start

# 3. Identify device
sudo mst status -v

# 4. Check current firmware
sudo mlxfwmanager --query -d /dev/mst/mt4125_pciconf0

# 5. Update firmware (requires firmware binary from NVIDIA)
sudo mlxfwmanager -d /dev/mst/mt4125_pciconf0 -u -i fw-ConnectX7-rel-28_40_1000-MCX713106AEHEA_Ax.bin

# 6. Reboot both nodes
sudo reboot
```