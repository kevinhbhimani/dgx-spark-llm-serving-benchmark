#!/usr/bin/env python3
"""Collect read-only host and serving-container facts for a benchmark run."""
from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Sequence
from operator import itemgetter
from pathlib import Path
from typing import TypeAlias, TypeGuard


JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]

COMMAND_TIMEOUT_SECONDS = 30
SSH_TIMEOUT_SECONDS = 90
SSH_CONNECT_TIMEOUT_SECONDS = 8
HOST_PATTERN = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._:-]*$")
CONTAINER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./:@_-]*$")
DMI_FIELDS = {
    "bios_vendor": "/sys/class/dmi/id/bios_vendor",
    "bios_version": "/sys/class/dmi/id/bios_version",
    "bios_date": "/sys/class/dmi/id/bios_date",
    "board_vendor": "/sys/class/dmi/id/board_vendor",
    "board_name": "/sys/class/dmi/id/board_name",
    "board_version": "/sys/class/dmi/id/board_version",
    "product_name": "/sys/class/dmi/id/product_name",
    "product_version": "/sys/class/dmi/id/product_version",
}
CONTAINER_PROBE_SOURCE = """
import datetime
import importlib.metadata
import json
import platform
import shutil
import subprocess
from operator import itemgetter

packages: list[dict[str, str]] = []
for distribution in importlib.metadata.distributions():
    package_name = distribution.metadata.get("Name")
    if package_name:
        packages.append({"name": package_name, "version": distribution.version})
packages.sort(key=itemgetter("name"))
package_versions = {
    package["name"].casefold(): package["version"] for package in packages
}
runtime_versions: dict[str, str | None] = {
    name: package_versions.get(name)
    for name in (
        "ray", "vllm", "torch", "transformers", "nvidia-ml-py",
        "nvidia-cuda-runtime",
    )
}
runtime_versions["python"] = platform.python_version()
try:
    os_release = platform.freedesktop_os_release()
except OSError:
    os_release = None
system_packages: dict[str, str | list[dict[str, str]]] = {
    "status": "unavailable",
    "error": "dpkg-query command not installed",
}
if shutil.which("dpkg-query") is not None:
    try:
        completed = subprocess.run(
            ["dpkg-query", "-W", "-f=${binary:Package}\\t${Version}\\n"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        system_packages = {
            "manager": "dpkg-query",
            "status": "unavailable",
            "error": str(error),
        }
    else:
        if completed.returncode == 0:
            system_packages = {
                "manager": "dpkg-query",
                "packages": [
                    {"name": name, "version": version}
                    for line in completed.stdout.splitlines()
                    for name, separator, version in [line.partition("\\t")]
                    if separator and name and version
                ],
            }
        else:
            system_packages = {
                "manager": "dpkg-query",
                "status": "error",
                "stderr": completed.stderr.strip(),
            }
print(json.dumps({
    "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "os_release": os_release,
    "runtime_versions": runtime_versions,
    "python_packages": packages,
    "system_packages": system_packages,
}, sort_keys=True, separators=(",", ":")))
"""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def is_json_value(value: object) -> TypeGuard[JsonValue]:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and is_json_value(item)
            for key, item in value.items()
        )
    return False


def command_result(
    arguments: Sequence[str], timeout: int = COMMAND_TIMEOUT_SECONDS
) -> JsonObject:
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "command": list(arguments)}
    except OSError as error:
        return {
            "status": "unavailable",
            "command": list(arguments),
            "error": str(error),
        }

    result: JsonObject = {
        "status": "ok" if completed.returncode == 0 else "error",
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
    }
    if completed.stderr.strip():
        result["stderr"] = completed.stderr.strip()
    return result


def read_text(path: str) -> str | None:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def python_package_versions() -> list[JsonObject]:
    packages: list[JsonObject] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            packages.append({"name": name, "version": distribution.version})
    packages.sort(key=itemgetter("name"))
    return packages


def system_package_versions() -> JsonObject:
    package_commands: tuple[tuple[str, list[str]], ...] = (
        (
            "dpkg-query",
            ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"],
        ),
        ("rpm", ["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\n"]),
    )
    for manager, arguments in package_commands:
        if shutil.which(arguments[0]) is None:
            continue
        result = command_result(arguments)
        if result.get("status") != "ok":
            return {"manager": manager, "collection": result}
        stdout = result.get("stdout", "")
        packages: list[JsonObject] = []
        if isinstance(stdout, str):
            for line in stdout.splitlines():
                name, separator, version = line.partition("\t")
                if separator and name and version:
                    packages.append({"name": name, "version": version})
        return {"manager": manager, "packages": packages}
    return {"status": "unavailable", "error": "no supported package query tool"}


def memory_snapshot() -> JsonObject:
    memory: JsonObject = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return {"status": "unavailable", "error": str(error)}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            memory[key] = value.strip()
    return memory


def cpu_snapshot() -> JsonObject:
    cpu: JsonObject = {"logical_cpu_count": os.cpu_count()}
    try:
        first_cpu = Path("/proc/cpuinfo").read_text(encoding="utf-8").split(
            "\n\n", 1
        )[0]
    except OSError:
        return cpu
    fields = {"model name", "Hardware", "Processor", "CPU implementer", "CPU part"}
    for line in first_cpu.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() in fields:
            cpu[name.strip()] = value.strip()
    return cpu


def firmware_snapshot() -> JsonObject:
    return {name: read_text(path) for name, path in DMI_FIELDS.items()}


def network_snapshot() -> JsonObject:
    interfaces: JsonObject = {}
    try:
        interface_names = socket.if_nameindex()
    except OSError as error:
        interface_names = []
        interfaces["collection_error"] = str(error)
    if shutil.which("ethtool") is None:
        interfaces["status"] = "ethtool command not installed"
    else:
        for _, interface_name in interface_names:
            if interface_name == "lo":
                continue
            device_path = Path("/sys/class/net") / interface_name / "device"
            if device_path.exists():
                interfaces[interface_name] = command_result(
                    ["ethtool", "-i", interface_name], timeout=10
                )
    rdma = (
        command_result(["rdma", "link", "show"], timeout=10)
        if shutil.which("rdma") is not None
        else {"status": "unavailable", "error": "rdma command not installed"}
    )
    return {
        "interface_driver_firmware": interfaces,
        "link_counters": command_result(["ip", "-s", "-j", "link"], timeout=10),
        "rdma_links": rdma,
    }


def host_snapshot() -> JsonObject:
    try:
        os_release: JsonValue = platform.freedesktop_os_release()
    except OSError:
        os_release = None
    try:
        disk_usage = shutil.disk_usage("/")
        root_filesystem: JsonValue = {
            "total_bytes": disk_usage.total,
            "used_bytes": disk_usage.used,
            "free_bytes": disk_usage.free,
        }
    except OSError as error:
        root_filesystem = {"status": "unavailable", "error": str(error)}

    return {
        "captured_at_utc": utc_now(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "os_release": os_release,
        "boot_id": read_text("/proc/sys/kernel/random/boot_id"),
        "proc_uptime": read_text("/proc/uptime"),
        "cpu": cpu_snapshot(),
        "memory": memory_snapshot(),
        "root_filesystem": root_filesystem,
        "firmware": firmware_snapshot(),
        "gpu_inventory": command_result(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,vbios_version,pci.bus_id,compute_cap,"
                "temperature.gpu,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            timeout=15,
        ),
        "network": network_snapshot(),
        "recent_kernel_errors": command_result(
            [
                "journalctl", "-k", "-b", "-p", "err..alert", "-n", "100",
                "--no-pager", "-o", "short-iso-precise",
            ],
            timeout=15,
        ),
        "system_packages": system_package_versions(),
        "python": {
            "version": platform.python_version(),
            "packages": python_package_versions(),
        },
    }


def parse_probe_output(completed: subprocess.CompletedProcess[str]) -> JsonObject:
    if completed.returncode != 0:
        return {
            "status": "error",
            "exit_code": completed.returncode,
            "stderr": completed.stderr.strip(),
            "stdout": completed.stdout.strip(),
        }
    for line in reversed(completed.stdout.splitlines()):
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and all(
            isinstance(key, str) and is_json_value(item)
            for key, item in value.items()
        ):
            result: JsonObject = {key: item for key, item in value.items()}
            result["probe_status"] = "ok"
            return result
    return {
        "status": "error",
        "error": "probe did not return a JSON object",
        "stdout": completed.stdout.strip(),
    }


def embedded_python_probe(
    arguments: Sequence[str], source: str, timeout: int
) -> JsonObject:
    try:
        completed = subprocess.run(
            arguments,
            input=source,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "command": list(arguments)}
    except OSError as error:
        return {
            "status": "unavailable",
            "command": list(arguments),
            "error": str(error),
        }
    return parse_probe_output(completed)


def node_snapshot(container_name: str, image: str) -> JsonObject:
    container: JsonObject = {
        "name": container_name,
        "declared_image": image,
        "image_id": command_result(
            ["docker", "inspect", "--format={{.Image}}", container_name],
            timeout=10,
        ),
        "runtime": embedded_python_probe(
            ["docker", "exec", "-i", container_name, "python3", "-"],
            CONTAINER_PROBE_SOURCE,
            timeout=60,
        ),
    }
    return {"status": "collected", "host": host_snapshot(), "container": container}


def validate_host(host: str) -> None:
    if HOST_PATTERN.fullmatch(host) is None:
        raise ValueError(f"unsafe SSH host value: {host!r}")


def validate_container(container_name: str) -> None:
    if CONTAINER_PATTERN.fullmatch(container_name) is None:
        raise ValueError(f"unsafe container name: {container_name!r}")


def validate_image(image: str) -> None:
    if image and IMAGE_PATTERN.fullmatch(image) is None:
        raise ValueError("image reference contains unsupported characters")


def remote_node_snapshot(
    host: str, container_name: str, image: str, source: str
) -> JsonObject:
    validate_host(host)
    validate_container(container_name)
    validate_image(image)
    remote_command = (
        "python3 - --node --container " + container_name + " --image " + image
    )
    arguments = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        "-o",
        "ConnectionAttempts=1",
        host,
        remote_command,
    ]
    return embedded_python_probe(arguments, source, SSH_TIMEOUT_SECONDS)


def write_snapshot(path: Path, snapshot: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(snapshot, handle, sort_keys=True, indent=2)
        handle.write("\n")


def parse_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--container", default="vllm-spark")
    parser.add_argument("--image", default="")
    parser.add_argument("--head-host", default="")
    parser.add_argument("--worker-host", default="")
    parser.add_argument("--head-ip", default="")
    parser.add_argument("--worker-ip", default="")
    parser.add_argument("--node", action="store_true")
    return parser.parse_args(arguments)


def main() -> int:
    arguments = parse_arguments(sys.argv[1:])
    if arguments.node:
        json.dump(
            node_snapshot(arguments.container, arguments.image),
            sys.stdout,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stdout.write("\n")
        return 0
    if arguments.output is None:
        raise SystemExit("--output is required")
    validate_host(arguments.worker_host)
    validate_container(arguments.container)
    validate_image(arguments.image)
    source = Path(__file__).read_text(encoding="utf-8")
    snapshot: JsonObject = {
        "schema_version": 1,
        "captured_at_utc": utc_now(),
        "configuration": {
            "head_host": arguments.head_host,
            "worker_host": arguments.worker_host,
            "head_ip": arguments.head_ip,
            "worker_ip": arguments.worker_ip,
            "container": arguments.container,
            "declared_image": arguments.image,
        },
        "nodes": [
            {
                "role": "head",
                "configured_host": arguments.head_host,
                **node_snapshot(arguments.container, arguments.image),
            },
            {
                "role": "worker",
                "configured_host": arguments.worker_host,
                **remote_node_snapshot(
                    arguments.worker_host,
                    arguments.container,
                    arguments.image,
                    source,
                ),
            },
        ],
    }
    write_snapshot(arguments.output, snapshot)
    print(f"[snapshot] saved read-only system snapshot: {arguments.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"[snapshot] {error}") from error
