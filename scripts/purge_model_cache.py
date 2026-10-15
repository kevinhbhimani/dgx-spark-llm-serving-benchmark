#!/usr/bin/env python3
"""Delete finished models' snapshots from the head and worker HF caches.

Runs inside the head vLLM/Ray container, as root and with the same cache mount
as serving (the cache is written by containers, so the host user usually
cannot delete it). The worker side runs as a Ray task pinned to the worker
node, the same no-SSH path mirror_worker_cache.py uses.

Only ``hub/models--<org>--<name>`` trees named by validated repo ids are
removed, plus their ``hub/.locks`` entries. With --compile-cache, the vLLM
torch.compile cache is cleared too; it lives in the container layer and grows
by one entry per model and parallel layout. Nothing is deleted on a node that
is still running a vLLM server or a Ray serving worker.

Usage: purge_model_cache.py [--worker IP] [--compile-cache] [--dry-run] REPO [...]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

HUB = "/root/.cache/huggingface/hub"
COMPILE_CACHE = os.path.join(
    os.environ.get("VLLM_CACHE_ROOT", "/root/.cache/vllm"), "torch_compile_cache"
)
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
# Command-line fragments of processes that may hold model files open.
BUSY_MARKERS = ("vllm serve", "RayWorkerWrapper", "EngineCore")


def repo_dir(repo: str) -> str:
    if not REPO_RE.fullmatch(repo) or ".." in repo:
        raise ValueError(f"refusing unsafe repo id {repo!r}")
    return "models--" + repo.replace("/", "--")


def tree_bytes(path: str) -> int:
    """Bytes of regular files under path; snapshot symlinks are not followed."""
    total = 0
    for root, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if not os.path.islink(os.path.join(root, name)):
                total += st.st_size
    return total


def busy_processes() -> list[str]:
    me = os.getpid()
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                cmdline = handle.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if any(marker in cmdline for marker in BUSY_MARKERS):
            found.append(f"{pid}: {cmdline[:120]}")
    return found


def purge_node(repos: list[str], compile_cache: bool, dry_run: bool) -> dict:
    """Delete the given repos on this node. Returns a JSON-safe report."""
    busy = busy_processes()
    if busy:
        return {"error": "serving processes still running: " + "; ".join(busy[:3])}
    hub_real = os.path.realpath(HUB)
    report = {"repos": [], "freed_bytes": 0, "dry_run": dry_run}
    for repo in repos:
        name = repo_dir(repo)
        target = os.path.join(HUB, name)
        if os.path.islink(target):
            return {"error": f"refusing symlinked cache entry {target}"}
        if os.path.dirname(os.path.realpath(target)) != hub_real:
            return {"error": f"cache entry escapes {HUB}: {target}"}
        if not os.path.isdir(target):
            report["repos"].append({"repo": repo, "bytes": 0, "status": "absent"})
            continue
        size = tree_bytes(target)
        if not dry_run:
            shutil.rmtree(target)
            lock = os.path.join(HUB, ".locks", name)
            if os.path.isdir(lock) and not os.path.islink(lock):
                shutil.rmtree(lock, ignore_errors=True)
        report["repos"].append({"repo": repo, "bytes": size,
                                "status": "would delete" if dry_run else "deleted"})
        report["freed_bytes"] += size
    if compile_cache and os.path.isdir(COMPILE_CACHE) \
            and not os.path.islink(COMPILE_CACHE):
        size = tree_bytes(COMPILE_CACHE)
        if not dry_run:
            for entry in os.listdir(COMPILE_CACHE):
                path = os.path.join(COMPILE_CACHE, entry)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
        report["compile_cache_bytes"] = size
        report["freed_bytes"] += size
    return report


def show(node: str, report: dict) -> bool:
    if report.get("error"):
        print(f"[purge] {node}: FAILED: {report['error']}", file=sys.stderr)
        return False
    for item in report["repos"]:
        print(f"[purge] {node}: {item['status']} {item['repo']} "
              f"({item['bytes'] / 1e9:.1f} GB)")
    if "compile_cache_bytes" in report:
        verb = "would clear" if report["dry_run"] else "cleared"
        print(f"[purge] {node}: {verb} vLLM compile cache "
              f"({report['compile_cache_bytes'] / 1e9:.2f} GB)")
    print(f"[purge] {node}: {report['freed_bytes'] / 1e9:.1f} GB "
          f"{'reclaimable' if report['dry_run'] else 'freed'}")
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worker", default="", help="Ray worker node IP to purge too")
    parser.add_argument("--compile-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("repos", nargs="+")
    args = parser.parse_args(argv)
    try:
        for repo in args.repos:
            repo_dir(repo)
    except ValueError as exc:
        print(f"[purge] {exc}", file=sys.stderr)
        return 2

    # Check the head before anything is deleted anywhere: the head runs every
    # serving driver, so a busy head means the worker may be busy too.
    busy = busy_processes()
    if busy:
        print("[purge] head: refusing while serving processes run: "
              + "; ".join(busy[:3]), file=sys.stderr)
        return 1

    ok = True
    if args.worker:
        import ray

        ray.init(address="auto", log_to_driver=False)
        alive = {node["NodeManagerAddress"] for node in ray.nodes() if node.get("Alive")}
        if args.worker not in alive:
            print(f"[purge] worker {args.worker} is not a live Ray node", file=sys.stderr)
            return 1
        task = ray.remote(num_cpus=0, resources={f"node:{args.worker}": 0.001})(purge_node)
        ok = show(f"worker {args.worker}",
                  ray.get(task.remote(args.repos, args.compile_cache, args.dry_run),
                          timeout=1800)) and ok
    ok = show("head", purge_node(args.repos, args.compile_cache, args.dry_run)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
