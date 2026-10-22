from __future__ import annotations

import tempfile
import unittest
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from campaign_output import (
    campaign_metadata,
    fingerprint_identity,
    prepare_implicit_campaign,
    campaign_root_from_input,
    resolve_campaign_dir,
    resolve_campaign_output_path,
    resolve_index_path,
)


class CampaignOutputTests(unittest.TestCase):
    def test_default_uses_utc_date(self) -> None:
        local_time = datetime(2026, 10, 21, 23, 30, tzinfo=timezone(timedelta(hours=-4)))
        with tempfile.TemporaryDirectory() as temporary:
            resolved = resolve_campaign_dir(cwd=temporary, now=local_time)
        self.assertEqual(resolved.name, "benchmark-2026-10-22")

    def test_explicit_nested_path_is_complete_and_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resolved = resolve_campaign_dir(
                "results/rdma-v4-paper-2026-10-22/nested",
                cwd=temporary,
                now=datetime(2027, 1, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(
            resolved,
            Path(temporary) / "results/rdma-v4-paper-2026-10-22/nested",
        )

    def test_explicit_resume_keeps_path_across_utc_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = "results/rdma-v4-paper-2026-10-22"
            first = resolve_campaign_dir(
                path, cwd=temporary,
                now=datetime(2026, 10, 22, tzinfo=timezone.utc),
            )
            after_midnight = resolve_campaign_dir(
                path, cwd=temporary,
                now=datetime(2026, 10, 23, tzinfo=timezone.utc),
            )
        self.assertEqual(first, after_midnight)

    def test_plan_only_does_not_create_default_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = resolve_campaign_dir(cwd=temporary)
            resumed = prepare_implicit_campaign(
                output, fingerprint_identity([], ["run=same"]), plan_only=True
            )
            self.assertFalse(resumed)
            self.assertFalse(output.exists())

    def test_implicit_collision_requires_matching_identity(self) -> None:
        identity = fingerprint_identity([], ["run=same"])
        with tempfile.TemporaryDirectory() as temporary:
            output = resolve_campaign_dir(cwd=temporary)
            self.assertFalse(prepare_implicit_campaign(output, identity))
            self.assertTrue(prepare_implicit_campaign(output, identity))
            with self.assertRaisesRegex(ValueError, "not a compatible resume"):
                prepare_implicit_campaign(
                    output, fingerprint_identity([], ["run=different"])
                )

    def test_manifest_names_date_and_resolved_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "results" / "rdma-v4-paper-2026-10-22"
            metadata = campaign_metadata(output)
        self.assertEqual(metadata["campaign_name"], "rdma-v4-paper")
        self.assertEqual(metadata["campaign_date"], "2026-10-22")
        self.assertEqual(metadata["resolved_out_dir"], str(output.resolve()))

    def test_plotting_accepts_directory_and_legacy_index_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary) / "legacy-results"
            campaign.mkdir()
            index = campaign / "index.csv"
            self.assertEqual(resolve_index_path(campaign), index)
            self.assertEqual(resolve_index_path(index), index)
            self.assertEqual(
                resolve_campaign_output_path("figs/stats", index),
                campaign / "figs/stats",
            )

    def test_plotting_paths_are_independent_of_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = root / "campaign"
            campaign.mkdir()
            index = campaign / "index.csv"
            other = root / "other"
            other.mkdir()
            original_cwd = Path.cwd()
            try:
                os.chdir(other)
                self.assertEqual(resolve_index_path(index), index)
                self.assertEqual(campaign_root_from_input(index), campaign)
                self.assertEqual(
                    resolve_campaign_output_path(None, index), campaign / "figs"
                )
            finally:
                os.chdir(original_cwd)

    def test_default_path_is_repo_relative_from_another_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original_cwd = Path.cwd()
            try:
                os.chdir(temporary)
                resolved = resolve_campaign_dir(
                    now=datetime(2026, 10, 22, tzinfo=timezone.utc)
                )
            finally:
                os.chdir(original_cwd)
        repository = Path(__file__).resolve().parents[1]
        self.assertEqual(resolved, repository / "results/benchmark-2026-10-22")


if __name__ == "__main__":
    unittest.main()
