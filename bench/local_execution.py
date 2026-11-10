"""Local execution identity and GPU runtime facts for solo campaigns."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Literal, Sequence, TypedDict

LOOPBACK_ADDRESS = "127.0.0.1"
GPU_QUERY = (
    ("gpu_name", "name"),
    ("gpu_uuid", "uuid"),
    ("driver", "driver_version"),
    ("power_limit_w", "power.limit"),
    ("compute_capability", "compute_cap"),
    ("max_graphics_clock_mhz", "clocks.max.graphics"),
    ("application_graphics_clock_mhz", "clocks.applications.graphics"),
    (
        "default_application_graphics_clock_mhz",
        "clocks.default_applications.graphics",
    ),
)
REQUIRED_GPU_FIELDS = frozenset(
    {"gpu_name", "gpu_uuid", "driver", "compute_capability", "max_graphics_clock_mhz"}
)
UNAVAILABLE_VALUES = frozenset(
    {"", "n/a", "[n/a]", "not supported", "[not supported]", "unknown"}
)
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class LocalExecutionRecord(TypedDict):
    schema_version: int
    execution_scope: Literal["single-node"]
    hostname: str
    loopback_address: str
    serving_container: str


class LocalGpuFacts(TypedDict):
    gpu_name: str
    gpu_uuid: str
    driver: str
    power_limit_w: str
    compute_capability: str
    max_graphics_clock_mhz: str
    application_graphics_clock_mhz: str
    default_application_graphics_clock_mhz: str


class LocalRuntimeFacts(TypedDict):
    execution_sha256: str
    hostname: str
    image: str
    loopback_address: str
    gpus: list[LocalGpuFacts]
    query_fields: list[str]
    runtime_env: dict[str, str]
    serving_container: str
    source: Literal["host-nvidia-smi"]


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _execution_record(
    hostname: str, serving_container: str
) -> LocalExecutionRecord:
    normalized_hostname: str = hostname.rstrip(".").casefold()
    if HOSTNAME_PATTERN.fullmatch(normalized_hostname) is None:
        raise ValueError(f"invalid normalized local hostname {hostname!r}")
    if not serving_container:
        raise ValueError("serving container must be non-empty")
    record: LocalExecutionRecord = {
        "schema_version": 1,
        "execution_scope": "single-node",
        "hostname": normalized_hostname,
        "loopback_address": LOOPBACK_ADDRESS,
        "serving_container": serving_container,
    }
    return record


def install_local_execution_record(
    results_directory: str | os.PathLike[str],
    hostname: str,
    serving_container: str,
) -> tuple[LocalExecutionRecord, str, Path]:
    """Install the canonical local execution record without replacing it."""
    record = _execution_record(hostname, serving_container)
    encoded = _canonical_json_bytes(record)
    digest = hashlib.sha256(encoded).hexdigest()
    destination = Path(results_directory) / "inputs" / "local_execution.json"
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() or destination.is_symlink():
        status = destination.lstat()
        if not stat.S_ISREG(status.st_mode) or destination.is_symlink():
            raise ValueError(f"refusing non-regular local execution input: {destination}")
        if destination.read_bytes() != encoded:
            raise ValueError(f"local execution input conflicts with campaign: {destination}")
        return record, digest, destination

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != encoded:
                raise ValueError(
                    f"local execution input conflicts with campaign: {destination}"
                )
    finally:
        temporary.unlink(missing_ok=True)
    return record, digest, destination


def local_execution_record_digest(hostname: str, serving_container: str) -> str:
    """Return the canonical execution-record digest without writing a file."""
    return hashlib.sha256(
        _canonical_json_bytes(_execution_record(hostname, serving_container))
    ).hexdigest()


def verify_local_execution_record(
    results_directory: str | os.PathLike[str],
    hostname: str,
    serving_container: str,
) -> str:
    """Verify the saved canonical execution record and return its digest."""
    expected = _canonical_json_bytes(_execution_record(hostname, serving_container))
    source = Path(results_directory) / "inputs" / "local_execution.json"
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"local execution input is missing or not regular: {source}")
    if not stat.S_ISREG(source.lstat().st_mode):
        raise ValueError(f"local execution input is not a regular file: {source}")
    actual = source.read_bytes()
    if actual != expected:
        raise ValueError(f"local execution input does not match this host: {source}")
    return hashlib.sha256(actual).hexdigest()


def collect_local_runtime_facts(
    execution_sha256: str,
    hostname: str,
    serving_container: str,
    image: str,
) -> LocalRuntimeFacts:
    """Collect required local GPU facts and applicable standalone settings."""
    query_command: list[str] = [
        "nvidia-smi",
        "--query-gpu=" + ",".join(field for _, field in GPU_QUERY),
        "--format=csv,noheader,nounits",
    ]
    try:
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            query_command,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"single-host nvidia-smi identity query failed: {error}") from error

    lines: list[str] = [
        line for line in completed.stdout.splitlines() if line.strip()
    ]
    if len(lines) != 1:
        raise RuntimeError(
            f"expected exactly one host GPU, nvidia-smi returned {len(lines)}"
        )
    values: list[str] = [
        value.strip() for value in next(csv.reader([lines[0]]))
    ]
    if len(values) != len(GPU_QUERY):
        raise RuntimeError(
            f"nvidia-smi returned {len(values)} fields, expected {len(GPU_QUERY)}"
        )
    for (field_name, _), value in zip(GPU_QUERY, values):
        if field_name in REQUIRED_GPU_FIELDS and value.casefold() in UNAVAILABLE_VALUES:
            raise RuntimeError(
                f"required nvidia-smi field {field_name} is unavailable: {value!r}"
            )

    execution_record = _execution_record(hostname, serving_container)
    if hashlib.sha256(_canonical_json_bytes(execution_record)).hexdigest() != execution_sha256:
        raise RuntimeError("local execution record digest does not match runtime input")
    if not image:
        raise RuntimeError("serving image must be non-empty")
    values_by_field: dict[str, str] = {
        field_name: value for (field_name, _), value in zip(GPU_QUERY, values)
    }
    gpu_facts: LocalGpuFacts = {
        "gpu_name": values_by_field["gpu_name"],
        "gpu_uuid": values_by_field["gpu_uuid"],
        "driver": values_by_field["driver"],
        "power_limit_w": values_by_field["power_limit_w"],
        "compute_capability": values_by_field["compute_capability"],
        "max_graphics_clock_mhz": values_by_field["max_graphics_clock_mhz"],
        "application_graphics_clock_mhz": values_by_field[
            "application_graphics_clock_mhz"
        ],
        "default_application_graphics_clock_mhz": values_by_field[
            "default_application_graphics_clock_mhz"
        ],
    }
    runtime_environment: dict[str, str] = {
        "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "1"),
        "TRANSFORMERS_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "1"),
        "TORCHINDUCTOR_AUTOGRAD_CACHE": os.environ.get(
            "TORCHINDUCTOR_AUTOGRAD_CACHE", "0"
        ),
    }
    runtime_facts: LocalRuntimeFacts = {
        "execution_sha256": execution_sha256,
        "hostname": execution_record["hostname"],
        "image": image,
        "loopback_address": LOOPBACK_ADDRESS,
        "gpus": [gpu_facts],
        "query_fields": [query_field for _, query_field in GPU_QUERY],
        "runtime_env": runtime_environment,
        "serving_container": serving_container,
        "source": "host-nvidia-smi",
    }
    return runtime_facts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("results_directory", type=Path)
    record_parser.add_argument("hostname")
    record_parser.add_argument("serving_container")
    identity_parser = subparsers.add_parser("identity")
    identity_parser.add_argument("hostname")
    identity_parser.add_argument("serving_container")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("results_directory", type=Path)
    verify_parser.add_argument("hostname")
    verify_parser.add_argument("serving_container")
    runtime_parser = subparsers.add_parser("runtime")
    runtime_parser.add_argument("execution_sha256")
    runtime_parser.add_argument("hostname")
    runtime_parser.add_argument("serving_container")
    runtime_parser.add_argument("image")
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            _, digest, _ = install_local_execution_record(
                args.results_directory, args.hostname, args.serving_container
            )
            print(digest)
        elif args.command == "identity":
            print(local_execution_record_digest(args.hostname, args.serving_container))
        elif args.command == "verify":
            print(verify_local_execution_record(
                args.results_directory, args.hostname, args.serving_container
            ))
        else:
            facts = collect_local_runtime_facts(
                args.execution_sha256,
                args.hostname,
                args.serving_container,
                args.image,
            )
            print(_canonical_json_bytes(facts).decode("utf-8"))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"local_execution.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
