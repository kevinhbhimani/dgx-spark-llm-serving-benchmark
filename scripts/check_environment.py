#!/usr/bin/env python3
"""Validate and optionally record the benchmark client environment.

The default invocation is read-only.  A manifest is written only when
``--manifest`` is supplied and every validation check succeeds.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import site
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


MINIMUM_PYTHON = (3, 12)
PIP_CHECK_TIMEOUT_SECONDS = 120.0
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_PATH = REPOSITORY_ROOT / "requirements.lock"
_PIN_RE = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9._+!-]*)"
)


class LockFileError(ValueError):
    """Raised when the lock file is not a list of unique exact pins."""


@dataclass(frozen=True)
class LockedDistribution:
    name: str
    version: str

    @property
    def normalized_name(self) -> str:
        return normalize_distribution_name(self.name)

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "normalized_name": self.normalized_name,
            "version": self.version,
        }


def normalize_distribution_name(name: str) -> str:
    """Return the distribution-name normalization used by package indexes."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> list[LockedDistribution]:
    """Parse *path*, accepting only unique ``distribution==version`` pins."""
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError as exc:
        raise LockFileError(f"lock file does not exist: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise LockFileError(f"cannot read lock file {path}: {exc}") from exc

    pins: list[LockedDistribution] = []
    seen: dict[str, int] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_RE.fullmatch(line)
        if match is None:
            raise LockFileError(
                f"{path}:{line_number}: expected exactly name==version"
            )
        pin = LockedDistribution(match.group("name"), match.group("version"))
        previous = seen.get(pin.normalized_name)
        if previous is not None:
            raise LockFileError(
                f"{path}:{line_number}: duplicate distribution {pin.name!r} "
                f"(first pinned on line {previous})"
            )
        seen[pin.normalized_name] = line_number
        pins.append(pin)

    if not pins:
        raise LockFileError(f"lock file contains no exact pins: {path}")
    return pins


def is_real_virtual_environment() -> bool:
    """Recognize an active stdlib/virtualenv environment, not an env variable."""
    prefix = Path(sys.prefix)
    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix))
    return prefix != base_prefix and (prefix / "pyvenv.cfg").is_file()


def _normalized_path(value: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def user_site_issues() -> list[str]:
    """Return failures when user-site packages are enabled or already active."""
    issues: list[str] = []
    if site.ENABLE_USER_SITE:
        issues.append(
            "the Python user site is enabled; restart with PYTHONNOUSERSITE=1 "
            "or python -s"
        )

    try:
        configured = site.getusersitepackages()
    except (AttributeError, OSError):
        configured = []
    user_sites = [configured] if isinstance(configured, str) else list(configured)
    active_paths = {
        _normalized_path(entry)
        for entry in sys.path
        if isinstance(entry, str) and entry
    }
    active_user_sites = sorted(
        path for path in user_sites
        if isinstance(path, str) and _normalized_path(path) in active_paths
    )
    if active_user_sites:
        issues.append(
            "user-site directory is active on sys.path: "
            + ", ".join(active_user_sites)
        )
    return issues


def runtime_issues(*, allow_non_venv: bool) -> list[str]:
    issues: list[str] = []
    if sys.version_info[:2] < MINIMUM_PYTHON:
        issues.append(
            "Python 3.12 or newer is required "
            f"(running {platform.python_version()})"
        )
    if not allow_non_venv and not is_real_virtual_environment():
        issues.append(
            "a real virtual environment is required (sys.prefix must differ "
            "from sys.base_prefix and contain pyvenv.cfg); use "
            "--allow-non-venv only for an intentional exception"
        )
    issues.extend(user_site_issues())
    return issues


def verify_locked_distributions(
    pins: Iterable[LockedDistribution],
) -> tuple[list[str], list[dict[str, str]]]:
    """Compare every pin to the active interpreter's distribution metadata."""
    issues: list[str] = []
    installed_locked: list[dict[str, str]] = []
    for pin in pins:
        try:
            actual = importlib.metadata.version(pin.name)
        except importlib.metadata.PackageNotFoundError:
            issues.append(f"locked distribution is missing: {pin.name}=={pin.version}")
            continue
        except Exception as exc:  # malformed third-party metadata must fail closed
            issues.append(f"cannot read metadata for {pin.name}: {exc}")
            continue

        if actual != pin.version:
            issues.append(
                f"locked distribution version mismatch: {pin.name} "
                f"requires {pin.version}, found {actual}"
            )
            continue
        installed_locked.append(pin.as_dict())
    return issues, installed_locked


def run_pip_check(
    *, timeout_seconds: float = PIP_CHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``pip check`` with fixed, isolated arguments and captured output."""
    command = [sys.executable, "-I", "-m", "pip", "check"]
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.update({
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PYTHONNOUSERSITE": "1",
    })
    result: dict[str, Any] = {
        "command": command,
        "ok": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }

    try:
        with tempfile.TemporaryDirectory(prefix="benchmark-pip-check-") as cwd:
            completed = subprocess.run(
                command,
                check=False,
                cwd=cwd,
                encoding="utf-8",
                env=env,
                errors="replace",
                shell=False,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
    except subprocess.TimeoutExpired as exc:
        result["stderr"] = f"timed out after {timeout_seconds:g} seconds"
        if exc.stdout:
            result["stdout"] = _decoded_subprocess_output(exc.stdout).rstrip()
        if exc.stderr:
            detail = _decoded_subprocess_output(exc.stderr).rstrip()
            result["stderr"] += f"; {detail}"
        return result
    except OSError as exc:
        result["stderr"] = f"could not execute pip check: {exc}"
        return result

    result.update({
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.rstrip(),
        "stderr": completed.stderr.rstrip(),
    })
    return result


def _decoded_subprocess_output(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def pip_check_issue(result: dict[str, Any]) -> str | None:
    if result["ok"]:
        return None
    returncode = result.get("returncode")
    status = "did not complete" if returncode is None else f"exited {returncode}"
    detail = result.get("stderr") or result.get("stdout") or "no diagnostic output"
    return f"pip check {status}: {detail}"


def installed_distributions() -> list[dict[str, str]]:
    """Return every visible distribution as a stable, duplicate-preserving list."""
    result: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name") or "<unknown>"
        version = distribution.version or "<unknown>"
        result.append({
            "name": str(name),
            "normalized_name": normalize_distribution_name(str(name)),
            "version": str(version),
        })
    result.sort(key=lambda item: (
        item["normalized_name"], item["name"].casefold(), item["version"]
    ))
    return result


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_manifest(
    lock_path: Path,
    locked: Sequence[dict[str, str]],
    pip_check: dict[str, Any],
) -> dict[str, Any]:
    installed = installed_distributions()
    version_info = sys.version_info
    return {
        "schema_version": 1,
        "executable": os.path.abspath(sys.executable),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "version_info": {
                "major": version_info.major,
                "minor": version_info.minor,
                "micro": version_info.micro,
                "releaselevel": version_info.releaselevel,
                "serial": version_info.serial,
            },
        },
        "platform": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "release": platform.release(),
            "system": platform.system(),
        },
        "virtual_environment": {
            "base_prefix": sys.base_prefix,
            "prefix": sys.prefix,
            "real": is_real_virtual_environment(),
            "user_site_enabled": site.ENABLE_USER_SITE,
        },
        "requirements_lock": {
            "path": str(lock_path.resolve()),
            "sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        },
        "locked_distributions": list(locked),
        "installed_distributions": installed,
        "installed_distributions_sha256": _json_sha256(installed),
        "pip_check": pip_check,
    }


def atomic_write_json(path: Path, value: Any) -> None:
    """Replace *path* atomically after fully flushing a same-directory file."""
    destination = path.absolute()
    parent = destination.parent
    if not parent.is_dir():
        raise OSError(f"manifest parent directory does not exist: {parent}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent, prefix=f".{destination.name}.", suffix=".tmp", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the exact, isolated Python client environment used for "
            "benchmarking. The default operation is read-only."
        )
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=DEFAULT_LOCK_PATH,
        help=f"exact requirements lock (default: {DEFAULT_LOCK_PATH})",
    )
    parser.add_argument(
        "--allow-non-venv",
        action="store_true",
        help="allow an interpreter that is not running from a real venv",
    )
    parser.add_argument(
        "--manifest",
        "--write-manifest",
        dest="manifest",
        type=Path,
        metavar="PATH",
        help="atomically write a JSON environment manifest after validation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    issues = runtime_issues(allow_non_venv=args.allow_non_venv)

    pins: list[LockedDistribution] = []
    locked: list[dict[str, str]] = []
    try:
        pins = parse_lock(args.lock)
    except LockFileError as exc:
        issues.append(str(exc))
    else:
        package_issues, locked = verify_locked_distributions(pins)
        issues.extend(package_issues)

    pip_result = run_pip_check()
    pip_issue = pip_check_issue(pip_result)
    if pip_issue is not None:
        issues.append(pip_issue)

    if issues:
        for issue in issues:
            print(f"ERROR: {issue}", file=sys.stderr)
        print(
            f"Environment validation failed with {len(issues)} error(s).",
            file=sys.stderr,
        )
        return 1

    if args.manifest is not None:
        try:
            manifest = build_manifest(args.lock, locked, pip_result)
            atomic_write_json(args.manifest, manifest)
        except (OSError, ValueError) as exc:
            print(f"ERROR: could not write manifest {args.manifest}: {exc}", file=sys.stderr)
            return 1

    message = (
        f"Environment validated: Python {platform.python_version()}, "
        f"{len(pins)} exact distributions, pip check passed"
    )
    if args.manifest is not None:
        message += f"; manifest written atomically to {args.manifest}"
    print(message + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
