"""Resolve the explicit exception to Git provenance requirements."""
from __future__ import annotations

import os


def skip_git_check() -> bool:
    value = os.environ.get("SKIP_GIT_CHECK", "0")
    if value not in ("0", "1"):
        raise ValueError(f"SKIP_GIT_CHECK must be exactly 0 or 1, got {value!r}")
    return value == "1"
