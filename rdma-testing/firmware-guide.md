# NVIDIA ConnectX-7 Firmware & RDMA Compatibility Guide

This document covers known firmware issues that may prevent RDMA/RoCE from working on the DGX Spark's ConnectX-7 NIC, and how to resolve them.

## Background

The DGX Spark (GB10) uses NVIDIA ConnectX-7 SmartNICs for high-speed networking. These NICs support:
- **200GbE Ethernet** (the current TCP mode)
- **RoCEv2** (RDMA over Converged Ethernet) — the desired mode
- **InfiniBand** (not used on this platform)

The firmware on these NICs controls whether RoCE is enabled, what RDMA features are available, and how the NIC interacts with the GPU via PCIe.

## Pre-Check: What Firmware Are You Running?

```bash
# Check NIC model and firmware version
sudo lspci -vvv -s $(lspci | grep -i mellanox | head -1 | awk '{print $1}') | grep -E "Product Name|firmware"

# Or use ethtool
sudo ethtool -i enp1s0f0np0

# Or use mlxfwmanager (from MFT)
sudo mst start
sudo mlxfwmanager --query
```

Expected output should show something like:
```
firmware-version: 28.40.1000 (or newer)
PSID: MT_0000000835
```

## Known Firmware Issues

### Issue 1: RoCE Disabled by Default

**Symptom**: `rdma link show` shows the device but `ibv_devinfo` reports "link_layer: Ethernet" with no RoCE support, or NCCL reports "No device found" for IB transport.

**Check**:
```bash
# Check if RoCE is enabled in firmware
sudo mlxconfig -d /dev/mst/mt4125_pciconf0 query | grep -i roce
# If output shows: ROCE_ENABLE    False(0) → RoCE is disabled!
```

**Fix**:
```bash
# Enable RoCE in firmware
sudo mlxconfig -d /dev/mst/mt4125_pciconf0 set ROCE_ENABLE=1

# Also set these for optimal performance:
sudo mlxconfig -d /dev/mst/mt4125_pciconf0 set ROCE_CC_PRIO_MASK_P1=255
sudo mlxconfig -d /dev/mst/mt4125_pciconf0 set ROCE_CONTROL=2

# REBOOT REQUIRED after firmware config changes
sudo reboot
```

### Issue 2: Outdated Firmware Lacking RDMA Features

**Symptom**: RoCE is enabled but performance is poor, or GDR (GPU Direct RDMA) doesn't work.

**Check**:
```bash
# Current firmware version
sudo mlxfwmanager --query | grep "FW Version"
# Compare against latest available from NVIDIA Enterprise Support
```

**Fix**: Update firmware to the latest version supported by your DGX Spark.

The recommended minimum firmware versions for ConnectX-7 RoCE on DGX platforms:
- **28.40.1000** or later for stable RoCEv2
- **28.42.1000** or later for full GDR support

### Issue 3: PCIe ACS (Access Control Services) Blocking P2P

**Symptom**: RDMA works but GPU Direct RDMA fails. NCCL reports "NET/IB : GPU Direct RDMA disabled" or falls back to staging buffers.

**Root Cause**: PCIe ACS may prevent peer-to-peer DMA between GPU and NIC.

**Check**:
```bash
# Check if ACS is enabled
sudo lspci -vvv -s $(lspci | grep -i mellanox | head -1 | awk '{print $1}') | grep ACSCtl
# If output shows "ACSCtl: SrcValid+" → ACS is enabled, may block P2P

# Check GPU visibility
nvidia-smi topo -m | grep -i mlx5
# Should show PIX (direct PCIe connection) or NODE, not SYS (across QPI)
```

**Fix**:
```bash
# Option A: Disable ACS if your kernel supports it (requires IOMMU)
# Add to kernel boot parameters: pci=noaer pcie_acs_override=downstream,multifunction
# Then rebuild grub and reboot

# Option B: Use nvidia-peermem (preferred on DGX systems)
sudo modprobe nvidia-peermem
# Make persistent:
echo "nvidia-peermem" | sudo tee /etc/modules-load.d/nvidia-peermem.conf
```

### Issue 4: DMA Mask / IOMMU Issues

**Symptom**: RDMA connections fail with "Cannot allocate memory" or "Resource temporarily unavailable".

**Check**:
```bash
# Check if IOMMU is enabled (needed for safe DMA)
dmesg | grep -i iommu | head -5
cat /proc/cmdline | grep iommu

# Check DMA address width
sudo lspci -vvv -s $(lspci | grep -i mellanox | head -1 | awk '{print $1}') | grep -i "64bit"
```

**Fix**:
```bash
# If IOMMU is causing issues, try:
# Kernel param: iommu=pt amd_iommu=on (for AMD) or intel_iommu=on (for Intel)

# For ARM-based Grace (DGX Spark), IOMMU is typically handled by SMMU:
# Kernel param: iommu.passthrough=1 arm-smmu.disable_bypass=0
```

### Issue 5: Locked Memory Limits

**Symptom**: RDMA memory registration fails. NCCL reports "ibv_reg_mr failed" or similar.

**Check**:
```bash
ulimit -l
# If this is low (e.g., 64), RDMA will fail for large buffers

# Check system limits
grep "memlock" /etc/security/limits.conf
```

**Fix**:
```bash
# Set unlimited locked memory (required for RDMA memory registration)
echo "*    hard   memlock   unlimited" | sudo tee -a /etc/security/limits.conf
echo "*    soft   memlock   unlimited" | sudo tee -a /etc/security/limits.conf

# Also for the Docker container:
# The --ulimit option in docker run can set memlock
# (start_ray.sh uses --network host --ipc=host which inherits host limits)
```

### Issue 6: RoCE Mode (v1 vs v2)

**Symptom**: RDMA works but only with RoCEv1, which is not routable and may have issues with some configurations.

**Check**:
```bash
# Check RoCE mode
sudo mlxconfig -d /dev/mst/mt4125_pciconf0 query | grep ROCE_CONTROL
# ROCE_CONTROL=1 → RoCEv1
# ROCE_CONTROL=2 → RoCEv2 (preferred)
```

**Fix**:
```bash
sudo mlxconfig -d /dev/mst/mt4125_pciconf0 set ROCE_CONTROL=2
sudo reboot
```

### Issue 7: Missing or Outdated RDMA Core Libraries in Container

**Symptom**: The Docker container doesn't have RDMA libraries, or they're incompatible with the host.

**Check**:
```bash
# Inside the container:
docker exec vllm-spark bash -c "
  dpkg -l | grep -E 'rdma|ibverbs|librdmacm' 2>/dev/null
  ldconfig -p | grep -E 'rdma|ibverbs' 2>/dev/null
"
```

**Fix**: The `nvcr.io/nvidia/vllm` image should include RDMA libraries. If not:
```bash
# You may need to install inside the container:
docker exec vllm-spark bash -c "
  apt-get update && apt-get install -y libibverbs1 librdmacm1
"
# Or use a newer image tag that includes them
```

## Firmware Update Procedure

### Step 1: Install MFT (Mellanox Firmware Tools)

```bash
# Download MFT from NVIDIA (requires NVIDIA Enterprise account)
# Check which version is compatible with your kernel
uname -r

# Download and install:
wget https://www.mellanox.com/downloads/MFT/mft-4.30.0-44-x86_64-deb.tgz
tar xzf mft-4.30.0-44-x86_64-deb.tgz
cd mft-4.30.0-44-x86_64-deb
sudo ./install.sh

# Verify installation
sudo mst start
sudo mst status -v
```

### Step 2: Identify Your NIC's PSID

```bash
sudo mlxfwmanager --query
# Note the PSID (e.g., MT_0000000835) — you need firmware matching this exact PSID
```

### Step 3: Download Correct Firmware

Firmware binaries are available from:
- NVIDIA Enterprise Support Portal: https://enterprise-support.nvidia.com/
- Look for "ConnectX-7 Firmware" matching your PSID

### Step 4: Update Firmware

```bash
# Backup current firmware first
sudo mlxfwmanager --query > /tmp/firmware_backup_$(date +%Y%m%d).txt

# Update
sudo mlxfwmanager -d /dev/mst/mt4125_pciconf0 -u -i fw-ConnectX7-rel-28_40_1000-<PSID>.bin

# Verify
sudo mlxfwmanager --query
```

### Step 5: Reboot and Verify

```bash
sudo reboot
# After reboot:
sudo mst start
sudo mlxfwmanager --query
rdma link show
ibv_devinfo
```

## Post-Firmware-Update Configuration

After ensuring the firmware is correct, apply the optimal configuration:

```bash
# Enable RoCEv2
sudo mlxconfig -d /dev/mst/mt4125_pciconf0 set ROCE_ENABLE=1 ROCE_CONTROL=2
sudo reboot

# After reboot, configure the RDMA link:
sudo rdma link set mlx5_0/1 netdev enp1s0f0np0

# Set MTU for jumbo frames
sudo ip link set dev enp1s0f0np0 mtu 9000
```

## Verifying Everything Works

After firmware update and configuration, run the comprehensive check:

```bash
cd rdma-testing
bash scripts/check-rdma-ready.sh
```

All critical checks should pass. Then verify with:

```bash
# RDMA bandwidth test
bash scripts/bench-rdma-vs-tcp.sh compare

# NCCL transport verification
NCCL_IB_DISABLE=0 bash scripts/verify-nccl-rdma.sh
```

## Platform-Specific Notes for DGX Spark (GB10)

The DGX Spark is a Grace Blackwell (GB10) system with:
- ARM-based Grace CPU
- Blackwell GPU
- ConnectX-7 NIC

Specific considerations:
- The Grace CPU uses **SMMU** (not Intel VT-d or AMD IOMMU) for DMA remapping
- PCIe topology: GPU and NIC are on separate PCIe root complexes, may limit GDR
- The nvidia-peermem module may need the `nvidia_peermem` kernel module loaded explicitly
- The `/dev/infiniband` devices use the `mlx5` driver, which should be loaded automatically by the NVIDIA GPU driver stack on DGX systems

## Still Not Working?

If firmware updates and configuration don't resolve the issue:

1. Check dmesg for RDMA-related errors: `sudo dmesg | grep -E "mlx5|rdma|ib_|roce"`
2. Check NCCL debug output: `NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET` in the container
3. Verify that the host and container both see the RDMA device: `ibv_devinfo` both inside and outside container
4. Ensure the RDMA CM port range isn't blocked: ports 20000-21000 typically for RoCE CM