from __future__ import annotations

import tempfile
import unittest
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from utils.campaign_output import (
    campaign_metadata,
    campaign_logs_dir,
    campaign_results_dir,
    fingerprint_identity,
    prepare_campaign,
    campaign_root_from_input,
    resolve_campaign_dir,
    resolve_campaign_output_path,
    resolve_campaign_logs_dir,
    resolve_campaign_results_dir,
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
            resumed = prepare_campaign(
                output, fingerprint_identity([], ["run=same"]), plan_only=True
            )
            self.assertFalse(resumed)
            self.assertFalse(output.exists())

    def test_implicit_collision_requires_matching_identity(self) -> None:
        identity = fingerprint_identity([], ["run=same"])
        with tempfile.TemporaryDirectory() as temporary:
            output = resolve_campaign_dir(cwd=temporary)
            self.assertFalse(prepare_campaign(output, identity))
            self.assertTrue((output / "results" / "campaign_run.json").is_file())
            self.assertTrue(prepare_campaign(output, identity))
            with self.assertRaisesRegex(ValueError, "not a compatible resume"):
                prepare_campaign(
                    output, fingerprint_identity([], ["run=different"])
                )

    def test_explicit_existing_empty_directory_can_be_claimed(self) -> None:
        identity = fingerprint_identity([], ["run=same"])
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "campaign"
            output.mkdir()
            self.assertFalse(
                prepare_campaign(
                    output, identity, plan_only=True, allow_empty_existing=True
                )
            )
            self.assertEqual(list(output.iterdir()), [])
            self.assertFalse(
                prepare_campaign(output, identity, allow_empty_existing=True)
            )
            self.assertTrue(prepare_campaign(output, identity))
            marker = output / "results" / "campaign_run.json"
            marker_before = marker.read_bytes()
            with self.assertRaisesRegex(ValueError, "not a compatible resume"):
                prepare_campaign(
                    output,
                    fingerprint_identity([], ["run=different"]),
                    allow_empty_existing=True,
                )
            self.assertEqual(marker.read_bytes(), marker_before)

    def test_explicit_markerless_results_are_rejected(self) -> None:
        identity = fingerprint_identity([], ["run=same"])
        with tempfile.TemporaryDirectory() as temporary:
            for name, index_path in (
                ("legacy", "index.csv"),
                ("nested", "results/index.csv"),
            ):
                with self.subTest(name=name):
                    output = Path(temporary) / name
                    index = output / index_path
                    index.parent.mkdir(parents=True)
                    index.write_text("existing results\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "not a compatible resume"):
                        prepare_campaign(
                            output, identity, allow_empty_existing=True
                        )
                    self.assertEqual(index.read_text(encoding="utf-8"), "existing results\n")
                    self.assertFalse((output / "results" / "campaign_run.json").exists())

    def test_manifest_names_date_and_resolved_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "results" / "rdma-v4-paper-2026-10-22"
            metadata = campaign_metadata(output)
        self.assertEqual(metadata["campaign_name"], "rdma-v4-paper")
        self.assertEqual(metadata["campaign_date"], "2026-10-22")
        self.assertEqual(metadata["resolved_out_dir"], str(output.resolve()))

    def test_plotting_accepts_directory_and_legacy_index_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary) / "new-campaign"
            campaign.mkdir()
            index = campaign / "results" / "index.csv"
            index.parent.mkdir()
            index.touch()
            self.assertEqual(resolve_index_path(campaign), index)
            self.assertEqual(resolve_index_path(index), index)
            self.assertEqual(campaign_root_from_input(index), campaign)
            self.assertEqual(
                resolve_campaign_output_path("figs/stats", index),
                campaign / "figs/stats",
            )

            legacy_campaign = Path(temporary) / "legacy-campaign"
            legacy_campaign.mkdir()
            legacy_index = legacy_campaign / "index.csv"
            legacy_index.touch()
            self.assertEqual(resolve_index_path(legacy_campaign), legacy_index)
            self.assertEqual(
                resolve_campaign_results_dir(legacy_campaign), legacy_campaign
            )
            self.assertEqual(
                resolve_campaign_logs_dir(legacy_campaign), legacy_campaign
            )

    def test_campaign_output_directories_are_beneath_campaign_root(
        self: CampaignOutputTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary) / "campaign"

            self.assertEqual(campaign_results_dir(campaign), campaign / "results")
            self.assertEqual(campaign_logs_dir(campaign), campaign / "logs")
            self.assertEqual(resolve_campaign_results_dir(campaign), campaign / "results")
            self.assertEqual(resolve_campaign_logs_dir(campaign), campaign / "logs")

    def test_plotting_paths_are_independent_of_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = root / "campaign"
            index = campaign / "results" / "index.csv"
            index.parent.mkdir(parents=True)
            index.touch()
            other = root / "other"
            other.mkdir()
            original_cwd = Path.cwd()
            try:
                os.chdir(other)
                self.assertEqual(resolve_index_path(index), index)
                self.assertEqual(campaign_root_from_input(index), campaign)
                self.assertEqual(
                    resolve_campaign_output_path(None, index),
                    campaign / "figs",
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
