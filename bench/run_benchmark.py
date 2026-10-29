"""Run one measurement point: warmup, then a steady measurement window."""
from __future__ import annotations
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
THERMAL_CAPTURE_STOP_TIMEOUT_SECONDS: float = 15.0
THERMAL_CAPTURE_KILL_TIMEOUT_SECONDS: float = 5.0
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.campaign_output import (
    next_available_campaign_dir,
    resolve_campaign_dir,
    resolve_campaign_results_dir,
)

from .config import ExperimentConfig
from .workload import Workload
from .runner import run_phase
from .monitors import PowerSampler, VLLMMetricsScraper, WorkerPowerSampler
from .metrics import aggregate, temperature_summary
from . import server_info  # Keep the existing deployment collector patch point.
from .hf_cache import configure_hf_cache
from .artifacts import (
    INDEX_FIELDS,
    append_index as _append_index,
    artifact_metadata as _artifact_metadata,
    atomic_write as _atomic_write,
    parse_expected_concurrencies as _parse_expected_concurrencies,
    validate_index_schema,
)
from .provenance import (
    METHODOLOGY_VERSION,
    client_environment,
    collect_deployment_info,
    config_file_fingerprint,
    deployment_fingerprint,
    deployment_state,
    experiment_fingerprint as _experiment_fingerprint,
    git_commit as _git_commit,
    sha256_json as _sha256_json,
    source_fingerprint as _source_fingerprint,
)
from .release_contracts import (
    load_serving_policy as _load_serving_policy,
    validate_observability as _validate_observability,
    validate_provenance as _validate_provenance,
    validate_result_quality as _validate_result_quality,
)


def _start_thermal_capture(
    output_path: Path,
    *,
    head_node_ip: str | None,
    worker_node_ip: str | None,
    worker_ssh_target: str | None,
) -> subprocess.Popen[str] | None:
    """Start the timestamped host ``sensors`` output capture process."""
    command = [
        sys.executable,
        str(ROOT / "scripts" / "capture_thermals.py"),
        "--output",
        str(output_path),
        "--head-ip",
        head_node_ip or "",
    ]
    if worker_node_ip:
        command.extend(("--worker-node-ip", worker_node_ip))
    if worker_ssh_target:
        command.extend(("--worker-ssh-target", worker_ssh_target))
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            text=True,
        )
    except OSError as error:
        print(f"[bench] WARNING: could not start sensors capture: {error}")
        return None


def _stop_thermal_capture(process: subprocess.Popen[str] | None) -> None:
    """Stop the sensor capture subprocess without changing benchmark status."""
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=THERMAL_CAPTURE_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=THERMAL_CAPTURE_KILL_TIMEOUT_SECONDS)


def _capture_client_environment() -> tuple[dict[str, object], str]:
    """Capture one environment object and hash those exact contents."""
    environment = client_environment()
    return environment, _sha256_json(environment)


def client_environment_fingerprint() -> str:
    """Hash the current client environment captured by this runner."""
    return _capture_client_environment()[1]


def _preflight_observability(
    power: PowerSampler,
    worker_power: WorkerPowerSampler | None,
    vmetrics: VLLMMetricsScraper | None,
    cfg: ExperimentConfig,
) -> None:
    """Fail in seconds when a required sampler is dead or misidentified."""
    if cfg.require_power_data:
        if not power.wait_for_samples(minimum=2, timeout_s=10.0):
            raise RuntimeError(
                f"power preflight failed for backend {cfg.power_backend!r}: "
                "fewer than two live samples"
            )
        if cfg.config_label in {"tp2", "pp2"}:
            if worker_power is None:
                raise RuntimeError("dual-node power preflight has no worker sampler")
            if not worker_power.wait_for_identity(timeout_s=10.0):
                raise RuntimeError(
                    "worker power preflight did not receive a Ray node identity"
                )
            if worker_power.identity_ip != cfg.worker_power_node_ip:
                raise RuntimeError(
                    "worker power sampler ran on the wrong Ray node: expected "
                    f"{cfg.worker_power_node_ip}, got {worker_power.identity_ip}"
                )
            if (cfg.head_node_ip
                    and worker_power.identity_ip == cfg.head_node_ip):
                raise RuntimeError(
                    "worker power sampler resolved to the head node; refusing "
                    "to double-count head power"
                )
            if not worker_power.wait_for_samples(minimum=2, timeout_s=10.0):
                raise RuntimeError(
                    "worker power preflight returned fewer than two live samples"
                )
    if cfg.require_vllm_metrics_data:
        if vmetrics is None or not vmetrics.wait_for_release_metrics(10.0):
            raise RuntimeError(
                "vLLM metrics preflight did not expose every required metric"
            )


def _validate_monitor_config(cfg: ExperimentConfig) -> None:
    """Reject impossible required-monitor policies before starting a phase."""
    if cfg.require_power_data and cfg.power_backend == "none":
        raise RuntimeError("require_power_data is true but power_backend is none")
    if cfg.require_vllm_metrics_data and not cfg.scrape_vllm_metrics:
        raise RuntimeError(
            "require_vllm_metrics_data is true but scraping is disabled"
        )
    if cfg.config_label in {"tp2", "pp2"} and cfg.require_power_data:
        if not cfg.head_node_ip:
            raise RuntimeError("dual-node release power requires head_node_ip")
        if cfg.worker_power_node_ip == cfg.head_node_ip:
            raise RuntimeError(
                "worker_power_node_ip must be distinct from head_node_ip"
            )


def _load_tokenizer(cfg: ExperimentConfig) -> object | None:
    """Load the configured tokenizer when tokenization is enabled."""
    if not cfg.tokenizer:
        return None
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
    from transformers import AutoTokenizer
    kwargs = {"trust_remote_code": cfg.trust_remote_code}
    if cfg.tokenizer_revision:
        kwargs["revision"] = cfg.tokenizer_revision
    return AutoTokenizer.from_pretrained(cfg.tokenizer, **kwargs)


def _resolve_model(cfg: ExperimentConfig) -> str:
    """Use the configured model or query the serving endpoint for its name."""
    if cfg.model:
        return cfg.model
    url = cfg.base_url.rstrip("/") + "/v1/models"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())["data"][0]["id"]


def _model_slug(model: str) -> str:
    """Short filesystem/plot-friendly slug from a served model name.

    e.g. "Qwen/Qwen3-8B" -> "qwen3-8b", "meta-llama/Llama-3.1-70B-Instruct"
    -> "llama-3.1-70b-instruct".
    """
    base = (model or "").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9._-]", "-", base)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("._-") or "model"


def _json_safe(obj: Any) -> Any:
    """Replace NaN/Inf with None so the output files are strict JSON.

    json.dump writes bare ``NaN``, which Python reads back but jq, JavaScript,
    and most non-Python parsers reject; empty percentile sets and failed
    requests would otherwise poison every consumer of the release artifacts.
    """
    # Artifacts contain heterogeneous nested JSON values of unknown depth.
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def main() -> None:
    """Run one benchmark point from command-line arguments."""
    ap = argparse.ArgumentParser(description="LLM serving benchmark for DGX Spark")
    ap.add_argument("--config", help="YAML config path")
    ap.add_argument("--config-label")
    ap.add_argument("--model-label", help="short model slug for tags/plots; "
                    "defaults to a slug of the served model name")
    ap.add_argument("--server-log", help="path to a captured vLLM stdout log for "
                    "exact capacity figures (max concurrency, gpu blocks)")
    ap.add_argument("--collect-hf-metadata", action="store_true", default=None,
                    help="query Hugging Face for model config/checkpoint metadata "
                    "when server logs do not provide capacity facts")
    ap.add_argument("--no-hf-metadata", action="store_false",
                    dest="collect_hf_metadata",
                    help="skip Hugging Face metadata lookups after measurement")
    ap.add_argument("--model", help="served model name; overrides config and auto-resolve")
    ap.add_argument("--model-revision", help="pinned Hugging Face model commit")
    ap.add_argument("--tokenizer", help="HF tokenizer id or path; empty string disables")
    ap.add_argument("--tokenizer-revision", help="pinned tokenizer commit")
    ap.add_argument("--serving-fingerprint",
                    help="hash of the server image and launch policy")
    ap.add_argument(
        "--serving-policy-file",
        help="immutable canonical launch-policy JSON (required for release runs)",
    )
    ap.add_argument("--deployment-fingerprint",
                    help="expected hash of the live server runtime/capacity")
    ap.add_argument("--repeat-id", type=int,
                    help="one-based repeated measurement-window number")
    ap.add_argument("--repeat-count", type=int,
                    help="intended total repeated windows for this campaign")
    ap.add_argument("--trust-remote-code", action="store_true", default=None,
                    help="pass trust_remote_code=True to the tokenizer and HF "
                    "config loads (models that ship custom code, e.g. Nemotron)")
    ap.add_argument("--mode", choices=["closed", "open"])
    ap.add_argument("--concurrency", type=int)
    ap.add_argument("--expected-concurrencies",
                    help="space-separated intended closed-loop load rectangle")
    ap.add_argument("--qps", type=float)
    ap.add_argument("--measure-s", type=float)
    ap.add_argument("--warmup-s", type=float)
    ap.add_argument("--cooldown-s", type=float)
    ap.add_argument("--base-url")
    ap.add_argument("--out-dir")
    ap.add_argument("--power-backend",
                    choices=["none", "nvidia_smi", "sysfs", "tegrastats"])
    ap.add_argument("--worker-power-container",
                    help="docker container holding the Ray cluster; with "
                    "--worker-power-node-ip, samples the worker node's GPU "
                    "power so dual-node energy covers both nodes")
    ap.add_argument("--worker-power-node-ip",
                    help="Ray node IP of the worker to sample (e.g. 192.0.2.11)")
    ap.add_argument("--head-node-ip",
                    help="Ray/head node IP; required with release-grade dual "
                    "power sampling to prove the worker is distinct")
    args = ap.parse_args()

    cfg = ExperimentConfig.from_yaml(args.config) if args.config else ExperimentConfig()
    if args.config_label:
        cfg.config_label = args.config_label
    if args.model is not None:
        cfg.model = args.model
    if args.model_revision is not None:
        cfg.model_revision = args.model_revision
    if args.tokenizer is not None:
        cfg.tokenizer = args.tokenizer
    if args.tokenizer_revision is not None:
        cfg.tokenizer_revision = args.tokenizer_revision
    if args.serving_fingerprint is not None:
        cfg.serving_fingerprint = args.serving_fingerprint
    if args.deployment_fingerprint is not None:
        cfg.deployment_fingerprint = args.deployment_fingerprint
    if args.repeat_id is not None:
        cfg.repeat_id = args.repeat_id
    if args.repeat_count is not None:
        cfg.repeat_count = args.repeat_count
    if args.trust_remote_code:
        cfg.trust_remote_code = True
    if args.mode:
        cfg.mode = args.mode
    if args.concurrency is not None:
        cfg.concurrency = args.concurrency
    if args.expected_concurrencies is not None:
        cfg.expected_concurrencies = args.expected_concurrencies
    if args.qps is not None:
        cfg.qps = args.qps
    if args.measure_s is not None:
        cfg.measure_s = args.measure_s
    if args.warmup_s is not None:
        cfg.warmup_s = args.warmup_s
    if args.cooldown_s is not None:
        cfg.cooldown_s = args.cooldown_s
    if args.base_url:
        cfg.base_url = args.base_url
    if args.out_dir is not None:
        campaign_dir = resolve_campaign_dir(args.out_dir)
    elif os.environ.get("OUT_DIR"):
        campaign_dir = resolve_campaign_dir(os.environ["OUT_DIR"])
    else:
        campaign_dir = next_available_campaign_dir()
    cfg.out_dir = str(resolve_campaign_results_dir(campaign_dir))
    if args.power_backend:
        cfg.power_backend = args.power_backend
    if args.worker_power_container:
        cfg.worker_power_container = args.worker_power_container
    if args.worker_power_node_ip:
        cfg.worker_power_node_ip = args.worker_power_node_ip
    if args.head_node_ip:
        cfg.head_node_ip = args.head_node_ip
    if args.model_label is not None:
        cfg.model_label = args.model_label
    if args.server_log is not None:
        cfg.server_log = args.server_log
    if args.collect_hf_metadata is not None:
        cfg.collect_hf_metadata = args.collect_hf_metadata

    if cfg.mode == "closed":
        expected_loads = _parse_expected_concurrencies(
            cfg.expected_concurrencies or str(cfg.concurrency),
            "benchmark config",
        )
        if cfg.concurrency not in expected_loads:
            raise SystemExit(
                f"--concurrency {cfg.concurrency} is not present in "
                "--expected-concurrencies"
            )
        cfg.expected_concurrencies = " ".join(map(str, expected_loads))
    elif cfg.expected_concurrencies:
        raise SystemExit("--expected-concurrencies is only valid in closed mode")
    if cfg.repeat_id < 1:
        raise SystemExit("--repeat-id must be at least 1")
    if cfg.repeat_count < 1:
        raise SystemExit("--repeat-count must be at least 1")
    if cfg.repeat_id > cfg.repeat_count:
        raise SystemExit("--repeat-id cannot exceed --repeat-count")
    cfg.validate()
    _validate_monitor_config(cfg)
    if (cfg.require_power_data and cfg.config_label in {"tp2", "pp2"}
            and not (cfg.worker_power_container and cfg.worker_power_node_ip)):
        raise SystemExit(
            "dual-node require_power_data needs both worker power arguments"
        )
    os.makedirs(cfg.out_dir, exist_ok=True)
    validate_index_schema(cfg.out_dir)
    config_file_sha256 = config_file_fingerprint(
        args.config,
        {
            "measure_s": args.measure_s,
            "warmup_s": args.warmup_s,
            "cooldown_s": args.cooldown_s,
            "collect_hf_metadata": args.collect_hf_metadata,
            "power_backend": args.power_backend,
        },
    )
    harness_source_sha256 = _source_fingerprint()
    harness_git_commit = _git_commit()

    cfg.model = _resolve_model(cfg)
    if (not cfg.tokenizer_revision and cfg.model_revision
            and cfg.tokenizer == cfg.model):
        cfg.tokenizer_revision = cfg.model_revision
    _validate_provenance(cfg)
    serving_policy, serving_policy_artifact = _load_serving_policy(
        args.serving_policy_file, cfg
    )
    if cfg.tokenizer and cfg.use_token_ids and cfg.tokenizer != cfg.model:
        print(f"[bench] WARNING: tokenizer '{cfg.tokenizer}' differs from the "
              f"served model '{cfg.model}'. Token-id prompts are only exact "
              "with the served model's own tokenizer; a mismatched vocabulary "
              "gives wrong input lengths or request errors. Pass --tokenizer "
              "matching the model unless this is intentional (e.g. a local "
              "copy of the same tokenizer).")
    # A model slug feeds the index/plots. Only an explicit label prefixes the run
    # tag (multi-model sweeps set one); the single-model workflow keeps its tags.
    explicit_label = bool(cfg.model_label)
    if not cfg.model_label:
        cfg.model_label = _model_slug(cfg.model)
    # Explicit labels were checked before any output-directory work. Validate
    # the derived model slug too, immediately before it can enter a run tag.
    cfg.validate()
    captured_client_environment, client_environment_sha256 = (
        _capture_client_environment()
    )
    # Before the first transformers/huggingface_hub import reads the HF env.
    if cfg.tokenizer or cfg.collect_hf_metadata:
        configure_hf_cache()
    workload = Workload(cfg, _load_tokenizer(cfg))

    point = cfg.concurrency if cfg.mode == "closed" else cfg.qps
    prefix = f"{cfg.model_label}_" if explicit_label else ""
    now_ns = time.time_ns()
    timestamp = (
        time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ns // 1_000_000_000))
        + f"-{now_ns % 1_000_000_000:09d}"
    )
    tag = (f"{prefix}{cfg.config_label}_{cfg.mode}_{point}_r{cfg.repeat_id}_"
           f"{timestamp}")
    print(f"[bench] model={cfg.model} config={cfg.config_label} "
          f"mode={cfg.mode} point={point}")

    power = PowerSampler(cfg.power_backend, cfg.power_interval_ms,
                         cfg.power_sysfs_path, cfg.power_rails)
    wpower = None
    if (cfg.power_backend != "none" and cfg.worker_power_container
            and cfg.worker_power_node_ip):
        wpower = WorkerPowerSampler(cfg.worker_power_container,
                                    cfg.worker_power_node_ip)
    vmet = VLLMMetricsScraper(cfg.base_url) if cfg.scrape_vllm_metrics else None
    thermal_name = f"{tag}.thermal.log"
    thermal_path = Path(cfg.out_dir) / thermal_name
    dual_node_run = cfg.config_label in {"tp2", "pp2"}
    thermal_capture = _start_thermal_capture(
        thermal_path,
        head_node_ip=cfg.head_node_ip or os.environ.get("HEAD_IP"),
        worker_node_ip=(
            cfg.worker_power_node_ip or os.environ.get("WORKER_IP")
            if dual_node_run else None
        ),
        worker_ssh_target=(
            os.environ.get("WORKER_SSH_TARGET") if dual_node_run else None
        ),
    )
    # One continuous phase. Closed-loop warmup is automatically extended until
    # every worker's initial request has completed. Cooldown is a minimum: load
    # remains at its configured level until every measurement request finishes,
    # then the non-measurement replacements drain.
    minimum_s = cfg.warmup_s + cfg.measure_s + cfg.cooldown_s
    print(f"[bench] continuous phase: minimum warmup {cfg.warmup_s:g}s + "
          f"measure {cfg.measure_s:g}s + minimum cooldown {cfg.cooldown_s:g}s "
          f"= {minimum_s:g}s, with automatic steady-state extensions")
    if cfg.min_window_requests:
        print(f"[bench] window extends in {cfg.window_extension_step_s:g}s steps "
              f"until {cfg.min_window_requests} requests have started in it "
              f"(cap {cfg.max_measure_s:g}s)")
    try:
        power.start()
        if wpower:
            wpower.start()
        if vmet:
            vmet.start()
            # Seed a baseline before window_start; the background thread interval
            # is longer than some smoke-test windows.
            vmet.scrape_once()
        _preflight_observability(power, wpower, vmet, cfg)
        wall_anchor = time.time()
        perf_anchor = time.perf_counter()
        records, phase = asyncio.run(run_phase(cfg, workload))
    finally:
        # Sampler subprocesses must not survive a failed/aborted phase and
        # contaminate the next point.
        power.stop()
        if wpower:
            wpower.stop()
        if vmet:
            vmet.stop()
        _stop_thermal_capture(thermal_capture)
    window_start = phase["window_start"]
    window_end = phase["window_end"]
    actual_warmup_s = window_start - phase["phase_start"]
    warmup_extension_s = max(0.0, actual_warmup_s - cfg.warmup_s)
    load_extension_s = max(0.0, phase["load_stop"] - phase["nominal_stop"])
    drain_s = max(0.0, phase["actual_end"] - phase["load_stop"])
    window_start_unix = wall_anchor + (window_start - perf_anchor)

    if wpower:
        if wpower.sample_count() == 0:
            print("[bench] WARNING: worker power sampler returned no samples; "
                  "this row's energy covers the head node only "
                  "(power_nodes=1). Check that Ray is up in "
                  f"{cfg.worker_power_container} and node "
                  f"{cfg.worker_power_node_ip} is alive.")
    # Re-collect the serving identity after the window. Required capacity fields
    # are retried and must still match the launch-time deployment fingerprint.
    info = collect_deployment_info(
        cfg.base_url, cfg.model, cfg.server_log,
        compute_from_hf=cfg.collect_hf_metadata,
        trust_remote_code=cfg.trust_remote_code,
    )
    actual_deployment_fingerprint = deployment_fingerprint(
        cfg.serving_fingerprint, info
    )
    if (cfg.deployment_fingerprint
            and cfg.deployment_fingerprint != actual_deployment_fingerprint):
        raise RuntimeError(
            "live server state changed after resume scheduling: expected "
            f"deployment {cfg.deployment_fingerprint}, observed "
            f"{actual_deployment_fingerprint}"
        )

    summary = aggregate(records, cfg, window_start, window_end,
                        power if cfg.power_backend != "none" else None, vmet,
                        worker_power=wpower)
    summary["telemetry_schema_version"] = 2
    summary["temperature"] = temperature_summary(
        power,
        wpower,
        window_start,
        window_end,
        expected_nodes=2 if cfg.config_label in {"tp2", "pp2"} else 1,
        head_node_ip=cfg.head_node_ip or os.environ.get("HEAD_IP") or None,
        worker_node_ip=(wpower.identity_ip if wpower and wpower.identity_ip
                        else cfg.worker_power_node_ip or None),
        worker_ray_node_id=(wpower.identity_node_id if wpower and wpower.identity_node_id
                            else None),
    )
    _validate_observability(summary, cfg)
    _validate_result_quality(summary, cfg)
    summary["run_tag"] = tag
    summary["methodology_version"] = METHODOLOGY_VERSION
    summary["config_file_sha256"] = config_file_sha256
    summary["trace_sha256"] = cfg.trace_sha256
    summary["harness_source_sha256"] = harness_source_sha256
    summary["client_environment_sha256"] = client_environment_sha256
    summary["client_environment"] = captured_client_environment
    summary["harness_git_commit"] = harness_git_commit
    summary["serving_fingerprint"] = cfg.serving_fingerprint
    if serving_policy is not None:
        summary["serving_policy"] = serving_policy
    summary["deployment_fingerprint"] = actual_deployment_fingerprint
    summary["repeat_id"] = cfg.repeat_id
    summary["repeat_count"] = cfg.repeat_count
    summary["expected_concurrencies"] = cfg.expected_concurrencies
    summary["model"] = cfg.model
    summary["model_label"] = cfg.model_label
    summary["warmup_s"] = actual_warmup_s
    summary["configured_warmup_s"] = cfg.warmup_s
    summary["warmup_extension_s"] = warmup_extension_s
    # aggregate() already set measure_s to the actual window length.
    summary["configured_measure_s"] = cfg.measure_s
    summary["measure_extension_s"] = max(0.0, summary["measure_s"] - cfg.measure_s)
    summary["window_extensions"] = phase.get("window_extensions", 0)
    summary["min_window_requests"] = cfg.min_window_requests
    summary["cooldown_s"] = cfg.cooldown_s
    summary["load_extension_s"] = load_extension_s
    # Wall-clock anchor for the perf_counter window bounds, so raw.jsonl can be
    # re-aggregated post hoc and cross-referenced with server logs.
    summary["window_start_unix"] = window_start_unix
    summary["server_info"] = info
    if wpower:
        summary["worker_power_identity"] = {
            "requested_ip": cfg.worker_power_node_ip,
            "observed_ip": wpower.identity_ip,
            "ray_node_id": wpower.identity_node_id,
        }
    summary["config"] = cfg.to_dict()
    summary["drain_s"] = drain_s
    summary["experiment_fingerprint"] = _experiment_fingerprint(
        summary["config"], harness_source_sha256
    )
    # Build every artifact in memory, then fsync+rename it before the index row
    # can make the point resumable.
    raw_name = f"{tag}.raw.jsonl"
    summary_name = f"{tag}.summary.json"
    raw_data = "".join(
        json.dumps(_json_safe(record.to_json()), sort_keys=True) + "\n"
        for record in records
    ).encode()
    artifacts = {"raw": _artifact_metadata(raw_name, raw_data)}
    if thermal_path.is_file():
        artifacts["thermal_samples"] = _artifact_metadata(
            thermal_name, thermal_path.read_bytes()
        )
    if serving_policy_artifact is not None:
        artifacts["serving_policy"] = serving_policy_artifact

    power_data = b""
    if cfg.power_backend != "none":
        power_name = f"{tag}.power.jsonl"
        head_node_ip = cfg.head_node_ip or os.environ.get("HEAD_IP") or None
        power_rows = [
            {"source": "head", "node_ip": head_node_ip,
             "telemetry_schema_version": 2, **record}
            for record in power.sample_records()
        ]
        if wpower:
            power_rows.extend(
                {"source": "worker", "node_ip": wpower.identity_ip,
                 "ray_node_id": wpower.identity_node_id,
                 "telemetry_schema_version": 2, **record}
                for record in wpower.sample_records()
            )
        power_rows.sort(key=lambda row: (
            row["t_perf"], row["source"], row.get("gpu_index") or ""
        ))
        power_data = "".join(
            json.dumps(_json_safe(row), sort_keys=True) + "\n"
            for row in power_rows
        ).encode()
        artifacts["power_samples"] = _artifact_metadata(power_name, power_data)

    vllm_data = b""
    if vmet is not None:
        vllm_name = f"{tag}.vllm_metrics.jsonl"
        metric_rows = []
        for metric, payload in vmet.raw_samples().items():
            for t, value in payload["samples"]:
                metric_rows.append({
                    "metric": metric,
                    "labels": payload["labels"],
                    "t_perf": t,
                    "value": value,
                })
        metric_rows.sort(key=lambda row: (row["t_perf"], row["metric"]))
        vllm_data = "".join(
            json.dumps(_json_safe(row), sort_keys=True) + "\n"
            for row in metric_rows
        ).encode()
        artifacts["vllm_samples"] = _artifact_metadata(vllm_name, vllm_data)

    summary["artifacts"] = artifacts
    safe_summary = _json_safe(summary)
    summary_data = (
        json.dumps(safe_summary, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    summary_artifact = _artifact_metadata(summary_name, summary_data)

    _atomic_write(os.path.join(cfg.out_dir, raw_name), raw_data)
    if "power_samples" in artifacts:
        _atomic_write(
            os.path.join(cfg.out_dir, artifacts["power_samples"]["path"]),
            power_data,
        )
    if "vllm_samples" in artifacts:
        _atomic_write(
            os.path.join(cfg.out_dir, artifacts["vllm_samples"]["path"]),
            vllm_data,
        )
    _atomic_write(os.path.join(cfg.out_dir, summary_name), summary_data)

    # The summary cannot contain its own digest without becoming circular; add
    # that final metadata only to the in-memory index representation.
    safe_summary["artifacts"]["summary"] = summary_artifact
    _append_index(cfg.out_dir, safe_summary)

    print(f"[bench] tok_thru={summary['output_token_throughput_tps']:.1f} tok/s  "
          f"ttft_p50={summary['ttft_s']['p50'] * 1000:.0f}ms  "
          f"itl_p50={summary['itl_s']['p50'] * 1000:.1f}ms  "
          f"goodput={summary['goodput_rps']:.2f} rps")


if __name__ == "__main__":
    main()
