from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bench.local_execution import (
    collect_local_runtime_facts,
    install_local_execution_record,
    local_execution_record_digest,
    verify_local_execution_record,
)
from bench.release_contracts import (
    SERVING_V3_FINGERPRINT_FIELDS,
    _validate_solo_serving_policy,
    load_serving_policy,
    serving_policy_fingerprint,
)
from scripts import system_snapshot
from scripts.campaign.plan import CampaignValidationError, ModelSpec, select_models


ROOT = Path(__file__).resolve().parents[1]
PINNED_IMAGE = "registry.example/vllm@sha256:" + "a" * 64
MODEL_REVISION = "b" * 40


def create_model(name: str, label: str, configs: tuple[str, ...]) -> ModelSpec:
    return ModelSpec(
        name=name,
        label=label,
        revision=MODEL_REVISION,
        configs=configs,
        max_model_len=2048,
        tokenizer=name,
        tokenizer_revision=MODEL_REVISION,
        quantization="",
        trust_remote_code=False,
        gpu_utilization=0.8,
    )


def create_solo_policy() -> dict[str, object]:
    hostname = "solo-test-host"
    execution_sha256 = "c" * 64
    runtime_facts: dict[str, object] = {
        "execution_sha256": execution_sha256,
        "hostname": hostname,
        "image": PINNED_IMAGE,
        "loopback_address": "127.0.0.1",
        "gpus": [{
            "gpu_name": "GB10",
            "gpu_uuid": "GPU-test",
            "driver": "590.0",
            "power_limit_w": "N/A",
            "compute_capability": "12.1",
            "max_graphics_clock_mhz": "2000",
            "application_graphics_clock_mhz": "N/A",
            "default_application_graphics_clock_mhz": "N/A",
        }],
        "query_fields": ["name", "uuid", "driver_version"],
        "runtime_env": {
            "HF_HUB_OFFLINE": "0",
            "TRANSFORMERS_OFFLINE": "0",
            "TORCHINDUCTOR_AUTOGRAD_CACHE": "0",
        },
        "serving_container": "standalone",
        "source": "host-nvidia-smi",
    }
    runtime_hash = hashlib.sha256(
        json.dumps(runtime_facts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    policy: dict[str, object] = {
        field: "" for field in SERVING_V3_FINGERPRINT_FIELDS
    }
    policy.update({
        "schema": "serving-v3",
        "config": "single",
        "image": PINNED_IMAGE,
        "model": "org/model",
        "revision": MODEL_REVISION,
        "tokenizer": "org/model",
        "tokenizer_revision": MODEL_REVISION,
        "dtype": "auto",
        "quant": "",
        "max_model_len": "2048",
        "gpu_util": "0.8",
        "prefix_caching": "0",
        "generation_config": "vllm",
        "torchinductor_autograd_cache": "0",
        "hf_hub_offline": "0",
        "trust_remote_code": "0",
        "power_backend": "nvidia-smi",
        "cluster_runtime": runtime_hash,
        "runtime_facts_sha256": runtime_hash,
        "runtime_scope": "single-host",
        "runtime_facts": runtime_facts,
        "max_num_seqs": "",
        "flashinfer_autotune": "0",
        "execution_scope": "single-node",
        "execution_sha256": execution_sha256,
        "hostname": hostname,
        "serving_container": "standalone",
        "serving_fingerprint": "",
    })
    policy["serving_fingerprint"] = serving_policy_fingerprint(policy)
    return policy


class SoloModeEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.env_file = self.root / ".env"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_mode(self, value: str | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("SINGLE_NODE_ONLY", None)
        environment.pop("DGX_SPARK_ENV_LOADED", None)
        environment["ENV_FILE"] = str(self.env_file)
        if value is not None:
            environment["SINGLE_NODE_ONLY"] = value
        return subprocess.run(
            [
                "bash", "-c",
                'source scripts/load_env.sh && resolve_single_node_mode && '
                'printf "%s\\n" "$SINGLE_NODE_ONLY"',
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_mode_values_and_dotenv_precedence(self) -> None:
        for supplied, expected in ((None, "0"), ("0", "0"), ("1", "1")):
            with self.subTest(supplied=supplied):
                result = self.run_mode(supplied)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
        for supplied in ("", "false", "2"):
            with self.subTest(supplied=supplied):
                result = self.run_mode(supplied)
                self.assertEqual(result.returncode, 2)
                self.assertIn("SINGLE_NODE_ONLY", result.stderr)
        self.env_file.write_text("SINGLE_NODE_ONLY=1\n", encoding="utf-8")
        self.assertEqual(self.run_mode().stdout.strip(), "1")
        self.assertEqual(self.run_mode("0").stdout.strip(), "0")

    def test_solo_environment_bypasses_topology_and_suffixes_once(self) -> None:
        environment = os.environ.copy()
        environment.update({
            "ENV_FILE": str(self.root / "missing.env"),
            "SINGLE_NODE_ONLY": "1",
            "DGX_SPARK_ENV_LOADED": "",
            "CLUSTER_CONFIG": str(self.root / "invalid-cluster.yaml"),
            "HEAD_IP": "198.51.100.20",
            "WORKER_IP": "198.51.100.21",
            "NCCL_SOCKET_IFNAME": "cx0",
            "VLLM_CONTAINER": "unit",
        })
        script = (
            "source scripts/connectx_env.sh || exit 1\n"
            '[[ "$VLLM_CONTAINER" == unit-solo && "$LOCAL_IP" == 127.0.0.1 ]] || exit 2\n'
            "! printenv HEAD_IP WORKER_IP NCCL_SOCKET_IFNAME CLUSTER_CONFIG "
            ">/dev/null || exit 3\n"
            "source scripts/connectx_env.sh || exit 5\n"
            '[[ "$VLLM_CONTAINER" == unit-solo ]] || exit 6\n'
        )
        result = subprocess.run(
            ["bash", "-c", script], cwd=ROOT, env=environment,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SoloEntrypointTests(unittest.TestCase):
    def test_cluster_only_entrypoints_reject_solo_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker_log = root / "docker.log"
            docker = root / "docker"
            docker.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\nexit 99\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            environment = os.environ.copy()
            environment.pop("DGX_SPARK_ENV_LOADED", None)
            environment.update({
                "SINGLE_NODE_ONLY": "1",
                "ENV_FILE": str(Path(temporary) / "missing.env"),
                "CLUSTER_CONFIG": str(Path(temporary) / "missing-cluster.yaml"),
                "DOCKER_LOG": str(docker_log),
                "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                "PURGE_WORKER": "0",
            })
            commands = (
                ("scripts/launch_server.sh", "tp2"),
                ("scripts/run_models.sh", "tp2"),
                ("scripts/run_all.sh", "tp2"),
                ("scripts/predownload.sh", "tp2"),
                ("scripts/start_ray.sh", "head"),
                ("scripts/mirror_worker_cache.sh", "all"),
                ("scripts/verify_connectx.sh",),
                ("scripts/ray_allreduce_bench.sh",),
                ("scripts/interconnect_extra_bench.sh",),
            )
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        ["bash", *command],
                        cwd=ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(docker_log.exists())

    def test_worker_purge_rejects_before_local_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker_log = root / "docker.log"
            docker = root / "docker"
            docker.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\nexit 99\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            environment = os.environ.copy()
            environment.pop("DGX_SPARK_ENV_LOADED", None)
            environment.update({
                "SINGLE_NODE_ONLY": "1",
                "ENV_FILE": str(Path(temporary) / "missing.env"),
                "CLUSTER_CONFIG": str(Path(temporary) / "missing-cluster.yaml"),
                "DOCKER_LOG": str(docker_log),
                "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                "PURGE_WORKER": "1",
            })
            result = subprocess.run(
                ["bash", "scripts/purge_model_cache.sh"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PURGE_WORKER=1", result.stderr)
            self.assertFalse(docker_log.exists())

    def test_auto_resource_scope_skips_docker_and_required_scope_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager_log = root / "manager.log"
            docker_log = root / "docker.log"
            manager = root / "resource-manager"
            manager.write_text(
                "#!/bin/sh\nprintf 'manager\\n' >> \"$MANAGER_LOG\"\n"
                "printf '[resources] local node is idle\\n'\n",
                encoding="utf-8",
            )
            manager.chmod(0o755)
            environment = os.environ.copy()
            environment.pop("DGX_SPARK_ENV_LOADED", None)
            environment.update({
                "SINGLE_NODE_ONLY": "1",
                "ENV_FILE": str(root / "missing.env"),
                "RESOURCE_MANAGER_PYTHON": str(manager),
                "MANAGER_LOG": str(manager_log),
                "DOCKER_LOG": str(docker_log),
            })
            script = r"""
source scripts/connectx_env.sh || exit 1
docker() {
  printf '%s\n' "$*" >> "$DOCKER_LOG"
  return 99
}
resource_check_once auto all >/dev/null || exit 2
if resource_check_once required all >/dev/null 2>&1; then exit 3; fi
"""
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(manager_log.read_text(encoding="utf-8").splitlines(), ["manager"])
            self.assertFalse(docker_log.exists())


class SoloSelectionTests(unittest.TestCase):
    def test_selection_filters_dual_only_labels(self) -> None:
        models = (
            create_model("org/shared", "local-model", ("single", "tp2")),
            create_model("org/shared", "dual-model", ("tp2", "pp2")),
        )
        selection = select_models(
            models, ("local-model", "dual-model"), None, single_node_only=True
        )
        self.assertEqual(
            [(model.label, model.configs) for model in selection.models],
            [("local-model", ("single",))],
        )
        self.assertEqual(selection.excluded_labels, ("dual-model",))

    def test_unknown_empty_and_dual_config_selections_fail(self) -> None:
        single = (create_model("org/model", "local", ("single",)),)
        with self.assertRaisesRegex(CampaignValidationError, "unknown label"):
            select_models(single, ("missing",), None, single_node_only=True)
        with self.assertRaisesRegex(CampaignValidationError, "empty"):
            select_models(
                (create_model("org/model", "dual", ("tp2",)),),
                None, None, single_node_only=True,
            )
        with self.assertRaisesRegex(CampaignValidationError, "permits only"):
            select_models(single, None, ("tp2",), single_node_only=True)


class LocalExecutionRecordTests(unittest.TestCase):
    def test_record_is_canonical_immutable_and_host_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record, digest, path = install_local_execution_record(
                temporary, "Solo-Test-Host.", "standalone"
            )
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(path.read_bytes(), encoded)
            self.assertEqual(digest, hashlib.sha256(encoded).hexdigest())
            self.assertEqual(
                verify_local_execution_record(temporary, "solo-test-host", "standalone"),
                digest,
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                install_local_execution_record(temporary, "other-host", "standalone")

    def test_record_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            inputs.mkdir()
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            (inputs / "local_execution.json").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "non-regular"):
                install_local_execution_record(root, "solo-test-host", "standalone")


class LocalRuntimeFactsTests(unittest.TestCase):
    def test_runtime_collection_preserves_optional_unavailable_fields(self) -> None:
        execution_sha256 = local_execution_record_digest(
            "solo-test-host", "standalone"
        )
        completed = subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=0,
            stdout="GB10, GPU-test, 590.0, N/A, 12.1, 2000, N/A, N/A\n",
            stderr="",
        )
        with (
            patch("bench.local_execution.subprocess.run", return_value=completed),
            patch.dict(
                os.environ,
                {"HF_HUB_OFFLINE": "1", "TORCHINDUCTOR_AUTOGRAD_CACHE": "0"},
            ),
        ):
            facts = collect_local_runtime_facts(
                execution_sha256,
                "solo-test-host",
                "standalone",
                PINNED_IMAGE,
            )
        self.assertEqual(len(facts["gpus"]), 1)
        self.assertEqual(facts["gpus"][0]["power_limit_w"], "N/A")
        self.assertEqual(facts["runtime_env"]["HF_HUB_OFFLINE"], "1")

    def test_runtime_collection_rejects_multiple_gpus_and_missing_identity(self) -> None:
        execution_sha256 = local_execution_record_digest(
            "solo-test-host", "standalone"
        )
        for output in (
            "GB10, GPU-a, 590.0, 200, 12.1, 2000, 2000, 2000\n"
            "GB10, GPU-b, 590.0, 200, 12.1, 2000, 2000, 2000\n",
            "GB10, N/A, 590.0, 200, 12.1, 2000, 2000, 2000\n",
        ):
            completed = subprocess.CompletedProcess(
                args=["nvidia-smi"], returncode=0, stdout=output, stderr=""
            )
            with patch(
                "bench.local_execution.subprocess.run", return_value=completed
            ):
                with self.assertRaises(RuntimeError):
                    collect_local_runtime_facts(
                        execution_sha256,
                        "solo-test-host",
                        "standalone",
                        PINNED_IMAGE,
                    )


class SoloServingPolicyTests(unittest.TestCase):
    def test_v3_policy_loads_and_hashes_local_runtime(self) -> None:
        policy = create_solo_policy()
        _validate_solo_serving_policy(policy)
        self.assertEqual(policy["cluster_runtime"], policy["runtime_facts_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "serving.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            config = SimpleNamespace(
                require_provenance=True,
                out_dir=temporary,
                config_label="single",
                model="org/model",
                model_revision=MODEL_REVISION,
                tokenizer="org/model",
                tokenizer_revision=MODEL_REVISION,
                serving_fingerprint=policy["serving_fingerprint"],
                trust_remote_code=False,
            )
            loaded, metadata = load_serving_policy(str(path), config)
            self.assertEqual(loaded, policy)
            self.assertIsNotNone(metadata)
        changed = dict(policy, hf_hub_offline="1")
        self.assertNotEqual(
            serving_policy_fingerprint(changed), policy["serving_fingerprint"]
        )

    def test_v3_policy_rejects_peer_fields_dual_config_and_bad_facts(self) -> None:
        policy = create_solo_policy()
        with self.assertRaisesRegex(RuntimeError, "peer/topology"):
            _validate_solo_serving_policy(dict(policy, worker_ip="198.51.100.21"))
        with self.assertRaisesRegex(RuntimeError, "config must be single"):
            _validate_solo_serving_policy(dict(policy, config="tp2"))
        facts = dict(policy["runtime_facts"])
        facts["hostname"] = "other-host"
        runtime_hash = hashlib.sha256(
            json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        invalid = dict(
            policy,
            runtime_facts=facts,
            runtime_facts_sha256=runtime_hash,
            cluster_runtime=runtime_hash,
        )
        invalid["serving_fingerprint"] = serving_policy_fingerprint(invalid)
        with self.assertRaisesRegex(RuntimeError, "runtime hostname"):
            _validate_solo_serving_policy(invalid)


class LocalSnapshotTests(unittest.TestCase):
    def test_local_snapshot_has_one_host_and_skips_peer_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            local = {"host": {"hostname": "solo-test-host"}, "container": {"status": "ok"}}
            with (
                patch.object(
                    sys, "argv",
                    ["system_snapshot.py", "--output", str(output), "--local-only"],
                ),
                patch.object(system_snapshot, "node_snapshot", return_value=local),
                patch.object(
                    system_snapshot, "read_topology",
                    side_effect=AssertionError("topology access is forbidden"),
                ),
                patch.object(
                    system_snapshot, "remote_node_snapshot",
                    side_effect=AssertionError("SSH is forbidden"),
                ),
            ):
                self.assertEqual(system_snapshot.main(), 0)
            snapshot = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["execution_scope"], "single-node")
        self.assertEqual(snapshot["peer_collection"]["status"], "skipped")
        self.assertEqual(snapshot["nodes"][0]["hostname"], "solo-test-host")
        self.assertEqual(snapshot["nodes"][0]["observed_state"], local)
        self.assertEqual(len(snapshot["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
