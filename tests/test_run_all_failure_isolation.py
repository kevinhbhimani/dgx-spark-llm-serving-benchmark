from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class SweepFailureIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.out_dir = self.root / "campaign"
        self.python_log = self.root / "python.log"
        self.topology_path = self.root / "cluster.yaml"
        self._write_topology(head_node="spark_a")
        self._command("hostname", '[[ "${1:-}" == "-s" ]] || exit 2; echo "$FAKE_HOSTNAME"\n')
        self._command(
            "ip",
            "case \"$*\" in\n"
            "  '-4 -o addr show dev fake0') if [[ \"$FAKE_HOSTNAME\" == test-head ]]; then printf '2: fake0 inet 10.10.10.1/24\\n'; else exit 1; fi;;\n"
            "  '-4 -o addr show dev worker0') if [[ \"$FAKE_HOSTNAME\" == test-worker ]]; then printf '3: worker0 inet 10.10.10.2/24\\n'; else exit 1; fi;;\n"
            "  '-o link show dev fake0') echo '2: fake0: <UP,LOWER_UP> state UP';;\n"
            "  '-o link show dev worker0') echo '3: worker0: <UP,LOWER_UP> state UP';;\n"
            "  '-4 route get 10.10.10.2 from 10.10.10.1') echo '10.10.10.2 dev fake0 src 10.10.10.1';;\n"
            "  '-4 route get 10.10.10.1 from 10.10.10.2') echo '10.10.10.1 dev worker0 src 10.10.10.2';;\n"
            "  *) exit 2;;\n"
            "esac\n",
        )
        self._command("ping", "exit 0\n")
        self._command(
            "curl",
            "if [[ \"${FAKE_SERVER_DOWN:-0}\" == 1 ]]; then exit 7; fi\n"
            "if [[ \"${FAKE_MODEL_MISMATCH:-0}\" == 1 ]]; then\n"
            "  printf '{\"data\":[{\"id\":\"org/other\"}]}\\n'\n"
            "else\n"
            "  printf '{\"data\":[{\"id\":\"org/model\"}]}\\n'\n"
            "fi\n",
        )
        self._command("sleep", "exit 0\n")
        self._command(
            "fake-python",
            "if [[ \"$1\" == '-c' ]]; then exec /usr/bin/python3 \"$@\"; fi\n"
            "if [[ \"$1\" != '-m' || \"$2\" != 'bench.run_benchmark' ]]; then exit 2; fi\n"
            "shift 2\n"
            "concurrency=''\n"
            "while (($#)); do\n"
            "  if [[ \"$1\" == '--concurrency' ]]; then concurrency=\"$2\"; shift; fi\n"
            "  shift\n"
            "done\n"
            "printf '%s\\n' \"$concurrency\" >> \"$FAKE_PYTHON_LOG\"\n"
            "[[ \"$concurrency\" != 2 ]] || exit 19\n"
            "exit 0\n",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_topology(self, *, head_node: str) -> None:
        self.topology_path.write_text(yaml.safe_dump({
            "schema_version": 1,
            "head_node": head_node,
            "rendezvous_rail": "rail_0",
            "ray_port": 6379,
            "nodes": [
                {
                    "id": "spark_a",
                    "hostname": "test-head",
                    "ssh_target": "test-head",
                    "interfaces": [{
                        "rail": "rail_0", "name": "fake0",
                        "ipv4": "10.10.10.1/24",
                    }],
                },
                {
                    "id": "spark_b",
                    "hostname": "test-worker",
                    "ssh_target": "test-worker",
                    "interfaces": [{
                        "rail": "rail_0", "name": "worker0",
                        "ipv4": "10.10.10.2/24",
                    }],
                },
            ],
        }))

    def _command(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
        path.chmod(0o755)

    def _run_sweep(
        self,
        *,
        server_down: bool = False,
        model_mismatch: bool = False,
        worker_selected_as_head: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin}:{env['PATH']}",
            "OUT_DIR": str(self.out_dir),
            "CAMPAIGN_LOCK_FILE": str(self.root / "campaign.lock"),
            "CONCURRENCIES": "1 2 3",
            "EXPECTED_CONCURRENCIES": "1 2 3",
            "REPEATS": "1",
            "PYTHON": str(self.bin / "fake-python"),
            "TOPOLOGY_PYTHON": "/usr/bin/python3",
            "CLUSTER_CONFIG": str(self.topology_path),
            "NCCL_IB_DISABLE": "1",
            "FAKE_HOSTNAME": "test-head",
            "MODEL": "org/model",
            "FAKE_PYTHON_LOG": str(self.python_log),
            "FAKE_SERVER_DOWN": "1" if server_down else "0",
            "FAKE_MODEL_MISMATCH": "1" if model_mismatch else "0",
            "ENV_FILE": str(self.root / "missing.env"),
        })
        command = [str(ROOT / "scripts/run_all.sh"), "single"]
        if worker_selected_as_head:
            self._write_topology(head_node="spark_b")
            env["FAKE_HOSTNAME"] = "test-worker"
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_failed_point_is_logged_and_later_points_continue(self) -> None:
        result = self._run_sweep()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.python_log.read_text().splitlines(), ["1", "2", "3"])
        self.assertIn("point FAILED (rc=19)", result.stderr)
        self.assertIn("1 independent point(s) failed", result.stderr)
        self.assertIn("results are partial", result.stderr)

    def test_failed_point_stops_when_shared_server_is_unhealthy(self) -> None:
        result = self._run_sweep(server_down=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.python_log.read_text().splitlines(), ["1", "2"])
        self.assertIn("shared serving state is unhealthy", result.stderr)

    def test_failed_point_stops_when_expected_model_is_not_served(self) -> None:
        result = self._run_sweep(model_mismatch=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.python_log.read_text().splitlines(), ["1", "2"])
        self.assertIn("expected model org/model is not served", result.stderr)

    def test_yaml_selected_worker_can_run_as_head(self) -> None:
        result = self._run_sweep(worker_selected_as_head=True)

        self.assertNotIn("single-node runs reject", result.stderr)
        self.assertEqual(self.python_log.read_text().splitlines(), ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
