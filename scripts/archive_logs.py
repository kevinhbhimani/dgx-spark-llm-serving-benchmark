#!/usr/bin/env python3
"""Create or restore one commit-ready ZIP archive per ignored log file."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path


ARCHIVE_SUFFIX: str = ".log.zip"
CHUNK_SIZE: int = 1024 * 1024


def get_repository_root() -> Path:
    script_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "-C", str(script_root), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def resolve_results_directory(repo_root: Path, argument: str) -> Path:
    candidate = Path(argument).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    results_root = (repo_root / "results").resolve(strict=True)
    candidate = candidate.resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError(f"not a directory: {candidate}")
    if results_root not in candidate.parents:
        raise ValueError(f"directory must be below {results_root}: {candidate}")
    return candidate


def is_gitignored(repo_root: Path, path: Path) -> bool:
    relative_path = path.relative_to(repo_root).as_posix()
    result = subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "check-ignore",
            "--quiet",
            "--",
            relative_path,
        ],
        cwd=repo_root,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(f"git check-ignore failed for {relative_path}")


def iter_regular_files(directory: Path, suffix: str) -> Iterator[Path]:
    for current, directory_names, file_names in os.walk(directory, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            path = current_path / name
            if name.endswith(suffix) and not path.is_symlink() and path.is_file():
                yield path


def archive_matches_file(archive_path: Path, source_path: Path) -> bool:
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != source_path.name:
                return False
            with archive.open(entries[0], "r") as archived_file:
                with source_path.open("rb") as source_file:
                    while True:
                        archived_chunk = archived_file.read(CHUNK_SIZE)
                        source_chunk = source_file.read(CHUNK_SIZE)
                        if archived_chunk != source_chunk:
                            return False
                        if not archived_chunk:
                            return True
    except (OSError, RuntimeError, zipfile.BadZipFile, EOFError):
        return False


def create_archive(repo_root: Path, log_path: Path) -> None:
    if not is_gitignored(repo_root, log_path):
        print(f"[archive-logs] skip non-ignored log: {log_path}")
        return

    archive_path = log_path.with_name(log_path.name + ".zip")
    if is_gitignored(repo_root, archive_path):
        raise ValueError(f"ZIP archive would also be ignored by Git: {archive_path}")
    if archive_path.is_symlink():
        raise ValueError(f"refusing to overwrite a symlink: {archive_path}")
    if archive_path.exists():
        if archive_matches_file(archive_path, log_path):
            print(f"[archive-logs] already archived: {archive_path}")
            return
        raise FileExistsError(f"existing archive does not match the log: {archive_path}")

    with tempfile.TemporaryDirectory(prefix=".archive-logs-", dir=log_path.parent) as temp_dir:
        temporary_archive = Path(temp_dir) / archive_path.name
        with zipfile.ZipFile(
            temporary_archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.write(log_path, arcname=log_path.name)
        if not archive_matches_file(temporary_archive, log_path):
            raise RuntimeError(f"ZIP validation failed: {temporary_archive}")
        try:
            os.link(temporary_archive, archive_path)
        except FileExistsError:
            if archive_matches_file(archive_path, log_path):
                print(f"[archive-logs] already archived: {archive_path}")
                return
            raise FileExistsError(f"existing archive does not match the log: {archive_path}")

    print(f"[archive-logs] created: {archive_path}")


def restore_archive(archive_path: Path) -> None:
    expected_name = archive_path.name[: -len(".zip")]
    if not expected_name.endswith(".log"):
        raise ValueError(f"archive name must end in .log.zip: {archive_path}")
    log_path = archive_path.with_name(expected_name)
    if archive_path.is_symlink() or log_path.is_symlink():
        raise ValueError(f"refusing to use a symlink: {archive_path}")

    if log_path.exists():
        if archive_matches_file(archive_path, log_path):
            print(f"[archive-logs] already restored: {log_path}")
            return
        raise FileExistsError(f"refusing to overwrite a different log: {log_path}")

    with zipfile.ZipFile(archive_path, "r") as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != log_path.name:
            raise ValueError(f"archive must contain only {log_path.name}: {archive_path}")
        with tempfile.TemporaryDirectory(prefix=".restore-logs-", dir=log_path.parent) as temp_dir:
            temporary_log = Path(temp_dir) / log_path.name
            with archive.open(entries[0], "r") as archived_file:
                with temporary_log.open("wb") as restored_file:
                    shutil.copyfileobj(archived_file, restored_file, CHUNK_SIZE)
            if not archive_matches_file(archive_path, temporary_log):
                raise RuntimeError(f"ZIP validation failed: {archive_path}")
            try:
                os.link(temporary_log, log_path)
            except FileExistsError:
                raise FileExistsError(f"log appeared during restore: {log_path}")

    print(f"[archive-logs] restored: {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive ignored .log files or restore sibling .log.zip files."
    )
    parser.add_argument("action", choices=("zip", "unzip"))
    parser.add_argument("results_dir", help="a campaign directory below results/")
    arguments = parser.parse_args()

    try:
        repo_root = get_repository_root()
        results_dir = resolve_results_directory(repo_root, arguments.results_dir)
        if arguments.action == "zip":
            logs = list(iter_regular_files(results_dir, ".log"))
            if not logs:
                print(f"[archive-logs] no .log files found below {results_dir}")
                return 0
            for log_path in logs:
                create_archive(repo_root, log_path)
        else:
            for archive_path in iter_regular_files(results_dir, ARCHIVE_SUFFIX):
                restore_archive(archive_path)
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"[archive-logs] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
