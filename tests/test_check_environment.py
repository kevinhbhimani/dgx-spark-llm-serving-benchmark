import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import check_environment
from scripts.contracts import dependency_lock


AUDITED_LOCK = """\
aiohappyeyeballs==2.7.1
aiohttp==3.14.5
aiosignal==1.4.0
annotated-doc==0.0.5
anyio==4.15.1
attrs==26.1.0
click==8.5.0
contourpy==1.4.0
cycler==0.12.1
filelock==4.2.0
fonttools==4.67.0
frozenlist==1.8.0
fsspec==2026.9.0
h11==0.16.0
hf-xet==1.7.0
httpcore2==2.13.1
httpx2==2.13.1
huggingface_hub==2.2.0
idna==3.20
kiwisolver==1.5.1
markdown-it-py==4.2.0
matplotlib==3.11.2
mdurl==0.1.2
multidict==7.1.0
numpy==2.5.4
packaging==26.3
pandas==3.0.6
pillow==12.3.0
propcache==0.5.4
Pygments==2.21.0
pyparsing==3.3.3
python-dateutil==2.9.0.post0
PyYAML==6.0.3
regex==2026.9.29
rich==15.0.0
safetensors==0.8.0
shellingham==1.5.4
six==1.17.0
tokenizers==0.23.3
tqdm==4.70.1
transformers==5.20.0
truststore==0.10.5
typer==0.27.3
typing_extensions==4.16.0
yarl==1.25.1
"""


class LockFileTests(unittest.TestCase):
    def test_environment_checker_reexports_shared_lock_contract(self):
        self.assertIs(check_environment.LockFileError, dependency_lock.LockFileError)
        self.assertIs(
            check_environment.LockedDistribution, dependency_lock.LockedDistribution
        )
        self.assertIs(
            check_environment.normalize_distribution_name,
            dependency_lock.normalize_distribution_name,
        )
        self.assertIs(check_environment.parse_lock, dependency_lock.parse_lock)

    def test_repository_lock_is_the_exact_audited_closure(self):
        lock = Path(__file__).resolve().parents[1] / "requirements.lock"

        self.assertEqual(lock.read_text(encoding="utf-8"), AUDITED_LOCK)
        pins = check_environment.parse_lock(lock)
        self.assertEqual(len(pins), 45)

    def test_parser_rejects_non_exact_and_duplicate_normalized_pins(self):
        invalid_contents = (
            "example>=1\n",
            "example==1; python_version >= '3.12'\n",
            "example==1 --hash=sha256:abc\n",
            "Foo_Bar==1\nfoo-bar==1\n",
            "# comments alone do not make a lock\n",
        )
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "requirements.lock"
            for content in invalid_contents:
                with self.subTest(content=content):
                    lock.write_text(content, encoding="utf-8")
                    with self.assertRaises(check_environment.LockFileError):
                        check_environment.parse_lock(lock)

    def test_shared_parser_reports_structured_errors_without_changing_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "requirements.lock"
            for content, code, line_number, normalized_name, message in (
                (
                    "example>=1\n", "invalid_pin", 1, None,
                    f"{lock}:1: expected exactly name==version",
                ),
                (
                    "Foo_Bar==1\nfoo-bar==1\n", "duplicate", 2, "foo-bar",
                    f"{lock}:2: duplicate distribution 'foo-bar' "
                    "(first pinned on line 1)",
                ),
                (
                    "# comment\n", "empty", None, None,
                    f"lock file contains no exact pins: {lock}",
                ),
            ):
                with self.subTest(code=code):
                    lock.write_text(content, encoding="utf-8")
                    with self.assertRaises(check_environment.LockFileError) as raised:
                        check_environment.parse_lock(lock)
                    error = raised.exception
                    self.assertEqual(str(error), message)
                    self.assertEqual(error.code, code)
                    self.assertEqual(error.line_number, line_number)
                    self.assertEqual(error.normalized_name, normalized_name)

    def test_direct_script_help_imports_shared_parser(self):
        repository_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "scripts/check_environment.py", "--help"],
            check=False,
            capture_output=True,
            cwd=repository_root,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--lock", completed.stdout)


class RuntimeIsolationTests(unittest.TestCase):
    def test_real_venv_needs_distinct_prefix_and_pyvenv_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary) / "venv"
            prefix.mkdir()
            base = Path(temporary) / "base"
            with mock.patch.object(check_environment.sys, "prefix", str(prefix)), \
                    mock.patch.object(check_environment.sys, "base_prefix", str(base)):
                self.assertFalse(check_environment.is_real_virtual_environment())
                (prefix / "pyvenv.cfg").write_text("home = /python\n")
                self.assertTrue(check_environment.is_real_virtual_environment())
                with mock.patch.object(check_environment.sys, "base_prefix", str(prefix)):
                    self.assertFalse(check_environment.is_real_virtual_environment())

    def test_active_user_site_fails_even_when_enable_flag_is_false(self):
        with tempfile.TemporaryDirectory() as user_site:
            with mock.patch.object(check_environment.site, "ENABLE_USER_SITE", False), \
                    mock.patch.object(
                        check_environment.site,
                        "getusersitepackages",
                        return_value=user_site,
                    ), \
                    mock.patch.object(check_environment.sys, "path", [user_site]):
                issues = check_environment.user_site_issues()

        self.assertEqual(len(issues), 1)
        self.assertIn("active on sys.path", issues[0])

    def test_allow_non_venv_does_not_allow_user_site(self):
        with mock.patch.object(check_environment.site, "ENABLE_USER_SITE", True), \
                mock.patch.object(
                    check_environment.site, "getusersitepackages", return_value=[]
                ), \
                mock.patch.object(check_environment.sys, "path", []):
            issues = check_environment.runtime_issues(allow_non_venv=True)

        self.assertTrue(any("user site is enabled" in issue for issue in issues))
        self.assertFalse(any("virtual environment" in issue for issue in issues))


class DistributionValidationTests(unittest.TestCase):
    def test_every_pin_is_checked_and_missing_or_mismatch_fails(self):
        pins = [
            check_environment.LockedDistribution("one", "1.0"),
            check_environment.LockedDistribution("two", "2.0"),
            check_environment.LockedDistribution("three", "3.0"),
        ]

        def version(name):
            if name == "one":
                return "1.0"
            if name == "two":
                return "2.1"
            raise check_environment.importlib.metadata.PackageNotFoundError(name)

        with mock.patch.object(
            check_environment.importlib.metadata, "version", side_effect=version
        ) as get_version:
            issues, locked = check_environment.verify_locked_distributions(pins)

        self.assertEqual(
            [call.args[0] for call in get_version.call_args_list],
            ["one", "two", "three"],
        )
        self.assertEqual(locked, [{
            "name": "one", "normalized_name": "one", "version": "1.0"
        }])
        self.assertEqual(len(issues), 2)
        self.assertTrue(any("version mismatch" in issue for issue in issues))
        self.assertTrue(any("is missing" in issue for issue in issues))


class PipCheckTests(unittest.TestCase):
    def test_pip_check_uses_fixed_isolated_command_and_sanitized_environment(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="No broken requirements found.\n", stderr=""
        )
        with mock.patch.dict(
            os.environ,
            {"PYTHONHOME": "/host", "PYTHONPATH": "/injected"},
        ), mock.patch.object(
            check_environment.subprocess, "run", return_value=completed
        ) as run:
            result = check_environment.run_pip_check(timeout_seconds=7.0)

        command = [sys.executable, "-I", "-m", "pip", "check"]
        self.assertEqual(run.call_args.args[0], command)
        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 7.0)
        self.assertNotIn("PYTHONHOME", kwargs["env"])
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertEqual(kwargs["env"]["PYTHONNOUSERSITE"], "1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "No broken requirements found.")

    def test_timeout_is_a_structured_failure(self):
        timeout = subprocess.TimeoutExpired(
            cmd=["python", "-m", "pip"], timeout=3, output=b"partial\n"
        )
        with mock.patch.object(
            check_environment.subprocess, "run", side_effect=timeout
        ):
            result = check_environment.run_pip_check(timeout_seconds=3)

        self.assertFalse(result["ok"])
        self.assertIsNone(result["returncode"])
        self.assertEqual(result["stdout"], "partial")
        self.assertIn("timed out after 3 seconds", result["stderr"])
        self.assertIn("did not complete", check_environment.pip_check_issue(result))


class ManifestTests(unittest.TestCase):
    def test_manifest_has_stable_full_distribution_hash(self):
        fake_distributions = [
            SimpleNamespace(metadata={"Name": "z_pkg"}, version="2"),
            SimpleNamespace(metadata={"Name": "A-Pkg"}, version="1"),
            SimpleNamespace(metadata={"Name": "a_pkg"}, version="1"),
        ]
        pip_result = {
            "command": ["python", "-m", "pip", "check"],
            "ok": True,
            "returncode": 0,
            "stdout": "No broken requirements found.",
            "stderr": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "requirements.lock"
            lock.write_text("a-pkg==1\n", encoding="utf-8")
            with mock.patch.object(
                check_environment.importlib.metadata,
                "distributions",
                return_value=fake_distributions,
            ):
                manifest = check_environment.build_manifest(
                    lock,
                    [{"name": "a-pkg", "normalized_name": "a-pkg", "version": "1"}],
                    pip_result,
                )

        installed = manifest["installed_distributions"]
        canonical = json.dumps(
            installed, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self.assertEqual(
            manifest["installed_distributions_sha256"],
            hashlib.sha256(canonical).hexdigest(),
        )
        self.assertEqual(len(installed), 3)  # normalized-name duplicates are retained
        self.assertEqual(manifest["locked_distributions"][0]["version"], "1")
        self.assertIn("version", manifest["python"])
        self.assertIn("platform", manifest["platform"])
        self.assertTrue(manifest["executable"])

    def test_atomic_writer_never_replaces_destination_with_partial_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "environment.json"
            destination.write_text('{"old": true}\n', encoding="utf-8")

            with self.assertRaises(TypeError):
                check_environment.atomic_write_json(destination, {"bad": object()})
            self.assertEqual(destination.read_text(), '{"old": true}\n')
            self.assertEqual(list(Path(temporary).glob(".*.tmp")), [])

            check_environment.atomic_write_json(destination, {"valid": True})
            self.assertEqual(json.loads(destination.read_text()), {"valid": True})

    def test_default_validation_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            lock = directory / "requirements.lock"
            lock.write_text("example==1\n", encoding="utf-8")
            pip_result = {
                "command": ["python", "-m", "pip", "check"],
                "ok": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            }
            locked = [{
                "name": "example", "normalized_name": "example", "version": "1"
            }]
            with mock.patch.object(
                check_environment, "runtime_issues", return_value=[]
            ), mock.patch.object(
                check_environment,
                "verify_locked_distributions",
                return_value=([], locked),
            ), mock.patch.object(
                check_environment, "run_pip_check", return_value=pip_result
            ):
                exit_code = check_environment.main(["--lock", str(lock)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(list(directory.iterdir()), [lock])


if __name__ == "__main__":
    unittest.main()
