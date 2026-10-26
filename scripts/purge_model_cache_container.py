#!/usr/bin/env python3
"""Container-side implementation for purge_model_cache.sh.

Runs inside the head vLLM/Ray container, as root and with the same cache mount
as serving. The worker purge runs as a Ray task pinned to that node.

Only model revisions named by validated repo ids are removed. Hugging Face's
cache API keeps shared blobs that other cached revisions still use and removes
blobs with no remaining references. With --compile-cache, the vLLM
torch.compile cache is cleared too. Nothing is deleted on a node that is still
running a vLLM server or a Ray serving worker.

Usage: purge_model_cache_container.py [--worker IP] [--compile-cache]
                                       [--dry-run]
                                       [--prune-cache-garbage | --no-prune-cache-garbage]
                                       REPO [...]
"""
from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import subprocess
import sys
from typing import TypedDict

from huggingface_hub import scan_cache_dir

HUB = os.environ.get("HF_HUB_CACHE", "/hf-cache/hub")
COMPILE_CACHE = os.path.join(
    os.environ.get("VLLM_CACHE_ROOT", "/root/.cache/vllm"), "torch_compile_cache"
)
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
# Command-line fragments of processes that may hold model files open.
<<<<<<< HEAD
BUSY_MARKERS = ("vllm serve", "RayWorkerWrapper", "EngineCore")
=======
# Ray worker actors: RayWorkerWrapper in older vLLM (0.19), RayWorkerProc in
# the pinned vLLM 0.29.0 (process title ray::RayWorkerProc...).
BUSY_MARKERS = ("vllm serve", "RayWorkerWrapper", "RayWorkerProc", "EngineCore")
>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2


class RepoPurgeResult(TypedDict):
    repo: str
    bytes: int
    status: str


class PurgeReport(TypedDict, total=False):
    repos: list[RepoPurgeResult]
    freed_bytes: int
    dry_run: bool
    compile_cache_bytes: int
    cache_prune_output: str
    error: str


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


def prune_cache_garbage(dry_run: bool) -> tuple[str, int]:
    space_before = os.statvfs(HUB)
    free_before = space_before.f_bavail * space_before.f_frsize
    command = ["hf", "cache", "prune", "--cache-dir", HUB]
    command.append("--dry-run" if dry_run else "--yes")
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"hf cache prune exited {result.returncode}")
    if dry_run:
        return output, 0
    space_after = os.statvfs(HUB)
    free_after = space_after.f_bavail * space_after.f_frsize
    return output, max(0, free_after - free_before)


def purge_node(
    repos: list[str], compile_cache: bool, dry_run: bool, prune_garbage: bool = True
) -> PurgeReport:
    """Delete the given repos on this node. Returns a JSON-safe report."""
    busy = busy_processes()
    if busy:
        return {
            "error": "serving processes still running: " + "; ".join(busy[:3]),
            "repos": [],
            "freed_bytes": 0,
            "dry_run": dry_run,
        }
    os.makedirs(HUB, exist_ok=True)
    hub_real = os.path.realpath(HUB)
    cache_info = scan_cache_dir(cache_dir=HUB) if repos else None
    cached_repos = (
        {
            cached_repo.repo_id: cached_repo
            for cached_repo in cache_info.repos
            if cached_repo.repo_type == "model"
        }
        if cache_info is not None
        else {}
    )
    report: PurgeReport = {
        "repos": [],
        "freed_bytes": 0,
        "dry_run": dry_run,
    }
    revision_hashes: list[str] = []
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
        cached_repo = cached_repos.get(repo)
        if cached_repo is None:
            return {"error": f"Hugging Face cache scan could not resolve {repo} safely"}
        revision_hashes.extend(
            revision.commit_hash for revision in cached_repo.revisions
        )
        size = cached_repo.size_on_disk
        report["repos"].append({"repo": repo, "bytes": size,
                                "status": "would delete" if dry_run else "deleted"})
    if revision_hashes and cache_info is not None:
        delete_strategy = cache_info.delete_revisions(*revision_hashes)
        report["freed_bytes"] = int(delete_strategy.expected_freed_size)
        if not dry_run:
            delete_strategy.execute()
    if not dry_run:
        locks_root = os.path.join(HUB, ".locks")
        locks_root_real = os.path.join(hub_real, ".locks")
        if (
            os.path.isdir(locks_root)
            and not os.path.islink(locks_root)
            and os.path.realpath(locks_root) == locks_root_real
        ):
            for repo in repos:
                lock_directory = os.path.join(locks_root, repo_dir(repo))
                if (
                    os.path.islink(lock_directory)
                    or os.path.realpath(lock_directory)
                    != os.path.join(locks_root_real, repo_dir(repo))
                ):
                    continue
                try:
                    os.rmdir(lock_directory)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                        raise
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
    if prune_garbage:
        prune_output, pruned_bytes = prune_cache_garbage(dry_run)
        report["cache_prune_output"] = prune_output
        report["freed_bytes"] += pruned_bytes
    return report


def show(node: str, report: PurgeReport) -> bool:
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
    if "cache_prune_output" in report and report["cache_prune_output"]:
        print(f"[purge] {node}: {report['cache_prune_output']}")
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worker", default="", help="Ray worker node IP to purge too")
    parser.add_argument("--compile-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prune-cache-garbage",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="prune detached revisions, incomplete files, and unreferenced blobs (default: enabled)",
    )
    parser.add_argument("repos", nargs="*")
    args = parser.parse_args(argv)
    if not args.repos and not args.prune_cache_garbage:
        parser.error("provide at least one repo when --no-prune-cache-garbage is set")
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
                  ray.get(task.remote(
                      args.repos, args.compile_cache, args.dry_run,
                      args.prune_cache_garbage,
                  ),
                          timeout=1800)) and ok
    ok = show("head", purge_node(
        args.repos, args.compile_cache, args.dry_run, args.prune_cache_garbage
    )) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
