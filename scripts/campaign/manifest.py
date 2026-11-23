"""Hash campaign artifacts and write the sealed manifest atomically."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping, Sequence

from utils.campaign_output import campaign_metadata
from scripts.campaign.plan import Campaign

ROOT = Path(__file__).resolve().parents[2]


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Return a streaming SHA-256 digest for an artifact."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_record(path: Path, out_dir: Path) -> dict[str, str | int]:
    """Record an artifact's display path, byte count, and digest."""
    resolved = path.resolve()
    try:
        display_path = resolved.relative_to(out_dir.resolve()).as_posix()
    except ValueError:
        try:
            display_path = resolved.relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            display_path = str(resolved)
    return {
        "path": display_path,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Persist a JSON object with an atomic rename and directory sync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except (AttributeError, OSError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_manifest(
    *,
    campaign: Campaign,
    out_dir: Path,
    concurrencies: tuple[int, ...],
    repeats: int,
    expected_count: int,
    artifacts: Mapping[str, Sequence[Path]],
    completed_only: bool = False,
    skipped_combos: Sequence[str] = (),
    allow_source_match_commits: bool = False,
    harness_git_commits: Sequence[str] = (),
    topology: Mapping[str, str] | None = None,
    execution: Mapping[str, object] | None = None,
    selection: Mapping[str, object] | None = None,
    git_checks_skipped: bool = False,
) -> dict[str, object]:
    """Describe the verified campaign and sorted artifact inventory."""
    manifest: dict[str, object] = {
        "schema_version": (
            3 if execution is not None else 2 if topology is not None else 1
        ),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "campaign": {
            **campaign_metadata(out_dir),
            "models_yaml": str(campaign.models_path),
            "experiment_yaml": str(campaign.experiment_path),
            "gpu_utilization": campaign.models[0].gpu_utilization,
            "concurrencies": list(concurrencies),
            "repeats": repeats,
            "expected_count": expected_count,
            "completed_only": completed_only,
            "skipped_combos": list(skipped_combos),
            "allow_source_match_commits": allow_source_match_commits,
            "harness_git_commits": list(harness_git_commits),
            "git_checks_skipped": git_checks_skipped,
            **({"selection": dict(selection)} if selection is not None else {}),
        },
        "artifacts": {
            kind: [
                _artifact_record(path, out_dir)
                for path in sorted(set(paths), key=lambda item: str(item))
            ]
            for kind, paths in artifacts.items()
        },
    }
    if topology is not None:
        manifest["topology"] = dict(topology)
    if execution is not None:
        manifest["execution"] = dict(execution)
    return manifest


