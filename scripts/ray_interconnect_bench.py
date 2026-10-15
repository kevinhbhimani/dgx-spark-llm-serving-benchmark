#!/usr/bin/env python3
"""Two-node interconnect microbench: all-reduce AND point-to-point over Ray.

This is the supplement to scripts/ray_allreduce_bench.py that the PMBS paper's
"why TP loses" section needs but the sealed campaign did not collect:

  * all-reduce (the TP=2 collective: two per layer per decode step), and
  * send/recv ping-pong (the PP=2 primitive: one activation hand-off per stage
    boundary) -- so the paper can *measure* the PP interconnect cost instead of
    asserting it, and contrast it with the TP all-reduce cost on the same link.

It also records the transport NCCL actually used. The launch environment
(NCCL_IB_DISABLE plus the socket-thread knobs) only says which transport was
*requested*: NCCL_IB_DISABLE=0 permits RDMA but NCCL still falls back to
sockets when the RoCE handshake fails, so a run can post TCP numbers under a
"roce" label. To label rows by what actually carried the data, each worker
enables NCCL_DEBUG=INFO for its own process and reads the per-channel
"via NET/IB" / "via NET/Socket" lines from NCCL_DEBUG_FILE. Transport is fixed
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

# NCCL INFO log markers naming the transport each channel used. The
# per-channel lines are authoritative; the plugin-selection lines appear even
# when the data path falls back to sockets, so they are only a fallback.
_NCCL_IB_CHANNEL_MARKER = "via NET/IB"
_NCCL_SOCKET_CHANNEL_MARKER = "via NET/Socket"
_NCCL_IB_PLUGIN_MARKER = "NET/IB : Using"
_NCCL_SOCKET_PLUGIN_MARKER = "NET/Socket : Using"


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


def _socket_tuning_suffix() -> str:
    """Return "-tuned-xN" when socket threading is raised above the default."""
    import os

    nsocks = int(os.environ.get("NCCL_NSOCKS_PERTHREAD", "1") or "1")
    nthreads = int(os.environ.get("NCCL_SOCKET_NTHREADS", "1") or "1")
    return f"-tuned-x{nsocks * nthreads}" if nsocks * nthreads > 1 else ""


def _env_transport_label() -> str:
    """Name the transport the launch environment *requested* (fallback only).

    scripts/start_ray.sh bakes NCCL_IB_DISABLE and the socket-thread knobs into
    the worker container. These say which transport was asked for, not which
    one NCCL settled on, so this is used only when the NCCL log is unavailable.
    """
    import os

    if os.environ.get("NCCL_IB_DISABLE", "0") == "0":
        return "roce"
    return f"tcp{_socket_tuning_suffix()}"


def _transport_label(debug_path: str) -> str:
    """Name the transport NCCL actually selected, from its INFO log.

    NCCL_IB_DISABLE=0 only permits RDMA; NCCL still falls back to sockets when
    the RoCE handshake fails, so a run can post TCP numbers while the launch
    environment says RoCE. The per-channel lines record the transport each
    channel really used, so they are the authority; the plugin-selection lines
    are only a fallback because they appear even when the data path later falls
    back to sockets. When the log is missing or inconclusive, fall back to the
    launch environment and flag the label with a trailing "?" so the row is not
    read as a verified transport.
    """
    try:
        with open(debug_path, encoding="utf-8", errors="replace") as handle:
            nccl_log = handle.read()
    except OSError:
        nccl_log = ""
    if _NCCL_IB_CHANNEL_MARKER in nccl_log:
        transport = "roce"
    elif _NCCL_SOCKET_CHANNEL_MARKER in nccl_log:
        transport = "tcp"
    elif _NCCL_IB_PLUGIN_MARKER in nccl_log:
        transport = "roce"
    elif _NCCL_SOCKET_PLUGIN_MARKER in nccl_log:
        transport = "tcp"
    else:
        return f"{_env_transport_label()}?"
    if transport == "tcp":
        return f"tcp{_socket_tuning_suffix()}"
    return transport


def _worker(rank, world, head_ip, iface, port, collective):
    import datetime
    import os

    os.environ["NCCL_SOCKET_IFNAME"] = iface
    os.environ["GLOO_SOCKET_IFNAME"] = iface
    # Do NOT force NCCL_IB_DISABLE here: the transport is whatever the Ray
    # worker was launched with, and overriding it per-process would mislabel
    # the measurement. Instead, log NCCL's own selection so the CSV reports the
    # transport it actually used rather than the one the launch environment
    # requested. NCCL_DEBUG only affects diagnostics, never transport selection,
    # and is set on this process alone so serving containers keep the default.
    debug_path = f"/tmp/nccl_transport_rank{rank}_{os.getpid()}.log"
    os.environ["NCCL_DEBUG"] = "INFO"
    os.environ["NCCL_DEBUG_FILE"] = debug_path
    try:
        os.remove(debug_path)
    except OSError:
        pass
    import torch
    import torch.distributed as dist

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
        # NCCL selects its transport lazily, on the first collective, so read
        # the log only after the sweep has actually moved data.
        return _transport_label(debug_path), results
    finally:
        if initialized:
            dist.destroy_process_group()


def _run(collective, head_ip, worker_ip, iface, port) -> tuple[str, list[tuple]]:
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
    for _, rank_results in all_results:
        if [row[0] for row in rank_results] != SIZES:
            raise SystemExit(
                f"[microbench] a rank returned an incomplete {collective} sweep")
    labels = [result[0] for result in all_results]
    verified = sorted({label for label in labels if not label.endswith("?")})
    if len(verified) > 1:
        sys.stderr.write(
            f"[microbench] ranks disagree on transport: {verified}\n")
    # Prefer a log-verified label over an environment-only fallback.
    transport = verified[0] if verified else min(labels)
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
