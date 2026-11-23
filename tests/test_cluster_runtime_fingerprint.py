from __future__ import annotations

from collections.abc import Callable
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
from typing import TypedDict
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RuntimeCollector = Callable[
    [dict[str, str], dict[str, str], str, str], dict[str, object]
]


class RuntimeModes(TypedDict):
    runtime_env: dict[str, str]
    shared_runtime_env: dict[str, str]


class ClusterRuntimeFingerprintTests(unittest.TestCase):
    def collect_runtime(
        self: ClusterRuntimeFingerprintTests,
        expected_mode: str,
        container_modes: tuple[str | None, str | None],
    ) -> str:
        node_addresses = ("192.0.2.1", "192.0.2.2")
        node_identifiers = ("head", "worker")
        node_interfaces = ("cx0,cx1", "mlx0,mlx1")
        node_settings = iter(zip(
            node_addresses, node_identifiers, node_interfaces, container_modes,
        ))
        ray = mock.Mock()
        ray.nodes.return_value = [
            {"NodeManagerAddress": address, "Alive": True, "Resources": {"GPU": 1}}
            for address in node_addresses
        ]
        ray.get.side_effect = lambda references, timeout: references
        ray.exceptions.GetTimeoutError = TimeoutError

        def select_node() -> str:
            address, identifier, interfaces, mode = next(node_settings)
            os.environ.update({
                "TOPOLOGY_SHA256": "test-topology", "LOCAL_NODE_ID": identifier,
                "NCCL_SOCKET_IFNAME": interfaces, "GLOO_SOCKET_IFNAME": interfaces,
                "BENCH_VLLM_IMAGE": "test-image", "BENCH_RAY_VERSION": "test-version",
            })
            os.environ.pop("NO_IFACE_MATCH", None)
            if mode is not None:
                os.environ["NO_IFACE_MATCH"] = mode
            return address

        def decorate(collector: RuntimeCollector) -> mock.Mock:
            remote_collector = mock.Mock()
            remote_collector.options.return_value = remote_collector
            remote_collector.remote.side_effect = collector
            return remote_collector

        ray.util.get_node_ip_address.side_effect = select_node
        ray.remote.return_value = decorate
        arguments = [
            "cluster_runtime_fingerprint.py", *node_addresses,
            "test-image", "test-version", "test-topology",
            *node_identifiers, *node_interfaces, expected_mode,
        ]
        output = io.StringIO()
        synthetic_query = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="GB10,test-driver,100,12.1,2000,N/A,N/A\n",
        )
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.dict(sys.modules, {
                    "ray": ray,
                    "torch": SimpleNamespace(version=SimpleNamespace(cuda="test-cuda")),
                }), \
                mock.patch("sys.argv", arguments), \
                mock.patch("sys.stdout", output), \
                mock.patch("subprocess.run", return_value=synthetic_query), \
                mock.patch("importlib.metadata.version", return_value="test-version"):
            runpy.run_path(str(ROOT / "scripts" / "cluster_runtime_fingerprint.py"))
        return output.getvalue()

    def test_validation_mode_is_recorded_on_both_nodes(
        self: ClusterRuntimeFingerprintTests,
    ) -> None:
        for mode in ("0", "1"):
            with self.subTest(mode=mode):
                rows: list[RuntimeModes] = json.loads(
                    self.collect_runtime(mode, (mode, mode))
                )
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertEqual(row["runtime_env"]["NO_IFACE_MATCH"], mode)
                    self.assertEqual(row["shared_runtime_env"]["NO_IFACE_MATCH"], mode)

    def test_different_node_validation_modes_are_rejected(
        self: ClusterRuntimeFingerprintTests,
    ) -> None:
        for expected_mode in ("0", "1"):
            with self.subTest(expected_mode=expected_mode):
                with self.assertRaisesRegex(RuntimeError, "NO_IFACE_MATCH mismatch"):
                    self.collect_runtime(expected_mode, ("1", "0"))

    def test_existing_containers_without_the_flag_use_strict_mode(
        self: ClusterRuntimeFingerprintTests,
    ) -> None:
        rows: list[RuntimeModes] = json.loads(self.collect_runtime("0", (None, None)))
        for row in rows:
            self.assertEqual(row["runtime_env"]["NO_IFACE_MATCH"], "0")
        with self.assertRaisesRegex(RuntimeError, "NO_IFACE_MATCH mismatch"):
            self.collect_runtime("1", (None, None))


if __name__ == "__main__":
    unittest.main()
