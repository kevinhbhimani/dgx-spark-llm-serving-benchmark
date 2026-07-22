#!/usr/bin/env python3
"""Two-node interconnect microbench: all-reduce AND point-to-point over Ray.

This is the supplement to scripts/ray_allreduce_bench.py that the PMBS paper's
"why TP loses" section needs but the sealed campaign did not collect:

  * all-reduce (the TP=2 collective: two per layer per decode step), and
  * send/recv ping-pong (the PP=2 primitive: one activation hand-off per stage
    boundary) -- so the paper can *measure* the PP interconnect cost instead of
    asserting it, and contrast it with the TP all-reduce cost on the same link.

It also records the transport NCCL actually used, taken from the Ray workers'
environment (NCCL_IB_DISABLE plus the socket-thread knobs). Transport is fixed
when the Ray workers are launched (scripts/start_ray.sh reads NCCL_IB_DISABLE),
so this script does NOT override it -- it measures whatever the running cluster
uses and labels the rows accordingly. To compare transports, restart Ray on
both nodes under each setting and run this once per setting:

    # TCP sockets (the sealed-campaign default)
    scripts/start_ray.sh head          # Hoedus: scripts/start_ray.sh worker
    scripts/interconnect_extra_bench.sh

    # RoCE / RDMA
    NCCL_IB_DISABLE=0 scripts/start_ray.sh head    # and on Hoedus
    scripts/interconnect_extra_bench.sh

    # multi-stream sockets (no RDMA, cheap tuning)
    NCCL_NSOCKS_PERTHREAD=4 NCCL_SOCKET_NTHREADS=4 scripts/start_ray.sh head
    scripts/interconnect_extra_bench.sh

Like the all-reduce bench it runs with NO model being served (each task
reserves its node's GPU through Ray and the wrapper rejects a live vLLM).

Prints CSV rows:  collective,transport,bytes,avg_s,min_s,algbw_GBps,busbw_GBps
For world size 2 all-reduce busbw == algbw (2(n-1)/n = 1); for the send/recv
ping-pong avg_s/min_s are one-way (round-trip halved) and busbw is the
one-way size/time.

Usage (inside the container):
    python3 ray_interconnect_bench.py HEAD_IP WORKER_IP IFACE [PORT]
"""
from __future__ import annotations
import math
import subprocess
import sys
import time

import ray
from ray.exceptions import GetTimeoutError

SIZES = [1 << exponent for exponent in range(3, 31)]  # 8 B .. 1 GiB
ITERS, WARMUP = 20, 5


def _gpu_processes() -> list[str]:
    """Return local compute processes without creating a CUDA context."""
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {completed.stderr.strip()}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _transport_label() -> str:
    """Name the transport from the worker's NCCL environment.

    NCCL selects its network at communicator init from these variables, which
    scripts/start_ray.sh bakes into the worker container, so reading them here
    reports the transport this measurement actually ran on. Not a substitute
    for an NCCL_DEBUG=INFO audit, but deterministic and enough to label rows.
    """
    import os

    if os.environ.get("NCCL_IB_DISABLE", "1") == "0":
        return "roce"
    nsocks = int(os.environ.get("NCCL_NSOCKS_PERTHREAD", "1") or "1")
    nthreads = int(os.environ.get("NCCL_SOCKET_NTHREADS", "1") or "1")
    if nsocks * nthreads > 1:
        return f"tcp-tuned-x{nsocks * nthreads}"
    return "tcp"


def _worker(rank, world, head_ip, iface, port, collective):
    import datetime
    import os

    os.environ["NCCL_SOCKET_IFNAME"] = iface
    os.environ["GLOO_SOCKET_IFNAME"] = iface
    # Do NOT force NCCL_IB_DISABLE here: the transport is whatever the Ray
    # worker was launched with, and overriding it per-process would mislabel
    # the measurement. We only read it (via _transport_label) for the CSV.
    import torch
    import torch.distributed as dist

    transport = _transport_label()
    initialized = False
    try:
        dist.init_process_group(
            "nccl",
            init_method=f"tcp://{head_ip}:{port}",
            rank=rank,
            world_size=world,
            timeout=datetime.timedelta(minutes=10),
        )
        initialized = True
        dev = torch.device("cuda:0")
        results = []
        for size in SIZES:
            n = max(1, size // 4)
            buf = torch.ones(n, dtype=torch.float32, device=dev)
            if collective == "allreduce":
                for _ in range(WARMUP):
                    dist.all_reduce(buf)
                torch.cuda.synchronize(dev)
                dist.barrier()
                times = []
                for _ in range(ITERS):
                    torch.cuda.synchronize(dev)
                    t0 = time.perf_counter()
                    dist.all_reduce(buf)
                    torch.cuda.synchronize(dev)
                    times.append(time.perf_counter() - t0)
                avg, mn = sum(times) / len(times), min(times)
            elif collective == "sendrecv":
                # Blocking ping-pong: rank 0 sends then receives, rank 1
                # mirrors. Round-trip halved is the one-way activation-transfer
                # cost -- exactly one PP stage-boundary hand-off per token.
                peer = 1 - rank
                for _ in range(WARMUP):
                    if rank == 0:
                        dist.send(buf, dst=peer)
                        dist.recv(buf, src=peer)
                    else:
                        dist.recv(buf, src=peer)
                        dist.send(buf, dst=peer)
                torch.cuda.synchronize(dev)
                dist.barrier()
                times = []
                for _ in range(ITERS):
                    torch.cuda.synchronize(dev)
                    t0 = time.perf_counter()
                    if rank == 0:
                        dist.send(buf, dst=peer)
                        dist.recv(buf, src=peer)
                    else:
                        dist.recv(buf, src=peer)
                        dist.send(buf, dst=peer)
                    torch.cuda.synchronize(dev)
                    times.append((time.perf_counter() - t0) / 2.0)  # one way
                avg, mn = sum(times) / len(times), min(times)
            else:
                raise ValueError(f"unknown collective {collective!r}")
            results.append((size, avg, mn))
        return transport, results
    finally:
        if initialized:
            dist.destroy_process_group()


def _run(collective, head_ip, worker_ip, iface, port) -> list[tuple]:
    remotes = []
    for rank, ip in enumerate([head_ip, worker_ip]):
        fn = ray.remote(
            num_cpus=0, num_gpus=1, resources={f"node:{ip}": 0.001}
        )(_worker)
        remotes.append(fn.remote(rank, 2, head_ip, iface, port, collective))
    try:
        all_results = ray.get(remotes, timeout=1800)
    except GetTimeoutError:
        for ref in remotes:
            ray.cancel(ref, force=True)
        raise SystemExit(f"[microbench] {collective} tasks timed out after 1800s")
    transport = all_results[0][0]
    for _, rank_results in all_results:
        if [row[0] for row in rank_results] != SIZES:
            raise SystemExit(
                f"[microbench] a rank returned an incomplete {collective} sweep")
    return transport, all_results[0][1]


def main() -> int:
    if not 4 <= len(sys.argv) <= 5:
        sys.stderr.write(
            "usage: ray_interconnect_bench.py HEAD_IP WORKER_IP IFACE [PORT]\n")
        return 2
    head_ip, worker_ip, iface = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        port = int(sys.argv[4]) if len(sys.argv) > 4 else 29518
    except ValueError as exc:
        sys.stderr.write(f"[microbench] invalid PORT: {exc}\n")
        return 2
    if not 1 <= port <= 65535:
        sys.stderr.write("[microbench] PORT must be in [1, 65535]\n")
        return 2

    ray.init(address="auto", log_to_driver=True)
    alive = sorted(
        node["NodeManagerAddress"] for node in ray.nodes() if node.get("Alive")
    )
    expected = sorted([head_ip, worker_ip])
    if alive != expected:
        sys.stderr.write(
            f"[microbench] expected exactly Ray nodes {expected}, got {alive}\n")
        return 1

    probes = []
    for ip in [head_ip, worker_ip]:
        probe = ray.remote(num_cpus=0, resources={f"node:{ip}": 0.001})(
            _gpu_processes)
        probes.append(probe.remote())
    try:
        processes = ray.get(probes, timeout=60)
    except GetTimeoutError:
        for ref in probes:
            ray.cancel(ref, force=True)
        sys.stderr.write("[microbench] timed out checking both GPUs for idleness\n")
        return 1
    busy = [
        f"{ip}: {', '.join(lines)}"
        for ip, lines in zip([head_ip, worker_ip], processes)
        if lines
    ]
    if busy:
        sys.stderr.write(
            "[microbench] both GPUs must be idle; compute processes found: "
            + "; ".join(busy) + "\n")
        return 1

    print("collective,transport,bytes,avg_s,min_s,algbw_GBps,busbw_GBps")
    transport_seen = None
    for collective in ("allreduce", "sendrecv"):
        transport, results = _run(collective, head_ip, worker_ip, iface, port)
        transport_seen = transport
        for size, avg, mn in results:
            bw = size / avg / 1e9
            print(f"{collective},{transport},{size},{avg:.6e},{mn:.6e},"
                  f"{bw:.3f},{bw:.3f}")
        lat8 = next((mn for sz, _, mn in results if sz == 8), math.nan)
        bw1g = next((sz / avg for sz, avg, _ in results if sz == 1 << 30),
                    math.nan) / 1e9
        sys.stderr.write(
            f"[microbench] {collective} ({transport}): 8B one-hop "
            f"{lat8 * 1e6:.1f} us, 1GiB {bw1g:.2f} GB/s\n")
    sys.stderr.write(
        f"[microbench] transport measured: {transport_seen}. To compare, "
        "restart Ray on BOTH nodes under a different NCCL_IB_DISABLE / "
        "socket-thread setting and rerun.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
