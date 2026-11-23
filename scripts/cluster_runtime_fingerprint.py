#!/usr/bin/env python3
"""Emit canonical two-node runtime facts and verify topology identity."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from typing import TypeAlias

import ray


JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
EXPECTED_ARGUMENT_COUNT = 11
RUNTIME_ENV_NAMES = (
    "NO_IFACE_MATCH",
    "NCCL_SOCKET_IFNAME",
    "GLOO_SOCKET_IFNAME",
    "NCCL_IB_DISABLE",
    "NCCL_DEBUG",
    "NCCL_DEBUG_SUBSYS",
    "NCCL_IB_HCA",
    "NCCL_NET_PLUGIN",
    "NCCL_IB_GID_INDEX",
    "NCCL_NSOCKS_PERTHREAD",
    "NCCL_SOCKET_NTHREADS",
    "TORCHINDUCTOR_AUTOGRAD_CACHE",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
)
SHARED_RUNTIME_ENV_NAMES = (
    "NO_IFACE_MATCH",
    "NCCL_IB_DISABLE",
    "NCCL_DEBUG",
    "NCCL_DEBUG_SUBSYS",
    "NCCL_NET_PLUGIN",
    "NCCL_NSOCKS_PERTHREAD",
    "NCCL_SOCKET_NTHREADS",
    "TORCHINDUCTOR_AUTOGRAD_CACHE",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
)


def package_version(package_name: str) -> str:
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return "<missing>"


if len(sys.argv) != EXPECTED_ARGUMENT_COUNT:
    raise SystemExit(
        "usage: cluster_runtime_fingerprint.py HEAD_IP WORKER_IP IMAGE "
        "RAY_VERSION TOPOLOGY_SHA256 HEAD_NODE_ID WORKER_NODE_ID "
        "HEAD_IFACES WORKER_IFACES NO_IFACE_MATCH"
    )

(
    head_ip,
    worker_ip,
    expected_image,
    expected_ray,
    expected_topology_sha256,
    head_node_id,
    worker_node_id,
    head_interfaces,
    worker_interfaces,
    expected_no_iface_match,
) = sys.argv[1:]
if expected_no_iface_match not in {"0", "1"}:
    raise SystemExit("NO_IFACE_MATCH must be exactly 0 or 1")
expected_nodes_by_ip: dict[str, str] = {
    head_ip: head_node_id,
    worker_ip: worker_node_id,
}
expected_interfaces_by_node: dict[str, str] = {
    head_node_id: head_interfaces,
    worker_node_id: worker_interfaces,
}
ray.init(address="auto", log_to_driver=False)


@ray.remote(num_cpus=0, num_gpus=0)
def facts(
    node_ids_by_ip: dict[str, str],
    interfaces_by_node: dict[str, str],
    topology_sha256: str,
    no_iface_match: str,
) -> JsonObject:
    import torch
    import ray as node_ray

    node_ip = node_ray.util.get_node_ip_address()
    expected_node_id = node_ids_by_ip.get(node_ip)
    if expected_node_id is None:
        raise RuntimeError(f"Ray task ran at undeclared node IP {node_ip}")
    expected_interfaces = interfaces_by_node[expected_node_id]
    observed_topology_sha256 = os.environ.get("TOPOLOGY_SHA256", "")
    observed_node_id = os.environ.get("LOCAL_NODE_ID", "")
    socket_interfaces = os.environ.get("NCCL_SOCKET_IFNAME", "")
    gloo_interfaces = os.environ.get("GLOO_SOCKET_IFNAME", "")
    observed_no_iface_match = os.environ.get("NO_IFACE_MATCH", "0")
    if observed_no_iface_match != no_iface_match:
        raise RuntimeError(
            f"node {node_ip} NO_IFACE_MATCH mismatch: "
            f"{observed_no_iface_match!r} != {no_iface_match!r}"
        )
    if observed_topology_sha256 != topology_sha256:
        raise RuntimeError(
            f"node {node_ip} topology digest mismatch: "
            f"{observed_topology_sha256!r} != {topology_sha256!r}"
        )
    if observed_node_id != expected_node_id:
        raise RuntimeError(
            f"node {node_ip} ID mismatch: {observed_node_id!r} != "
            f"{expected_node_id!r}"
        )
    if socket_interfaces != expected_interfaces or gloo_interfaces != expected_interfaces:
        raise RuntimeError(
            f"node {node_ip} interface mismatch: expected {expected_interfaces!r}, "
            f"NCCL={socket_interfaces!r}, Gloo={gloo_interfaces!r}"
        )

    gpu_query = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,power.limit,compute_cap,"
            "clocks.max.graphics,clocks.applications.graphics,"
            "clocks.default_applications.graphics",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip().splitlines()
    if len(gpu_query) != 1:
        raise RuntimeError(
            f"expected one GPU, nvidia-smi returned {len(gpu_query)}"
        )
    (
        gpu_name,
        driver,
        power_limit,
        compute_capability,
        max_graphics_clock,
        application_graphics_clock,
        default_application_graphics_clock,
    ) = (part.strip() for part in gpu_query[0].split(","))
    installed_packages = sorted(
        line.strip()
        for line in subprocess.run(
            [sys.executable, "-m", "pip", "freeze", "--all"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
        if line.strip()
    )
    runtime_environment: dict[str, str] = {
        name: os.environ.get(name, "") for name in RUNTIME_ENV_NAMES
    }
    runtime_environment["NO_IFACE_MATCH"] = observed_no_iface_match
    shared_runtime_environment: dict[str, str] = {
        name: runtime_environment[name] for name in SHARED_RUNTIME_ENV_NAMES
    }
    return {
        "node_id": observed_node_id,
        "node_ip": node_ip,
        "topology_sha256": observed_topology_sha256,
        "expected_interfaces": expected_interfaces,
        "runtime_env": runtime_environment,
        "shared_runtime_env": shared_runtime_environment,
        "declared_image": os.environ.get("BENCH_VLLM_IMAGE", ""),
        "declared_ray": os.environ.get("BENCH_RAY_VERSION", ""),
        "python": platform.python_version(),
        "ray": package_version("ray"),
        "vllm": package_version("vllm"),
        "torch": package_version("torch"),
        "cuda_runtime": str(torch.version.cuda),
        "pip_freeze": installed_packages,
        "pip_freeze_sha256": hashlib.sha256(
            "\n".join(installed_packages).encode()
        ).hexdigest(),
        "gpu_name": gpu_name,
        "compute_capability": compute_capability,
        "driver": driver,
        "power_limit_w": power_limit,
        "max_graphics_clock_mhz": max_graphics_clock,
        "application_graphics_clock_mhz": application_graphics_clock,
        "default_application_graphics_clock_mhz": default_application_graphics_clock,
    }


expected_ips = [head_ip, worker_ip]
active_gpu_ips = sorted(
    node["NodeManagerAddress"]
    for node in ray.nodes()
    if node.get("Alive") and float(node.get("Resources", {}).get("GPU", 0)) > 0
)
if active_gpu_ips != sorted(expected_ips):
    raise SystemExit(
        f"expected GPU Ray nodes {sorted(expected_ips)}, found {active_gpu_ips}"
    )

references = [
    facts.options(resources={f"node:{ip}": 0.001}).remote(
        expected_nodes_by_ip,
        expected_interfaces_by_node,
        expected_topology_sha256,
        expected_no_iface_match,
    )
    for ip in expected_ips
]
try:
    rows = sorted(ray.get(references, timeout=90), key=lambda row: row["node_ip"])
except ray.exceptions.GetTimeoutError:
    for reference in references:
        ray.cancel(reference, force=True)
    raise SystemExit("timed out after 90s collecting pinned per-node runtime facts")

for row in rows:
    if row["declared_image"] != expected_image:
        raise SystemExit(
            f"node {row['node_ip']} image mismatch: "
            f"{row['declared_image']!r} != {expected_image!r}"
        )
    if row["declared_ray"] != expected_ray or row["ray"] != expected_ray:
        raise SystemExit(
            f"node {row['node_ip']} Ray mismatch: declared={row['declared_ray']!r}, "
            f"installed={row['ray']!r}, expected={expected_ray!r}"
        )

shared_node_facts = (
    "declared_image",
    "python",
    "ray",
    "vllm",
    "torch",
    "cuda_runtime",
    "pip_freeze_sha256",
    "shared_runtime_env",
    "gpu_name",
    "compute_capability",
    "driver",
    "power_limit_w",
    "max_graphics_clock_mhz",
    "application_graphics_clock_mhz",
    "default_application_graphics_clock_mhz",
)
for key in shared_node_facts:
    values = {
        json.dumps(row[key], sort_keys=True, separators=(",", ":")) for row in rows
    }
    if len(values) != 1:
        raise SystemExit(f"cluster runtime mismatch for {key}: {sorted(values)}")

print(json.dumps(rows, sort_keys=True, separators=(",", ":")))
