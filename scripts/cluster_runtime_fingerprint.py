#!/usr/bin/env python3
"""Emit canonical two-node runtime facts from inside the Ray head container.

The caller hashes the one-line JSON into the serving fingerprint. Any mismatch
between Diya and Hoedus is a hard preflight failure before a model is loaded.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys

import ray


if len(sys.argv) != 5:
    raise SystemExit(
        "usage: cluster_runtime_fingerprint.py HEAD_IP WORKER_IP IMAGE RAY_VERSION"
    )

head_ip, worker_ip, expected_image, expected_ray = sys.argv[1:5]
ray.init(address="auto", log_to_driver=False)


@ray.remote(num_cpus=0, num_gpus=0)
def facts():
    import torch
    import ray as node_ray

    def version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "<missing>"

    smi = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,power.limit,compute_cap,"
            "clocks.max.graphics,clocks.applications.graphics,"
            "clocks.default_applications.graphics",
            "--format=csv,noheader,nounits",
        ],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip().splitlines()
    if len(smi) != 1:
        raise RuntimeError(f"expected one GPU, nvidia-smi returned {len(smi)}")
    (
        gpu_name, driver, power_limit, compute_capability,
        max_graphics_clock, application_graphics_clock,
        default_application_graphics_clock,
    ) = (part.strip() for part in smi[0].split(","))

    freeze = sorted(
        line.strip()
        for line in subprocess.run(
            [sys.executable, "-m", "pip", "freeze", "--all"],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.splitlines()
        if line.strip()
    )
    runtime_env_names = (
        "NCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME", "NCCL_IB_DISABLE",
        "NCCL_DEBUG", "NCCL_DEBUG_SUBSYS", "NCCL_IB_HCA", "NCCL_NET_PLUGIN",
        "NCCL_IB_GID_INDEX", "NCCL_NSOCKS_PERTHREAD", "NCCL_SOCKET_NTHREADS",
        "TORCHINDUCTOR_AUTOGRAD_CACHE", "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
    )
    return {
        "node_ip": node_ray.util.get_node_ip_address(),
        "declared_image": os.environ.get("BENCH_VLLM_IMAGE", ""),
        "declared_ray": os.environ.get("BENCH_RAY_VERSION", ""),
        "runtime_env": {
            name: os.environ.get(name, "") for name in runtime_env_names
        },
        "python": platform.python_version(),
        "ray": version("ray"),
        "vllm": version("vllm"),
        "torch": version("torch"),
        "cuda_runtime": str(torch.version.cuda),
        "pip_freeze": freeze,
        "pip_freeze_sha256": hashlib.sha256(
            "\n".join(freeze).encode()
        ).hexdigest(),
        "gpu_name": gpu_name,
        "compute_capability": compute_capability,
        "driver": driver,
        "power_limit_w": power_limit,
        "max_graphics_clock_mhz": max_graphics_clock,
        "application_graphics_clock_mhz": application_graphics_clock,
        "default_application_graphics_clock_mhz":
            default_application_graphics_clock,
    }


expected_ips = [head_ip, worker_ip]
active_gpu_ips = sorted(
    node["NodeManagerAddress"] for node in ray.nodes()
    if node.get("Alive") and float(node.get("Resources", {}).get("GPU", 0)) > 0
)
if active_gpu_ips != sorted(expected_ips):
    raise SystemExit(
        f"expected GPU Ray nodes {sorted(expected_ips)}, found {active_gpu_ips}"
    )

refs = [
    facts.options(resources={f"node:{ip}": 0.001}).remote()
    for ip in expected_ips
]
try:
    rows = sorted(
        ray.get(refs, timeout=90), key=lambda row: row["node_ip"]
    )
except ray.exceptions.GetTimeoutError:
    for ref in refs:
        ray.cancel(ref, force=True)
    raise SystemExit(
        "timed out after 90s collecting pinned per-node runtime facts"
    )
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

same_on_both = (
    "declared_image", "python", "ray", "vllm", "torch", "cuda_runtime",
    "pip_freeze_sha256", "runtime_env", "gpu_name", "compute_capability",
    "driver", "power_limit_w", "max_graphics_clock_mhz",
    "application_graphics_clock_mhz",
    "default_application_graphics_clock_mhz",
)
for key in same_on_both:
    values = {
        json.dumps(row[key], sort_keys=True, separators=(",", ":"))
        for row in rows
    }
    if len(values) != 1:
        raise SystemExit(f"cluster runtime mismatch for {key}: {sorted(values)}")

print(json.dumps(rows, sort_keys=True, separators=(",", ":")))
