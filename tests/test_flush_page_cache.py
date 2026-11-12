from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FlushPageCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.command_log = self.directory / "commands.log"
        cluster_config = self.directory / "cluster.yaml"
        cluster_config.write_text(
            "schema_version: 1\nhead_node: test_head\nrendezvous_rail: rail_0\n"
            "ray_port: 6379\nnodes:\n"
            "  - id: test_head\n    hostname: cache-test-host\n"
            "    ssh_target: test-head\n    interfaces:\n"
            "      - {rail: rail_0, name: cx0, ipv4: 192.0.2.1/30}\n"
            "  - id: test_worker\n    hostname: cache-test-worker\n"
            "    ssh_target: test-worker\n    interfaces:\n"
            "      - {rail: rail_0, name: cx0, ipv4: 192.0.2.2/30}\n",
            encoding="utf-8",
        )
        commands = {
            "hostname": "printf 'cache-test-host\\n'\n",
            "sync": 'printf "sync\\n" >> "$COMMAND_LOG"\n',
            "sudo": 'printf "sudo %s input=%s\\n" "$*" "$(cat)" >> "$COMMAND_LOG"\n'
                    'exit "${SUDO_STATUS:-0}"\n',
            "ssh": 'printf "ssh %s\\n" "$*" >> "$COMMAND_LOG"\n'
                   'exit "${SSH_STATUS:-0}"\n',
        }
        for name, body in commands.items():
            command = self.directory / name
            command.write_text("#!/bin/sh\n" + body, encoding="utf-8")
            command.chmod(0o755)
        self.environment = {
            "PATH": str(self.directory) + os.pathsep + os.defpath,
            "HOME": str(self.directory),
            "ENV_FILE": str(self.directory / "missing.env"),
            "CLUSTER_CONFIG": str(cluster_config),
            "TOPOLOGY_PYTHON": sys.executable,
            "COMMAND_LOG": str(self.command_log),
        }

    def run_flush(self, config: str) -> subprocess.CompletedProcess[str]:
        self.command_log.write_text("", encoding="utf-8")
        return subprocess.run(
            ["bash", str(ROOT / "scripts/flush_page_cache.sh"), config],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_solo_and_default_cluster_flushes(self) -> None:
        for mode in (None, "0", "1"):
            if mode is None:
                self.environment.pop("SINGLE_NODE_ONLY", None)
            else:
                self.environment["SINGLE_NODE_ONLY"] = mode
            self.environment["CLUSTER_CONFIG"] = str(
                self.directory / ("missing-cluster.yaml" if mode == "1" else "cluster.yaml")
            )
            configs = ("single",) if mode == "1" else ("single", "tp2", "pp2")
            for config in configs:
                with self.subTest(mode=mode, config=config):
                    result = self.run_flush(config)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    node_name = "cache-test-host" if mode == "1" else "test_head"
                    self.assertIn(f"page cache before flush on {node_name}", result.stdout)
                    self.assertIn(f"page cache after flush on {node_name}", result.stdout)
                    calls = self.command_log.read_text(encoding="utf-8")
                    self.assertTrue(calls.startswith(
                        "sync\nsudo -n /usr/bin/tee /proc/sys/vm/drop_caches input=3\n"
                    ), calls)
                    if config == "single":
                        self.assertNotIn("ssh", calls)
                    else:
                        self.assertIn("ssh -T -o BatchMode=yes", calls)
                        self.assertIn("test-worker set -eu", calls)

    def test_local_flush_failure_stops_before_worker(self) -> None:
        self.environment["SUDO_STATUS"] = "1"
        for mode, config, node_name in (
            ("0", "tp2", "test_head"),
            ("1", "single", "cache-test-host"),
        ):
            with self.subTest(mode=mode):
                self.environment["SINGLE_NODE_ONLY"] = mode
                result = self.run_flush(config)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"drop_caches on {node_name}", result.stderr)
                self.assertNotIn("page cache after flush", result.stdout)
                self.assertNotIn("ssh", self.command_log.read_text(encoding="utf-8"))

    def test_worker_flush_failure_is_preserved(self) -> None:
        self.environment["SSH_STATUS"] = "23"
        result = self.run_flush("pp2")
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertIn("page cache after flush on test_head", result.stdout)
        self.assertIn("test-worker set -eu", self.command_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
