from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UnprivilegedExecutionTests(unittest.TestCase):
    def test_obsolete_flush_refuses_without_running_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {"PATH": directory}
            for configuration in ("single", "tp2", "pp2"):
                result = subprocess.run(
                    ["/bin/bash", str(ROOT / "scripts/flush_page_cache.sh"), configuration],
                    env=environment, capture_output=True, text=True, check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("no longer supported", result.stderr)

    def test_obsolete_option_fails_before_hardware_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command_log = Path(directory) / "commands.log"
            for name in ("sudo", "docker", "ssh", "hostname"):
                command = Path(directory) / name
                command.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "$0" >> "$COMMAND_LOG"\nexit 99\n',
                    encoding="utf-8",
                )
                command.chmod(0o755)
            environment_file = Path(directory) / "settings.env"
            environment_file.write_text("DROP_CACHES=1\n", encoding="utf-8")
            for from_environment_file in (False, True):
                environment = {
                    "PATH": directory + os.pathsep + os.defpath,
                    "COMMAND_LOG": str(command_log),
                    "ENV_FILE": str(environment_file if from_environment_file
                                    else Path(directory) / "missing.env"),
                    "SINGLE_NODE_ONLY": "1",
                }
                if not from_environment_file:
                    environment["DROP_CACHES"] = "1"
                for entrypoint in ("start_ray.sh", "launch_server.sh", "run_models.sh",
                                   "run_job.sh"):
                    with self.subTest(entrypoint=entrypoint,
                                      from_environment_file=from_environment_file):
                        result = subprocess.run(
                            ["/bin/bash", str(ROOT / "scripts" / entrypoint)],
                            env=environment, capture_output=True, text=True,
                            check=False, timeout=10,
                        )
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn("DROP_CACHES is unsupported", result.stderr)
                        self.assertFalse(command_log.exists())

    def test_default_and_disabled_option_allow_unprivileged_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for option in (None, "0", ""):
                environment = {
                    "PATH": os.defpath,
                    "HOME": directory,
                    "ENV_FILE": str(Path(directory) / "missing.env"),
                    "SINGLE_NODE_ONLY": "1",
                }
                if option is not None:
                    environment["DROP_CACHES"] = option
                with self.subTest(option=option):
                    result = subprocess.run(
                        ["/bin/bash", "-eu", "-c", 'source "$1"', "bash",
                         str(ROOT / "scripts/connectx_env.sh")],
                        env=environment, capture_output=True, text=True,
                        check=False, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_scripts_do_not_invoke_privilege_escalation(self) -> None:
        for script in (ROOT / "scripts").glob("*.sh"):
            with self.subTest(script=script.name):
                contents = script.read_text(encoding="utf-8")
                self.assertNotRegex(contents, r"\bsudo\b|/proc/sys/vm/drop_caches")
        self.assertNotIn("flush_page_cache.sh", (ROOT / "scripts/run_models.sh").read_text())


if __name__ == "__main__":
    unittest.main()
