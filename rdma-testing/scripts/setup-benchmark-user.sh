#!/usr/bin/env bash
# =============================================================================
# Create a user that can run the DGX Spark benchmark pipeline.
#
#   sudo bash setup-benchmark-user.sh [USERNAME]
#
# Docker is the ONLY group that matters: the Docker CLI talks to the daemon
# socket (/var/run/docker.sock) which is owned by root:docker, mode 0660.
# Without docker group, every `docker run|exec|rm|logs` needs sudo.
#
# Everything else — nvidia-smi, ibv_devinfo, mlxconfig, modprobe — you can
# reach with sudo when needed. Those do not need permanent group membership.
#
# After this script, log out and back in for the docker group to take effect.
# =============================================================================
set -euo pipefail

USERNAME="${1:-benchmark}"

if [[ "$(id -u)" != "0" ]]; then
    echo "Must run as root: sudo bash $0 [USERNAME]" >&2
    exit 1
fi

# ── User account ────────────────────────────────────────────────────────────
if id "$USERNAME" &>/dev/null; then
    echo "User '$USERNAME' already exists"
else
    useradd --create-home --shell /bin/bash "$USERNAME"
    echo "Created user '$USERNAME'"
fi

# ── Docker group ────────────────────────────────────────────────────────────
# The docker daemon socket is root:docker 0660. Membership is the only way to
# run docker commands without sudo. Not negotiable: every script in this repo
# calls docker directly (run, exec, rm, cp, logs, inspect).
getent group docker &>/dev/null || groupadd docker
usermod --append --groups docker "$USERNAME"
echo "Added to group: docker"

echo ""
echo "Done. Log out and back in, then verify:"
echo "  groups          # should show docker"
echo "  docker info     # should connect without sudo"