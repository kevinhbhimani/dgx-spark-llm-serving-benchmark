# RDMA Testing

Tools, guides, and scripts for transitioning the DGX Spark LLM serving benchmark from TCP to RDMA/RoCE.

## Directory Structure

```
rdma-testing/
├── README.md                        # This file
├── overview.md                      # Holistic overview of the parent benchmark repo
├── rdma-verification.md             # Guide: verifying RDMA usage, speed tests, TCP comparison
├── nccl-rdma-guide.md               # Guide: configuring NCCL for RDMA, with modified scripts
├── firmware-guide.md                # Guide: NVIDIA ConnectX-7 firmware issues & fixes
│
├── scripts/                         # Standalone RDMA testing utilities
│   ├── setup-benchmark-user.sh      # Create & configure user with all permissions
│   ├── check-rdma-ready.sh          # Read-only RDMA hardware readiness check
│   ├── bench-rdma-vs-tcp.sh          # Raw RDMA vs TCP bandwidth/latency comparison
│   └── verify-nccl-rdma.sh          # Verify NCCL is actually using RDMA transport
│
├── scripts-rdma/                    # Minimally modified copies of parent scripts for RDMA
│   ├── connectx_env_rdma.sh         # ← NCCL_IB_DISABLE=0 + RDMA vars
│   ├── start_ray_rdma.sh            # ← Passes RDMA NCCL vars to Docker
│   ├── launch_server_rdma.sh        # ← Passes RDMA NCCL vars to vLLM
│   ├── run_all_rdma.sh              # ← Sources RDMA env for concurrency sweeps
│   └── ray_allreduce_bench_rdma.sh  # ← RDMA interconnect bench
│
└── results/                         # Output directory for benchmark results
```

## Quick Start

### Step 0: Create the Benchmark User (One-Time)

On **both nodes**, as root:
```bash
cd rdma-testing
sudo bash scripts/setup-benchmark-user.sh benchmark
```

Then log out and back in as `benchmark` (or reboot) for group memberships to take effect.

### Step 1: Check RDMA Readiness

On **both nodes**:
```bash
cd rdma-testing
bash scripts/check-rdma-ready.sh
```

### Step 2: Compare TCP vs RDMA (Server + Client)

On **Hoedus** (worker) — leave this running:
```bash
cd rdma-testing
bash scripts/bench-rdma-vs-tcp.sh server
```

On **Diya** (head) — in another terminal:
```bash
cd rdma-testing
bash scripts/bench-rdma-vs-tcp.sh compare
```

### Step 3: Start Ray with RDMA

On **Diya**:
```bash
cd rdma-testing
bash scripts-rdma/start_ray_rdma.sh head
```

On **Hoedus**:
```bash
cd rdma-testing
bash scripts-rdma/start_ray_rdma.sh worker
```

### Step 4: Verify NCCL Uses RDMA

On **Diya**:
```bash
cd rdma-testing
bash scripts/verify-nccl-rdma.sh
```

Expected: "NCCL INFO NET/IB : Using [0]mlx5_0:1/RoCE"

### Step 5: Run Benchmarks

```bash
# Interconnect benchmark
cd rdma-testing
bash scripts-rdma/ray_allreduce_bench_rdma.sh

# Launch vLLM server with RDMA
bash scripts-rdma/launch_server_rdma.sh tp2

# Run concurrency sweep
bash scripts-rdma/run_all_rdma.sh tp2
```

## What Changed from the Parent Scripts

The `scripts-rdma/` directory contains minimally modified copies. Here are the exact changes:

### `connectx_env_rdma.sh` vs `scripts/connectx_env.sh`
- `NCCL_IB_DISABLE` defaults to **`0`** (was `1`) — this enables RDMA
- Added: `NCCL_IB_HCA=mlx5_0`, `NCCL_IB_GID_INDEX=3`, `NCCL_IB_TC=106`
- Added: `NCCL_IB_QPS_PER_CONNECTION=4`, `NCCL_IB_TIMEOUT=22`
- Added: `NCCL_NET_GDR_LEVEL=5`

### `start_ray_rdma.sh` vs `scripts/start_ray.sh`
- Sources `connectx_env_rdma.sh` instead of `connectx_env.sh`
- Added all RDMA NCCL vars to the Docker `-e` flags
- Added a log line showing NCCL_IB_DISABLE status

### `launch_server_rdma.sh` vs `scripts/launch_server.sh`
- Sources `connectx_env_rdma.sh` instead of `connectx_env.sh`
- Added all RDMA NCCL vars to `single` mode Docker run
- Added log line showing RDMA mode
- Note: for `tp2`/`pp2`, NCCL vars are inherited from the Ray container

### `run_all_rdma.sh` vs `scripts/run_all.sh`
- Sources `connectx_env_rdma.sh` instead of `connectx_env.sh`
- Added log line showing RDMA mode

### `ray_allreduce_bench_rdma.sh` vs `scripts/ray_allreduce_bench.sh`
- Sources `connectx_env_rdma.sh` instead of `connectx_env.sh`
- Added all RDMA NCCL vars to docker exec
- Updated log message for transport identification

## Important Notes

1. **No files outside `rdma-testing/` have been modified.** The original scripts remain untouched.

2. **For dual-node serving (tp2/pp2)**, you MUST restart Ray on BOTH nodes with the new RDMA settings. NCCL env vars are baked into the container at `start_ray.sh` time.

3. **The serving fingerprint includes `NCCL_IB_DISABLE`** — results with RDMA enabled will have different fingerprints and won't conflict with existing TCP results.

4. **Firmware issues**: If RDMA doesn't work, read `firmware-guide.md` for ConnectX-7 firmware troubleshooting.

5. **MTU**: Set to at least 4096 on both nodes for optimal RDMA performance:
   ```bash
   sudo ip link set dev enp1s0f0np0 mtu 4096
   ```