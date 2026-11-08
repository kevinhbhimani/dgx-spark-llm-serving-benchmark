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
RESULTS_DIRECTORY = "results"
LOGS_DIRECTORY = "logs"
IDENTITY_FILENAME = "campaign_run.json"
PREFLIGHT_DIRECTORY = "preflight"
DATE_SUFFIX = re.compile(r"-(\d{4}-\d{2}-\d{2})(?:-(\d{3,}))?$")
CAMPAIGN_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
CAMPAIGN_COUNTER_WIDTH: int = 3
FIRST_CAMPAIGN_COUNTER: int = 1


def utc_date(now: datetime | None = None) -> str:
    """Return the current UTC date or the UTC date for a supplied time."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("campaign date requires a timezone-aware datetime")
    return current.astimezone(timezone.utc).date().isoformat()


def _is_bare_campaign_name(raw_target: str) -> bool:
    """Return whether an output value is a name instead of a filesystem path."""
    target = Path(raw_target).expanduser()
    return (
        not target.is_absolute()
        and os.sep not in raw_target
        and raw_target not in {".", ".."}
    )


def _has_valid_campaign_date_suffix(name: str) -> bool:
    """Return whether a name ends in a valid campaign date and optional counter."""
    match: re.Match[str] | None = DATE_SUFFIX.search(name)
    if match is None:
        return False
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return False
    return True


def _campaign_base_dir(
    default_name: str,
    *,
    cwd: str | os.PathLike[str] | None,
    now: datetime | None,
) -> Path:
    """Build the unnumbered result path used to allocate a campaign sequence."""
    if not CAMPAIGN_NAME_PATTERN.fullmatch(default_name):
        raise ValueError(f"invalid default campaign name: {default_name!r}")
    base = (
        Path(cwd)
        if cwd is not None
        else Path(__file__).resolve().parents[1]
    )
    return base / RESULTS_DIRECTORY / f"{default_name}-{utc_date(now)}"


def resolve_campaign_dir(
    out_dir: str | os.PathLike[str] | None = None,
    *,
    default_name: str = DEFAULT_CAMPAIGN_NAME,
    cwd: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> Path:
    """Resolve a complete path, a campaign prefix, or a dated campaign name."""
    if out_dir:
        base = Path(cwd) if cwd is not None else Path.cwd()
        raw_target: str = os.fspath(out_dir)
        target = Path(raw_target).expanduser()
        if not target.is_absolute():
            if _is_bare_campaign_name(raw_target):
                if _has_valid_campaign_date_suffix(raw_target):
                    target = base / RESULTS_DIRECTORY / raw_target
                else:
                    campaign_name = f"{raw_target}-{default_name}"
                    if not CAMPAIGN_NAME_PATTERN.fullmatch(campaign_name):
                        raise ValueError(f"invalid campaign name: {raw_target!r}")
                    return next_available_campaign_dir(
                        default_name=campaign_name, cwd=base, now=now
                    ).resolve()
            else:
                target = base / target
    else:
        campaign_base: Path = _campaign_base_dir(
            default_name, cwd=cwd, now=now
        )
        target = campaign_base.with_name(
            f"{campaign_base.name}-{FIRST_CAMPAIGN_COUNTER:0{CAMPAIGN_COUNTER_WIDTH}d}"
        )
    return target.resolve()


def next_available_campaign_dir(
    *,
    default_name: str = DEFAULT_CAMPAIGN_NAME,
    cwd: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> Path:
    """Select the next unused numbered path for a campaign name."""
    target = _campaign_base_dir(default_name, cwd=cwd, now=now)
    maximum_counter = 0
    counter_prefix = f"{target.name}-"
    for existing_path in target.parent.glob(f"{target.name}-*"):
        suffix = existing_path.name[len(counter_prefix) :]
        if suffix.isdigit() and (existing_path.exists() or existing_path.is_symlink()):
            maximum_counter = max(maximum_counter, int(suffix))

    counter = maximum_counter + 1
    while True:
        candidate = target.with_name(
            f"{target.name}-{counter:0{CAMPAIGN_COUNTER_WIDTH}d}"
        )
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        counter += 1


def resolve_new_campaign_dir(
    out_dir: str | os.PathLike[str] | None = None,
    *,
    default_name: str = DEFAULT_CAMPAIGN_NAME,
    cwd: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> Path:
    """Select a numbered path for a new campaign or preserve an explicit path."""
    if not out_dir:
        return next_available_campaign_dir(
            default_name=default_name, cwd=cwd, now=now
        )

    return resolve_campaign_dir(
        out_dir, default_name=default_name, cwd=cwd, now=now
    )


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


def campaign_results_dir(out_dir: str | os.PathLike[str]) -> Path:
    """Return the results directory for a campaign root."""
    return Path(out_dir).expanduser().resolve() / RESULTS_DIRECTORY


def campaign_logs_dir(out_dir: str | os.PathLike[str]) -> Path:
    """Return the logs directory for a campaign root."""
    return Path(out_dir).expanduser().resolve() / LOGS_DIRECTORY


def _has_legacy_campaign_artifacts(directory: Path) -> bool:
    """Return whether campaign artifacts still exist directly under the root."""
    patterns: tuple[str, ...] = (
        "index.csv",
        "campaign_run.json",
        "campaign_manifest.json",
        "client_environment.json",
        "serving_policy_*.json",
        "*.summary.json",
        "*.raw.jsonl",
        "*.power.jsonl",
        "*.vllm_metrics.jsonl",
        "server_*.log",
        "run_job_*.log",
        "allreduce_ray_*.csv",
        "allreduce_ray_*_raw.log",
        "allreduce_ray_*_nccl.log",
        "interconnect_extra_*.csv",
        "interconnect_extra_*_raw.log",
        "interconnect_extra_*_nccl.log",
        ".canary_passed",
    )
    return any(
        path.is_file()
        for pattern in patterns
        for path in directory.glob(pattern)
    )


def _has_only_preflight_artifacts(directory: Path) -> bool:
    """Return whether a new campaign root contains only preflight output."""
    children: list[Path] = list(directory.iterdir())
    allowed_names: set[str] = {
        PREFLIGHT_DIRECTORY,
        RESULTS_DIRECTORY,
        LOGS_DIRECTORY,
    }
    if not children or any(child.name not in allowed_names for child in children):
        return False

    preflight_directory = directory / PREFLIGHT_DIRECTORY
    if preflight_directory.is_symlink() or not preflight_directory.is_dir():
        return False

    for subdirectory_name in (RESULTS_DIRECTORY, LOGS_DIRECTORY):
        subdirectory = directory / subdirectory_name
        if subdirectory.is_symlink():
            return False
        if not subdirectory.exists():
            continue
        if not subdirectory.is_dir() or any(subdirectory.iterdir()):
            return False
    return True


def resolve_campaign_results_dir(out_dir: str | os.PathLike[str]) -> Path:
    """Resolve new results paths while retaining support for older campaigns."""
    directory = Path(out_dir).expanduser().resolve()
    nested_results = directory / RESULTS_DIRECTORY
    if (nested_results / "index.csv").is_file():
        return nested_results
    if _has_legacy_campaign_artifacts(directory):
        return directory
    return nested_results


def resolve_campaign_logs_dir(out_dir: str | os.PathLike[str]) -> Path:
    """Resolve the logs directory for new and legacy campaign layouts."""
    directory = Path(out_dir).expanduser().resolve()
    nested_results = directory / RESULTS_DIRECTORY
    if (nested_results / "index.csv").is_file() or (
        nested_results / IDENTITY_FILENAME
    ).is_file():
        return campaign_logs_dir(directory)
    if _has_legacy_campaign_artifacts(directory):
        return directory
    if (directory / LOGS_DIRECTORY).is_dir():
        return directory / LOGS_DIRECTORY
    return campaign_logs_dir(directory)


def campaign_root_from_input(path: str | os.PathLike[str]) -> Path:
    """Resolve a campaign directory or an index path to its directory."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if candidate.is_dir() and candidate.name == RESULTS_DIRECTORY:
        return candidate.parent
    if candidate.is_dir() or candidate.suffix.lower() != ".csv":
        return candidate
    if candidate.parent.name == RESULTS_DIRECTORY:
        return candidate.parent.parent
    return candidate.parent


def resolve_index_path(
    index_path: str | os.PathLike[str] | None = None,
    *,
    default_name: str = DEFAULT_CAMPAIGN_NAME,
) -> Path:
    """Resolve an index file or campaign directory to its index.csv path."""
    if index_path is None:
        campaign_dir = resolve_campaign_dir(default_name=default_name)
        return resolve_campaign_results_dir(campaign_dir) / "index.csv"
    candidate = Path(index_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if candidate.is_dir() or candidate.suffix.lower() != ".csv":
        nested_index = candidate / RESULTS_DIRECTORY / "index.csv"
        legacy_index = candidate / "index.csv"
        if nested_index.is_file():
            return nested_index
        if legacy_index.is_file():
            return legacy_index
        if candidate.name == RESULTS_DIRECTORY:
            return legacy_index
        if _has_legacy_campaign_artifacts(candidate):
            return legacy_index
        return nested_index
    return candidate


def resolve_campaign_output_path(
    output_path: str | os.PathLike[str] | None,
    index_path: str | os.PathLike[str],
    *,
    default_subdir: str = "figs",
) -> Path:
    """Place a default or relative report path within the selected campaign root."""
    campaign_dir = campaign_root_from_input(resolve_index_path(index_path))
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
    marker = directory / RESULTS_DIRECTORY / IDENTITY_FILENAME
    if not marker.is_file():
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
    index = directory / RESULTS_DIRECTORY / "index.csv"
    if not index.exists() and marker.parent == directory:
        index = directory / "index.csv"
    if index.exists() and (not index.is_file() or index.is_symlink()):
        return None
    return identity


def prepare_campaign(
    out_dir: str | os.PathLike[str],
    identity: dict[str, Any],
    *,
    plan_only: bool = False,
    allow_empty_existing: bool = False,
) -> bool:
    """Create a marker or accept a matching resume.

    If allowed, an existing root may contain only preflight output and empty
    campaign results and logs directories.
    Returns True for an existing compatible campaign and False for a new one.
    Plan-only mode validates existing paths without creating files or folders.
    """
    directory = Path(out_dir).expanduser().resolve()
    created_directory = False
    if not directory.exists() and not plan_only:
        try:
            directory.mkdir(parents=True, exist_ok=False)
            created_directory = True
        except FileExistsError:
            pass

    if directory.exists():
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"campaign path is not a directory: {directory}")
        existing_identity = _read_existing_identity(directory)
        if existing_identity == identity:
            return True
        existing_paths: list[Path] = list(directory.iterdir())
        if existing_paths and not (
            allow_empty_existing and _has_only_preflight_artifacts(directory)
        ):
            raise ValueError(
                f"campaign path already exists and is not a compatible "
                f"resume: {directory}; set OUT_DIR to a fresh path"
            )
        if not existing_paths and not created_directory and not allow_empty_existing:
            raise ValueError(
                f"campaign path already exists and is not a compatible "
                f"resume: {directory}; set OUT_DIR to a fresh path"
            )
    elif not plan_only:
        raise ValueError(f"campaign path is not a directory: {directory}")

    if plan_only:
        return False

    result_directory = campaign_results_dir(directory)
    result_directory.mkdir(exist_ok=True)
    payload = {
        **campaign_metadata(directory),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "identity": identity,
    }
    marker = campaign_results_dir(directory) / IDENTITY_FILENAME
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        existing_identity = _read_existing_identity(directory)
        if existing_identity == identity:
            return True
        raise ValueError(
            f"campaign path was claimed by an incompatible run: {directory}"
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
    resolve.add_argument(
        "--next-available",
        action="store_true",
        help="select the next numbered default path for a new campaign",
    )

    results = commands.add_parser("results-dir", help="resolve a campaign results directory")
    results.add_argument("--out-dir", required=True)

    logs = commands.add_parser("logs-dir", help="resolve a campaign logs directory")
    logs.add_argument("--out-dir", required=True)

    identity = commands.add_parser("identity", help="fingerprint campaign inputs")
    identity.add_argument("--file", action="append", default=[])
    identity.add_argument("--value", action="append", default=[])

    prepare = commands.add_parser("prepare", help="guard a campaign path")
    prepare.add_argument("--out-dir", required=True)
    prepare.add_argument("--identity-json", required=True)
    prepare.add_argument("--plan-only", action="store_true")
    prepare.add_argument("--allow-empty-existing", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resolve":
            default_name: str = args.default_name
            campaign_name: str | None = os.environ.get("CAMPAIGN_NAME")
            if (
                not args.out_dir
                and default_name == DEFAULT_CAMPAIGN_NAME
                and campaign_name
            ):
                default_name = f"{campaign_name}-{default_name}"
            if args.next_available and not args.out_dir:
                output_dir = next_available_campaign_dir(default_name=default_name)
            else:
                output_dir = resolve_new_campaign_dir(
                    args.out_dir, default_name=default_name
                )
            print(output_dir)
        elif args.command == "results-dir":
            print(resolve_campaign_results_dir(args.out_dir))
        elif args.command == "logs-dir":
            print(resolve_campaign_logs_dir(args.out_dir))
        elif args.command == "identity":
            print(json.dumps(fingerprint_identity(args.file, args.value), sort_keys=True))
        else:
            identity = json.loads(args.identity_json)
            resumed = prepare_campaign(
                args.out_dir,
                identity,
                plan_only=args.plan_only,
                allow_empty_existing=args.allow_empty_existing,
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
