"""Resolve and identify benchmark campaign output directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence


DEFAULT_CAMPAIGN_NAME = "benchmark"
IDENTITY_FILENAME = "campaign_run.json"
DATE_SUFFIX = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


def utc_date(now: datetime | None = None) -> str:
    """Return the current UTC date or the UTC date for a supplied time."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("campaign date requires a timezone-aware datetime")
    return current.astimezone(timezone.utc).date().isoformat()


def resolve_campaign_dir(
    out_dir: str | os.PathLike[str] | None = None,
    *,
    default_name: str = DEFAULT_CAMPAIGN_NAME,
    cwd: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> Path:
    """Resolve an explicit output path or the default UTC-dated campaign path."""
    if out_dir:
        base = Path(cwd) if cwd is not None else Path.cwd()
        target = Path(out_dir).expanduser()
        if not target.is_absolute():
            target = base / target
    else:
        base = Path(cwd) if cwd is not None else Path(__file__).resolve().parent
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", default_name):
            raise ValueError(f"invalid default campaign name: {default_name!r}")
        target = base / "results" / f"{default_name}-{utc_date(now)}"
    return target.resolve()


def campaign_metadata(out_dir: str | os.PathLike[str]) -> dict[str, str | None]:
    """Describe a campaign directory for a sealed manifest."""
    resolved = Path(out_dir).expanduser().resolve()
    name = resolved.name
    match = DATE_SUFFIX.search(name)
    if match:
        try:
            date.fromisoformat(match.group(1))
        except ValueError:
            match = None
    return {
        "campaign_name": name[:match.start()] if match else name,
        "campaign_date": match.group(1) if match else None,
        "campaign_directory": name,
        "resolved_out_dir": str(resolved),
    }


def campaign_root_from_input(path: str | os.PathLike[str]) -> Path:
    """Resolve a campaign directory or an index path to its directory."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if candidate.is_dir() or candidate.suffix.lower() != ".csv":
        return candidate
    return candidate.parent


def resolve_index_path(
    index_path: str | os.PathLike[str] | None = None,
    *,
    default_name: str = DEFAULT_CAMPAIGN_NAME,
) -> Path:
    """Resolve an index file or campaign directory to its index.csv path."""
    if index_path is None:
        return resolve_campaign_dir(default_name=default_name) / "index.csv"
    candidate = Path(index_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if candidate.is_dir() or candidate.suffix.lower() != ".csv":
        return candidate / "index.csv"
    return candidate


def resolve_campaign_output_path(
    output_path: str | os.PathLike[str] | None,
    index_path: str | os.PathLike[str],
    *,
    default_subdir: str = "figs",
) -> Path:
    """Place a default or relative report path within the selected campaign."""
    campaign_dir = resolve_index_path(index_path).parent
    if output_path is None:
        return (campaign_dir / default_subdir).resolve()
    output = Path(output_path).expanduser()
    if not output.is_absolute():
        output = campaign_dir / output
    return output.resolve()


def fingerprint_identity(files: Sequence[str], values: Sequence[str]) -> dict[str, Any]:
    """Build a stable identity object from campaign inputs and runtime values."""
    file_hashes: dict[str, str] = {}
    for filename in files:
        path = Path(filename).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"campaign identity input is not a file: {path}")
        if path.name in file_hashes:
            raise ValueError(f"campaign identity contains duplicate basename: {path.name}")
        file_hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()

    identity_values: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise ValueError(f"identity values must use KEY=VALUE: {item!r}")
        if key in identity_values:
            raise ValueError(f"campaign identity contains duplicate value: {key}")
        identity_values[key] = value

    payload = {"files": file_hashes, "values": identity_values}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {**payload, "sha256": hashlib.sha256(canonical.encode()).hexdigest()}


def _read_existing_identity(directory: Path) -> dict[str, Any] | None:
    marker = directory / IDENTITY_FILENAME
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("resolved_out_dir") != str(directory):
        return None
    identity = payload.get("identity")
    if not isinstance(identity, dict) or not isinstance(identity.get("sha256"), str):
        return None
    identity_payload = {"files": identity.get("files"), "values": identity.get("values")}
    if not isinstance(identity_payload["files"], dict) or not isinstance(
        identity_payload["values"], dict
    ):
        return None
    canonical = json.dumps(identity_payload, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical.encode()).hexdigest() != identity["sha256"]:
        return None
    index = directory / "index.csv"
    if index.exists() and (not index.is_file() or index.is_symlink()):
        return None
    return identity


def prepare_implicit_campaign(
    out_dir: str | os.PathLike[str],
    identity: dict[str, Any],
    *,
    plan_only: bool = False,
) -> bool:
    """Create a campaign marker or accept a matching implicit resume.

    Returns True for an existing compatible campaign and False for a new one.
    Plan-only mode validates existing paths without creating files or folders.
    """
    directory = Path(out_dir).expanduser().resolve()
    if directory.exists():
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"implicit campaign path is not a directory: {directory}")
        existing_identity = _read_existing_identity(directory)
        if existing_identity != identity:
            raise ValueError(
                f"implicit campaign path already exists and is not a compatible "
                f"resume: {directory}; set OUT_DIR to a fresh path"
            )
        return True

    if plan_only:
        return False

    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        existing_identity = _read_existing_identity(directory)
        if existing_identity == identity:
            return True
        raise ValueError(
            f"implicit campaign path was claimed by an incompatible run: {directory}"
        )
    payload = {
        **campaign_metadata(directory),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "identity": identity,
    }
    marker = directory / IDENTITY_FILENAME
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        existing_identity = _read_existing_identity(directory)
        if existing_identity == identity:
            return True
        raise ValueError(
            f"implicit campaign path was claimed by an incompatible run: {directory}"
        )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve", help="resolve a campaign output path")
    resolve.add_argument("--out-dir")
    resolve.add_argument("--default-name", default=DEFAULT_CAMPAIGN_NAME)

    identity = commands.add_parser("identity", help="fingerprint campaign inputs")
    identity.add_argument("--file", action="append", default=[])
    identity.add_argument("--value", action="append", default=[])

    prepare = commands.add_parser("prepare", help="guard an implicit campaign path")
    prepare.add_argument("--out-dir", required=True)
    prepare.add_argument("--identity-json", required=True)
    prepare.add_argument("--plan-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resolve":
            print(resolve_campaign_dir(args.out_dir, default_name=args.default_name))
        elif args.command == "identity":
            print(json.dumps(fingerprint_identity(args.file, args.value), sort_keys=True))
        else:
            identity = json.loads(args.identity_json)
            resumed = prepare_implicit_campaign(
                args.out_dir, identity, plan_only=args.plan_only
            )
            if resumed:
                print(f"compatible resume: {Path(args.out_dir).resolve()}")
            elif args.plan_only:
                print(f"new campaign path: {Path(args.out_dir).resolve()}")
            else:
                print(f"created campaign path: {Path(args.out_dir).resolve()}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"campaign_output.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
