#!/usr/bin/env python3
"""Build and apply offline source releases without replacing runtime artifacts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterator


RELEASE_FILENAME = "SOURCE_RELEASE.json"
RECEIPT_FILENAME = ".offline-release.json"
HISTORY_DIRECTORY = ".offline-releases"
TRANSACTION_FILENAME = ".offline-transaction.json"
SOURCE_ARCHIVE = "dgx-spark-benchmark-src.tar.gz"
HASH_CHUNK_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 10
ENVIRONMENT_CHECK_TIMEOUT_SECONDS = 180
DEPENDENCY_PATHS = (
    "requirements.txt", "requirements.lock", "offline/Dockerfile.ray",
    "scripts/runtime_defaults.sh", "scripts/ray-runtime-constraints.txt",
)
EXCLUDED_NAMES = frozenset((
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".aws", ".ssh",
    ".gnupg", ".codex", ".cache", "__pycache__", "figs", ".netrc", ".pypirc",
    RECEIPT_FILENAME, HISTORY_DIRECTORY, TRANSACTION_FILENAME, ".offline-release.lock",
))


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def excluded_path(name: str) -> bool:
    return any(
        part in EXCLUDED_NAMES or part.startswith((".env", "results", "offline-bundle", ".offline-"))
        or part.endswith(".pyc")
        for part in PurePosixPath(name).parts
    )


def is_packaged_path(root: Path, name: str) -> bool:
    if excluded_path(name) or name == "commit.md":
        return False
    path = root / name
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if not candidate.is_symlink() and candidate.is_dir() and (
            (candidate / "BUNDLE_INFO").is_file() or (candidate / "PATCH_INFO.json").is_file()
        ):
            return False
    return True


def safe_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    path = PurePosixPath(name)
    if not name or name == "." or path.is_absolute() or ".." in path.parts or "\\" in name \
            or path.as_posix() != name or excluded_path(name):
        raise ValueError(f"unsafe or unmanaged source path: {name!r}")
    return name


def destination(root: Path, name: str) -> Path:
    path = root / safe_name(name)
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"source path has a symlink parent: {name}")
    return path


@dataclass(frozen=True)
class SourceFile:
    path: str
    kind: str
    mode: int
    content: str

    @classmethod
    def inspect(cls, root: Path, name: str) -> SourceFile:
        path = destination(root, name)
        mode = stat.S_IMODE(path.lstat().st_mode) & 0o777
        if path.is_symlink():
            target = os.readlink(path)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if posixpath.isabs(target) or resolved == ".." or resolved.startswith("../"):
                raise ValueError(f"symlink escapes source tree: {name}")
            return cls(name, "symlink", mode, target)
        if not path.is_file():
            raise ValueError(f"source entry is not a regular file: {name}")
        return cls(name, "file", mode, sha256_file(path))


def inventory(root: Path) -> tuple[SourceFile, ...]:
    entries: list[SourceFile] = []
    for directory, directories, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in sorted(directories + filenames):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if not is_packaged_path(root, relative):
                if name in directories:
                    directories.remove(name)
                continue
            if path.is_symlink() or path.is_file():
                entries.append(SourceFile.inspect(root, relative))
                if name in directories:
                    directories.remove(name)
    return tuple(sorted(entries, key=lambda entry: entry.path))


def extract_source(archive_path: Path, root: Path) -> tuple[SourceFile, ...]:
    seen: set[str] = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if member.name in (".", "./"):
                continue
            name = safe_name(member.name.rstrip("/") if member.isdir() else member.name)
            if name in seen:
                raise ValueError(f"duplicate source archive path: {name}")
            seen.add(name)
            path = destination(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
            elif member.isfile():
                contents = archive.extractfile(member)
                if contents is None:
                    raise ValueError(f"missing archive contents: {name}")
                with contents, path.open("xb") as output:
                    shutil.copyfileobj(contents, output)
                path.chmod(member.mode & 0o777)
            elif member.issym():
                path.symlink_to(member.linkname)
                SourceFile.inspect(root, name)
            else:
                raise ValueError(f"unsupported source archive entry: {name}")
    return installed_files(root)


def installed_files(root: Path) -> tuple[SourceFile, ...]:
    entries = list(inventory(root))
    if (root / "commit.md").exists() or (root / "commit.md").is_symlink():
        entries.append(SourceFile.inspect(root, "commit.md"))
    return tuple(sorted(entries, key=lambda entry: entry.path))


def write_archive(root: Path, entries: tuple[SourceFile, ...], output: Path) -> None:
    with tarfile.open(output, "w:gz", dereference=False) as archive:
        for entry in entries:
            path = destination(root, entry.path)
            member = tarfile.TarInfo(entry.path)
            member.mode = entry.mode
            if entry.kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = entry.content
                archive.addfile(member)
            else:
                member.size = path.stat().st_size
                with path.open("rb") as contents:
                    archive.addfile(member, contents)


def write_json(path: Path, value: object) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".offline-json-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_receipt(root: Path, release: Release) -> None:
    write_json(root / RECEIPT_FILENAME, {
        **release.serialize(), "installed_utc": datetime.now(timezone.utc).isoformat(),
    })


def read_mapping(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"expected JSON object: {path}")
    return dict(value)


@dataclass(frozen=True)
class Release:
    files: tuple[SourceFile, ...]
    dependencies: dict[str, str]
    archive_sha256: str
    previous_source_sha256: str = ""
    previous_snapshot_sha256: str = ""

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(canonical_json([asdict(entry) for entry in self.files])).hexdigest()

    def serialize(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source_sha256": self.source_sha256,
            "dependencies": self.dependencies,
            "archive_sha256": self.archive_sha256,
            "previous_source_sha256": self.previous_source_sha256,
            "previous_snapshot_sha256": self.previous_snapshot_sha256,
            "files": [asdict(entry) for entry in self.files],
        }

    @classmethod
    def read(cls, path: Path) -> Release:
        document = read_mapping(path)
        records = document.get("files")
        dependencies = document.get("dependencies")
        if document.get("schema_version") != 1 or not isinstance(records, list) \
                or not isinstance(dependencies, dict) \
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in dependencies.items()):
            raise ValueError(f"invalid source release: {path}")
        entries: list[SourceFile] = []
        for record in records:
            if not isinstance(record, dict) or set(record) != {"path", "kind", "mode", "content"}:
                raise ValueError(f"invalid source file record: {path}")
            name, kind, mode, content = (record[key] for key in ("path", "kind", "mode", "content"))
            if not isinstance(name, str) or not isinstance(kind, str) or kind not in ("file", "symlink") \
                    or type(mode) is not int or not 0 <= mode <= 0o777 or not isinstance(content, str):
                raise ValueError(f"invalid source file fields: {path}")
            if kind == "file" and (len(content) != 64 or any(character not in "0123456789abcdef" for character in content)):
                raise ValueError(f"invalid source file digest: {name}")
            entries.append(SourceFile(safe_name(name), kind, mode, content))
        names = [entry.path for entry in entries]
        archive_hash = document.get("archive_sha256")
        previous_hash = document.get("previous_source_sha256", "")
        snapshot_hash = document.get("previous_snapshot_sha256", "")
        if names != sorted(set(names)) or not isinstance(archive_hash, str) \
                or not isinstance(previous_hash, str) or not isinstance(snapshot_hash, str):
            raise ValueError(f"invalid source release inventory: {path}")
        for digest in (archive_hash, previous_hash, snapshot_hash, str(document.get("source_sha256"))):
            if digest and (len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)):
                raise ValueError(f"invalid release digest: {path}")
        release = cls(tuple(entries), dict(dependencies), archive_hash, previous_hash, snapshot_hash)
        if document.get("source_sha256") != release.source_sha256:
            raise ValueError(f"source manifest hash mismatch: {path}")
        return release


def dependency_files(entries: tuple[SourceFile, ...]) -> dict[str, str]:
    by_path = {entry.path: entry for entry in entries}
    identities: dict[str, str] = {}
    for dependency_path in DEPENDENCY_PATHS:
        if dependency_path not in by_path:
            identities[dependency_path] = "missing"
            continue
        name = dependency_path
        visited: set[str] = set()
        chain: list[dict[str, object]] = []
        while True:
            if name in visited or name not in by_path:
                raise ValueError(f"dependency symlink is cyclic or unresolved: {dependency_path}")
            visited.add(name)
            entry = by_path[name]
            chain.append(asdict(entry))
            if entry.kind == "file":
                break
            name = safe_name(posixpath.normpath(posixpath.join(posixpath.dirname(name), entry.content)))
        identities[dependency_path] = hashlib.sha256(canonical_json(chain)).hexdigest()
    return identities


def bundle_release(bundle: Path) -> Release:
    checksums: dict[str, str] = {}
    for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        checksum, name = line.split(maxsplit=1)
        checksums[name.lstrip("*").removeprefix("./")] = checksum
    for name in ("BUNDLE_INFO", SOURCE_ARCHIVE):
        if checksums.get(name) != sha256_file(bundle / name):
            raise ValueError(f"bundle checksum mismatch: {name}")
    with tempfile.TemporaryDirectory(prefix="offline-source-") as temporary:
        entries = extract_source(bundle / SOURCE_ARCHIVE, Path(temporary))
    dependencies = dependency_files(entries)
    dependencies.update({
        "artifact:" + name: checksum for name, checksum in checksums.items()
        if name.startswith(("wheels/", "vllm-ray-image.tar.")) or name == "BUNDLE_INFO"
    })
    if not any(name.startswith("artifact:vllm-ray-image.tar.") for name in dependencies):
        raise ValueError("bundle image checksum is missing")
    release = Release(entries, dependencies, checksums[SOURCE_ARCHIVE])
    if (bundle / RELEASE_FILENAME).exists() and Release.read(bundle / RELEASE_FILENAME) != release:
        raise ValueError("bundle source manifest differs from its archive")
    return release


def report_changes(before: tuple[SourceFile, ...], after: tuple[SourceFile, ...], prefix: str = "") -> None:
    old = {entry.path: entry for entry in before}
    new = {entry.path: entry for entry in after}
    for name in sorted(old.keys() | new.keys()):
        if old.get(name) != new.get(name):
            action = "add" if name not in old else "delete" if name not in new else "change"
            print(f"{prefix}{action} {name}")


def verify_tree(root: Path, release: Release) -> None:
    report_changes(release.files, installed_files(root), "local ")


def verify_payload(directory: Path, release: Release, stage: Path) -> None:
    archive = directory / SOURCE_ARCHIVE
    if sha256_file(archive) != release.archive_sha256:
        raise ValueError("source archive checksum mismatch")
    if extract_source(archive, stage) != release.files:
        raise ValueError("source archive differs from its manifest")
    if dependency_files(release.files) != {
        name: release.dependencies.get(name) for name in DEPENDENCY_PATHS
    }:
        raise ValueError("source dependency inputs differ from the runtime bundle")


def source_snapshot(root: Path, output: Path, metadata: Path | None = None) -> None:
    if output.exists():
        raise ValueError(f"output path already exists: {output}")
    entries = inventory(root)
    commit = "unknown"
    worktree_state = "unknown"
    try:
        repository = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root, text=True,
            capture_output=True, check=True, timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        repository = ""
    if (root / ".git").exists() and (not repository or Path(repository).resolve() != root):
        raise ValueError("cannot validate the source tree's Git metadata")
    if repository and Path(repository).resolve() == root:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True,
            check=True, timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
            text=True, capture_output=True, check=True, timeout=GIT_TIMEOUT_SECONDS,
        ).stdout
        worktree_state = "modified" if status else "clean"
    with tempfile.TemporaryDirectory(prefix="offline-source-") as temporary:
        stage = Path(temporary)
        for entry in entries:
            target = destination(stage, entry.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.kind == "symlink":
                target.symlink_to(entry.content)
            else:
                shutil.copy2(destination(root, entry.path), target)
        if metadata is not None:
            shutil.copy2(metadata, stage / "commit.md")
        else:
            (stage / "commit.md").write_text(
                f"# Source release metadata\n\n- Commit: {commit}\n- Git working tree: {worktree_state}\n",
                encoding="utf-8",
            )
        staged = list(inventory(stage))
        staged.append(SourceFile.inspect(stage, "commit.md"))
        for entry in entries:
            if SourceFile.inspect(root, entry.path) != entry:
                raise ValueError(f"source changed while its snapshot was being copied: {entry.path}")
        if any(SourceFile.inspect(stage, entry.path) != entry for entry in entries):
            raise ValueError("source changed while its snapshot was being copied")
        write_archive(stage, tuple(sorted(staged, key=lambda entry: entry.path)), output)


def build_patch(root: Path, base: Path, output: Path) -> None:
    if output.exists():
        raise ValueError(f"output path already exists: {output}")
    baseline = bundle_release(base) if (base / "BUNDLE_INFO").exists() else Release.read(base / RELEASE_FILENAME)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".offline-build-", dir=output.parent) as temporary:
        stage = Path(temporary)
        source_snapshot(root, stage / SOURCE_ARCHIVE)
        with tempfile.TemporaryDirectory(prefix="offline-source-") as extracted:
            entries = extract_source(stage / SOURCE_ARCHIVE, Path(extracted))
        changed_dependencies = [
            name for name, checksum in dependency_files(entries).items()
            if baseline.dependencies.get(name) != checksum
        ]
        if changed_dependencies:
            raise ValueError("runtime rebuild required for: " + ", ".join(changed_dependencies))
        release = Release(entries, baseline.dependencies, sha256_file(stage / SOURCE_ARCHIVE), baseline.source_sha256)
        write_json(stage / RELEASE_FILENAME, release.serialize())
        write_json(stage / "PATCH_INFO.json", {
            "base_source_sha256": baseline.source_sha256,
            "target_source_sha256": release.source_sha256,
            "created_utc": datetime.now(timezone.utc).isoformat(),
        })
        shutil.copy2(Path(__file__), stage / "source_release.py")
        with (stage / "SHA256SUMS").open("w", encoding="utf-8") as checksums:
            for path in sorted(stage.iterdir()):
                if path.name != "SHA256SUMS":
                    checksums.write(f"{sha256_file(path)}  {path.name}\n")
        os.rename(stage, output)
    print(f"source update written to {output}")


@contextmanager
def installation_lock(root: Path) -> Iterator[None]:
    with (root / ".offline-release.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def validate_environment(root: Path, stage: Path) -> None:
    python = root / ".venv/bin/python"
    if not python.exists():
        raise ValueError("installed .venv/bin/python is missing")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    subprocess.run(
        [str(python), str(stage / "scripts/check_environment.py")],
        cwd=stage, env=environment, check=True, timeout=ENVIRONMENT_CHECK_TIMEOUT_SECONDS,
    )


def check_update(root: Path, current: Release, target: Release, restored_release: Release | None = None) -> None:
    if (root / TRANSACTION_FILENAME).exists():
        raise ValueError("interrupted source update; run rollback before applying another update")
    if target.previous_source_sha256 != current.source_sha256:
        raise ValueError("source update requires a different installed baseline")
    runtime_release = target if restored_release is None else restored_release
    if current.dependencies != runtime_release.dependencies:
        raise ValueError("source update requires a different runtime bundle")


def updated_files(current: Release, target: Release, installed: tuple[SourceFile, ...]) -> tuple[SourceFile, ...]:
    old = {entry.path: entry for entry in current.files}
    new = {entry.path: entry for entry in target.files}
    changed_paths = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
    replacements = [entry for entry in target.files if entry.path in changed_paths]
    retained = [
        entry for entry in installed if entry.path not in changed_paths and not any(
            entry.path.startswith(replacement.path + "/") or replacement.path.startswith(entry.path + "/")
            for replacement in replacements
        )
    ]
    return tuple(sorted(retained + replacements, key=lambda entry: entry.path))


def activate_tree(root: Path, stage: Path, old: Release, new: Release, force: bool = False) -> None:
    old_files = {entry.path: entry for entry in old.files}
    new_paths = {entry.path for entry in new.files}
    for entry in reversed(old.files):
        if entry.path not in new_paths:
            path = destination(root, entry.path)
            if path.is_symlink() or path.is_file():
                path.unlink()
    for entry in new.files:
        if not force and old_files.get(entry.path) == entry:
            continue
        path = destination(root, entry.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_dir() and not child.is_symlink():
                    child.rmdir()
            path.rmdir()
        descriptor, temporary = tempfile.mkstemp(prefix=".offline-file-", dir=path.parent)
        os.close(descriptor)
        temporary_path = Path(temporary)
        try:
            if entry.kind == "symlink":
                temporary_path.unlink()
                temporary_path.symlink_to(entry.content)
            else:
                shutil.copy2(destination(stage, entry.path), temporary_path)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def save_history(root: Path, release: Release) -> Path:
    entries = installed_files(root)
    snapshot_hash = hashlib.sha256(canonical_json({
        "release": release.serialize(), "files": [asdict(entry) for entry in entries],
    })).hexdigest()
    history = root / HISTORY_DIRECTORY / snapshot_hash
    if not history.exists():
        history.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".offline-save-", dir=history.parent) as temporary:
            stage = Path(temporary)
            write_archive(root, entries, stage / SOURCE_ARCHIVE)
            saved = Release(entries, {**release.dependencies, **dependency_files(entries)}, sha256_file(stage / SOURCE_ARCHIVE))
            write_json(stage / RELEASE_FILENAME, saved.serialize())
            with tempfile.TemporaryDirectory(prefix="offline-source-") as extracted:
                verify_payload(stage, saved, Path(extracted))
            receipt = root / RECEIPT_FILENAME
            write_json(stage / RECEIPT_FILENAME, read_mapping(receipt) if receipt.exists() else release.serialize())
            os.rename(stage, history)
    return history


def apply_update(root: Path, patch: Path, check_only: bool, restored_release: Release | None = None) -> None:
    current = Release.read(root / RECEIPT_FILENAME)
    target = Release.read(patch / RELEASE_FILENAME)
    check_update(root, current, target, restored_release)
    with tempfile.TemporaryDirectory(prefix="offline-source-") as temporary:
        stage = Path(temporary)
        verify_payload(patch, target, stage)
        if restored_release is None:
            validate_environment(root, stage)
        verify_tree(root, current)
        report_changes(current.files, target.files)
        if check_only:
            print(f"source update validated: {current.source_sha256} -> {target.source_sha256}")
            return
        with installation_lock(root):
            check_update(root, current, target, restored_release)
            history = save_history(root, current)
            before = replace(Release.read(history / RELEASE_FILENAME), previous_snapshot_sha256=history.name)
            entries = updated_files(current, target, before.files)
            after = Release(entries, {**target.dependencies, **dependency_files(entries)}, "")
            receipt = replace(
                target if restored_release is None else restored_release,
                previous_source_sha256=current.source_sha256, previous_snapshot_sha256=history.name,
            )
            write_json(root / TRANSACTION_FILENAME, {"before": before.serialize(), "after": after.serialize()})
            try:
                activate_tree(root, stage, before, after)
                if installed_files(root) != after.files:
                    raise ValueError("installed source differs from the planned update")
                verify_tree(root, target)
                write_receipt(root, receipt)
                (root / TRANSACTION_FILENAME).unlink()
            except BaseException:
                recover_transaction(root)
                raise
    print(f"installed source SHA-256: {receipt.source_sha256}")


def recover_transaction(root: Path) -> None:
    journal = read_mapping(root / TRANSACTION_FILENAME)
    with tempfile.TemporaryDirectory(prefix="offline-source-") as temporary:
        stage = Path(temporary)
        for key in ("before", "after"):
            write_json(stage / (key + ".json"), journal.get(key))
        before = Release.read(stage / "before.json")
        after = Release.read(stage / "after.json")
        old = {entry.path: entry for entry in before.files}
        new = {entry.path: entry for entry in after.files}
        for name in old.keys() | new.keys():
            path = destination(root, name)
            if path.is_dir() and not path.is_symlink() and any(
                candidate.startswith(name + "/") for candidate in old.keys() | new.keys()
            ):
                continue
            if path.exists() or path.is_symlink():
                if SourceFile.inspect(root, name) not in (old.get(name), new.get(name)):
                    raise ValueError(f"cannot recover over a local edit: {name}")
        history = root / HISTORY_DIRECTORY / (before.previous_snapshot_sha256 or before.source_sha256)
        saved = Release.read(history / RELEASE_FILENAME)
        extracted = stage / "source"
        extracted.mkdir()
        verify_payload(history, saved, extracted)
        # Recovery rewrites the baseline because an interrupted file may still
        # have its pre-update contents while the journal describes the target.
        activate_tree(root, extracted, after, before, force=True)
        recovered = {entry.path: entry for entry in installed_files(root)}
        if any(recovered.get(name) != old.get(name) for name in old.keys() | new.keys()):
            raise ValueError("recovered source differs from its snapshot")
        verify_tree(root, before)
        receipt = history / RECEIPT_FILENAME
        if receipt.exists():
            write_json(root / RECEIPT_FILENAME, read_mapping(receipt))
        else:
            write_receipt(root, before)
        (root / TRANSACTION_FILENAME).unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--metadata", type=Path)
    record = commands.add_parser("record-bundle")
    record.add_argument("--bundle", type=Path, required=True)
    register = commands.add_parser("register")
    register.add_argument("--bundle", type=Path, required=True)
    register.add_argument("--source", type=Path, required=True)
    build = commands.add_parser("build-patch")
    build.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument("--base", type=Path, required=True)
    build.add_argument("output", type=Path)
    for name in ("check", "apply", "rollback", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        if name in ("check", "apply"):
            command.add_argument("--patch", type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args()
    try:
        if arguments.command == "record-bundle":
            release = bundle_release(arguments.bundle.resolve())
            write_json(arguments.bundle / RELEASE_FILENAME, release.serialize())
        elif arguments.command == "snapshot":
            source_snapshot(arguments.source.resolve(), arguments.output.resolve(), arguments.metadata)
        elif arguments.command == "build-patch":
            build_patch(arguments.source.resolve(), arguments.base.resolve(), arguments.output.resolve())
        else:
            root = arguments.source.resolve(strict=True)
            if root == Path("/"):
                raise ValueError("source directory cannot be /")
            if arguments.command == "register":
                release = bundle_release(arguments.bundle.resolve())
                verify_tree(root, release)
                with installation_lock(root):
                    receipt = root / RECEIPT_FILENAME
                    if receipt.exists() and Release.read(receipt) != release:
                        raise ValueError("installation already has a different release receipt")
                    write_receipt(root, release)
                print(f"registered source SHA-256: {release.source_sha256}")
            elif arguments.command == "verify":
                if (root / TRANSACTION_FILENAME).exists():
                    raise ValueError("interrupted source update; run rollback before verifying")
                release = Release.read(root / RECEIPT_FILENAME)
                verify_tree(root, release)
                print(release.source_sha256)
            elif arguments.command == "rollback":
                if (root / TRANSACTION_FILENAME).exists():
                    with installation_lock(root):
                        recover_transaction(root)
                else:
                    current = Release.read(root / RECEIPT_FILENAME)
                    if not current.previous_source_sha256:
                        raise ValueError("installation has no previous source release")
                    history = root / HISTORY_DIRECTORY / (current.previous_snapshot_sha256 or current.previous_source_sha256)
                    previous = Release.read(history / RELEASE_FILENAME)
                    previous_receipt = history / RECEIPT_FILENAME
                    restored_release = Release.read(previous_receipt) if previous_receipt.exists() else previous
                    with tempfile.TemporaryDirectory(prefix="offline-source-") as temporary:
                        patch = Path(temporary)
                        shutil.copy2(history / SOURCE_ARCHIVE, patch / SOURCE_ARCHIVE)
                        target = Release(previous.files, previous.dependencies, previous.archive_sha256, current.source_sha256)
                        write_json(patch / RELEASE_FILENAME, target.serialize())
                        apply_update(root, patch, False, restored_release)
            else:
                apply_update(root, arguments.patch.resolve(), arguments.command == "check")
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"[offline-source] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
