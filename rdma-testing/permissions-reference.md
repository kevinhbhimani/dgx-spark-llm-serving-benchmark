# Required Permissions & Groups — Reference

This document explains every permission, group, and kernel setting needed to run the DGX Spark LLM Serving Benchmark pipeline — both the TCP-only baseline and the RDMA/RoCE transport.

## Quick Setup

A script automates everything below:

```bash
sudo bash rdma-testing/scripts/setup-benchmark-user.sh [USERNAME]
```

Read on for the rationale behind each setting.

---

## Tier 1: Daily Benchmark Operation (No Sudo)

These are needed to run benchmarks day to day. The user must be a member of these groups.

### `docker` group

| What | Why |
|------|-----|
| `docker run` | Launch the vLLM serving container (single-node) and Ray cluster containers (dual-node) |
| `docker exec` | Run commands inside running containers — server launch, Ray status checks, interconnect benchmarks, model cache mirroring |
| `docker rm/rm -f` | Tear down containers between model runs |
| `docker logs` | Read container logs for debugging |
| `docker cp` | Copy scripts into the container (mirror\_worker\_cache.py) |
| `docker inspect` | Check container running state before polling Ray |

**Files touched**: Docker socket `/var/run/docker.sock` (group `docker`, mode `0660`).

### `video` + `render` groups

| What | Why |
|------|-----|
| `nvidia-smi` (host) | Query GPU model, driver version, power limit, clock policy, compute capability — used by `run_models.sh` for the single-host runtime fingerprint and by `check_rdma_ready.sh` for GDR topology checks |
| `/dev/nvidia*` | CUDA applications access GPU devices through these nodes; the NVIDIA container toolkit forwards them into containers |

On DGX systems with the NVIDIA driver and container toolkit, GPU access inside containers is handled by `nvidia-container-toolkit` hooks, not host group membership. The `video` and `render` groups are for _host-side_ nvidia-smi queries run directly (not through Docker).

### `rdma` group

| What | Why |
|------|-----|
| `/dev/infiniband/uverbs*` | Userspace RDMA verbs — needed by `ibv_devinfo`, `ib_write_bw`, `ib_send_bw`, `ib_send_lat`, and crucially by NCCL when `NCCL_IB_DISABLE=0` |
| `/dev/infiniband/rdma_cm` | RDMA Connection Manager — needed to establish RDMA connections between nodes |
| `/dev/infiniband/umad*` | Management datagram interface — used by `ibstat` for port status queries |

Without this group, the user gets `Permission denied` on `/dev/infiniband/uverbs0`, and NCCL falls back to TCP sockets silently (or crashes with "No device found").

### `adm` group (optional)

| What | Why |
|------|-----|
| `journalctl` | Read system logs without sudo — useful for debugging Ray/vLLM crashes |
| `/var/log/syslog` | Check for kernel OOM, driver, or RDMA errors |

---

## Tier 2: Kernel Limits (Set Once by Root)

### `memlock: unlimited`

**Critical for RDMA.** Set in `/etc/security/limits.d/99-benchmark-rdma.conf`.

| What | Why |
|------|-----|
| `memlock` ulimit | RDMA memory registration (`ibv_reg_mr`) pins physical memory pages. Each NCCL communication buffer must be registered. At 200 GbE with large models, NCCL may register gigabytes of memory. The default limit (often 64 KB) causes `ibv_reg_mr` to fail with "Cannot allocate memory". |

This must be set **before** the user's login session starts — either in `/etc/security/limits.conf` (PAM) or `/etc/systemd/system.conf` (systemd user slices).

---

## Tier 3: Optional Sudo-Backed Operations

These are convenience operations that require elevated privileges but are not needed for every benchmark run. The setup script grants passwordless sudo for these specific commands.

### Page Cache Drops (`tee /proc/sys/vm/drop_caches`)

| Trigger | Why |
|---------|-----|
| `DROP_CACHES=1 scripts/start_ray.sh` | On Grace unified memory, stale filesystem cache (from downloading model checkpoints) consumes the same MemFree pool that CUDA uses. After downloading a large model, MemFree may drop below the GPU memory utilization threshold, causing vLLM to refuse to start. Dropping clean page cache reclaims this memory without evicting active process pages. |

### MTU Change (`ip link set dev enp1s0f0np0 mtu`)

| Trigger | Why |
|---------|-----|
| One-time setup | RDMA/RoCE performs significantly better with jumbo frames (MTU ≥ 4096, ideally 9000). The default 1500-byte MTU forces packet fragmentation on every RDMA transfer, cutting bandwidth by 30-50%. This is a one-time persistent configuration (add to netplan/networkd), but the benchmark user may need to verify or adjust it. |

### NVIDIA UVM Reset (`rmmod nvidia_uvm && modprobe nvidia_uvm`)

| Trigger | Why |
|---------|-----|
| After a killed CUDA process | On Grace (unified memory architecture), a killed CUDA process can leak GPU memory allocations in the `nvidia_uvm` driver. nvidia-smi shows no processes, but free memory stays low. The only recovery short of a reboot is to unload and reload `nvidia_uvm`. This is an emergency operation, not routine. |

### RDMA Diagnostic Tools

| Command | Why |
|--------|-----|
| `ibstat` | Query InfiniBand/RoCE port status (needed by `check-rdma-ready.sh`) |
| `rdma link set` | Bind RDMA device to a netdev (one-time RoCE setup) |
| `ethtool` | Check NIC firmware version, link speed, driver info |

### Package Installation

| Command | Why |
|--------|-----|
| `apt install` | One-time installation of RDMA userspace tools: `infiniband-diags`, `perftest`, `ibverbs-utils`, `librdmacm-utils`, `rdma-core` |

---

## Tier 4: Root-Only (Firmware & Hardware Configuration)

These operations always require full root and are NOT granted via sudoers. They are performed once by a system administrator.

| Operation | When |
|-----------|------|
| `mlxconfig set ROCE_ENABLE=1` | Enable RoCE on ConnectX-7 firmware (disabled by default on some builds) |
| `mlxfwmanager --update` | Update ConnectX-7 firmware |
| `modprobe nvidia-peermem` | Load GPU Direct RDMA kernel module |
| `mst start` | Start Mellanox Software Tools (kernel driver for firmware access) |
| `apt install nvidia-container-toolkit` | One-time Docker GPU passthrough setup |

---

## Complete Permission Checklist

```
┌──────────────────────────────────────────────────────────────────┐
│                     PERMISSION CHECKLIST                          │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  [ ] Member of 'docker' group                                     │
│  [ ] Member of 'video' group (host nvidia-smi)                    │
│  [ ] Member of 'render' group (host GPU access)                   │
│  [ ] Member of 'rdma' group (/dev/infiniband/*)                   │
│  [ ] Member of 'adm' group (logs, optional)                       │
│                                                                   │
│  [ ] memlock: unlimited in /etc/security/limits.d/                │
│  [ ] DefaultLimitMEMLOCK=infinity in systemd user.conf            │
│                                                                   │
│  [ ] udev rule: /dev/infiniband/* → rdma:0660                     │
│                                                                   │
│  [ ] Docker daemon running + socket accessible                    │
│  [ ] nvidia-container-toolkit installed                           │
│                                                                   │
│  [ ] sudo: /usr/bin/tee /proc/sys/vm/drop_caches                  │
│  [ ] sudo: /usr/sbin/ip link set dev enp1s0f0np0 mtu *            │
│  [ ] sudo: /usr/sbin/rmmod nvidia_uvm (emergency only)            │
│  [ ] sudo: /usr/sbin/modprobe nvidia_uvm (emergency only)         │
│  [ ] sudo: /usr/bin/apt, /usr/bin/apt-get (one-time packages)     │
│                                                                   │
│  One-time (root-only, not granted):                               │
│  [ ] RoCE enabled in ConnectX firmware (mlxconfig)                │
│  [ ] MTU set to 4096+ on enp1s0f0np0 (persistent)                │
│  [ ] nvidia-peermem loaded (GPU Direct RDMA)                      │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

## Verifying Your Setup

After setup, run as the benchmark user:

```bash
# Check group memberships
groups

# Check memlock limit (should show "unlimited")
ulimit -l

# Check Docker access
docker info

# Check GPU access
nvidia-smi

# Check RDMA device access
ibv_devinfo

# Run the comprehensive readiness check
cd rdma-testing
bash scripts/check-rdma-ready.sh
```