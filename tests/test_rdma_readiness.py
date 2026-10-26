from __future__ import annotations

import importlib.util
import io
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import time
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
<<<<<<< HEAD
                        CALL_LOG=str(self.calls))
=======
                        RESOURCE_PROC_ROOT=str(self.root / "proc"),
                        CALL_LOG=str(self.calls))
        (self.root / "proc").mkdir()
>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2
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

    def _start_script(
        self: ConnectXPreflightTests,
        script_name: str,
        *arguments: str,
        **environment: str,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["bash", str(ROOT / "scripts" / script_name), *arguments],
            cwd=ROOT,
            env=dict(self.env, **environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _wait_for_file(
        self: ConnectXPreflightTests,
        path: Path,
        process: subprocess.Popen[str],
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if path.is_file():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"script exited before creating {path.name}:\n{stdout}{stderr}"
                )
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path.name}")

    def _wait_for_log_line(
        self: ConnectXPreflightTests,
        path: Path,
        previous_line_count: int,
        process: subprocess.Popen[str],
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if path.is_file() and len(path.read_text().splitlines()) > previous_line_count:
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"script exited before writing to {path.name}:\n{stdout}{stderr}"
                )
            time.sleep(0.01)
        self.fail(f"timed out waiting for a new line in {path.name}")

    def _wait_for_resource_lock_release(
        self: ConnectXPreflightTests,
        lock_path: Path,
        process: subprocess.Popen[str],
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            lock_probe = subprocess.run(
                ["flock", "-n", str(lock_path), "true"],
                capture_output=True,
                text=True,
                check=False,
            )
            if lock_probe.returncode == 0:
                return
            if lock_probe.returncode != 1:
                self.fail(f"flock probe failed: {lock_probe.stderr}")
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"script exited while holding its resource lock:\n{stdout}{stderr}"
                )
            time.sleep(0.01)
        self.fail(f"resource lock remained held at {lock_path}")

    def _assert_resource_lock_held(
        self: ConnectXPreflightTests,
        lock_path: Path,
    ) -> None:
        lock_probe = subprocess.run(
            ["flock", "-n", str(lock_path), "true"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            lock_probe.returncode,
            1,
            f"expected {lock_path} to remain locked; {lock_probe.stderr}",
        )

    def _configure_launch_fixture(
        self: ConnectXPreflightTests,
        *,
        server_becomes_ready: bool = True,
    ) -> tuple[Path, Path, Path, Path, Path, Path]:
        server_started = self.root / "mock-server-started"
        allow_api_ready = self.root / "mock-allow-api-ready"
        api_ready = self.root / "mock-api-ready"
        api_probed = self.root / "mock-api-probed"
        server_stop = self.root / "mock-server-stop"
        api_responses = self.root / "mock-api-responses"

        class ReadinessHandler(BaseHTTPRequestHandler):
            def do_GET(self: BaseHTTPRequestHandler) -> None:
                if self.path != "/v1/models":
                    status = 404
                elif not api_ready.is_file():
                    status = 503
                else:
                    status = 200
                    api_probed.touch()
                with api_responses.open("a", encoding="utf-8") as response_log:
                    response_log.write(f"{status} GET {self.path}\n")
                body = b'{"object":"list","data":[]}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(
                self: BaseHTTPRequestHandler,
                format_string: str,
                *args: object,
            ) -> None:
                del format_string, args

        readiness_server = ThreadingHTTPServer(("127.0.0.1", 0), ReadinessHandler)
        readiness_thread = Thread(target=readiness_server.serve_forever, daemon=True)
        readiness_thread.start()
        self.addCleanup(readiness_server.server_close)
        self.addCleanup(readiness_thread.join)
        self.addCleanup(readiness_server.shutdown)
        base_url = f"http://127.0.0.1:{readiness_server.server_address[1]}"

        self.env.update(
            BASE_URL=base_url,
            CAMPAIGN_LOCK_FILE=str(self.root / "resource.lock"),
            LAUNCH_READY_TIMEOUT="1800",
            MOCK_ALLOW_API_READY_FILE=str(allow_api_ready),
            MOCK_MEMORY_GIB="100.0",
            MOCK_SERVER_BECOMES_READY="1" if server_becomes_ready else "0",
            MOCK_API_PROBED_FILE=str(api_probed),
            MOCK_SERVER_STARTED_FILE=str(server_started),
            MOCK_SERVER_STOP_FILE=str(server_stop),
            MOCK_API_READY_FILE=str(api_ready),
        )
        self.command("sleep", "exec /bin/sleep 0.01")
        self.command("python3", '''if [[ "$1" == "-" ]]; then
          script="$(cat)"
          if [[ "$script" == *"MemAvailable"* ]]; then
            memory_gib="$MOCK_MEMORY_GIB"
            needed_gib="$(awk -v util="$GPU_UTIL" -v memory="$memory_gib" \
              'BEGIN {printf "%.1f", util * memory}')"
            printf '[launch]   free %s (available %s) of %s GiB, need %s GiB: ok\\n' \
              "$memory_gib" "$memory_gib" "$memory_gib" "$needed_gib"
            exit 0
          fi
          printf '%s\\n' "$script" | /usr/bin/python3 "$@"
          exit $?
        fi
        exec /usr/bin/python3 "$@"''')
        self.command("docker", '''case "$*" in
          *'vllm serve'*)
            printf '%s\\n' "$@" > "$CALL_LOG"
            rm -f "$MOCK_API_READY_FILE"
            rm -f "$MOCK_API_PROBED_FILE"
            : > "$MOCK_SERVER_STARTED_FILE"
            if [[ "$MOCK_SERVER_BECOMES_READY" != "1" ]]; then exit 0; fi
            while [[ ! -e "$MOCK_ALLOW_API_READY_FILE" ]]; do /bin/sleep 0.01; done
            : > "$MOCK_API_READY_FILE"
            while [[ ! -e "$MOCK_API_PROBED_FILE" || ! -e "$MOCK_SERVER_STOP_FILE" ]]; do
              /bin/sleep 0.01
            done
            ;;
          'ps --format {{.Names}}') printf '%s\\n' "$VLLM_CONTAINER";;
          'top '*) printf 'PID COMMAND\\n';;
          'inspect -f {{.State.Running}} '*) echo true;;
          *'ray status'*) printf 'Active:\\n';;
          *'--worker'*) printf '[resources] active Ray nodes are idle: %s, %s\\n' \
            "$HEAD_IP" "$HOEDUS_IP";;
          *'EXPECTED_HEAD='*) printf '[launch] expected two Ray GPU nodes present: %s, %s\\n' \
            "$HEAD_IP" "$HOEDUS_IP";;
          *'MODEL='*) printf '[launch]   node %s: pinned cache OK\\n' \
            "$HEAD_IP"
            printf '[launch]   node %s: pinned cache OK\\n' "$HOEDUS_IP";;
          *'GPU_UTIL='*)
            for argument in "$@"; do
              if [[ "$argument" == GPU_UTIL=* ]]; then
                gpu_util="${argument#GPU_UTIL=}"
              fi
            done
            [[ -n "${gpu_util:-}" ]] || exit 99
            needed_gib="$(awk -v util="$gpu_util" -v memory="$MOCK_MEMORY_GIB" \
              'BEGIN {printf "%.1f", util * memory}')"
            printf '[launch]   node %s: free %s (available %s) of %s GiB, need %s GiB: ok\\n' \
              "$HEAD_IP" "$MOCK_MEMORY_GIB" "$MOCK_MEMORY_GIB" \
              "$MOCK_MEMORY_GIB" "$needed_gib"
            printf '[launch]   node %s: free %s (available %s) of %s GiB, need %s GiB: ok\\n' \
              "$HOEDUS_IP" "$MOCK_MEMORY_GIB" "$MOCK_MEMORY_GIB" \
              "$MOCK_MEMORY_GIB" "$needed_gib";;
          *'/tmp/cache_access_probe.py'*) exit 0;;
          *) exit 99;;
        esac''')
        return (
            server_started,
            allow_api_ready,
            api_ready,
            api_probed,
            server_stop,
            api_responses,
        )

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
        self.env["CAMPAIGN_LOCK_FILE"] = str(self.root / "resource.lock")
        self.command("docker", '''printf '%s\\n' "$*" >> "$CALL_LOG"
          case "$*" in
            'ps --format {{.Names}}') printf '%s\\n' "$VLLM_CONTAINER";;
            'top '*) printf 'PID COMMAND\\n';;
            'inspect -f {{.State.Running}} '*) echo true;;
            *'ray status'*) printf 'Active:\\n';;
            *'--worker'*) printf '[resources] active Ray nodes are idle: %s, %s\\n' \
              "$HEAD_IP" "$HOEDUS_IP";;
            *) exit 99;;
          esac''')
        result = self.run_script("start_ray.sh", "head", MOCK_MISSING_RAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no ACTIVE/LINK_UP RDMA port mapped to cx1", result.stderr)
        docker_calls = self.calls.read_text().splitlines()
        self.assertIn("ps --format {{.Names}}", docker_calls)
        self.assertIn("inspect -f {{.State.Running}} vllm-spark", docker_calls)
        self.assertIn("top vllm-spark -eo pid,args", docker_calls)
        self.assertTrue(any("ray status" in call for call in docker_calls))
        self.assertTrue(any("--worker" in call for call in docker_calls))
        self.assertFalse(any(call.startswith(("rm ", "run ")) for call in docker_calls))

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
        (
            server_started,
            allow_api_ready,
            api_ready,
            api_probed,
            server_stop,
            api_responses,
        ) = self._configure_launch_fixture()
        for config in ("single", "tp2", "pp2"):
            server_started.unlink(missing_ok=True)
            allow_api_ready.unlink(missing_ok=True)
            api_ready.unlink(missing_ok=True)
            api_probed.unlink(missing_ok=True)
            server_stop.unlink(missing_ok=True)
            response_start = (
                len(api_responses.read_text().splitlines())
                if api_responses.exists()
                else 0
            )
            process = self._start_script(
                "launch_server.sh",
                config,
                MAX_NUM_SEQS="4",
                SINGLE_CONTAINER="ray",
                GPU_UTIL="0.85",
                HF_CACHE=str(self.root / "empty-cache"),
            )
            try:
                self._wait_for_file(server_started, process)
                self._assert_resource_lock_held(
                    Path(self.env["CAMPAIGN_LOCK_FILE"])
                )
                self._wait_for_log_line(api_responses, response_start, process)
                responses_before_ready = api_responses.read_text().splitlines()[
                    response_start:
                ]
                self.assertTrue(
                    any(response.startswith("503 GET ")
                        for response in responses_before_ready),
                    responses_before_ready,
                )
                allow_api_ready.touch()
                self._wait_for_file(api_probed, process)
                self.assertTrue(api_ready.is_file())
                self.assertIsNone(process.poll())
                self._wait_for_resource_lock_release(
                    Path(self.env["CAMPAIGN_LOCK_FILE"]), process
                )
                args = self.calls.read_text().splitlines()
                gpu_memory_index = args.index("--gpu-memory-utilization")
                self.assertEqual(args[gpu_memory_index + 1], "0.85")
                max_num_seqs_index = args.index("--max-num-seqs")
                self.assertEqual(args[max_num_seqs_index + 1], "4")
<<<<<<< HEAD
=======
                # FlashInfer autotune is off by default in every mode; the
                # mock rewrites the call log for each serve command.
                self.assertEqual(args.count("--no-enable-flashinfer-autotune"), 1)
>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2
            finally:
                allow_api_ready.touch()
                server_stop.touch()
                stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertIn("API ready; released the resource lock", stdout)
            new_responses = api_responses.read_text().splitlines()[response_start:]
            self.assertTrue(
                any(response == "200 GET /v1/models"
                    for response in new_responses),
                new_responses,
            )
            self.assertTrue(api_probed.is_file())

    def test_direct_launch_defaults_gpu_memory_fraction_to_80_percent(
        self: ConnectXPreflightTests,
    ) -> None:
        (
            server_started,
            allow_api_ready,
            api_ready,
            api_probed,
            server_stop,
            api_responses,
        ) = self._configure_launch_fixture()
        response_start = 0
        process = self._start_script(
            "launch_server.sh",
            "single",
            SINGLE_CONTAINER="ray",
            GPU_UTIL="",
            HF_CACHE=str(self.root / "empty-cache"),
        )
        try:
            self._wait_for_file(server_started, process)
            self._assert_resource_lock_held(Path(self.env["CAMPAIGN_LOCK_FILE"]))
            self._wait_for_log_line(api_responses, response_start, process)
            self.assertTrue(
                any(
                    response.startswith("503 GET ")
                    for response in api_responses.read_text().splitlines()
                )
            )
            allow_api_ready.touch()
            self._wait_for_file(api_probed, process)
            self.assertIsNone(process.poll())
            self._wait_for_resource_lock_release(
                Path(self.env["CAMPAIGN_LOCK_FILE"]), process
            )
            args = self.calls.read_text().splitlines()
            gpu_memory_index = args.index("--gpu-memory-utilization")
            self.assertEqual(args[gpu_memory_index + 1], "0.80")
        finally:
            allow_api_ready.touch()
            server_stop.touch()
            stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIn("API ready; released the resource lock", stdout)
        self.assertTrue(api_ready.is_file())
        self.assertTrue(api_probed.is_file())
        self.assertTrue(
            any(
                response == "200 GET /v1/models"
                for response in api_responses.read_text().splitlines()
            )
        )

<<<<<<< HEAD
=======
    def test_flashinfer_autotune_opt_in_keeps_vllm_default(
        self: ConnectXPreflightTests,
    ) -> None:
        (
            server_started,
            allow_api_ready,
            _api_ready,
            api_probed,
            server_stop,
            _api_responses,
        ) = self._configure_launch_fixture()
        process = self._start_script(
            "launch_server.sh",
            "pp2",
            FLASHINFER_AUTOTUNE="1",
            HF_CACHE=str(self.root / "empty-cache"),
        )
        try:
            self._wait_for_file(server_started, process)
            allow_api_ready.touch()
            self._wait_for_file(api_probed, process)
            args = self.calls.read_text().splitlines()
            self.assertIn("--pipeline-parallel-size", args)
            self.assertNotIn("--no-enable-flashinfer-autotune", args)
        finally:
            allow_api_ready.touch()
            server_stop.touch()
            stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stdout + stderr)

    def test_flashinfer_autotune_rejects_invalid_values_before_docker(self):
        for value in ("yes", "2", "true"):
            with self.subTest(value=value):
                self.calls.write_text("")
                result = self.run_script(
                    "launch_server.sh", "pp2", FLASHINFER_AUTOTUNE=value
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("FLASHINFER_AUTOTUNE must be 0 or 1", result.stderr)
                self.assertNotIn("docker", self.calls.read_text().split())

>>>>>>> e645017f15a9a2d16ceb6997e594467fbefa20a2
    def test_direct_launch_rejects_server_exit_before_api_readiness(
        self: ConnectXPreflightTests,
    ) -> None:
        (
            _server_started,
            _allow_api_ready,
            api_ready,
            api_probed,
            _server_stop,
            api_responses,
        ) = self._configure_launch_fixture(server_becomes_ready=False)
        result = self.run_script(
            "launch_server.sh",
            "single",
            SINGLE_CONTAINER="ray",
            HF_CACHE=str(self.root / "empty-cache"),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("server command exited before the API became ready", result.stderr)
        self.assertNotIn("API ready; released the resource lock", result.stdout)
        self.assertFalse(api_ready.exists())
        self.assertFalse(api_probed.exists())
        self.assertIn("503 GET /v1/models", api_responses.read_text())

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
        self.command("docker", '''case "$*" in
          *'ps --format '*) exit 0;;
          *'inspect -f '*) echo true;;
          *'python3 - --mode '*) echo '[resources] worker is idle';;
          *'python3 -u - '*) echo 'Ray startup diagnostic'
            echo 'NCCL failure diagnostic' >&2
            exit 3;;
          *) exit 99;; esac''')
        for script in ("ray_allreduce_bench.sh", "interconnect_extra_bench.sh"):
            out = self.root / script
            result = self.run_script(script, CLUSTER_RUNTIME_FINGERPRINT="a" * 64,
                                     CAMPAIGN_LOCK_FILE=str(self.root / "campaign.lock"),
                                     OUT_DIR=str(out))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list((out / "results").glob("*.csv")), [])
            raw_logs = list((out / "logs").glob("*_raw.log"))
            self.assertEqual(len(raw_logs), 1, result.stdout + result.stderr)
            (raw,) = raw_logs
            self.assertIn("Ray startup diagnostic", raw.read_text())
            diagnostic_logs = list((out / "logs").glob("*_nccl.log"))
            self.assertEqual(len(diagnostic_logs), 1, result.stdout + result.stderr)
            (diag,) = diagnostic_logs
            self.assertIn("NCCL failure diagnostic", diag.read_text())


if __name__ == "__main__":
    unittest.main()
