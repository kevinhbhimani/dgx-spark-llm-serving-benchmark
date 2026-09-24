#!/usr/bin/env bash
# Check that RDMA devices are present and active. Run on each node.
set -euo pipefail

echo "=== $(hostname) ==="

# Kernel modules
for mod in rdma_cm ib_core ib_uverbs mlx5_core mlx5_ib; do
    lsmod | grep -q "^$mod " && echo "  [OK] $mod" || echo "  [MISSING] $mod"
done

# RDMA link state
echo ""
rdma link show 2>/dev/null || echo "  rdma command not found"

# Verbs devices
echo ""
if command -v ibv_devinfo &>/dev/null; then
    for dev in $(ibv_devices 2>/dev/null | awk 'NR>1{print $1}'); do
        link=$(ibv_devinfo -d "$dev" 2>/dev/null | grep link_layer | awk '{print $2}')
        state=$(ibv_devinfo -d "$dev" 2>/dev/null | grep state | head -1 | awk '{print $2}')
        echo "  $dev: link=$link state=$state"
    done
else
    echo "  ibv_devinfo not installed (apt install ibverbs-utils)"
fi

# MTU
echo ""
CX="${CX_IFACE:-enp1s0f0np0}"
mtu=$(ip link show "$CX" 2>/dev/null | grep -oP 'mtu \K[0-9]+' || echo "N/A")
echo "  $CX mtu=$mtu"

# GPUDirect
echo ""
lsmod | grep -q nvidia_peermem && echo "  [OK] nvidia_peermem loaded" || echo "  [NOT LOADED] nvidia_peermem (modprobe nvidia-peermem)"