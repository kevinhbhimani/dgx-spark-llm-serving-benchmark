"""Regression coverage for scripts/system_snapshot.py."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from scripts import system_snapshot

# journalctl 255 prints this, exits 0, and lists no entries for a caller that
# cannot read the system journal.
UNREADABLE_JOURNAL_STDERR = (
    "Hint: You are currently not seeing messages from other users and the system.\n"
    "      Users in groups 'adm', 'systemd-journal' can see all messages.\n"
    "      Pass -q to turn off this notice."
)


class KernelErrorSnapshotTests(unittest.TestCase):
    def _snapshot(self, stderr: str) -> system_snapshot.JsonObject:
        completed = subprocess.CompletedProcess(
            args=["journalctl"], returncode=0, stdout="-- No entries --\n",
            stderr=stderr,
        )
        with mock.patch.object(system_snapshot.subprocess, "run",
                               return_value=completed):
            return system_snapshot.kernel_error_snapshot()

    def test_unreadable_journal_is_unavailable(self) -> None:
        result = self._snapshot(UNREADABLE_JOURNAL_STDERR)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("root", str(result["error"]))

    def test_readable_journal_without_errors_is_ok(self) -> None:
        result = self._snapshot("")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stdout"], "-- No entries --")


if __name__ == "__main__":
    unittest.main()
