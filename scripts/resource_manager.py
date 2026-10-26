#!/usr/bin/env python3
"""Report whether benchmark and cache resources are idle on this cluster."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable


# Ray worker actors: RayWorkerWrapper in older vLLM (0.19), RayWorkerProc in
# the pinned vLLM 0.29.0 (process title ray::RayWorkerProc...).
SERVING_MARKERS = ("vllm serve", "RayWorkerWrapper", "RayWorkerProc", "EngineCore")
CACHE_MARKERS = (
    "download_hf_snapshot.py",
    "predownload.sh",
    "mirror_worker_cache.py",
    "mirror_worker_cache.sh",
    "purge_model_cache_container.py",
    "purge_model_cache.sh",
    "hf cache prune",
)


def process_command_lines() -> Iterable[tuple[int, str]]:
    """Yield readable process command lines from this node's procfs."""
    ancestors = {os.getpid()}
    parent = os.getpid()
    while True:
        try:
            with open(f"/proc/{parent}/stat", encoding="ascii") as handle:
                stat = handle.read()
            fields = stat[stat.rfind(")") + 2 :].split()
            parent = int(fields[1])
        except (OSError, IndexError, ValueError):
            break
        if parent <= 1 or parent in ancestors:
            break
        ancestors.add(parent)

    # Tests point RESOURCE_PROC_ROOT at a fake procfs so the guard does not see
    # unrelated vLLM servers on the machine running the suite.
    proc_root = os.environ.get("RESOURCE_PROC_ROOT", "/proc")
    for entry in os.listdir(proc_root):
        if not entry.isdigit() or int(entry) in ancestors:
            continue
        try:
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as handle:
                command = handle.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if command:
            yield int(entry), command.strip()


def matching_processes(mode: str) -> list[str]:
    markers = SERVING_MARKERS
    if mode == "all":
        markers += CACHE_MARKERS
    return [
        f"pid {pid}: {command[:200]}"
        for pid, command in process_command_lines()
        if any(marker in command for marker in markers)
    ]


def docker_container_processes(mode: str) -> list[str]:
    """Check all local containers, including containers hidden by procfs."""
    try:
        containers = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot inspect local Docker containers: {exc}") from exc

    markers = SERVING_MARKERS
    if mode == "all":
        markers += CACHE_MARKERS
    found: list[str] = []
    for name in containers:
        try:
            result = subprocess.run(
                ["docker", "top", name, "-eo", "pid,args"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            try:
                current_containers = subprocess.run(
                    ["docker", "ps", "--format", "{{.Names}}"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout.splitlines()
            except (OSError, subprocess.SubprocessError) as inspect_exc:
                raise RuntimeError(
                    f"cannot refresh local Docker containers: {inspect_exc}"
                ) from inspect_exc
            if name not in current_containers:
                continue
            failure_detail = ""
            if isinstance(exc, subprocess.CalledProcessError):
                stderr = (exc.stderr or "").strip()
                failure_detail = f" (exit {exc.returncode})"
                if stderr:
                    failure_detail += f": {stderr}"
            raise RuntimeError(
                f"cannot inspect processes in container {name}{failure_detail}"
            ) from exc
        for line in result.stdout.splitlines()[1:]:
            if any(marker in line for marker in markers):
                found.append(f"container {name}: {line[:200]}")
    return found


def check_local(mode: str) -> int:
    try:
        busy = matching_processes(mode) + docker_container_processes(mode)
    except RuntimeError as exc:
        print(f"[resources] UNKNOWN: {exc}", file=sys.stderr)
        return 2
    if busy:
        print("[resources] BUSY on local node:")
        for process in busy[:8]:
            print(f"  {process}")
        return 1
    print("[resources] local node is idle")
    return 0


def check_ray_nodes(
    head_ip: str, worker_ip: str, mode: str, allow_missing_worker: bool
) -> int:
    try:
        import ray

        ray.init(address="auto", log_to_driver=False)
        active_nodes = {
            node["NodeManagerAddress"]
            for node in ray.nodes()
            if node.get("Alive") and node.get("NodeManagerAddress")
        }
        required_nodes = {head_ip}
        if not allow_missing_worker:
            required_nodes.add(worker_ip)
        missing = required_nodes - active_nodes
        if missing:
            raise RuntimeError(
                "Ray is missing required active node(s): " + ", ".join(sorted(missing))
            )

        inspect = ray.remote(num_cpus=0)(matching_processes)
        processes_by_node: dict[str, list[str]] = {}
        for node_ip in sorted(active_nodes):
            processes_by_node[node_ip] = ray.get(
                inspect.options(resources={f"node:{node_ip}": 0.001}).remote(mode),
                timeout=30,
            )
    except Exception as exc:  # Availability checks fail closed on Ray errors.
        print(
            f"[resources] UNKNOWN: cannot inspect Ray nodes from {worker_ip}: {exc}",
            file=sys.stderr,
        )
        return 2

    busy_nodes = {ip: items for ip, items in processes_by_node.items() if items}
    if busy_nodes:
        for node_ip, processes in busy_nodes.items():
            print(f"[resources] BUSY on Ray node {node_ip}:")
            for process in processes[:8]:
                print(f"  {process}")
        return 1
    print("[resources] active Ray nodes are idle: " + ", ".join(sorted(active_nodes)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("all", "serving"), default="all")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--allow-missing-worker", action="store_true")
    parser.add_argument("--head-ip", default="")
    parser.add_argument("--worker-ip", default="")
    args = parser.parse_args()

    if args.worker:
        if not args.head_ip or not args.worker_ip:
            parser.error("--worker requires --head-ip and --worker-ip")
        return check_ray_nodes(
            args.head_ip,
            args.worker_ip,
            args.mode,
            args.allow_missing_worker,
        )
    return check_local(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
