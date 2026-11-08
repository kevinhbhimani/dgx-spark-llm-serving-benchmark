"""Release gates shared by live measurements and campaign verification."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Protocol

from .artifacts import artifact_metadata
from .monitors import VLLMMetricsScraper


class ObservabilityConfig(Protocol):
    """Monitor requirements used by the observability gate."""
    require_power_data: bool
    power_backend: str
    config_label: str
    power_interval_ms: int
    require_vllm_metrics_data: bool
    scrape_vllm_metrics: bool


class ResultQualityConfig(Protocol):
    """Request requirements used by the result-quality gate."""

    require_zero_request_failures: bool
    min_successful_requests: int


class ProvenanceConfig(Protocol):
    """Identity requirements used by the provenance gate."""

    require_provenance: bool
    model_revision: str
    use_token_ids: bool
    tokenizer: str
    tokenizer_revision: str
    serving_fingerprint: str
    deployment_fingerprint: str


class ServingPolicyConfig(Protocol):
    """Expected identity and location for a serving policy file."""

    require_provenance: bool
    out_dir: str
    config_label: str
    model: str
    model_revision: str
    tokenizer: str
    tokenizer_revision: str
    serving_fingerprint: str
    trust_remote_code: bool


# Summaries and policies contain heterogeneous nested JSON objects.
JSONMapping = Mapping[str, Any]
SERVING_V2_FINGERPRINT_FIELDS = (
    "schema",
    "config",
    "image",
    "model",
    "revision",
    "tokenizer",
    "tokenizer_revision",
    "dtype",
    "quant",
    "max_model_len",
    "gpu_util",
    "prefix_caching",
    "generation_config",
    "torchinductor_autograd_cache",
    "hf_hub_offline",
    "trust_remote_code",
    "ray_version",
    "connectx_iface",
    "nccl_ib_disable",
    "head_ip",
    "worker_ip",
    "power_backend",
    "cluster_runtime",
    "runtime_facts_sha256",
    "topology_sha256",
    "head_node_id",
    "worker_node_id",
    "head_cx_iface",
    "worker_cx_iface",
    "nccl_ib_hca",
    "nccl_ib_gid_index",
    "nccl_net_plugin",
    "max_num_seqs",
    "flashinfer_autotune",
)


def serving_policy_fingerprint(policy: Mapping[str, object]) -> str:
    """Hash topology-aware serving fields as canonical JSON."""
    missing = [field for field in SERVING_V2_FINGERPRINT_FIELDS if field not in policy]
    if missing:
        raise RuntimeError(
            "serving-v2 fingerprint input is missing: " + ", ".join(missing)
        )
    fields = {field: policy[field] for field in SERVING_V2_FINGERPRINT_FIELDS}
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_observability(
    summary: JSONMapping, cfg: ObservabilityConfig
) -> None:
    """Reject paper runs whose required monitor data is incomplete."""

    def finite_number(value: object) -> bool:
        return (not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value))

    if cfg.require_power_data:
        if cfg.power_backend == "none":
            raise RuntimeError(
                "require_power_data is true but power_backend is none"
            )
        energy = summary.get("energy") or {}
        expected_nodes = 2 if cfg.config_label in {"tp2", "pp2"} else 1
        if energy.get("power_nodes") != expected_nodes:
            raise RuntimeError(
                f"required power coverage is incomplete: expected "
                f"{expected_nodes} node(s), got {energy.get('power_nodes', 0)}"
            )

        energy_per_token = energy.get("energy_per_token_j")
        if not finite_number(energy_per_token) or energy_per_token <= 0:
            raise RuntimeError(
                "required power samples do not produce positive window energy"
            )

        allowed_power_gap = max(
            5.0, 10.0 * cfg.power_interval_ms / 1000.0
        )
        gaps = [energy.get("power_max_gap_s_head")]
        counts = [energy.get("power_samples_head")]
        node_fields = ["mean_power_w_head"]
        if expected_nodes == 2:
            gaps.append(energy.get("power_max_gap_s_worker"))
            counts.append(energy.get("power_samples_worker"))
            node_fields.append("mean_power_w_worker")

        if any(not finite_number(count) or count < 2 for count in counts):
            raise RuntimeError(
                "required power sampler has fewer than two window samples"
            )
        if any(not finite_number(gap) or gap > allowed_power_gap
               for gap in gaps):
            raise RuntimeError(
                f"required power sampler gap exceeds {allowed_power_gap:g}s"
            )
        for field in node_fields:
            mean_power = energy.get(field)
            if not finite_number(mean_power) or mean_power <= 0:
                raise RuntimeError(
                    f"required power sampler reported invalid "
                    f"{field}={mean_power!r}"
                )

    if cfg.require_vllm_metrics_data:
        if not cfg.scrape_vllm_metrics:
            raise RuntimeError(
                "require_vllm_metrics_data is true but scraping is disabled"
            )
        metrics = summary.get("vllm_metrics") or {}
        allowed_metric_gap = 5.0
        min_span = max(
            0.0, summary.get("measure_s", 0.0) - allowed_metric_gap
        )

        for name, stat in VLLMMetricsScraper.REQUIRED_RELEASE_STATS.items():
            series = metrics.get(name) or {}
            # Custom/test scraper adapters predating series identities are
            # treated as one series; the built-in scraper always reports it.
            if series.get("series_count", 1) != 1:
                raise RuntimeError(
                    f"required vLLM metric {name} has "
                    f"{series.get('series_count', 0)} label series; release "
                    "aggregation requires exactly one"
                )
            value = series.get(stat)
            if not finite_number(value):
                raise RuntimeError(
                    f"required vLLM metric {name}.{stat} is missing or "
                    "non-finite for the measurement window"
                )
            count = series.get("sample_count")
            if not finite_number(count) or count < 2:
                raise RuntimeError(
                    f"required vLLM metric {name} has too few window samples"
                )
            gap = series.get("max_gap_s")
            if not finite_number(gap) or gap > allowed_metric_gap:
                raise RuntimeError(
                    f"required vLLM metric {name} scrape gap exceeds 5s"
                )
            span = series.get("sample_span_s")
            if not finite_number(span) or span < min_span:
                raise RuntimeError(
                    f"required vLLM metric {name} samples do not span the window"
                )

        generation = metrics["vllm:generation_tokens_total"]
        if (summary.get("total_output_tokens", 0) > 0
                and generation.get("delta", 0.0) <= 0):
            raise RuntimeError(
                "client received tokens but the server generation counter "
                "did not advance"
            )


def validate_result_quality(
    summary: JSONMapping, cfg: ResultQualityConfig
) -> None:
    """Reject unusable release measurements before publishing any artifact."""
    started = summary.get("requests_started")
    completed = summary.get("requests_completed")
    failed = summary.get("requests_failed")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (started, completed, failed)):
        raise RuntimeError("request accounting contains invalid counts")
    if started != completed + failed:
        raise RuntimeError(
            "request accounting mismatch: requests_started must equal "
            "requests_completed + requests_failed"
        )
    if not cfg.require_zero_request_failures:
        return
    if failed:
        raise RuntimeError(
            f"release point has {failed} failed request(s); no row was published"
        )
    if summary.get("failed_requests_with_window_tokens", 0):
        raise RuntimeError(
            "release point includes window tokens from failed request(s); "
            "no row was published"
        )
    if completed < cfg.min_successful_requests:
        raise RuntimeError(
            f"release point has {completed} successful request(s), fewer than "
            f"min_successful_requests={cfg.min_successful_requests}"
        )
    if summary.get("total_output_tokens", 0) <= 0:
        raise RuntimeError("release point received no output tokens in the window")


def validate_provenance(cfg: ProvenanceConfig) -> None:
    """Require immutable model/server identity for release measurements."""
    if not cfg.require_provenance:
        return
    commit = re.compile(r"[0-9a-fA-F]{40}")
    digest = re.compile(r"[0-9a-fA-F]{64}")
    problems = []
    if not commit.fullmatch(cfg.model_revision or ""):
        problems.append("model_revision must be an exact 40-hex commit")
    if cfg.use_token_ids:
        if not cfg.tokenizer:
            problems.append("exact token prompts require a tokenizer")
        if not commit.fullmatch(cfg.tokenizer_revision or ""):
            problems.append("tokenizer_revision must be an exact 40-hex commit")
    if not digest.fullmatch(cfg.serving_fingerprint or ""):
        problems.append("serving_fingerprint must be a 64-hex policy hash")
    if (cfg.deployment_fingerprint
            and not digest.fullmatch(cfg.deployment_fingerprint)):
        problems.append("deployment_fingerprint must be a 64-hex live-state hash")
    if problems:
        raise RuntimeError("required provenance is incomplete: " + "; ".join(problems))


def load_serving_policy(
    path: str | None, cfg: ServingPolicyConfig
) -> tuple[dict[str, object] | None, dict[str, str | int] | None]:
    """Load and cross-check the immutable launch policy before measurement."""
    if not path:
        if cfg.require_provenance:
            raise RuntimeError(
                "release provenance requires --serving-policy-file"
            )
        return None, None

    supplied = Path(path)
    source = supplied.resolve()
    output = Path(cfg.out_dir).resolve()
    if source.parent != output:
        raise RuntimeError(
            "serving policy must be a direct child of the benchmark OUT_DIR"
        )
    if supplied.is_symlink() or source.is_symlink() or not source.is_file():
        raise RuntimeError(
            f"serving policy is not a regular, non-symlink file: {source}"
        )
    data = source.read_bytes()
    if not data:
        raise RuntimeError(f"serving policy is empty: {source}")

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        policy = json.loads(data, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"invalid serving policy {source}: {exc}") from exc
    if not isinstance(policy, dict):
        raise RuntimeError(f"serving policy {source} must contain an object")

    legacy_fields = (
        "schema", "config", "image", "model", "revision", "tokenizer",
        "tokenizer_revision", "dtype", "quant", "max_model_len",
        "gpu_util", "prefix_caching", "generation_config",
        "torchinductor_autograd_cache", "hf_hub_offline",
        "trust_remote_code", "ray_version", "connectx_iface",
        "nccl_ib_disable", "head_ip", "worker_ip", "power_backend",
        "runtime_facts", "runtime_facts_sha256", "serving_fingerprint",
    )
    missing = [field for field in legacy_fields if field not in policy]
    if missing:
        raise RuntimeError(
            f"serving policy {source} is missing: " + ", ".join(missing)
        )
    if not isinstance(policy["schema"], str) or policy["schema"] not in {
        "serving-v1", "serving-v2"
    }:
        raise RuntimeError(
            f"unsupported serving policy schema {policy['schema']!r}"
        )
    fingerprint = str(policy["serving_fingerprint"])
    runtime_hash = str(policy["runtime_facts_sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise RuntimeError("serving policy fingerprint is not a SHA-256 digest")
    if not re.fullmatch(r"[0-9a-f]{64}", runtime_hash):
        raise RuntimeError("serving runtime fingerprint is not a SHA-256 digest")
    if not re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", str(policy["image"])):
        raise RuntimeError("serving policy image is not pinned by digest")

    runtime_canonical = json.dumps(
        policy["runtime_facts"], sort_keys=True, separators=(",", ":")
    ).encode()
    if hashlib.sha256(runtime_canonical).hexdigest() != runtime_hash:
        raise RuntimeError("serving policy runtime facts do not match their hash")
    if str(policy.get("cluster_runtime")) != runtime_hash:
        raise RuntimeError("serving policy cluster_runtime hash is inconsistent")

    legacy_input = "".join((
        f"schema={policy['schema']}\n",
        f"config={policy['config']}\n",
        f"image={policy['image']}\n",
        f"model={policy['model']}\nrevision={policy['revision']}\n",
        f"tokenizer={policy['tokenizer']}\n"
        f"tokenizer_revision={policy['tokenizer_revision']}\n",
        f"dtype={policy['dtype']}\nquant={policy['quant']}\n",
        f"max_model_len={policy['max_model_len']}\n"
        f"gpu_util={policy['gpu_util']}\n",
        f"prefix_caching={policy['prefix_caching']}\n"
        f"generation_config={policy['generation_config']}\n",
        "torchinductor_autograd_cache="
        f"{policy['torchinductor_autograd_cache']}\n"
        f"hf_hub_offline={policy['hf_hub_offline']}\n",
        f"trust_remote_code={policy['trust_remote_code']}\n"
        f"ray_version={policy['ray_version']}\n",
        f"connectx_iface={policy['connectx_iface']}\n"
        f"nccl_ib_disable={policy['nccl_ib_disable']}\n",
        f"head_ip={policy['head_ip']}\nworker_ip={policy['worker_ip']}\n"
        f"power_backend={policy['power_backend']}\n",
        f"cluster_runtime={runtime_hash}\n",
    ))
    max_num_seqs = policy.get("max_num_seqs", "")
    if max_num_seqs != "":
        if (not isinstance(max_num_seqs, str)
                or re.fullmatch(r"[1-9][0-9]*", max_num_seqs) is None):
            raise RuntimeError(
                "serving policy max_num_seqs must be a positive integer string"
            )
        legacy_input += f"max_num_seqs={max_num_seqs}\n"
    # Present only when autotuning was disabled; absence is vLLM's default.
    flashinfer_autotune = policy.get("flashinfer_autotune", "")
    if flashinfer_autotune != "":
        allowed_autotune = (
            {"0"} if policy["schema"] == "serving-v1" else {"0", "1"}
        )
        if not isinstance(flashinfer_autotune, str) or flashinfer_autotune not in allowed_autotune:
            raise RuntimeError(
                "serving policy flashinfer_autotune has an unsupported value"
            )
        if policy["schema"] == "serving-v1" and flashinfer_autotune == "0":
            legacy_input += "flashinfer_autotune=0\n"
    if policy["schema"] == "serving-v1" and hashlib.sha256(legacy_input.encode()).hexdigest() != fingerprint:
        raise RuntimeError(
            "serving policy fields do not reproduce serving_fingerprint"
        )
    if policy["schema"] == "serving-v2":
        topology_fields = (
            "topology_sha256", "head_node_id", "worker_node_id",
            "head_cx_iface", "worker_cx_iface", "nccl_ib_hca",
            "nccl_ib_gid_index", "nccl_net_plugin", "max_num_seqs",
            "flashinfer_autotune",
        )
        missing_topology_fields = [
            field for field in topology_fields if field not in policy
        ]
        if missing_topology_fields:
            raise RuntimeError(
                "serving-v2 policy is missing: "
                + ", ".join(missing_topology_fields)
            )
        non_string_fields = [
            field
            for field in SERVING_V2_FINGERPRINT_FIELDS
            if not isinstance(policy.get(field), str)
        ]
        if non_string_fields:
            raise RuntimeError(
                "serving-v2 fingerprint fields must be strings: "
                + ", ".join(non_string_fields)
            )
        if re.fullmatch(r"[0-9a-f]{64}", str(policy["topology_sha256"])) is None:
            raise RuntimeError("serving policy topology digest is invalid")
        if not isinstance(policy["nccl_ib_disable"], str) or policy["nccl_ib_disable"] not in {"0", "1"}:
            raise RuntimeError("serving policy NCCL transport mode is invalid")
        if policy["connectx_iface"] != policy["head_cx_iface"]:
            raise RuntimeError(
                "serving-v2 connectx_iface does not match the head interfaces"
            )
        if serving_policy_fingerprint(policy) != fingerprint:
            raise RuntimeError(
                "serving-v2 fields do not reproduce serving_fingerprint"
            )
        expected_runtime_scope = (
            "single-host" if policy["config"] == "single" else "dual-cluster"
        )
        if policy.get("runtime_scope") != expected_runtime_scope:
            raise RuntimeError(
                f"serving-v2 runtime_scope must be {expected_runtime_scope}"
            )
        if expected_runtime_scope == "single-host":
            runtime_fact = policy["runtime_facts"]
            if not isinstance(runtime_fact, dict):
                raise RuntimeError(
                    "single serving-v2 policy needs one runtime fact object"
                )
            if runtime_fact.get("node_id") != policy["head_node_id"]:
                raise RuntimeError(
                    "single serving-v2 runtime node ID does not match the head"
                )
            if runtime_fact.get("node_ip") != policy["head_ip"]:
                raise RuntimeError(
                    "single serving-v2 runtime IP does not match the head"
                )
            if runtime_fact.get("topology_sha256") != policy["topology_sha256"]:
                raise RuntimeError(
                    "single serving-v2 runtime topology digest does not match"
                )
            expected_interfaces = policy["head_cx_iface"]
            if runtime_fact.get("expected_interfaces") != expected_interfaces:
                raise RuntimeError(
                    "single serving-v2 runtime interfaces do not match the head"
                )
            runtime_environment = runtime_fact.get("runtime_env")
            if not isinstance(runtime_environment, dict):
                raise RuntimeError(
                    "single serving-v2 runtime has no environment facts"
                )
            expected_runtime_environment = {
                "NCCL_SOCKET_IFNAME": expected_interfaces,
                "GLOO_SOCKET_IFNAME": expected_interfaces,
                "NCCL_IB_DISABLE": policy["nccl_ib_disable"],
                "NCCL_IB_HCA": policy["nccl_ib_hca"],
                "NCCL_IB_GID_INDEX": policy["nccl_ib_gid_index"],
                "NCCL_NET_PLUGIN": policy["nccl_net_plugin"],
            }
            for variable, expected_value in expected_runtime_environment.items():
                if runtime_environment.get(variable) != expected_value:
                    raise RuntimeError(
                        f"single serving-v2 {variable} does not match the head"
                    )
        else:
            runtime_facts = policy["runtime_facts"]
            if not isinstance(runtime_facts, list) or len(runtime_facts) != 2:
                raise RuntimeError(
                    "dual serving-v2 policy needs two per-node runtime facts"
                )
            expected_nodes = {
                str(policy["head_node_id"]): (
                    str(policy["head_ip"]), str(policy["head_cx_iface"])
                ),
                str(policy["worker_node_id"]): (
                    str(policy["worker_ip"]), str(policy["worker_cx_iface"])
                ),
            }
            observed_nodes: set[str] = set()
            runtime_environments: dict[str, dict[str, object]] = {}
            for runtime_fact in runtime_facts:
                if not isinstance(runtime_fact, dict):
                    raise RuntimeError("serving-v2 node runtime fact is malformed")
                node_id = str(runtime_fact.get("node_id", ""))
                expected_node = expected_nodes.get(node_id)
                if expected_node is None or node_id in observed_nodes:
                    raise RuntimeError(
                        f"serving-v2 runtime has invalid node ID {node_id!r}"
                    )
                observed_nodes.add(node_id)
                runtime_environment = runtime_fact.get("runtime_env")
                if not isinstance(runtime_environment, dict):
                    raise RuntimeError(
                        f"serving-v2 runtime for {node_id} has no environment facts"
                    )
                runtime_environments[node_id] = runtime_environment
                expected_ip, expected_interfaces = expected_node
                if runtime_fact.get("node_ip") != expected_ip:
                    raise RuntimeError(
                        f"serving-v2 node {node_id} IP does not match topology"
                    )
                if runtime_fact.get("topology_sha256") != policy["topology_sha256"]:
                    raise RuntimeError(
                        f"serving-v2 node {node_id} topology digest does not match"
                    )
                if runtime_fact.get("expected_interfaces") != expected_interfaces:
                    raise RuntimeError(
                        f"serving-v2 node {node_id} interfaces do not match topology"
                    )
                for variable in ("NCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME"):
                    if runtime_environment.get(variable) != expected_interfaces:
                        raise RuntimeError(
                            f"serving-v2 {variable} does not match for {node_id}"
                        )
                for variable in ("NCCL_IB_DISABLE", "NCCL_NET_PLUGIN"):
                    value = runtime_environment.get(variable)
                    expected_value = (
                        policy["nccl_ib_disable"]
                        if variable == "NCCL_IB_DISABLE"
                        else policy["nccl_net_plugin"]
                    )
                    if value != expected_value:
                        raise RuntimeError(
                            f"serving-v2 {variable} does not match shared policy "
                            f"for {node_id}"
                        )
                for variable in ("NCCL_IB_HCA", "NCCL_IB_GID_INDEX"):
                    if not isinstance(runtime_environment.get(variable), str):
                        raise RuntimeError(
                            f"serving-v2 runtime for {node_id} has no {variable} fact"
                        )
            head_environment = runtime_environments[str(policy["head_node_id"])]
            for variable, field in (
                ("NCCL_IB_HCA", "nccl_ib_hca"),
                ("NCCL_IB_GID_INDEX", "nccl_ib_gid_index"),
            ):
                if head_environment.get(variable) != policy[field]:
                    raise RuntimeError(
                        f"serving-v2 {variable} does not match the head policy"
                    )
            if observed_nodes != set(expected_nodes):
                raise RuntimeError(
                    "serving-v2 runtime does not cover both topology nodes"
                )

    expected = {
        "config": cfg.config_label,
        "model": cfg.model,
        "revision": cfg.model_revision,
        "tokenizer": cfg.tokenizer,
        "tokenizer_revision": cfg.tokenizer_revision,
        "serving_fingerprint": cfg.serving_fingerprint,
    }
    mismatches = [
        f"{field}: {policy.get(field)!r} != {value!r}"
        for field, value in expected.items()
        if str(policy.get(field, "")) != str(value or "")
    ]
    policy_trust = str(policy["trust_remote_code"]).strip().lower()
    policy_trust_bool = policy_trust in {"1", "true", "yes", "on"}
    if policy_trust_bool != bool(cfg.trust_remote_code):
        mismatches.append(
            "trust_remote_code: "
            f"{policy['trust_remote_code']!r} != {cfg.trust_remote_code!r}"
        )
    if mismatches:
        raise RuntimeError(
            "serving policy does not match benchmark arguments: "
            + "; ".join(mismatches)
        )
    return policy, artifact_metadata(source.name, data)


