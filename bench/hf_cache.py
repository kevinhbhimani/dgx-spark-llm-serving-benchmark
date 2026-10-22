"""Selection and access checks for the Hugging Face Hub cache."""
from __future__ import annotations

from collections.abc import MutableMapping
import os
from pathlib import Path
import tempfile


_CACHE_PROBE_CONTENTS = b"cache access probe\n"


def configure_hf_cache(
    environment: MutableMapping[str, str] = os.environ,
) -> Path:
    """Select one cache root and verify host create/read/delete access."""
    configured_root = environment.get("HF_CACHE")
    if configured_root:
        home = Path(configured_root).expanduser()
        hub = home / "hub"
        modules = home / "modules"
    else:
        home = Path(
            environment.get("HF_HOME", "~/.cache/huggingface")
        ).expanduser()
        hub = Path(environment.get("HF_HUB_CACHE", str(home / "hub"))).expanduser()
        modules = home / "modules"

    environment["HF_HOME"] = str(home)
    environment["HF_HUB_CACHE"] = str(hub)
    environment["HF_MODULES_CACHE"] = str(modules)
    try:
        hub.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".cache-access-probe-", dir=hub
        ) as probe_directory:
            probe_path = Path(probe_directory) / "probe"
            with probe_path.open("wb") as probe:
                probe.write(_CACHE_PROBE_CONTENTS)
                probe.flush()
                os.fsync(probe.fileno())
            if probe_path.read_bytes() != _CACHE_PROBE_CONTENTS:
                raise OSError("probe contents changed after write")
            probe_path.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"HF cache create/read/delete failed at {hub}; effective "
            f"UID:GID {os.geteuid()}:{os.getegid()}: {type(exc).__name__}: {exc}"
        ) from exc
    return hub
