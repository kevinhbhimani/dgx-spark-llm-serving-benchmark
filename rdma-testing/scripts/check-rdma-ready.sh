#!/usr/bin/env bash
# =============================================================================
# rdma-testing/scripts/check-rdma-ready.sh
# Run on BOTH nodes to verify RDMA hardware and software prerequisites.
# This script is READ-ONLY — it only inspects, never changes config.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass()  { echo -e "  ${GREEN}[PASS]${NC} $*"; }
fail()  { echo -e "  ${RED}[FAIL]${NC} $*"; }
warn()  { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
info()  { echo -e "  [INFO] $*"; }

FAILS=0
WARNS=0

host="$(hostname)"
echo "============================================"
echo " RDMA Readiness Check — $host"
echo "============================================"

# ---- Kernel modules ----
echo ""
echo "--- RDMA Kernel Modules ---"
for mod in rdma_cm ib_core ib_uverbs mlx5_core mlx5_ib; do
    if lsmod | grep -q "^$mod "; then
        pass "module $mod loaded"
    else
        fail "module $mod NOT loaded"
        FAILS=$((FAILS + 1))
    fi
done

# ---- RDMA devices ----
echo ""
echo "--- RDMA Devices ---"
if command -v rdma &>/dev/null; then
    rdma link show 2>/dev/null || true
else
    fail "rdma command not found (install iproute2)"
    FAILS=$((FAILS + 1))
fi

if command -v ibstat &>/dev/null; then
    ibstat 2>/dev/null || { fail "ibstat found no devices"; FAILS=$((FAILS + 1)); }
else
    fail "ibstat not found (install infiniband-diags)"
    FAILS=$((FAILS + 1))
fi

# ---- IB Verbs ----
echo ""
echo "--- IB Verbs Devices ---"
if command -v ibv_devinfo &>/dev/null; then
    for dev in $(ibv_devices 2>/dev/null | awk 'NR>1 {print $1}'); do
        info "device: $dev"
        link_layer=$(ibv_devinfo -d "$dev" 2>/dev/null | grep link_layer | awk '{print $2}')
        state=$(ibv_devinfo -d "$dev" 2>/dev/null | grep state | head -1 | awk '{print $2}')
        info "  link_layer=$link_layer  state=$state"
        if [[ "$link_layer" == "Ethernet" ]]; then
            pass "device $dev: Ethernet link layer (correct for RoCE)"
        elif [[ "$link_layer" == "InfiniBand" ]]; then
            warn "device $dev: InfiniBand link layer (RoCE expects Ethernet)"
            WARNS=$((WARNS + 1))
        else
            fail "device $dev: unknown link layer '$link_layer'"
            FAILS=$((FAILS + 1))
        fi
        if [[ "$state" == *"ACTIVE"* ]]; then
            pass "device $dev: port ACTIVE"
        else
            fail "device $dev: port NOT active ($state)"
            FAILS=$((FAILS + 1))
        fi

        # Check GID table for RoCEv2 entries
        gid_count=0
        rocev2_count=0
        for gid_path in /sys/class/infiniband/"$dev"/ports/1/gid_attrs/types/*/gid_type 2>/dev/null; do
            if [[ -f "$gid_path" ]]; then
                gid_count=$((gid_count + 1))
                gid_val=$(cat "$gid_path" 2>/dev/null || echo "")
                if [[ "$gid_val" == *"RoCE v2"* ]] || [[ "$gid_val" == *"roce-v2"* ]]; then
                    rocev2_count=$((rocev2_count + 1))
                fi
            fi
        done
        info "  $gid_count GIDs, $rocev2_count RoCEv2 entries"
        if (( rocev2_count > 0 )); then
            pass "device $dev: RoCEv2 GID(s) present"
        else
            warn "device $dev: no RoCEv2 GID found (RoCEv1 only?)"
            WARNS=$((WARNS + 1))
        fi
    done
else
    fail "ibv_devinfo not found (install ibverbs-utils)"
    FAILS=$((FAILS + 1))
fi

# ---- ConnectX interface ----
echo ""
echo "--- ConnectX Network Interface ---"
CX_IFACE="${CX_IFACE:-enp1s0f0np0}"
if ip link show "$CX_IFACE" &>/dev/null; then
    pass "interface $CX_IFACE exists"
    mtu=$(ip link show "$CX_IFACE" | grep -oP 'mtu \K[0-9]+')
    if (( mtu >= 4096 )); then
        pass "MTU=$mtu (>=4096, good for RDMA)"
    elif (( mtu >= 1500 )); then
        warn "MTU=$mtu (<4096, RDMA performs better with jumbo frames)"
        WARNS=$((WARNS + 1))
    else
        fail "MTU=$mtu (too small)"
        FAILS=$((FAILS + 1))
    fi
    ip -4 -o addr show "$CX_IFACE" | awk '{print "  IP:", $4}'
else
    fail "interface $CX_IFACE not found"
    FAILS=$((FAILS + 1))
fi

# ---- PCIe Check ----
echo ""
echo "--- PCIe Link ---"
CX_PCIE=$(lspci -D | grep -i mellanox | head -1 | awk '{print $1}')
if [[ -n "$CX_PCIE" ]]; then
    link_status=$(sudo lspci -vvv -s "$CX_PCIE" 2>/dev/null | grep LnkSta: | head -1 || echo "")
    info "NIC PCIe: $CX_PCIE"
    info "  $link_status"
else
    warn "Could not identify Mellanox NIC in lspci"
    WARNS=$((WARNS + 1))
fi

# ---- GPU Direct RDMA check ----
echo ""
echo "--- GPU Direct RDMA ---"
if command -v nvidia-smi &>/dev/null; then
    if nvidia-smi topo -m 2>/dev/null | grep -qi "mlx5\|ConnectX"; then
        pass "NIC visible in nvidia-smi topology (GDR possible)"
    else
        warn "NIC not visible in nvidia-smi topology (GDR may not work)"
        WARNS=$((WARNS + 1))
    fi

    # Check nvidia-peermem
    if lsmod | grep -q nvidia_peermem; then
        pass "nvidia_peermem loaded (GPU Direct RDMA supported)"
    else
        warn "nvidia_peermem not loaded (GPU Direct RDMA may need: modprobe nvidia-peermem)"
        WARNS=$((WARNS + 1))
    fi
else
    warn "nvidia-smi not found — skipping GDR check"
    WARNS=$((WARNS + 1))
fi

# ---- NCCL environment preview ----
echo ""
echo "--- NCCL Environment ---"
for var in NCCL_IB_DISABLE NCCL_SOCKET_IFNAME NCCL_DEBUG NCCL_IB_HCA NCCL_NET_GDR_LEVEL; do
    val="${!var:-<unset>}"
    info "  $var=$val"
done
if [[ "${NCCL_IB_DISABLE:-1}" != "0" ]]; then
    warn "NCCL_IB_DISABLE is not 0 — RDMA must be explicitly enabled"
    WARNS=$((WARNS + 1))
fi

# ---- Summary ----
echo ""
echo "============================================"
echo " Summary: $FAILS failure(s), $WARNS warning(s)"
if (( FAILS == 0 && WARNS == 0 )); then
    echo -e "${GREEN} All checks passed — RDMA should be ready!${NC}"
elif (( FAILS == 0 )); then
    echo -e "${YELLOW} All critical checks passed; some warnings to review.${NC}"
else
    echo -e "${RED} Failures detected — resolve before proceeding.${NC}"
fi
echo "============================================"
exit $FAILS