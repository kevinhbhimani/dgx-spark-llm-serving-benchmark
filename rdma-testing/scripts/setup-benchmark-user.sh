#!/usr/bin/env bash
# =============================================================================
# rdma-testing/scripts/setup-benchmark-user.sh
#
# Creates and configures a user account with all permissions needed to run the
# DGX Spark LLM Serving Benchmark pipeline — including Docker, GPU, RDMA, and
# optional sudo-backed operations (page-cache drops, MTU changes, firmware).
#
# Usage:
#   sudo bash setup-benchmark-user.sh [USERNAME]
#
# If USERNAME is omitted, defaults to 'benchmark'.
#
# What this script does:
#   1. Creates the user and home directory (if they don't exist)
#   2. Adds the user to every group required for Docker, GPU, and RDMA
#   3. Sets memlock limits for RDMA memory registration
#   4. Creates udev rules so /dev/infiniband devices are group-readable
#   5. Prints a summary of remaining one-time sudo-only setup steps
#
# IMPORTANT: After running this script, the user MUST log out and back in
# (or reboot) for group memberships to take effect.
# =============================================================================
set -euo pipefail

USERNAME="${1:-benchmark}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Safety: must run as root ────────────────────────────────────────────────
if [[ "$(id -u)" != "0" ]]; then
    echo -e "${RED}This script must be run as root (sudo).${NC}" >&2
    echo "  sudo bash $0 [USERNAME]" >&2
    exit 1
fi

echo -e "${CYAN}══════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  DGX Spark Benchmark — User Setup${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "Target user: ${GREEN}$USERNAME${NC}"
echo ""

# ═════════════════════════════════════════════════════════════════════════════
# 1. Create the user
# ═════════════════════════════════════════════════════════════════════════════
echo -e "${CYAN}── Step 1: Create user account ──${NC}"

if id "$USERNAME" &>/dev/null; then
    echo -e "  ${GREEN}[OK]${NC} User '$USERNAME' already exists"
else
    useradd --create-home --shell /bin/bash "$USERNAME" \
        --comment "DGX Spark Benchmark User"
    echo -e "  ${GREEN}[OK]${NC} Created user '$USERNAME'"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 2. Groups required for daily benchmark operation
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}── Step 2: Add to required groups ──${NC}"

# Map of: group_name → rationale
# We create groups that don't exist (some are DGX-specific).
declare -A GROUPS=(
    # ── Docker: container lifecycle, exec, cp, logs ──
    ["docker"]="Docker container management (docker run/exec/rm/logs/inspect)"

    # ── GPU access ──
    # On DGX/Ubuntu systems the NVIDIA driver and container toolkit handle GPU
    # access. The user doesn't strictly need video/render for containerized GPU
    # access (nvidia-container-toolkit hooks do this), but host-side nvidia-smi
    # and power monitoring call nvidia-smi directly.
    ["video"]="GPU device access (/dev/nvidia* — for nvidia-smi host queries)"
    ["render"]="DRM render node access (supplementary GPU path)"

    # ── RDMA / InfiniBand device access ──
    # /dev/infiniband/uverbs* and /dev/infiniband/rdma_cm are needed for
    # ibv_devinfo, ib_write_bw, and NCCL's RDMA transport from user-space.
    # On stock Ubuntu the 'rdma' group may not exist — we create it and add
    # a udev rule below.
    ["rdma"]="RDMA device access (/dev/infiniband/* — ibverbs, perftest, NCCL)"

    # ── System monitoring (optional but convenient) ──
    ["adm"]="Read system logs without sudo (journalctl, /var/log)"
)

REQUIRED_GROUPS=()
for group in "${!GROUPS[@]}"; do
    REQUIRED_GROUPS+=("$group")
    if getent group "$group" &>/dev/null; then
        echo -e "  ${GREEN}[OK]${NC} Group '$group' exists — ${GROUPS[$group]}"
    else
        echo -e "  ${YELLOW}[NEW]${NC} Creating group '$group' — ${GROUPS[$group]}"
        groupadd --force "$group"
    fi
done

# Actually add the user to all groups
for group in "${REQUIRED_GROUPS[@]}"; do
    usermod --append --groups "$group" "$USERNAME"
done
echo -e "  ${GREEN}[OK]${NC} User '$USERNAME' added to: ${REQUIRED_GROUPS[*]}"

# ═════════════════════════════════════════════════════════════════════════════
# 3. memlock limits (critical for RDMA)
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}── Step 3: Set memlock limits (RDMA memory registration) ──${NC}"

LIMITS_FILE="/etc/security/limits.d/99-benchmark-rdma.conf"
cat > "$LIMITS_FILE" <<'LIMITS'
# DGX Spark LLM Benchmark — RDMA requires unlimited locked memory.
# Without this, ibv_reg_mr() fails for large buffers and NCCL falls
# back to TCP or crashes.
*       hard    memlock     unlimited
*       soft    memlock     unlimited
LIMITS
echo -e "  ${GREEN}[OK]${NC} Wrote $LIMITS_FILE"

# Also ensure systemd user slices respect this (some DGX Ubuntu releases need it)
SYSTEMD_CONF="/etc/systemd/system.conf.d/99-benchmark-memlock.conf"
if [[ -d "$(dirname "$SYSTEMD_CONF")" ]] || mkdir -p "$(dirname "$SYSTEMD_CONF")"; then
    cat > "$SYSTEMD_CONF" <<'SYSTEMD'
[Manager]
DefaultLimitMEMLOCK=infinity
SYSTEMD
    echo -e "  ${GREEN}[OK]${NC} Wrote $SYSTEMD_CONF (systemd DefaultLimitMEMLOCK)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 4. udev rule for /dev/infiniband (RDMA device nodes)
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}── Step 4: udev rules for RDMA device access ──${NC}"

UDEV_RULES="/etc/udev/rules.d/99-benchmark-rdma.rules"
cat > "$UDEV_RULES" <<'UDEV'
# Let the 'rdma' group read/write RDMA userspace device nodes.
# These are needed by ibverbs, perftest, NCCL (with NCCL_IB_DISABLE=0).
KERNEL=="uverbs*",    GROUP="rdma", MODE="0660"
KERNEL=="rdma_cm",    GROUP="rdma", MODE="0660"
KERNEL=="umad*",      GROUP="rdma", MODE="0660"
KERNEL=="issm*",      GROUP="rdma", MODE="0660"

# mlx5 devices (ConnectX) — supplementary access
SUBSYSTEM=="infiniband", GROUP="rdma", MODE="0660"
UDEV
echo -e "  ${GREEN}[OK]${NC} Wrote $UDEV_RULES"

# Reload udev rules and apply
if command -v udevadm &>/dev/null; then
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=infiniband 2>/dev/null || true
    echo -e "  ${GREEN}[OK]${NC} udev rules reloaded and triggered"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 5. Sudoers for optional operations (page-cache drops, MTU, emergency UVM)
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}── Step 5: Passwordless sudo for specific benchmark operations ──${NC}"

SUDOERS_FILE="/etc/sudoers.d/benchmark-${USERNAME}"
cat > "$SUDOERS_FILE" << SUDOERS
# DGX Spark Benchmark — passwordless sudo for specific operations.
# These are OPTIONAL quality-of-life operations; the core benchmark
# pipeline runs without sudo when these are omitted.

# Drop page cache before large model loads on Grace unified memory
# (avoids false-OOM when HF cache pages consume MemFree).
$USERNAME ALL=(root) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches

# One-time MTU change for RDMA jumbo frames
$USERNAME ALL=(root) NOPASSWD: /usr/sbin/ip link set dev enp1s0f0np0 mtu *

# Emergency recovery: reset leaked GPU unified memory allocator
# (docs/SUDO_NEED_FOR.md explains when this is needed)
$USERNAME ALL=(root) NOPASSWD: /usr/sbin/rmmod nvidia_uvm
$USERNAME ALL=(root) NOPASSWD: /usr/sbin/modprobe nvidia_uvm

# RDMA firmware/diagnostic tools (one-time setup, not daily use)
$USERNAME ALL=(root) NOPASSWD: /usr/sbin/mst, /usr/bin/mlxconfig, /usr/bin/mlxfwmanager, /usr/bin/mlxlink, /usr/bin/ibstat, /usr/sbin/rdma
$USERNAME ALL=(root) NOPASSWD: /usr/sbin/ethtool

# Install packages (one-time: infiniband-diags, perftest, etc.)
$USERNAME ALL=(root) NOPASSWD: /usr/bin/apt, /usr/bin/apt-get
SUDOERS

# Validate sudoers syntax before committing
if visudo --check --file="$SUDOERS_FILE" 2>/dev/null; then
    chmod 0440 "$SUDOERS_FILE"
    echo -e "  ${GREEN}[OK]${NC} Wrote $SUDOERS_FILE (validated)"
else
    echo -e "  ${RED}[FAIL]${NC} sudoers syntax error in $SUDOERS_FILE — removing"
    rm -f "$SUDOERS_FILE"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 6. Per-user Docker daemon check (informational)
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}── Step 6: Verify Docker is installed and running ──${NC}"

if command -v docker &>/dev/null; then
    echo -e "  ${GREEN}[OK]${NC} Docker CLI found: $(docker --version 2>/dev/null || echo 'version check failed')"
    if docker info &>/dev/null; then
        echo -e "  ${GREEN}[OK]${NC} Docker daemon is running and accessible"
    else
        echo -e "  ${YELLOW}[WARN]${NC} Docker daemon not accessible — is it running?"
        echo "         systemctl start docker"
    fi
else
    echo -e "  ${RED}[FAIL]${NC} Docker is not installed. Install it:"
    echo "         sudo apt update && sudo apt install docker.io"
    echo "         sudo systemctl enable --now docker"
fi

# NVIDIA container toolkit
if dpkg -l nvidia-container-toolkit &>/dev/null 2>&1; then
    echo -e "  ${GREEN}[OK]${NC} nvidia-container-toolkit installed"
else
    echo -e "  ${YELLOW}[WARN]${NC} nvidia-container-toolkit not found — GPU passthrough to containers may fail"
    echo "         See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 7. Summary and next steps
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  User '$USERNAME' is ready for benchmarking.${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "Groups:"
groups "$USERNAME" 2>/dev/null || echo "  (log out and back in to see groups)"
echo ""
echo -e "${YELLOW}── NEXT STEPS (do these on BOTH nodes) ──${NC}"
echo ""
echo "1. ${CYAN}Log out and back in${NC} (or reboot) so group memberships take effect."
echo "   Verify with:  groups"
echo ""
echo "2. ${CYAN}One-time setup (requires sudo on first run):${NC}"
echo "   • Set MTU for RDMA:   sudo ip link set dev enp1s0f0np0 mtu 4096"
echo "   • Install RDMA tools:  sudo apt install -y infiniband-diags perftest ibverbs-utils librdmacm-utils rdma-core"
echo "   • Load nvidia-peermem: sudo modprobe nvidia-peermem"
echo "   • Verify RDMA:         rdma link show && ibv_devinfo"
echo ""
echo "3. ${CYAN}Test everything:${NC}"
echo "   cd rdma-testing"
echo "   bash scripts/check-rdma-ready.sh"
echo ""
echo -e "${YELLOW}── Permission Summary ──${NC}"
echo ""
echo "  ┌─────────────────────┬──────────────────────────────────┐"
echo "  │ Group / Permission  │ Needed for                       │"
echo "  ├─────────────────────┼──────────────────────────────────┤"
echo "  │ docker              │ Container lifecycle (run/exec/rm)│"
echo "  │ video, render       │ nvidia-smi host-side GPU queries │"
echo "  │ rdma                │ /dev/infiniband (ibverbs, NCCL)  │"
echo "  │ adm                 │ Journalctl / syslog reading      │"
echo "  │ memlock: unlimited  │ RDMA memory registration (NCCL)  │"
echo "  │ sudo: tee drop_cache│ Optional: flush page cache       │"
echo "  │ sudo: ip link set   │ Optional: set MTU (jumbo frames) │"
echo "  │ sudo: modprobe      │ Optional: reset leaked GPU UVM   │"
echo "  │ sudo: apt           │ Optional: install RDMA packages  │"
echo "  └─────────────────────┴──────────────────────────────────┘"