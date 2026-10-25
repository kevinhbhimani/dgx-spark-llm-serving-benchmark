"""Parse exact dependency pins shared by benchmark validation commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


_PIN_RE = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9._+!-]*)"
)
LockErrorCode = Literal["missing", "unreadable", "invalid_pin", "duplicate", "empty"]


class LockFileError(ValueError):
    """Raised when the lock file is not a list of unique exact pins."""

    def __init__(
        self,
        message: str,
        *,
        code: LockErrorCode | None = None,
        line_number: int | None = None,
        normalized_name: str | None = None,
    ) -> None:
        """Keep the diagnostic and expose details for command-specific errors."""
        super().__init__(message)
        self.code = code
        self.line_number = line_number
        self.normalized_name = normalized_name


@dataclass(frozen=True)
class LockedDistribution:
    """One exact distribution pin from the lock file."""

    name: str
    version: str

    @property
    def normalized_name(self) -> str:
        """Return the package-index form of this distribution's name."""
        return normalize_distribution_name(self.name)

    def as_dict(self) -> dict[str, str]:
        """Return the name and version fields used by manifests."""
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
        raise LockFileError(
            f"lock file does not exist: {path}", code="missing"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise LockFileError(
            f"cannot read lock file {path}: {exc}", code="unreadable"
        ) from exc

    pins: list[LockedDistribution] = []
    seen: dict[str, int] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_RE.fullmatch(line)
        if match is None:
            raise LockFileError(
                f"{path}:{line_number}: expected exactly name==version",
                code="invalid_pin",
                line_number=line_number,
            )
        pin = LockedDistribution(match.group("name"), match.group("version"))
        previous = seen.get(pin.normalized_name)
        if previous is not None:
            raise LockFileError(
                f"{path}:{line_number}: duplicate distribution {pin.name!r} "
                f"(first pinned on line {previous})",
                code="duplicate",
                line_number=line_number,
                normalized_name=pin.normalized_name,
            )
        seen[pin.normalized_name] = line_number
        pins.append(pin)

    if not pins:
        raise LockFileError(
            f"lock file contains no exact pins: {path}", code="empty"
        )
    return pins
