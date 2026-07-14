#!/usr/bin/env python3
"""Mirror HF model repos from the head node's cache into each worker's cache.

Runs INSIDE the head's vLLM container (docker exec'd by mirror_worker_cache.sh),
where /root/.cache/huggingface is the head's cache mount and Ray reaches every
node. The worker (Hoedus) cannot download from huggingface.co (no DNS) and
there is no SSH from head to worker, so this is the only path that populates
its cache. With HF_HUB_OFFLINE=1 the head hands remote Ray workers a resolved
local snapshot path, so a tp2/pp2 launch needs the exact same snapshot revision
present on every node; a miss surfaces as HFValidationError from RayWorkerWrapper.

Control goes over Ray; the bytes go over a raw TCP stream on the cluster
network (GNU tar at both ends), which the object store cannot match for
hundreds of GiB. Per repo, only files missing or size-mismatched on the worker
are sent, so reruns after an interrupted transfer resume at file granularity.

Received files are fsync'd and then dropped from the worker's page cache
(POSIX_FADV_DONTNEED): on Grace unified memory CUDA free tracks MemFree, so
leaving hundreds of GiB in page cache would make the next vLLM launch refuse
to start (see launch_server.sh preflight).

Usage: mirror_worker_cache.py REPO [REPO...]     # e.g. Qwen/Qwen3-32B
Env:   MIRROR_MARGIN_GB  free disk to keep on the worker (default 10)
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

import ray

HUB = "/root/.cache/huggingface/hub"
MARGIN = int(os.environ.get("MIRROR_MARGIN_GB", "10")) << 30
CHUNK = 4 << 20  # sendall unit; big enough to keep the NIC busy


def repo_dir(repo: str) -> str:
    return "models--" + repo.replace("/", "--")


def manifest(dirname: str) -> tuple[dict[str, int], dict[str, str]]:
    """Regular files (rel path -> size) and symlinks (rel path -> target)."""
    files: dict[str, int] = {}
    links: dict[str, str] = {}
    base = os.path.join(HUB, dirname)
    for root, _dirs, names in os.walk(base):
        for name in names:
            if name.endswith((".incomplete", ".lock")):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, HUB)
            if os.path.islink(path):
                links[rel] = os.readlink(path)
            else:
                files[rel] = os.path.getsize(path)
    return files, links


def drop_from_page_cache(paths: list[str]) -> None:
    for path in paths:
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)  # DONTNEED only evicts clean pages
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


@ray.remote(num_cpus=0)
class Receiver:
    """Runs on a worker node; unpacks one tar stream per transfer."""

    def missing(self, files: dict[str, int], links: dict[str, str]) -> list[str]:
        need = []
        for rel, size in files.items():
            path = os.path.join(HUB, rel)
            if os.path.islink(path) or not os.path.isfile(path) \
                    or os.path.getsize(path) != size:
                need.append(rel)
        for rel, target in links.items():
            path = os.path.join(HUB, rel)
            if not os.path.islink(path) or os.readlink(path) != target:
                need.append(rel)
        return sorted(need)

    def disk_free(self) -> int:
        os.makedirs(HUB, exist_ok=True)
        st = os.statvfs(HUB)
        return st.f_bavail * st.f_frsize

    def start(self) -> int:
        import threading

        self.result: dict = {}
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.listen(1)

        def pump() -> None:
            try:
                conn, _ = self.sock.accept()
                # --overwrite: a rerun may be replacing a truncated file
                proc = subprocess.Popen(
                    ["tar", "-C", HUB, "-xf", "-", "--overwrite"],
                    stdin=conn.fileno())
                self.result["rc"] = proc.wait()
                conn.close()
            except Exception as exc:  # surfaced via finish()
                self.result["error"] = repr(exc)

        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()
        return self.sock.getsockname()[1]

    def finish(self, sent: list[str]) -> dict:
        self.thread.join(timeout=600)
        self.sock.close()
        drop_from_page_cache([os.path.join(HUB, rel) for rel in sent])
        return self.result


def mirror_repo(repo: str, receiver, ip: str) -> tuple[bool, str]:
    dirname = repo_dir(repo)
    base = os.path.join(HUB, dirname)
    if not os.path.isdir(base):
        return False, f"not in the head cache; run scripts/predownload.sh first"
    files, links = manifest(dirname)
    weights = (".safetensors", ".gguf", ".bin", ".pth")
    if not any(rel.endswith(weights) for rel in files) \
            and not any(rel.endswith(weights) for rel in links):
        return False, "head cache has no weights (metadata-only stub); predownload it"

    need = ray.get(receiver.missing.remote(files, links))
    if not need:
        return True, "already complete on worker; skipped"

    need_bytes = sum(files.get(rel, 0) for rel in need)
    free = ray.get(receiver.disk_free.remote())
    if free < need_bytes + MARGIN:
        return False, (f"worker disk too low: {free / 2**30:.1f} GiB free, "
                       f"need {need_bytes / 2**30:.1f} + {MARGIN / 2**30:.0f} margin")

    port = ray.get(receiver.start.remote())
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as tmp:
        tmp.write("\n".join(need) + "\n")
        listfile = tmp.name
    tar = subprocess.Popen(
        ["tar", "-C", HUB, "-cf", "-", "--verbatim-files-from", "-T", listfile],
        stdout=subprocess.PIPE)
    sock = socket.create_connection((ip, port), timeout=60)
    sent = 0
    t0 = last = time.monotonic()
    try:
        while True:
            chunk = tar.stdout.read(CHUNK)
            if not chunk:
                break
            sock.sendall(chunk)
            sent += len(chunk)
            now = time.monotonic()
            if now - last >= 10:
                rate = sent / (now - t0) / 2**20
                print(f"[mirror]   ... {sent / 2**30:.1f} GiB sent "
                      f"({rate:.0f} MiB/s)", flush=True)
                last = now
        sock.shutdown(socket.SHUT_WR)
    finally:
        sock.close()
        os.unlink(listfile)
    if tar.wait() != 0:
        ray.get(receiver.finish.remote([]), timeout=900)
        return False, f"tar -c exited {tar.returncode} on the head"

    result = ray.get(receiver.finish.remote(need), timeout=900)
    drop_from_page_cache([os.path.join(HUB, rel) for rel in need])  # head side
    if result.get("error") or result.get("rc") != 0:
        return False, f"worker-side tar failed: {result}"

    left = ray.get(receiver.missing.remote(files, links))
    if left:
        return False, f"{len(left)} file(s) still missing after transfer: {left[:3]}"
    secs = time.monotonic() - t0
    return True, (f"sent {sent / 2**30:.1f} GiB in {secs:.0f}s "
                  f"({sent / secs / 2**20:.0f} MiB/s), verified")


def main() -> int:
    repos = sys.argv[1:]
    if not repos:
        print("usage: mirror_worker_cache.py REPO [REPO...]", file=sys.stderr)
        return 2

    ray.init(address="auto", log_to_driver=False)
    head_ip = ray.util.get_node_ip_address()
    worker_ips = sorted(
        node["NodeManagerAddress"] for node in ray.nodes()
        if node.get("Alive") and node["NodeManagerAddress"] != head_ip)
    if not worker_ips:
        print("[mirror] no alive worker nodes in the Ray cluster", file=sys.stderr)
        return 1

    # smallest first so quick wins land before a long transfer can fail
    sizes = {r: sum(manifest(repo_dir(r))[0].values())
             if os.path.isdir(os.path.join(HUB, repo_dir(r))) else 0
             for r in repos}
    repos = sorted(repos, key=lambda r: sizes[r])

    failures = 0
    for ip in worker_ips:
        print(f"[mirror] worker {ip}")
        receiver = Receiver.options(resources={f"node:{ip}": 0.001}).remote()
        for repo in repos:
            print(f"[mirror] === {repo} ({sizes[repo] / 2**30:.1f} GiB) ===", flush=True)
            ok, msg = mirror_repo(repo, receiver, ip)
            print(f"[mirror] {'done' if ok else 'FAILED'} {repo}: {msg}", flush=True)
            failures += 0 if ok else 1
        ray.kill(receiver)

    if failures:
        print(f"[mirror] {failures} repo(s) failed; see above", file=sys.stderr)
        return 1
    print("[mirror] all repos mirrored and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
