"""Client, configuration, source, and deployment identities for measurements."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import site
import subprocess
import sys
import time

from scripts.contracts.dependency_lock import normalize_distribution_name

from .config import ExperimentConfig
from . import server_info


# The window may extend under min_window_requests; repeats use fresh launches.
METHODOLOGY_VERSION = "steady-exact-v4"


def sha256_json(value: object) -> str:
    """Hash an object's canonical JSON serialization."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def client_environment() -> dict[str, object]:
    """Return the result-affecting client runtime in a stable form.

    Source hashes alone do not protect a resumed campaign from tokenizer,
    percentile, YAML, or streaming-client behavior changing after a package
    upgrade. Missing packages are recorded explicitly instead of being omitted.
    """
    packages = (
        "aiohttp", "numpy", "transformers", "tokenizers",
        "huggingface-hub", "PyYAML", "pandas", "matplotlib",
    )
    versions: dict[str, str] = {}
    for name in packages:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "<missing>"
    installed: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            installed[normalize_distribution_name(name)] = dist.version
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "virtual_environment": sys.prefix != sys.base_prefix,
        "user_site_enabled": bool(site.ENABLE_USER_SITE),
        "installed_distributions": [
            f"{name}=={version}" for name, version in sorted(installed.items())
        ],
        **versions,
    }


def client_environment_fingerprint() -> str:
    """Hash the current client environment snapshot."""
    return sha256_json(client_environment())


DEPLOYMENT_INFO_FIELDS = (
    "vllm_version", "max_model_len", "block_size", "gpu_blocks",
    "total_kv_tokens", "total_kv_tokens_source", "kv_cache_dtype",
    "gpu_memory_utilization", "enable_prefix_caching", "hybrid_cache",
    "worker_weight_gb", "worker_kv_cache_gb", "max_concurrency",
)
DEPLOYMENT_REQUIRED_FIELDS = (
    "vllm_version", "max_model_len", "gpu_blocks", "total_kv_tokens",
    "gpu_memory_utilization", "enable_prefix_caching",
)


def deployment_state(info: dict[str, object]) -> dict[str, object]:
    """Return only stable, serving-relevant live capacity fields."""
    missing = [name for name in DEPLOYMENT_REQUIRED_FIELDS
               if info.get(name) in (None, "")]
    if missing:
        raise RuntimeError(
            "live deployment metadata is incomplete: " + ", ".join(missing)
        )
    return {name: info.get(name) for name in DEPLOYMENT_INFO_FIELDS}


def deployment_fingerprint(
    serving_fingerprint: str, info: dict[str, object]
) -> str:
    """Hash launch policy plus the capacity/runtime observed after startup."""
    return sha256_json({
        "serving_fingerprint": serving_fingerprint,
        "deployment_state": deployment_state(info),
    })


_EXPERIMENT_FINGERPRINT_TRANSIENT_FIELDS = frozenset((
    "repeat_id",
    "repeat_count",
    "server_log",
    "deployment_fingerprint",
))


def experiment_fingerprint(
    resolved_config: dict[str, object], harness_source_sha256: str
) -> str:
    """Hash measurement identity: what was requested, not what booted.

    Live capacity is deliberately excluded. vLLM's memory profiling settles on
    a slightly different KV block count on every launch, so folding it in here
    would give repeats of one arm different experiment identities whenever they
    span a server restart -- which every repeat does when repeats are collected
    across launches on purpose. deployment_fingerprint() records that live
    state separately, per row.
    """
    stable_config = {
        key: value for key, value in resolved_config.items()
        if key not in _EXPERIMENT_FINGERPRINT_TRANSIENT_FIELDS
    }
    return sha256_json({
        "methodology_version": METHODOLOGY_VERSION,
        "harness_source_sha256": harness_source_sha256,
        "resolved_config": stable_config,
    })


def collect_deployment_info(base_url: str, model: str, server_log: str,
                            *, compute_from_hf: bool = False,
                            trust_remote_code: bool = False,
                            attempts: int = 3,
                            delay_s: float = 1.0) -> dict[str, object]:
    """Collect live identity with retries and merge transiently missing fields."""
    merged: dict[str, object] = {}
    for attempt in range(attempts):
        current = server_info.collect(
            base_url, model, server_log, compute_from_hf=compute_from_hf,
            trust_remote_code=trust_remote_code,
        )
        merged.update({key: value for key, value in current.items()
                       if value is not None})
        if attempt + 1 < attempts:
            time.sleep(delay_s)
    deployment_state(merged)
    return merged


def config_file_fingerprint(path: str | None,
                            overrides: dict[str, object] | None = None) -> str:
    """Hash the canonical effective campaign configuration.

    Applying overrides to the parsed dataclass makes an explicit override equal
    to the YAML value hash identically, while a genuinely different timing,
    metadata, or power policy cannot be resumed into the base campaign.
    """
    cfg = ExperimentConfig.from_yaml(path) if path else ExperimentConfig()
    effective_overrides = {
        key: value for key, value in (overrides or {}).items()
        if value is not None
    }
    for key, value in effective_overrides.items():
        if not hasattr(cfg, key):
            raise ValueError(f"unknown ExperimentConfig override {key!r}")
        setattr(cfg, key, value)
    cfg.validate()
    return sha256_json(cfg.to_dict())


def source_fingerprint() -> str:
    """Hash measurement-affecting source files, including uncommitted edits."""
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "bench").glob("*.py"))
    paths += [root / "scripts" / name for name in (
        "launch_server.sh", "start_ray.sh", "run_all.sh", "run_models.sh",
        "run_job.sh", "preflight.sh", "hardware_smoke.sh",
        "predownload.sh", "mirror_worker_cache.sh", "purge_model_cache.sh",
        "ray_allreduce_bench.sh", "interconnect_extra_bench.sh",
        "connectx_env.sh", "verify_connectx.sh", "cluster_topology.py",
        "system_snapshot.py", "models_for_config.py", "swept_concurrencies.py",
        "ray_allreduce_bench.py", "ray_interconnect_bench.py",
        "cluster_runtime_fingerprint.py", "contracts/dependency_lock.py",
        "campaign/plan.py", "campaign/manifest.py",
    )]
    h = hashlib.sha256()
    for path in sorted((p for p in paths if p.exists()), key=lambda p: str(p)):
        h.update(str(path.relative_to(root)).encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


def git_commit() -> str | None:
    """Return the current Git commit when it is available."""
    try:
        root = Path(__file__).resolve().parents[1]
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                           text=True, capture_output=True, timeout=5, check=True)
        return p.stdout.strip() or None
    except Exception:
        return None


