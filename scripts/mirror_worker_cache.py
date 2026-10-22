#!/usr/bin/env python3
"""Mirror exact pinned HF snapshots from the head cache to the Ray worker.

Runs inside the head vLLM container. Control goes over Ray and bytes go over a
raw ConnectX TCP stream. Only the requested ``snapshots/<commit>`` tree and the
blobs referenced by that tree are considered; stale revisions, refs, and
unrelated blobs in the same repository are never copied.

Received files are fsync'd and dropped from the worker page cache so model
weights do not consume Grace unified memory before vLLM starts.

Usage: mirror_worker_cache.py REPO|REVISION|KIND [...]
KIND is ``model`` (weights required) or ``tokenizer``.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time

import ray
from ray.exceptions import GetTimeoutError

HUB = os.environ.get("HF_HUB_CACHE", "/hf-cache/hub")
MARGIN = int(os.environ.get("MIRROR_MARGIN_GB", "10")) << 30
CHUNK = 4 << 20
REVISION_RE = re.compile(r"[0-9a-f]{40}")
RPC_SHORT_S = 120
RECEIVER_TRANSFER_S = 3600
# finish() can legitimately wait for the full receiver deadline. Give the
# outer Ray RPC enough headroom to return its structured failure instead of
# racing that same deadline and surfacing an opaque GetTimeoutError.
RPC_FINISH_S = RECEIVER_TRANSFER_S + 120
SOCKET_TIMEOUT_S = 120


def repo_dir(repo: str) -> str:
    return "models--" + repo.replace("/", "--")


def parse_spec(raw: str) -> tuple[str, str, str]:
    parts = raw.split("|")
    if len(parts) != 3:
        raise ValueError(f"invalid mirror spec {raw!r}; expected REPO|REVISION|KIND")
    repo, revision, kind = (part.strip() for part in parts)
    if not repo or not REVISION_RE.fullmatch(revision):
        raise ValueError(f"invalid pinned repo/revision in mirror spec {raw!r}")
    if kind not in {"model", "tokenizer"}:
        raise ValueError(f"invalid mirror kind {kind!r} in {raw!r}")
    return repo, revision, kind


def path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def snapshot_manifest(repo: str, revision: str) -> tuple[dict[str, int], dict[str, str]]:
    """Return a pinned snapshot and every repo-local or shared blob it references."""
    dirname = repo_dir(repo)
    repo_root = os.path.join(HUB, dirname)
    snapshot = os.path.join(repo_root, "snapshots", revision)
    hub_root = os.path.realpath(HUB)
    repo_root_real = os.path.realpath(repo_root)
    repo_blob_root = os.path.realpath(os.path.join(repo_root, "blobs"))
    shared_blob_root = os.path.realpath(os.path.join(HUB, "blobs"))
    if not all(path_is_within(root, hub_root) for root in
               (repo_root_real, repo_blob_root, shared_blob_root)):
        raise RuntimeError(f"cache blob directory escapes {HUB}")
    if not os.path.isdir(snapshot):
        raise FileNotFoundError(
            f"{repo}@{revision} is not in the head cache; run predownload first"
        )

    files: dict[str, int] = {}
    links: dict[str, str] = {}
    for root, dirs, names in os.walk(snapshot, followlinks=False):
        linked_dirs = [name for name in dirs if os.path.islink(os.path.join(root, name))]
        if linked_dirs:
            raise RuntimeError(
                f"{repo}@{revision} contains unsupported directory symlink(s): "
                + ", ".join(linked_dirs[:3])
            )
        for name in names:
            path = os.path.join(root, name)
            if name.endswith((".incomplete", ".lock")):
                raise RuntimeError(f"partial cache file in pinned snapshot: {path}")
            rel = os.path.relpath(path, HUB)
            if "\n" in rel:
                raise RuntimeError(f"newline in cache path is unsupported: {rel!r}")
            if os.path.islink(path):
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise RuntimeError(f"broken or unsafe snapshot link: {rel} -> {target}")
                links[rel] = target
                blob_path = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if os.path.islink(blob_path):
                    if not path_is_within(blob_path, repo_root_real):
                        raise RuntimeError(
                            f"unsafe repo blob link: {os.path.relpath(blob_path, HUB)}"
                        )
                    blob_rel = os.path.relpath(blob_path, HUB)
                    blob_target = os.readlink(blob_path)
                    if "\n" in blob_rel or os.path.isabs(blob_target):
                        raise RuntimeError(f"unsafe repo blob link: {blob_rel}")
                    links[blob_rel] = blob_target
                    blob_path = os.path.normpath(
                        os.path.join(os.path.dirname(blob_path), blob_target)
                    )
                resolved = os.path.realpath(blob_path)
                if not (path_is_within(resolved, repo_blob_root)
                        or path_is_within(resolved, shared_blob_root)) \
                        or not os.path.isfile(resolved):
                    raise RuntimeError(f"broken or unsafe snapshot link: {rel} -> {target}")
                blob_rel = os.path.relpath(resolved, HUB)
                files[blob_rel] = os.path.getsize(resolved)
            elif os.path.isfile(path):
                files[rel] = os.path.getsize(path)
            else:
                raise RuntimeError(f"unsupported cache entry: {path}")
    if not files and not links:
        raise RuntimeError(f"pinned snapshot is empty: {repo}@{revision}")
    return files, links


def ray_get(ref, timeout: int, context: str):
    try:
        return ray.get(ref, timeout=timeout)
    except GetTimeoutError as exc:
        ray.cancel(ref)
        raise RuntimeError(f"{context} timed out after {timeout}s") from exc


def drop_from_page_cache(paths: list[str]) -> None:
    for path in paths:
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


@ray.remote(num_cpus=0)
class Receiver:
    """Runs on the worker node and unpacks one bounded tar stream."""

    def missing(self, files: dict[str, int], links: dict[str, str]) -> list[str]:
        need = []
        for rel, size in files.items():
            path = os.path.join(HUB, rel)
            if (os.path.islink(path) or not os.path.isfile(path)
                    or os.path.getsize(path) != size):
                need.append(rel)
        for rel, target in links.items():
            path = os.path.join(HUB, rel)
            if not os.path.islink(path) or os.readlink(path) != target:
                need.append(rel)
        return sorted(set(need))

    def disk_free(self) -> int:
        os.makedirs(HUB, exist_ok=True)
        st = os.statvfs(HUB)
        return st.f_bavail * st.f_frsize

    def start(self) -> int:
        import threading

        self.result: dict = {}
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(SOCKET_TIMEOUT_S)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.listen(1)

        def pump() -> None:
            proc = None
            conn = None
            try:
                conn, _ = self.sock.accept()
                cache_uid = int(os.environ.get("HF_CACHE_UID", str(os.getuid())))
                cache_gid = int(os.environ.get("HF_CACHE_GID", str(os.getgid())))
                extract_code = (
                    "import os; "
                    f"os.setgroups([{cache_gid}]); "
                    f"os.setgid({cache_gid}); os.setuid({cache_uid}); "
                    "os.umask(0o002); "
                    "os.execvp('tar', ['tar', '-C', "
                    f"{HUB!r}, '-xf', '-', '--overwrite'])"
                )
                proc = subprocess.Popen(
                    [sys.executable, "-c", extract_code],
                    stdin=conn.fileno(),
                )
                self.result["rc"] = proc.wait(timeout=RECEIVER_TRANSFER_S)
            except Exception as exc:
                self.result["error"] = repr(exc)
            finally:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                if conn is not None:
                    conn.close()

        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()
        return self.sock.getsockname()[1]

    def finish(self, sent: list[str]) -> dict:
        self.thread.join(timeout=RECEIVER_TRANSFER_S + 30)
        self.sock.close()
        if self.thread.is_alive():
            self.result["error"] = "worker receiver did not finish before deadline"
        elif self.result.get("rc") == 0:
            drop_from_page_cache([os.path.join(HUB, rel) for rel in sent])
        return self.result


def mirror_snapshot(repo: str, revision: str, kind: str, receiver, ip: str) -> tuple[bool, str]:
    try:
        files, links = snapshot_manifest(repo, revision)
    except Exception as exc:
        return False, str(exc)

    snapshot_prefix = f"{repo_dir(repo)}/snapshots/{revision}/"
    visible = [rel for rel in (*files, *links) if rel.startswith(snapshot_prefix)]
    weights = (".safetensors", ".gguf", ".bin", ".pth")
    if kind == "model" and not any(rel.endswith(weights) for rel in visible):
        return False, "pinned head snapshot has no model weights"

    try:
        need = ray_get(
            receiver.missing.remote(files, links), RPC_SHORT_S,
            f"worker manifest check for {repo}@{revision}",
        )
        if not need:
            return True, "exact pinned snapshot already complete on worker; skipped"

        need_bytes = sum(files.get(rel, 0) for rel in need)
        free = ray_get(receiver.disk_free.remote(), 60, "worker free-space check")
        if free < need_bytes + MARGIN:
            return False, (
                f"worker disk too low: {free / 2**30:.1f} GiB free, need "
                f"{need_bytes / 2**30:.1f} + {MARGIN / 2**30:.0f} margin"
            )
        port = ray_get(receiver.start.remote(), 60, "worker receiver startup")
    except Exception as exc:
        return False, str(exc)

    listfile = None
    tar = None
    sock = None
    sent = 0
    t0 = last = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as tmp:
            tmp.write("\n".join(need) + "\n")
            listfile = tmp.name
        tar = subprocess.Popen(
            ["tar", "-C", HUB, "-cf", "-", "--verbatim-files-from", "-T", listfile],
            stdout=subprocess.PIPE,
        )
        sock = socket.create_connection((ip, port), timeout=60)
        sock.settimeout(SOCKET_TIMEOUT_S)
        assert tar.stdout is not None
        while True:
            chunk = tar.stdout.read(CHUNK)
            if not chunk:
                break
            sock.sendall(chunk)
            sent += len(chunk)
            now = time.monotonic()
            if now - last >= 10:
                rate = sent / (now - t0) / 2**20
                print(
                    f"[mirror]   ... {sent / 2**30:.1f} GiB sent "
                    f"({rate:.0f} MiB/s)", flush=True,
                )
                last = now
        sock.shutdown(socket.SHUT_WR)
        if tar.wait(timeout=60) != 0:
            return False, f"head tar exited {tar.returncode}"
        result = ray_get(
            receiver.finish.remote(need), RPC_FINISH_S,
            f"worker extraction for {repo}@{revision}",
        )
        if result.get("error") or result.get("rc") != 0:
            return False, f"worker-side tar failed: {result}"
        left = ray_get(
            receiver.missing.remote(files, links), RPC_SHORT_S,
            f"final worker verification for {repo}@{revision}",
        )
        if left:
            return False, f"{len(left)} file(s) still missing: {left[:3]}"
        secs = max(time.monotonic() - t0, 1e-9)
        return True, (
            f"sent {sent / 2**30:.1f} GiB in {secs:.0f}s "
            f"({sent / secs / 2**20:.0f} MiB/s), verified"
        )
    except Exception as exc:
        return False, f"transfer failed: {type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            sock.close()
        if tar is not None and tar.poll() is None:
            tar.terminate()
            try:
                tar.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tar.kill()
                tar.wait()
        if listfile is not None and os.path.exists(listfile):
            os.unlink(listfile)
        drop_from_page_cache([os.path.join(HUB, rel) for rel in need])


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: mirror_worker_cache.py REPO|REVISION|KIND [...]", file=sys.stderr)
        return 2
    try:
        parsed = [parse_spec(raw) for raw in sys.argv[1:]]
    except ValueError as exc:
        print(f"[mirror] {exc}", file=sys.stderr)
        return 2

    merged: dict[tuple[str, str], str] = {}
    for repo, revision, kind in parsed:
        key = (repo, revision)
        if key not in merged or kind == "model":
            merged[key] = kind
    specs = [(repo, revision, kind) for (repo, revision), kind in merged.items()]

    ray.init(address="auto", log_to_driver=False)
    head_ip = ray.util.get_node_ip_address()
    alive = sorted(
        node["NodeManagerAddress"] for node in ray.nodes() if node.get("Alive")
    )
    expected_head = os.environ.get("EXPECTED_HEAD", head_ip)
    expected_worker = os.environ.get("EXPECTED_WORKER", "")
    expected = sorted([expected_head, expected_worker])
    if not expected_worker or alive != expected:
        print(f"[mirror] expected exactly Ray nodes {expected}, got {alive}", file=sys.stderr)
        return 1

    plans = []
    failures = 0
    for repo, revision, kind in specs:
        try:
            files, _ = snapshot_manifest(repo, revision)
            plans.append((sum(files.values()), repo, revision, kind))
        except Exception as exc:
            print(f"[mirror] FAILED {repo}@{revision}: {exc}", file=sys.stderr)
            failures += 1
    if failures:
        return 1
    plans.sort()

    ip = expected_worker
    print(f"[mirror] worker {ip}")
    for size, repo, revision, kind in plans:
        print(
            f"[mirror] === {repo}@{revision} ({size / 2**30:.1f} GiB) ===",
            flush=True,
        )
        receiver = Receiver.options(resources={f"node:{ip}": 0.001}).remote()
        try:
            ok, msg = mirror_snapshot(repo, revision, kind, receiver, ip)
        except Exception as exc:
            ok, msg = False, f"unexpected failure: {type(exc).__name__}: {exc}"
        finally:
            ray.kill(receiver, no_restart=True)
        print(f"[mirror] {'done' if ok else 'FAILED'} {repo}@{revision}: {msg}", flush=True)
        failures += 0 if ok else 1

    if failures:
        print(f"[mirror] {failures} snapshot(s) failed; see above", file=sys.stderr)
        return 1
    print("[mirror] all exact pinned snapshots mirrored and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
