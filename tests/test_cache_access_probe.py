from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cache_access_probe  # noqa: E402


class CacheAccessProbeTests(unittest.TestCase):
    def test_probe_creates_reads_and_removes_only_temporary_files(
        self: CacheAccessProbeTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "hub"

            cache_access_probe.probe_cache(str(cache))

            self.assertEqual(list(cache.iterdir()), [])

    def test_probe_error_identifies_path_and_effective_identity(
        self: CacheAccessProbeTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(OSError, "effective UID:GID"):
                    cache_access_probe.probe_cache(temporary)


if __name__ == "__main__":
    unittest.main()
