from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench.hf_cache import configure_hf_cache


class HubCacheTests(unittest.TestCase):
    def test_hf_cache_root_sets_one_hub_and_modules_path(
        self: HubCacheTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "HF_CACHE": temporary,
                "HF_HOME": "/ignored/home",
                "HF_HUB_CACHE": "/ignored/hub",
                "HF_MODULES_CACHE": "/ignored/modules",
            }

            hub_cache = configure_hf_cache(environment)

            self.assertEqual(environment["HF_HOME"], temporary)
            self.assertEqual(environment["HF_HUB_CACHE"],
                             str(Path(temporary) / "hub"))
            self.assertEqual(environment["HF_MODULES_CACHE"],
                             str(Path(temporary) / "modules"))
            self.assertEqual(hub_cache, Path(temporary) / "hub")

    def test_unwritable_hub_cache_fails_without_selecting_a_fallback(
        self: HubCacheTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {"HF_CACHE": temporary}
            with mock.patch(
                "bench.hf_cache.tempfile.TemporaryDirectory",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "HF cache create/read/delete failed"
                ):
                    configure_hf_cache(environment)

            self.assertEqual(environment["HF_HUB_CACHE"],
                             str(Path(temporary) / "hub"))


if __name__ == "__main__":
    unittest.main()
