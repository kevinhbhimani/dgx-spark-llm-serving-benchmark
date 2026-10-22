from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_microbench(name):
    ray = ModuleType("ray")
    exceptions = ModuleType("ray.exceptions")
    exceptions.GetTimeoutError = type("GetTimeoutError", (Exception,), {})
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"ray": ray, "ray.exceptions": exceptions}):
        spec.loader.exec_module(module)
    return module


class TransportEvidenceTests(unittest.TestCase):
    def test_plugin_discovery_is_not_channel_evidence_and_mixed_is_rejected(self):
        allreduce = load_microbench("ray_allreduce_bench")
        interconnect = load_microbench("ray_interconnect_bench")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"NCCL_IB_DISABLE": "0"}):
            log = Path(tmp) / "nccl.log"
            log.write_text("NCCL INFO NET/IB : Using [0]roce0:1/RoCE\n")
            self.assertEqual(allreduce._transport_label(str(log)), "unknown")
            self.assertEqual(interconnect._transport_label(str(log)), "roce?")
            log.write_text("via NET/IB/0/GDRDMA\nvia NET/Socket/0\n")
            self.assertEqual(allreduce._transport_label(str(log)), "mixed")
            self.assertEqual(interconnect._transport_label(str(log)), "mixed")
            log.write_text("via NET/Socket/0\n")
            self.assertEqual(allreduce._transport_label(str(log)), "Socket")
            with mock.patch.dict(os.environ, {"NCCL_NSOCKS_PERTHREAD": "4", "NCCL_SOCKET_NTHREADS": "4"}):
                self.assertEqual(interconnect._transport_label(str(log)), "tcp-tuned-x16")

    def test_allreduce_requires_both_ranks_and_uses_slower_rank(self):
        bench = load_microbench("ray_allreduce_bench")
        fast = [(8, 0.00002, 0.00001), (1024, 0.00004, 0.00003)]
        slow = [(8, 0.00004, 0.00003), (1024, 0.00008, 0.00006)]
        self.assertEqual(bench._validated_results([("IB", fast), ("IB", slow)], [8, 1024], "IB"), slow)
        for rows in (
            [("IB", fast), ("Socket", slow)],
            [("IB", fast), ("unknown", slow)],
            [("IB", fast), ("IB", slow[:-1])],
            [("IB", fast), ("IB", [(8, float("nan"), 0.1), slow[1]])],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                bench._validated_results(rows, [8, 1024], "IB")

    def test_interconnect_rejects_unverified_or_disagreeing_ranks(self):
        bench = load_microbench("ray_interconnect_bench")
        rows = [(size, 0.00002, 0.00001) for size in bench.SIZES]
        bench.ray.remote = mock.Mock(return_value=lambda _: SimpleNamespace(remote=lambda *args: object()))
        for labels in (("roce", "tcp"), ("roce", "roce?"), ("mixed", "mixed")):
            bench.ray.get = mock.Mock(return_value=[(label, rows, "") for label in labels])
            with self.subTest(labels=labels), mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                bench._run("allreduce", "10.10.10.1", "10.10.10.2", "cx0", 29518)


class ConnectXPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        DGX_SPARK_ENV_LOADED="1", CX_IFACE="cx0,cx1",
                        NCCL_IB_DISABLE="0",
                        DIYA_IP="10.10.10.1", HOEDUS_IP="10.10.10.2", HEAD_IP="10.10.10.1",
                        HF_CACHE=str(self.root / "hf-cache"),
                        CALL_LOG=str(self.calls))
        self.command("getent", '''case "$2" in
          cisc-diya)
            echo "10.10.10.1 STREAM"
            if [[ "${MOCK_LOCAL_LOOPBACK:-0}" == "1" ]]; then echo "127.0.0.1 STREAM"; fi
            if [[ "${MOCK_LOCAL_SECONDARY_ALIAS:-0}" == "1" ]]; then echo "10.10.11.1 STREAM"; fi;;
          cisc-hoedus) echo "10.10.10.2 STREAM";;
        esac''')
        self.command("ip", '''case "$*" in
          '-4 -o addr show cx0') echo '1: cx0 inet 10.10.10.1/24';;
          '-4 -o addr show cx1') echo '2: cx1 inet 10.10.11.1/24';;
          '-4 route get 10.10.10.2') echo "10.10.10.2 dev ${MOCK_ROUTE_IFACE:-cx0} src 10.10.10.1";;
          '-s link show '*) echo counters;;
          *) exit 1;; esac''')
        self.command("ping", 'printf "ping %s\\n" "$*" >> "$CALL_LOG"')
        self.command("rdma", '''echo 'link roce0/1 state ACTIVE physical_state LINK_UP netdev cx0'
          if [ "${MOCK_MISSING_RAIL:-0}" != 1 ]; then
            echo 'link roce1/1 state ACTIVE physical_state LINK_UP netdev cx1'
          fi''')
        self.command("docker", 'echo docker >> "$CALL_LOG"; exit 99')

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        path.chmod(0o755)

    def run_script(self, name, *args, **env):
        return subprocess.run(["bash", str(ROOT / "scripts" / name), *args],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=10)

    def test_verify_probes_every_interface_and_checks_active_rdma(self):
        result = self.run_script("verify_connectx.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "ping -I cx0 -W 2 -c 3 10.10.10.2", "ping -I cx1 -W 2 -c 3 10.10.11.2"])

    def test_verify_ignores_local_loopback_but_rejects_secondary_aliases(self):
        result = self.run_script("verify_connectx.sh", MOCK_LOCAL_LOOPBACK="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cisc-diya -> 10.10.10.1", result.stdout)
        self.calls.unlink()

        result = self.run_script("verify_connectx.sh", MOCK_LOCAL_SECONDARY_ALIAS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-loopback: '10.10.10.1\n10.10.11.1'", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_verify_rejects_wrong_route_and_missing_rdma_rail(self):
        result = self.run_script("verify_connectx.sh", MOCK_ROUTE_IFACE="wlan0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside CX_IFACE", result.stderr)
        self.assertFalse(self.calls.exists())
        result = self.run_script("verify_connectx.sh", MOCK_MISSING_RAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no ACTIVE/LINK_UP RDMA port mapped to cx1", result.stderr)

    def test_start_ray_rejects_missing_rail_before_removing_container(self):
        result = self.run_script("start_ray.sh", "head", MOCK_MISSING_RAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no ACTIVE/LINK_UP", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_local_role_accepts_secondary_ipv4_on_selected_interface(self):
        self.command("ip", "printf '1: cx0 inet 192.168.1.1/24\\n1: cx0 inet 10.10.10.1/24\\n'")
        result = subprocess.run(["bash", "-c", 'source "$1"; require_local_connectx_ip "$HEAD_IP"',
                                 "bash", str(ROOT / "scripts" / "connectx_env.sh")],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_scheduler_limit_fails_before_host_or_docker_operations(self):
        for value in ("0", "-1", "4.5", "abc"):
            result = self.run_script("launch_server.sh", MAX_NUM_SEQS=value)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("MAX_NUM_SEQS must be a positive integer", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_invalid_gpu_util_fails_before_host_or_docker_operations(self) -> None:
        for value in ("0", "1.1", "nan", "inf", "invalid"):
            with self.subTest(value=value):
                result = self.run_script("launch_server.sh", GPU_UTIL=value)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("GPU_UTIL must satisfy 0 < value <= 1", result.stderr)
                self.assertFalse(self.calls.exists())

    def test_campaign_matrix_emits_run_wide_memory_fraction(
        self: ConnectXPreflightTests,
    ) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "models_for_config.py"),
                str(ROOT / "configs" / "models.yaml"),
                "single",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        fractions = {line.split("|")[4] for line in result.stdout.splitlines()}
        models_config = yaml.safe_load((ROOT / "configs" / "models.yaml").read_text())
        self.assertEqual(fractions, {str(models_config["gpu_util_default"])})

    def test_gpu_memory_fraction_and_scheduler_limit_reach_all_serving_modes(
        self: ConnectXPreflightTests,
    ) -> None:
        self.command("python3", '''if [[ "$1" == "-" ]]; then
          cat >/dev/null
          exit 0
        fi
        exec /usr/bin/python3 "$@"''')
        self.command("docker", '''case "$*" in
          *'ray status'*) echo Active;;
          *) printf '%s\\n' "$@" > "$CALL_LOG";; esac''')
        for config in ("single", "tp2", "pp2"):
            result = self.run_script("launch_server.sh", config, MAX_NUM_SEQS="4",
                                     SINGLE_CONTAINER="ray", GPU_UTIL="0.85",
                                     HF_CACHE=str(self.root / "empty-cache"))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = self.calls.read_text().splitlines()
            gpu_memory_index = args.index("--gpu-memory-utilization")
            self.assertEqual(args[gpu_memory_index + 1], "0.85")
            max_num_seqs_index = args.index("--max-num-seqs")
            self.assertEqual(args[max_num_seqs_index + 1], "4")

    def test_direct_launch_defaults_gpu_memory_fraction_to_80_percent(
        self: ConnectXPreflightTests,
    ) -> None:
        self.command("python3", '''if [[ "$1" == "-" ]]; then
          cat >/dev/null
          exit 0
        fi
        exec /usr/bin/python3 "$@"''')
        self.command("docker", '''case "$*" in
          *'ray status'*) echo Active;;
          *) printf '%s\\n' "$@" > "$CALL_LOG";; esac''')
        result = self.run_script(
            "launch_server.sh",
            "single",
            SINGLE_CONTAINER="ray",
            GPU_UTIL="",
            HF_CACHE=str(self.root / "empty-cache"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.calls.read_text().splitlines()
        gpu_memory_index = args.index("--gpu-memory-utilization")
        self.assertEqual(args[gpu_memory_index + 1], "0.80")

    def test_purge_wrapper_passes_explicit_cache_pruning_opt_out(
        self: ConnectXPreflightTests,
    ) -> None:
        self.command("docker", '''case "$*" in
          *'ray status'*) echo Active;;
          *) printf '%s\\n' "$@" > "$CALL_LOG";; esac''')
        result = self.run_script(
            "purge_model_cache.sh",
            MODEL_LABELS="llama3.1-8b",
            MODELS_YAML=str(ROOT / "configs" / "models.yaml"),
            PURGE_CACHE_GARBAGE="0",
            PURGE_WORKER="0",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = self.calls.read_text().splitlines()
        self.assertIn("--no-prune-cache-garbage", arguments)

    def test_purge_wrapper_prunes_cache_garbage_by_default(
        self: ConnectXPreflightTests,
    ) -> None:
        self.command("docker", '''case "$*" in
          *'ray status'*) echo Active;;
          *) printf '%s\\n' "$@" > "$CALL_LOG";; esac''')
        result = self.run_script(
            "purge_model_cache.sh",
            MODELS_YAML=str(ROOT / "configs" / "models.yaml"),
            PURGE_CACHE_GARBAGE="",
            PURGE_WORKER="0",
            DRY_RUN="1",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = self.calls.read_text().splitlines()
        self.assertIn("--dry-run", arguments)
        self.assertNotIn("--no-prune-cache-garbage", arguments)

    def test_socket_tuning_is_forwarded_to_ray_container(self):
        self.command("docker", '''case "$*" in
          'inspect '*) echo true;;
          *'ray status'*) echo 'Active:';;
          'run '*) printf '%s\\n' "$@" > "$CALL_LOG";; esac''')
        result = self.run_script("start_ray.sh", "head", NCCL_IB_DISABLE="1",
                                 NCCL_NSOCKS_PERTHREAD="4", NCCL_SOCKET_NTHREADS="4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.calls.read_text().splitlines()
        self.assertIn("NCCL_NSOCKS_PERTHREAD=4", args)
        self.assertIn("NCCL_SOCKET_NTHREADS=4", args)

    def test_interconnect_identity_is_bound_to_requested_policy(self):
        env = {"CLUSTER_RUNTIME_FINGERPRINT": "a" * 64,
               "CAMPAIGN_LOCK_FILE": str(self.root / "campaign.lock")}
        rdma = self.run_script("interconnect_extra_bench.sh", "--fingerprint-only", **env)
        tcp = self.run_script("interconnect_extra_bench.sh", "--fingerprint-only", NCCL_IB_DISABLE="1", **env)
        self.assertEqual(rdma.returncode, 0, rdma.stderr)
        self.assertEqual(tcp.returncode, 0, tcp.stderr)
        self.assertRegex(rdma.stdout.strip(), r"^[0-9a-f]{64}$")
        self.assertNotEqual(rdma.stdout, tcp.stdout)
        self.assertFalse(self.calls.exists())

    def test_failed_microbench_keeps_diagnostics_without_publishing_csv(self):
        self.command("docker", '''case "$*" in *'pgrep '*) exit 1;; esac
          echo 'Ray startup diagnostic'
          echo 'NCCL failure diagnostic' >&2
          exit 3''')
        for script in ("ray_allreduce_bench.sh", "interconnect_extra_bench.sh"):
            out = self.root / script
            result = self.run_script(script, CLUSTER_RUNTIME_FINGERPRINT="a" * 64,
                                     CAMPAIGN_LOCK_FILE=str(self.root / "campaign.lock"),
                                     OUT_DIR=str(out))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(out.glob("*.csv")), [])
            raw, = out.glob("*_raw.log")
            self.assertIn("Ray startup diagnostic", raw.read_text())
            diag, = out.glob("*_nccl.log")
            self.assertIn("NCCL failure diagnostic", diag.read_text())


if __name__ == "__main__":
    unittest.main()
