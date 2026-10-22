from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from failure_diagnostics import redact_diagnostic  # noqa: E402
import download_hf_snapshot  # noqa: E402
import hf_snapshot_size  # noqa: E402


class FailureDiagnosticTests(unittest.TestCase):
    def test_redacts_explicit_secrets_headers_and_query_credentials(self) -> None:
        message = (
            "token=secret-query Authorization: Bearer header-secret "
            "literal-secret"
        )

        result = redact_diagnostic(
            message,
            ("literal-secret", "header-secret"),
        )

        self.assertNotIn("secret-query", result)
        self.assertNotIn("header-secret", result)
        self.assertNotIn("literal-secret", result)
        self.assertEqual(result.count("<redacted>"), 3)

    def test_diagnostic_is_single_line_and_bounded(self) -> None:
        result = redact_diagnostic("line one\n" + "x" * 100, limit=20)

        self.assertEqual(len(result), 20)
        self.assertNotIn("\n", result)
        self.assertTrue(result.endswith("…"))

    def test_snapshot_failure_logs_context_without_hf_token(self) -> None:
        token = "hf_test_secret"
        hub = types.ModuleType("huggingface_hub")

        def fail_download(*args, **kwargs):
            raise RuntimeError(f"Authorization: Bearer {token}; failed")

        hub.snapshot_download = fail_download
        output = io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"huggingface_hub": hub}),
            mock.patch.dict(os.environ, {"HF_TOKEN": token,
                                         "HF_HUB_CACHE": "/hf-cache/hub"}),
            contextlib.redirect_stderr(output),
        ):
            status = download_hf_snapshot.main(["org/model", "a" * 40, "4"])

        self.assertEqual(status, 1)
        self.assertNotIn(token, output.getvalue())
        self.assertIn("RuntimeError", output.getvalue())
        self.assertIn("cache=/hf-cache/hub", output.getvalue())
        self.assertIn("check repository access", output.getvalue())

    def test_snapshot_size_failure_redacts_token_and_preserves_failure_status(self) -> None:
        token = "hf_size_secret"
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"HF_TOKEN": token}),
            mock.patch.object(
                hf_snapshot_size,
                "snapshot_files",
                side_effect=RuntimeError(f"Bearer {token}: failed"),
            ),
            contextlib.redirect_stderr(output),
        ):
            status = hf_snapshot_size.main([tmp, f"org/model@{'a' * 40}"])

        self.assertEqual(status, 1)
        self.assertNotIn(token, output.getvalue())
        self.assertIn("RuntimeError", output.getvalue())
        self.assertIn("check repository access", output.getvalue())


if __name__ == "__main__":
    unittest.main()
