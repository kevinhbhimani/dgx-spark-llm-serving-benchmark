from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from offline import source_release


class SourceReleaseTests(unittest.TestCase):
    def setUp(self: SourceReleaseTests) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="source release tests ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in source_release.DEPENDENCY_PATHS:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic dependency input\n", encoding="utf-8")
        (self.source / "scripts/check_environment.py").write_text(
            'from pathlib import Path\n'
            'assert Path("requirements.lock").read_text() == "synthetic dependency input\\n"\n',
            encoding="utf-8",
        )
        (self.source / "configs").mkdir()
        (self.source / "configs/experiment.yaml").write_text("synthetic: baseline\n", encoding="utf-8")
        (self.source / "obsolete.py").write_text("synthetic = True\n", encoding="utf-8")
        self.bundle = self.root / "original bundle"
        self.bundle.mkdir()
        source_release.source_snapshot(self.source, self.bundle / source_release.SOURCE_ARCHIVE)
        (self.bundle / "BUNDLE_INFO").write_text("bundle_format=2\n", encoding="utf-8")
        (self.bundle / "vllm-ray-image.tar.gz").write_bytes(b"synthetic image fixture")
        (self.bundle / "wheels").mkdir()
        (self.bundle / "wheels/client.whl").write_bytes(b"synthetic wheel fixture")
        (self.bundle / "SHA256SUMS").write_text(
            "".join(
                f"{source_release.sha256_file(path)}  {path.relative_to(self.bundle).as_posix()}\n"
                for path in sorted(self.bundle.rglob("*")) if path.is_file()
            ), encoding="utf-8",
        )
        self.baseline = source_release.bundle_release(self.bundle)
        self.installed = self.root / "installed"
        self.installed.mkdir()
        source_release.extract_source(self.bundle / source_release.SOURCE_ARCHIVE, self.installed)
        source_release.write_json(self.installed / source_release.RECEIPT_FILENAME, self.baseline.serialize())
        (self.installed / ".venv/bin").mkdir(parents=True)
        (self.installed / ".venv/bin/python").symlink_to(sys.executable)
        (self.installed / ".env").write_text("LOCAL_SETTING=preserve\n", encoding="utf-8")
        (self.installed / "results").mkdir()
        (self.installed / "results/raw.txt").write_text("synthetic retained result\n", encoding="utf-8")
        self.patch = self.root / "source patch"

    def build_patch(self: SourceReleaseTests) -> source_release.Release:
        (self.source / "configs/experiment.yaml").write_text("synthetic: updated\n", encoding="utf-8")
        (self.source / "obsolete.py").unlink()
        (self.source / "new_script.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.source / "new_script.sh").chmod(0o755)
        (self.source / "config_link").symlink_to("configs/experiment.yaml")
        source_release.build_patch(self.source, self.bundle, self.patch)
        return source_release.Release.read(self.patch / source_release.RELEASE_FILENAME)

    def test_check_apply_and_rollback_preserve_local_state(self: SourceReleaseTests) -> None:
        target = self.build_patch()
        receipt = self.installed / source_release.RECEIPT_FILENAME
        before = receipt.read_bytes()
        source_release.apply_update(self.installed, self.patch, True)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertTrue((self.installed / "obsolete.py").exists())
        self.assertFalse((self.installed / source_release.HISTORY_DIRECTORY).exists())
        source_release.apply_update(self.installed, self.patch, False)
        source_release.verify_tree(self.installed, target)
        self.assertFalse((self.installed / "obsolete.py").exists())
        self.assertEqual((self.installed / "new_script.sh").stat().st_mode & 0o777, 0o755)
        self.assertTrue((self.installed / "config_link").is_symlink())
        self.assertEqual((self.installed / ".env").read_text(), "LOCAL_SETTING=preserve\n")
        self.assertEqual((self.installed / "results/raw.txt").read_text(), "synthetic retained result\n")
        result = subprocess.run(
            [sys.executable, str(self.patch / "source_release.py"), "rollback", "--source", str(self.installed)],
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source_release.verify_tree(self.installed, self.baseline)
        self.assertFalse((self.installed / "new_script.sh").exists())

    def test_dependencies_require_a_runtime_rebuild(self: SourceReleaseTests) -> None:
        for name in source_release.DEPENDENCY_PATHS:
            with self.subTest(path=name):
                path = self.source / name
                original = path.read_bytes()
                path.write_bytes(original + b"changed\n")
                with self.assertRaisesRegex(ValueError, "runtime rebuild required"):
                    source_release.build_patch(self.source, self.bundle, self.patch)
                self.assertFalse(self.patch.exists())
                path.write_bytes(original)

    def test_dependency_symlink_target_changes_require_a_rebuild(self: SourceReleaseTests) -> None:
        lock = self.source / "requirements.lock"
        lock.rename(self.source / "client-dependencies.lock")
        lock.symlink_to("client-dependencies.lock")
        archive = self.bundle / source_release.SOURCE_ARCHIVE
        archive.unlink()
        source_release.source_snapshot(self.source, archive)
        checksums = self.bundle / "SHA256SUMS"
        checksums.write_text(
            "".join(f"{source_release.sha256_file(path)}  {path.relative_to(self.bundle).as_posix()}\n"
                    for path in sorted(self.bundle.rglob("*")) if path.is_file() and path != checksums),
            encoding="utf-8",
        )
        (self.source / "client-dependencies.lock").write_text("synthetic changed dependency\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "runtime rebuild required for: requirements.lock"):
            source_release.build_patch(self.source, self.bundle, self.patch)

    def test_drift_and_local_collisions_are_rejected_without_mutation(self: SourceReleaseTests) -> None:
        self.build_patch()
        original = (self.installed / "obsolete.py").read_bytes()
        (self.installed / "obsolete.py").write_text("local edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "drift"):
            source_release.apply_update(self.installed, self.patch, False)
        self.assertEqual((self.installed / "obsolete.py").read_text(), "local edit")
        (self.installed / "obsolete.py").write_bytes(original)
        (self.installed / "new_script.sh").write_text("local file", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected installed source path"):
            source_release.apply_update(self.installed, self.patch, True)
        self.assertEqual((self.installed / "new_script.sh").read_text(), "local file")

    def test_verify_rejects_unexpected_source_files(self: SourceReleaseTests) -> None:
        (self.installed / "unexpected.py").write_text("synthetic = True\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected installed source path"):
            source_release.verify_tree(self.installed, self.baseline)

    def test_wrong_baseline_and_corrupt_payload_are_rejected(self: SourceReleaseTests) -> None:
        target = self.build_patch()
        wrong = source_release.Release(target.files, target.dependencies, target.archive_sha256, "0" * 64)
        source_release.write_json(self.patch / source_release.RELEASE_FILENAME, wrong.serialize())
        with self.assertRaisesRegex(ValueError, "different installed baseline"):
            source_release.apply_update(self.installed, self.patch, False)
        source_release.write_json(self.patch / source_release.RELEASE_FILENAME, target.serialize())
        (self.patch / source_release.SOURCE_ARCHIVE).write_bytes(b"corrupted synthetic payload")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            source_release.apply_update(self.installed, self.patch, False)
        source_release.verify_tree(self.installed, self.baseline)

    def test_environment_failure_does_not_change_installation(self: SourceReleaseTests) -> None:
        self.build_patch()
        with mock.patch.object(source_release, "validate_environment", side_effect=subprocess.CalledProcessError(1, "synthetic check")):
            with self.assertRaises(subprocess.CalledProcessError):
                source_release.apply_update(self.installed, self.patch, False)
        source_release.verify_tree(self.installed, self.baseline)

    def test_receipt_failure_restores_baseline(self: SourceReleaseTests) -> None:
        self.build_patch()
        original_write = source_release.write_json
        failed = False

        def fail_receipt(path: Path, value: object) -> None:
            nonlocal failed
            if path == self.installed / source_release.RECEIPT_FILENAME and not failed:
                failed = True
                raise OSError("synthetic receipt failure")
            original_write(path, value)

        with mock.patch.object(source_release, "write_json", side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, "synthetic receipt failure"):
                source_release.apply_update(self.installed, self.patch, False)
        source_release.verify_tree(self.installed, self.baseline)
        self.assertFalse((self.installed / source_release.TRANSACTION_FILENAME).exists())

    def test_interrupted_update_can_be_recovered(self: SourceReleaseTests) -> None:
        target = self.build_patch()
        source_release.save_history(self.installed, self.baseline)
        source_release.write_json(self.installed / source_release.TRANSACTION_FILENAME, {
            "before": self.baseline.serialize(), "after": target.serialize(),
        })
        (self.installed / "configs/experiment.yaml").write_text("synthetic: updated\n", encoding="utf-8")
        (self.installed / "obsolete.py").unlink()
        with self.assertRaisesRegex(ValueError, "interrupted source update"):
            source_release.apply_update(self.installed, self.patch, False)
        source_release.recover_transaction(self.installed)
        source_release.verify_tree(self.installed, self.baseline)

    def test_successive_patches_use_the_previous_source_release(self: SourceReleaseTests) -> None:
        self.build_patch()
        source_release.apply_update(self.installed, self.patch, False)
        (self.source / "next.py").write_text("synthetic = 2\n", encoding="utf-8")
        next_patch = self.root / "next patch"
        source_release.build_patch(self.source, self.patch, next_patch)
        source_release.apply_update(self.installed, next_patch, False)
        self.assertTrue((self.installed / "next.py").exists())

    def test_file_directory_transition_can_be_rolled_back(self: SourceReleaseTests) -> None:
        (self.source / "obsolete.py").unlink()
        (self.source / "obsolete.py").mkdir()
        (self.source / "obsolete.py/nested.py").write_text("synthetic = 2\n", encoding="utf-8")
        source_release.build_patch(self.source, self.bundle, self.patch)
        source_release.apply_update(self.installed, self.patch, False)
        result = subprocess.run(
            [sys.executable, str(self.patch / "source_release.py"), "rollback", "--source", str(self.installed)],
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source_release.verify_tree(self.installed, self.baseline)

    def test_apply_rechecks_dependency_inputs(self: SourceReleaseTests) -> None:
        self.build_patch()
        (self.source / "requirements.lock").write_text("synthetic changed dependency\n", encoding="utf-8")
        archive = self.patch / source_release.SOURCE_ARCHIVE
        archive.unlink()
        source_release.source_snapshot(self.source, archive)
        with tempfile.TemporaryDirectory() as temporary:
            entries = source_release.extract_source(archive, Path(temporary))
        target = source_release.Release(entries, self.baseline.dependencies, source_release.sha256_file(archive), self.baseline.source_sha256)
        source_release.write_json(self.patch / source_release.RELEASE_FILENAME, target.serialize())
        with self.assertRaisesRegex(ValueError, "dependency inputs differ"):
            source_release.apply_update(self.installed, self.patch, False)
        source_release.verify_tree(self.installed, self.baseline)

    def test_old_bundles_can_register_without_rebuilding(self: SourceReleaseTests) -> None:
        (self.installed / source_release.RECEIPT_FILENAME).unlink()
        with mock.patch.object(sys, "argv", ["source_release.py", "register", "--bundle", str(self.bundle), "--source", str(self.installed)]):
            self.assertEqual(source_release.main(), 0)
        self.assertEqual(source_release.Release.read(self.installed / source_release.RECEIPT_FILENAME), self.baseline)

    def test_archive_paths_and_symlinks_cannot_escape_or_replace_local_state(self: SourceReleaseTests) -> None:
        for name, link in (("../escape", ""), ("/absolute", ""), (".env", ""), (".offline-release.json", ""), ("link", "../escape"), ("link", "/absolute")):
            with self.subTest(name=name, link=link):
                archive_path = self.root / "unsafe.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                        archive.addfile(member)
                    else:
                        member.size = 1
                        archive.addfile(member, io.BytesIO(b"x"))
                with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                    with self.assertRaises(ValueError):
                        source_release.extract_source(archive_path, Path(temporary))
        self.assertFalse((self.root / "escape").exists())

    def test_manifest_digests_are_validated(self: SourceReleaseTests) -> None:
        self.build_patch()
        path = self.patch / source_release.RELEASE_FILENAME
        document = json.loads(path.read_text())
        document["previous_source_sha256"] = "../outside"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid release digest"):
            source_release.Release.read(path)

    def test_hardlinked_files_have_restorable_source_history(self: SourceReleaseTests) -> None:
        root = self.root / "hardlinked source"
        root.mkdir()
        (root / "first.py").write_text("synthetic = True\n", encoding="utf-8")
        os.link(root / "first.py", root / "second.py")
        entries = source_release.inventory(root)
        release = source_release.Release(entries, source_release.dependency_files(entries), "0" * 64)
        history = source_release.save_history(root, release)
        saved = source_release.Release.read(history / source_release.RELEASE_FILENAME)
        with tempfile.TemporaryDirectory() as temporary:
            source_release.verify_payload(history, saved, Path(temporary))
            source_release.verify_tree(Path(temporary), saved)

    def test_git_backed_snapshot_rejects_uncommitted_source(self: SourceReleaseTests) -> None:
        commands = [
            subprocess.CompletedProcess([], 0, str(self.source) + "\n"),
            subprocess.CompletedProcess([], 0, b"obsolete.py\0"),
            subprocess.CompletedProcess([], 0, b"obsolete.py\0"),
            subprocess.CompletedProcess([], 0, b""),
        ]
        with mock.patch.object(source_release.subprocess, "run", side_effect=commands):
            with self.assertRaisesRegex(ValueError, "clean committed packaged files"):
                source_release.source_snapshot(self.source, self.root / "snapshot.tar.gz")


if __name__ == "__main__":
    unittest.main()
