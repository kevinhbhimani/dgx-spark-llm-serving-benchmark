"""Regression coverage for scripts/node_postmortem.sh."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FULL_BOOT = "f" * 32
FIRST_ATTEMPT_BOOT = "a" * 32
LAST_ATTEMPT_BOOT = "b" * 32
CURRENT_BOOT = "c" * 32
SUDO_CALLER_ID = "1234"

# Hoedus on 2026-10-27: a boot that ran for hours, then power-on attempts whose
# journals stop seconds after the flush. FAKE_PREVIOUS_BOOT_SPAN sets how many
# seconds boot -1 spans.
FAKE_JOURNALCTL = f"""
boot=""
previous=""
for argument in "$@"; do
  [[ "$previous" == "-b" ]] && boot="$argument"
  previous="$argument"
done
case " $* " in
  *" --list-boots "*)
    printf 'IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY\\n'
    printf ' -3 {FULL_BOOT} Mon 2026-10-26 15:29:41 EDT Mon 2026-10-26 17:26:12 EDT\\n'
    printf ' -2 {FIRST_ATTEMPT_BOOT} Mon 2026-10-26 17:27:36 EDT Mon 2026-10-26 17:27:36 EDT\\n'
    printf ' -1 {LAST_ATTEMPT_BOOT} Tue 2026-10-27 09:51:40 EDT Tue 2026-10-27 09:51:46 EDT\\n'
    printf '  0 {CURRENT_BOOT} Tue 2026-10-27 09:52:17 EDT Tue 2026-10-27 10:00:06 EDT\\n'
    exit 0 ;;
  *" short-unix "*)
    case "$boot" in
      {FULL_BOOT}) first=1761506981; last=1761513972 ;;
      {FIRST_ATTEMPT_BOOT}) first=1761514056; last=1761514056 ;;
      {LAST_ATTEMPT_BOOT}) first=1761573100; last=$((first + FAKE_PREVIOUS_BOOT_SPAN)) ;;
      *) exit 0 ;;
    esac
    if [[ " $* " == *" -n 1 "* ]]; then
      printf '%s.000000 fakehost kernel: last entry\\n' "$last"
    else
      printf '%s.000000 fakehost kernel: first entry\\n' "$first"
      printf '%s.000000 fakehost kernel: last entry\\n' "$last"
    fi
    exit 0 ;;
esac
printf 'kernel: thermal shutdown marker boot=%s\\n' "$boot"
"""


class NodePostmortemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        (self.repository / "scripts").mkdir(parents=True)
        self.script = self.repository / "scripts/node_postmortem.sh"
        shutil.copy2(ROOT / "scripts/node_postmortem.sh", self.script)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.chown_log = self.root / "chown.log"
        self._command("id", '[[ "${1:-}" == "-u" ]] && { echo "$FAKE_UID"; exit 0; }\n'
                            'exec /usr/bin/id "$@"\n')
        self._command("hostname", "echo fakehost\n")
        self._command("journalctl", FAKE_JOURNALCTL)
        self._command("chown", 'printf "%s\\n" "$*" >> "$CHOWN_LOG"\n')
        for name in ("last", "nvidia-smi", "docker", "sensors"):
            self._command(name, f'echo "fake {name} $*"\n')

    def _command(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
        path.chmod(0o755)

    def _postmortem(
        self, *, user_id: str, previous_boot_span_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{self.bin}:{environment['PATH']}",
            "FAKE_UID": user_id,
            "FAKE_PREVIOUS_BOOT_SPAN": str(previous_boot_span_seconds),
            "SUDO_UID": SUDO_CALLER_ID,
            "SUDO_GID": SUDO_CALLER_ID,
            "CHOWN_LOG": str(self.chown_log),
        })
        # Run outside the repository: the output location must not follow cwd.
        return subprocess.run(
            ["bash", str(self.script)], cwd=self.root, env=environment,
            text=True, capture_output=True, timeout=60, check=False,
        )

    def _evidence_directory(self) -> Path:
        directories = sorted(
            (self.repository / "results").glob("node-postmortem-fakehost-*")
        )
        self.assertEqual(len(directories), 1, directories)
        return directories[0]

    def test_refuses_to_run_without_root(self) -> None:
        result = self._postmortem(user_id="1000", previous_boot_span_seconds=3)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("sudo", result.stderr)
        self.assertFalse((self.repository / "results").exists())

    def test_captures_the_last_full_boot_behind_failed_power_on_attempts(self) -> None:
        result = self._postmortem(user_id="0", previous_boot_span_seconds=6)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "results").exists())
        evidence = self._evidence_directory()
        self.assertIn("boot=-1", (evidence / "prev_boot_tail.txt").read_text())
        for name in ("last_full_boot_tail.txt", "last_full_boot_kernel.txt",
                     "last_full_boot_shutdown_actors.txt"):
            self.assertIn(f"boot={FULL_BOOT}", (evidence / name).read_text())
        self.assertIn(f"boot={FIRST_ATTEMPT_BOOT}",
                      (evidence / "after_full_boot_kernel.txt").read_text())
        self.assertIn(FULL_BOOT, (evidence / "SUMMARY.txt").read_text())
        self.assertIn(f"{SUDO_CALLER_ID}:{SUDO_CALLER_ID} {evidence}",
                      self.chown_log.read_text())

    def test_full_previous_boot_needs_no_extra_capture(self) -> None:
        result = self._postmortem(user_id="0", previous_boot_span_seconds=7200)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = self._evidence_directory()
        self.assertTrue((evidence / "prev_boot_tail.txt").exists())
        self.assertFalse((evidence / "last_full_boot_tail.txt").exists())
        self.assertFalse((evidence / "after_full_boot_kernel.txt").exists())


if __name__ == "__main__":
    unittest.main()
