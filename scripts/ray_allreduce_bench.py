#!/usr/bin/env python3
"""Two-node NCCL all-reduce microbenchmark over the shared Ray cluster.

The nccl-tests path (scripts/interconnect_microbench.sh) needs mpirun with SSH
from head to worker, which this cluster does not have. This script runs inside
the serving container on the head node (see scripts/ray_allreduce_bench.sh),
pins one actor per Ray node, and times torch.distributed all-reduce across the
ConnectX link for sizes from 8 B to 1 GiB. The numbers feed the paper's
communication model for TP2 (per-layer all-reduce latency) and PP2 (per-stage
activation transfer bandwidth).

Run it while NO model is being served. Each task reserves its node's GPU through
Ray, and the shell wrapper also rejects a live vLLM process.

Prints one CSV row per size:  bytes,avg_s,min_s,algbw_GBps,busbw_GBps
(busbw equals algbw for all-reduce at world size 2: 2(n-1)/n = 1).

Usage (inside the container):
    python3 ray_allreduce_bench.py HEAD_IP WORKER_IP IFACE [PORT]
"""
from __future__ import annotations
import sys
import time

import ray


def _worker(rank, world, head_ip, iface, port, sizes, iters, warmup):
    import os
    os.environ.setdefault("NCCL_SOCKET_IFNAME", iface)
    os.environ.setdefault("GLOO_SOCKET_IFNAME", iface)
    os.environ.setdefault("NCCL_IB_DISABLE", os.environ.get("NCCL_IB_DISABLE", "1"))
    import torch
    import torch.distributed as dist

    dist.init_process_group(
        "nccl", init_method=f"tcp://{head_ip}:{port}",
        rank=rank, world_size=world)
    dev = torch.device("cuda:0")
    results = []
    for size in sizes:
        n = max(1, size // 4)                    # float32 elements
        buf = torch.ones(n, dtype=torch.float32, device=dev)
        for _ in range(warmup):
            dist.all_reduce(buf)
        torch.cuda.synchronize(dev)
        dist.barrier()
        times = []
        for _ in range(iters):
            torch.cuda.synchronize(dev)
            t0 = time.perf_counter()
            dist.all_reduce(buf)
            torch.cuda.synchronize(dev)
            times.append(time.perf_counter() - t0)
        avg = sum(times) / len(times)
        results.append((size, avg, min(times)))
    dist.destroy_process_group()
    return results


def main() -> int:
    if len(sys.argv) < 4:
        sys.stderr.write("usage: ray_allreduce_bench.py HEAD_IP WORKER_IP "
                         "IFACE [PORT]\n")
        return 2
    head_ip, worker_ip, iface = sys.argv[1], sys.argv[2], sys.argv[3]
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 29517

    sizes, s = [], 8
    while s <= (1 << 30):
        sizes.append(s)
        s *= 2
    iters, warmup = 20, 5

    ray.init(address="auto", log_to_driver=True)
    remotes = []
    for rank, ip in enumerate([head_ip, worker_ip]):
        fn = ray.remote(num_cpus=0, num_gpus=1,
                        resources={f"node:{ip}": 0.001})(_worker)
        remotes.append(fn.remote(rank, 2, head_ip, iface, port,
                                 sizes, iters, warmup))
    results = ray.get(remotes, timeout=1800)[0]   # rank 0's timings

    print("bytes,avg_s,min_s,algbw_GBps,busbw_GBps")
    for size, avg, mn in results:
        bw = size / avg / 1e9
        print(f"{size},{avg:.6e},{mn:.6e},{bw:.3f},{bw:.3f}")
    lat = next((mn for size, _, mn in results if size == 8), None)
    if lat is not None:
        sys.stderr.write(f"[microbench] 8B all-reduce min latency: "
                         f"{lat * 1e6:.1f} us\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
