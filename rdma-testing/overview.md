# DGX Spark LLM Serving Benchmark — Holistic Overview

## Purpose

This repository benchmarks LLM inference serving on **NVIDIA DGX Spark** devices (Grace Blackwell GB10). The goal is to measure throughput, latency, goodput, and energy efficiency across different serving configurations: **single-node** (TP=1, PP=1), **tensor-parallel dual-node** (TP=2), and **pipeline-parallel dual-node** (PP=2). The results feed into an academic paper (PMBS26).

## Hardware Topology

```
┌──────────────────────────┐       ┌──────────────────────────┐
│       Diya (Head)        │       │      Hoedus (Worker)     │
│    DGX Spark / GB10      │       │    DGX Spark / GB10      │
│                          │       │                          │
│  IP: 10.10.10.1          │       │  IP: 10.10.10.2          │
│                          │       │                          │
│    ┌──────────────────┐  │       │  ┌──────────────────┐    │
│    │  Docker Container │  │       │  │  Docker Container │   │
│    │  vllm-spark       │  │       │  │  vllm-spark       │   │
│    │                   │  │       │  │                   │   │
│    │  Ray Head         │◄─┼──TCP──┼──┤  Ray Worker      │   │
│    │  vLLM Server      │  │       │  │  vLLM Worker      │   │
│    │  NCCL (TCP)       │  │       │  │  NCCL (TCP)       │   │
│    └────────┬──────────┘  │       │  └────────┬──────────┘   │
│             │             │       │           │              │
│    ┌────────┴──────────┐  │       │  ┌────────┴──────────┐   │
│    │ ConnectX-7 NIC    │◄─┼──200GbE─┼──┤ ConnectX-7 NIC   │   │
│    │ enp1s0f0np0       │  │ Direct │  │ enp1s0f0np0       │  │
│    └───────────────────┘  │ Attach │  └───────────────────┘   │
└──────────────────────────┘       └──────────────────────────┘
```

- **Two nodes** directly connected via 200GbE ConnectX-7 NIC on `enp1s0f0np0`
- **/etc/hosts** maps `Diya → 10.10.10.1`, `Hoedus → 10.10.10.2`
- Currently communicating via **TCP sockets** over the ConnectX link (~2.08 GB/s measured all-reduce bandwidth)
- **Planned upgrade**: 4 nodes with a network switch + **RDMA/RoCE** instead of TCP

## Architecture Layers

```
┌─────────────────────────────────────────────────┐
│                  Orchestration                   │
│  scripts/run_job.sh                             │
│    → Full unattended campaign                   │
│    → Calls run_models.sh per config             │
├─────────────────────────────────────────────────┤
│                  Per-Model Loop                  │
│  scripts/run_models.sh                          │
│    → Iterates over models in configs/models.yaml│
│    → Per model: launch server, run sweep, teardown│
├─────────────────────────────────────────────────┤
│             Launch & Concurrency Sweep           │
│  scripts/launch_server.sh  (vLLM serve)         │
│  scripts/run_all.sh        (sweep concurrencies)│
│    → Calls bench/run_benchmark.py per (rep, c)  │
├─────────────────────────────────────────────────┤
│              Ray Cluster Management              │
│  scripts/start_ray.sh (head|worker|stop)         │
│    → Docker containers, Ray install, cluster     │
├─────────────────────────────────────────────────┤
│             Benchmark Framework (Python)          │
│  bench/config.py     → ExperimentConfig          │
│  bench/run_benchmark.py → Main entry point       │
│  bench/runner.py     → Warmup/measure/cooldown   │
│  bench/workload.py   → Request generation        │
│  bench/client.py     → HTTP client to vLLM       │
│  bench/metrics.py    → Aggregation (TTFT, ITL)   │
│  bench/monitors.py   → Power + vLLM metrics      │
├─────────────────────────────────────────────────┤
│              Interconnect Benchmarks             │
│  scripts/ray_allreduce_bench.sh    (all-reduce)   │
│  scripts/interconnect_extra_bench.sh (all-reduce + send/recv) │
│  scripts/interconnect_microbench.sh (nccl-tests)  │
└─────────────────────────────────────────────────┘
```

## Key Configuration Files

### 1. `scripts/connectx_env.sh` — The Central Network Config
This is the single most important configuration file. Every script sources it. Key variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CX_IFACE` | `enp1s0f0np0` | ConnectX network interface |
| `DIYA_IP` | `10.10.10.1` | Head node IP |
| `HOEDUS_IP` | `10.10.10.2` | Worker node IP |
| `HEAD_IP` | `$DIYA_IP` | Ray head IP |
| `NCCL_SOCKET_IFNAME` | `$CX_IFACE` | Interface for NCCL sockets |
| `GLOO_SOCKET_IFNAME` | `$CX_IFACE` | Interface for Gloo |
| `NCCL_IB_DISABLE` | **`1`** | **Disables InfiniBand/RDMA (forces TCP)** |
| `NCCL_DEBUG` | `WARN` | NCCL logging level |
| `VLLM_IMAGE` | `nvcr.io/nvidia/vllm@sha256:...` | Pinned Docker image |
| `VLLM_CONTAINER` | `vllm-spark` | Container name |
| `RAY_VERSION` | `2.53.0` | Pinned Ray version |

### 2. `configs/experiment.yaml` — Benchmark Parameters
Defines workload buckets, measurement windows, SLOs, and power monitoring settings.

### 3. `configs/models.yaml` — Model Matrix
Lists models to benchmark with their revisions, precision, and supported serving configs.

## Script Flow

### Starting a Full Campaign:
```bash
# On HEAD (Diya):
scripts/start_ray.sh head

# On WORKER (Hoedus):
scripts/start_ray.sh worker

# On HEAD (Diya), in tmux:
scripts/run_job.sh
```

### What `run_job.sh` Does:
1. **Prep** (aborts on failure): validates environment, Ray health, head cache, mirrors to worker, all-reduce microbenchmark
2. **Canaries** (optional): Launch each tp2/pp2 model once to verify streaming contract
3. **TP2 sweep**: All models with `tp2` config, all concurrencies, with resume support
4. **PP2 sweep**: All models with `pp2` config
5. **Single sweep** (LAST): Tears down Ray cluster — nothing dual-node can run after

### Running a Single Model Manually:
```bash
# 1. Start Ray cluster
scripts/start_ray.sh head    # on Diya
scripts/start_ray.sh worker  # on Hoedus

# 2. Launch server
MODEL="Qwen/Qwen3-8B" scripts/launch_server.sh tp2

# 3. Run sweep
scripts/run_all.sh tp2

# 4. Or use run_models.sh for automated model cycling:
scripts/run_models.sh tp2 --skip-done
```

### Interconnect Benchmarks:
```bash
# All-reduce via Ray (no SSH needed):
scripts/ray_allreduce_bench.sh

# All-reduce + send/recv via Ray:
scripts/interconnect_extra_bench.sh

# All-reduce via nccl-tests + mpirun (needs SSH):
scripts/interconnect_microbench.sh
```

## Where NCCL Transport Is Configured

NCCL transport selection is controlled by **two places**:

### 1. `connectx_env.sh` (line ~37):
```bash
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"   # 1 = TCP, 0 = RDMA/RoCE
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"
export NCCL_SOCKET_IFNAME="$CX_IFACE"
```

### 2. `start_ray.sh` (lines ~40-45):
```bash
docker_common=( --name "$VLLM_CONTAINER" --gpus all --network host --ipc=host
  -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG"
  ...
)
```

### 3. `launch_server.sh` (single-node mode only):
```bash
run_env=( -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
          -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG"
          ...
)
```

**Key insight**: For tp2/pp2 (dual-node), NCCL env vars are baked into the Ray container at `start_ray.sh` time. They are NOT re-read at `launch_server.sh` time. To change NCCL transport, you must restart Ray on **both** nodes with the new settings.

### 4. The interconnect benchmarks also read these same env vars:
- `ray_allreduce_bench.sh` and `interconnect_extra_bench.sh` pass `NCCL_IB_DISABLE` and `NCCL_DEBUG` into the container
- Inside the Python workers, NCCL respects these env vars at `dist.init_process_group` time

## Current State: TCP Only
- `NCCL_IB_DISABLE=1` is hard-defaulted everywhere
- The ConnectX-7 NIC is used as a plain Ethernet NIC via TCP sockets
- Measured peak all-reduce bandwidth: ~2.08 GB/s
- Comment in `connectx_env.sh`: *"ConnectX is an Ethernet NIC. Keep NCCL on sockets unless you have verified RoCE is configured, in which case set NCCL_IB_DISABLE=0"*

## What Needs to Change for RDMA/RoCE

1. **NIC firmware/config**: Ensure ConnectX-7 has RoCE enabled and firmware supports it
2. **Network config**: RDMA CM, RoCEv2 configuration, PFC (Priority Flow Control) if using a switch
3. **MTU**: Jumbo frames (e.g., 4096 or 9000 bytes)
4. **`connectx_env.sh`**: Set `NCCL_IB_DISABLE=0`, add `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`, etc.
5. **Ray restart**: Both `start_ray.sh` calls must use new NCCL settings
6. **Verification**: Run interconnect benchmarks and confirm RDMA transport