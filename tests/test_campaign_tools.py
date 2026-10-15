"""Tests for the steady-exact-v4 campaign helpers in scripts/."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_nccl_transport  # noqa: E402
import campaign_schedule  # noqa: E402
import compare_campaigns  # noqa: E402
import hf_snapshot_size  # noqa: E402
import purge_model_cache  # noqa: E402


def _entry(label, name, configs):
    return {"label": label, "name": name, "configs": configs}


class CampaignScheduleTests(unittest.TestCase):
    def test_rotation_puts_every_config_in_every_slot_once(self):
        models = [_entry("m", "org/m", ["single", "tp2", "pp2"])]
        (group,) = campaign_schedule.build_groups(models, 3)
        by_rep = {}
        for rep, config, _ in group["steps"]:
            by_rep.setdefault(rep, []).append(config)

        self.assertEqual(by_rep[1], ["single", "tp2", "pp2"])
        self.assertEqual(by_rep[2], ["tp2", "pp2", "single"])
        self.assertEqual(by_rep[3], ["pp2", "single", "tp2"])
        for slot in range(3):
            self.assertEqual({by_rep[r][slot] for r in (1, 2, 3)},
                             {"single", "tp2", "pp2"})
        self.assertTrue(group["needs_dual"])

    def test_entries_sharing_a_repo_form_one_group_in_file_order(self):
        models = [
            _entry("a", "org/a", ["single"]),
            _entry("b", "org/shared", ["tp2"]),
            _entry("c", "org/shared", ["tp2", "pp2"]),
        ]
        groups = campaign_schedule.build_groups(models, 1)

        self.assertEqual([g["labels"] for g in groups], [["a"], ["b", "c"]])
        self.assertFalse(groups[0]["needs_dual"])
        self.assertEqual(groups[1]["steps"],
                         [(1, "tp2", "b"), (1, "tp2", "c"), (1, "pp2", "c")])

    def test_label_and_config_filters(self):
        models = [_entry("a", "org/a", ["single", "tp2"]),
                  _entry("b", "org/b", ["single", "tp2", "pp2"])]
        groups = campaign_schedule.build_groups(models, 2, labels={"b"},
                                                configs={"pp2"})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["steps"], [(1, "pp2", "b"), (2, "pp2", "b")])

    def test_cli_rejects_unknown_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.yaml"
            path.write_text("models:\n  - {label: a, name: org/a, configs: [single]}\n")
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    campaign_schedule.main([str(path), "3", "--labels", "zzz"]), 2)

    def test_shipped_matrix_launch_count(self):
        import yaml
        models = yaml.safe_load((ROOT / "configs/models.yaml").read_text())["models"]
        groups = campaign_schedule.build_groups(models, 3)
        curves = sum(len(entry["configs"]) for entry in models)
        self.assertEqual(curves, 53)
        self.assertEqual(sum(len(g["steps"]) for g in groups), curves * 3)


class PurgeModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self.tmp.name) / "hub"
        for repo in ("org--keep", "org--gone"):
            blobs = self.hub / f"models--{repo}" / "blobs"
            blobs.mkdir(parents=True)
            (blobs / "abc").write_bytes(b"x" * 1000)
            snap = self.hub / f"models--{repo}" / "snapshots" / ("0" * 40)
            snap.mkdir(parents=True)
            (snap / "model.safetensors").symlink_to(blobs / "abc")
        (self.hub / ".locks" / "models--org--gone").mkdir(parents=True)
        self.patches = [
            mock.patch.object(purge_model_cache, "HUB", str(self.hub)),
            mock.patch.object(purge_model_cache, "busy_processes", return_value=[]),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def test_deletes_only_the_named_repo_and_counts_blob_bytes(self):
        report = purge_model_cache.purge_node(["org/gone", "org/absent"], False, False)

        self.assertNotIn("error", report)
        self.assertFalse((self.hub / "models--org--gone").exists())
        self.assertFalse((self.hub / ".locks" / "models--org--gone").exists())
        self.assertTrue((self.hub / "models--org--keep").exists())
        self.assertEqual(report["freed_bytes"], 1000)
        self.assertEqual([r["status"] for r in report["repos"]], ["deleted", "absent"])

    def test_dry_run_deletes_nothing(self):
        report = purge_model_cache.purge_node(["org/gone"], False, True)
        self.assertTrue((self.hub / "models--org--gone").exists())
        self.assertEqual(report["repos"][0]["status"], "would delete")

    def test_unsafe_ids_and_symlinked_entries_are_refused(self):
        for bad in ("../etc", "org/../x", "org", "/abs/path", "org/a/b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    purge_model_cache.repo_dir(bad)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.hub / "models--org--link").symlink_to(outside)
        report = purge_model_cache.purge_node(["org/link"], False, False)
        self.assertIn("symlinked", report["error"])
        self.assertTrue(outside.exists())

    def test_busy_node_refuses(self):
        with mock.patch.object(purge_model_cache, "busy_processes",
                               return_value=["42: vllm serve x"]):
            report = purge_model_cache.purge_node(["org/gone"], False, False)
        self.assertIn("serving processes", report["error"])
        self.assertTrue((self.hub / "models--org--gone").exists())


class SnapshotSizeTests(unittest.TestCase):
    REVISION = "a" * 40

    def _payload(self):
        return {
            "sha": self.REVISION,
            "siblings": [
                {"rfilename": "model.safetensors", "size": 1000,
                 "lfs": {"sha256": "f" * 64, "size": 1000}},
                {"rfilename": "config.json", "size": 10, "blobId": "b" * 40},
                {"rfilename": "original/consolidated.pth", "size": 5000},
                {"rfilename": "metal/model.bin", "size": 7000},
            ],
        }

    def test_ignored_duplicates_and_cached_blobs_are_not_reserved(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            json.dumps(self._payload()).encode())
        with tempfile.TemporaryDirectory() as tmp:
            blobs = Path(tmp) / "hub" / "models--org--m" / "blobs"
            blobs.mkdir(parents=True)
            (blobs / ("f" * 64)).write_bytes(b"x" * 1000)  # weights complete
            out = io.StringIO()
            with mock.patch("urllib.request.urlopen", return_value=response), \
                    mock.patch("sys.stdout", out):
                rc = hf_snapshot_size.main([tmp, f"org/m@{self.REVISION}"])

        self.assertEqual(rc, 0)
        self.assertIn("total_needed_bytes=10", out.getvalue())

    def test_revision_mismatch_fails_before_download(self):
        payload = self._payload()
        payload["sha"] = "c" * 40
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch("sys.stderr", io.StringIO()), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(hf_snapshot_size.main(["/x", f"org/m@{self.REVISION}"]), 1)


class TransportAuditTests(unittest.TestCase):
    FP = "1" * 64

    def _campaign(self, tmp, log_lines, ib_disable="0"):
        out = Path(tmp)
        (out / f"serving_policy_m_tp2_{self.FP}.json").write_text(json.dumps({
            "config": "tp2", "serving_fingerprint": self.FP,
            "nccl_ib_disable": ib_disable,
        }))
        log = out / f"server_m_tp2_{self.FP[:16]}_20261016T000000Z_pid1.log"
        log.write_text("\n".join(log_lines) + "\n")
        return out

    def _run(self, out):
        with mock.patch("sys.stdout", io.StringIO()):
            return audit_nccl_transport.main([str(out), "--md", str(out / "a.md")])

    def test_rdma_launch_is_verified_with_devices_and_gdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._campaign(tmp, [
                "NCCL INFO NET/IB : Using [0]rocep1s0f0:1/RoCE "
                "[1]roceP2p1s0f0:1/RoCE [RO]; OOB enp1s0f0np0:10.10.10.1<0>",
                "NCCL INFO Channel 00/0 : 0[0] -> 1[0] [send] via NET/IB/0/GDRDMA",
                "NCCL INFO Channel 01/0 : 0[0] -> 1[0] [send] via NET/IB/1",
            ])
            self.assertEqual(self._run(out), 0)
            report = (out / "a.md").read_text()
        self.assertIn("| 2 | 1 | 0 | 2 (roceP2p1s0f0, rocep1s0f0) | ok |", report)

    def test_socket_fallback_under_rdma_policy_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._campaign(tmp, [
                "NCCL INFO Channel 00/0 : 0[0] -> 1[0] [send] via NET/Socket/0",
            ])
            self.assertEqual(self._run(out), 1)
            self.assertIn("WRONG TRANSPORT", (out / "a.md").read_text())

    def test_tcp_ablation_and_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._campaign(tmp, ["via NET/Socket/0"], ib_disable="1")
            self.assertEqual(self._run(out), 0)
        with tempfile.TemporaryDirectory() as tmp:
            out = self._campaign(tmp, ["INFO vllm ready"])
            self.assertEqual(self._run(out), 1)
            self.assertIn("no evidence", (out / "a.md").read_text())

    def test_empty_or_policyless_campaign_is_not_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(Path(tmp)), 1)
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(audit_nccl_transport.main([tmp, "--allow-unverified"]), 0)
        with tempfile.TemporaryDirectory() as tmp:
            out = self._campaign(tmp, ["via NET/IB/0"], ib_disable="invalid")
            self.assertEqual(self._run(out), 1)
            self.assertIn("no policy", (out / "a.md").read_text())

    def test_exploratory_mode_never_allows_wrong_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._campaign(tmp, ["via NET/IB/0", "via NET/Socket/0"])
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(audit_nccl_transport.main([str(out), "--allow-unverified"]), 1)


class CompareCampaignsTests(unittest.TestCase):
    def test_ratios_and_replicate_interval(self):
        common = {"model_label": "m", "config_label": "tp2", "mode": "closed"}
        new = pd.DataFrame([
            {**common, "concurrency": 1, "tok_thru_tps": t, "itl_p50": i}
            for t, i in ((20.0, 0.050), (22.0, 0.052), (24.0, 0.054))
        ] + [{**common, "concurrency": 8, "tok_thru_tps": 100.0, "itl_p50": 0.08}])
        base = pd.DataFrame([
            {**common, "concurrency": 1, "tok_thru_tps": 11.0, "itl_p50": 0.104},
            {**common, "concurrency": 8, "tok_thru_tps": 50.0, "itl_p50": 0.16},
        ])
        rows = compare_campaigns.summarize(compare_campaigns.point_stats(new),
                                           compare_campaigns.point_stats(base))

        (row,) = rows
        self.assertEqual(row["loads"], 2)
        self.assertAlmostEqual(row["itl_new"], 52.0)
        self.assertGreater(row["itl_new_ci"], 0)
        self.assertEqual(compare_campaigns.ratio(row["itl_new"], row["itl_base"]), "0.50x")
        self.assertEqual(row["peak_new_c"], 8)
        self.assertEqual(compare_campaigns.ratio(row["peak_new"], row["peak_base"]), "2.00x")


if __name__ == "__main__":
    unittest.main()
