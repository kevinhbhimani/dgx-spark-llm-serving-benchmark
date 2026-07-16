"""Capture the served model's capacity and launch configuration.

Most of what a serving paper needs to characterize a deployment is available
from the running server over HTTP and does not depend on the client's
measurement window:

- ``/v1/models`` gives ``max_model_len``.
- vLLM's Prometheus ``vllm:cache_config_info`` gauge carries the launch-time KV
  cache configuration as label key/values: ``block_size``, ``cache_dtype``
  (the KV cache dtype), ``gpu_memory_utilization``, ``num_gpu_blocks``,
  ``num_cpu_blocks``, ``enable_prefix_caching``, and sometimes
  ``kv_cache_memory_bytes``.

For ordinary attention caches, ``num_gpu_blocks * block_size`` is the token
capacity. Hybrid cache groups need vLLM's group-aware ``kv_cache_size_tokens``
or the startup log's ``GPU KV cache size`` instead; multiplying the physical
hybrid block size is incorrect. Two memory footprints a reader also wants -- model weights
in GiB and KV cache in GiB -- are not always in ``/metrics`` (this vLLM build
reports ``kv_cache_memory_bytes="None"``), so this module records two sources:

1. the vLLM startup log, which prints the exact per-worker figures vLLM computed
   (``worker_weight_gb``, ``worker_kv_cache_gb``, ``max_concurrency``), used
   when the launcher tees the server output; and
2. optional portable estimates from the Hugging Face model config plus the KV
   cache configuration above (``compute_kv_cache_gb`` / ``compute_weight_gb``).

All network/config lookups are best effort: any field that cannot be resolved is
left as ``None`` rather than failing the benchmark.
"""
from __future__ import annotations
import json
import re
import urllib.request

HF_METADATA_TIMEOUT_S = 10.0


# ---- HTTP: served model length and KV cache configuration --------------------
def _get_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _get_text(url: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _max_model_len(base_url: str, model: str | None) -> int | None:
    try:
        data = _get_json(base_url.rstrip("/") + "/v1/models").get("data", [])
    except Exception:
        return None
    if not data:
        return None
    entry = next((d for d in data if model and d.get("id") == model), data[0])
    val = entry.get("max_model_len")
    return int(val) if val is not None else None


def _server_version(base_url: str) -> str | None:
    try:
        value = _get_json(base_url.rstrip("/") + "/version").get("version")
        return str(value) if value is not None else None
    except Exception:
        return None


_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_labels(line: str) -> dict:
    inside = line[line.find("{") + 1: line.rfind("}")]
    return dict(_LABEL_RE.findall(inside))


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cache_config(base_url: str) -> dict:
    """Pull vllm:cache_config_info labels into a normalized dict."""
    try:
        text = _get_text(base_url.rstrip("/") + "/metrics")
    except Exception:
        return {}
    for line in text.splitlines():
        if line.startswith("vllm:cache_config_info{"):
            lab = _parse_labels(line)
            blocks = _as_int(lab.get("num_gpu_blocks"))
            bsize = _as_int(lab.get("block_size"))
            info = {
                "block_size": bsize,
                "gpu_blocks": blocks,
                "num_cpu_blocks": _as_int(lab.get("num_cpu_blocks")),
                "kv_cache_dtype": lab.get("cache_dtype"),
                "gpu_memory_utilization": _as_float(lab.get("gpu_memory_utilization")),
                "enable_prefix_caching": lab.get("enable_prefix_caching"),
                "mamba_page_size_padded": lab.get("mamba_page_size_padded"),
            }
            mamba_page = lab.get("mamba_page_size_padded")
            is_hybrid = mamba_page not in (None, "", "None")
            group_tokens = _as_int(lab.get("kv_cache_size_tokens"))
            group_concurrency = _as_float(lab.get("kv_cache_max_concurrency"))
            info["hybrid_cache"] = is_hybrid
            if group_tokens is not None:
                info["total_kv_tokens"] = group_tokens
                info["total_kv_tokens_source"] = "vllm_group_aware_metric"
            elif not is_hybrid and blocks is not None and bsize is not None:
                info["total_kv_tokens"] = blocks * bsize
                info["total_kv_tokens_source"] = "gpu_blocks_x_block_size"
            else:
                info["total_kv_tokens"] = None
                info["total_kv_tokens_source"] = None
            if group_concurrency is not None:
                info["max_concurrency"] = group_concurrency
            kv_bytes = _as_int(lab.get("kv_cache_memory_bytes"))
            if kv_bytes:
                info["kv_cache_gb"] = kv_bytes / 1024 ** 3
            return info
    return {}


# ---- portable computation from the Hugging Face config -----------------------
_DTYPE_BYTES = {
    "auto": None,          # resolved from the model's own dtype below
    "bfloat16": 2, "bf16": 2, "float16": 2, "fp16": 2, "half": 2, "float32": 4,
    "fp8": 1, "fp8_e4m3": 1, "fp8_e5m2": 1, "int8": 1, "uint8": 1,
}


def _load_hf_config(model_id: str, trust_remote_code: bool = False):
    try:
        from transformers import AutoConfig
        return AutoConfig.from_pretrained(model_id,
                                          trust_remote_code=trust_remote_code)
    except Exception:
        return None


def _kv_dtype_bytes(kv_cache_dtype: str | None, cfg) -> int | None:
    key = (kv_cache_dtype or "auto").lower()
    b = _DTYPE_BYTES.get(key, None)
    if b is not None:
        return b
    # "auto" (or an unrecognized value): follow the model's stored dtype
    td = str(getattr(cfg, "torch_dtype", "") or "").lower().replace("torch.", "")
    return _DTYPE_BYTES.get(td, 2)   # default to 2 bytes (bf16/fp16) if unknown


def compute_kv_cache_gb(model_id, gpu_blocks, block_size, kv_cache_dtype,
                        trust_remote_code: bool = False) -> float | None:
    """Full-model KV cache footprint in GiB for the allocated blocks.

    KV bytes/token = 2 (K and V) * layers * kv_heads * head_dim * dtype_bytes,
    with GQA handled via num_key_value_heads, times total_kv_tokens
    (= gpu_blocks * block_size). This is the aggregate across all tensor/pipeline
    shards; per GPU it is roughly this divided by the parallel size (e.g. TP=2
    stores half on each GB10). Returns None if the config or any required field
    is unavailable.
    """
    if not gpu_blocks or not block_size:
        return None
    cfg = _load_hf_config(model_id, trust_remote_code)
    if cfg is None:
        return None
    n_layers = getattr(cfg, "num_hidden_layers", None)
    n_heads = getattr(cfg, "num_attention_heads", None)
    n_kv = getattr(cfg, "num_key_value_heads", None) or n_heads
    hidden = getattr(cfg, "hidden_size", None)
    head_dim = getattr(cfg, "head_dim", None) or (
        hidden // n_heads if (hidden and n_heads) else None)
    if not (n_layers and n_kv and head_dim):
        return None
    dbytes = _kv_dtype_bytes(kv_cache_dtype, cfg)
    bytes_per_token = 2 * n_layers * n_kv * head_dim * dbytes
    return gpu_blocks * block_size * bytes_per_token / 1024 ** 3


def compute_weight_gb(model_id: str,
                      timeout_s: float = HF_METADATA_TIMEOUT_S) -> float | None:
    """Full-model weight footprint in GiB from the checkpoint's safetensors sizes.

    Uses the ``total_size`` recorded in a sharded safetensors index when
    present, otherwise sums the ``.safetensors`` file sizes from the Hub. This
    is the whole stored checkpoint (per-GPU is roughly this divided by the
    parallel size); for a model served in a different dtype or quantization it
    is an approximation, so treat it as indicative. Returns None if the metadata
    cannot be fetched (offline, gated without a token, etc.).
    """
    try:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(model_id, "model.safetensors.index.json",
                               etag_timeout=timeout_s)
        with open(path) as f:
            total = json.load(f).get("metadata", {}).get("total_size")
        if total:
            return int(total) / 1024 ** 3
    except Exception:
        pass
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(model_id, files_metadata=True,
                                  timeout=timeout_s)
        total = sum(getattr(s, "size", 0) or 0 for s in info.siblings
                    if s.rfilename.endswith(".safetensors"))
        return total / 1024 ** 3 if total else None
    except Exception:
        return None


# ---- vLLM startup log (authoritative when available) -------------------------
# vLLM phrases these slightly differently across versions; match flexibly and
# keep whatever is found. Sizes are normalized to GiB.
_LOG_PATTERNS = {
    "weight_gb": re.compile(
        r"(?:[Mm]odel weights take|[Mm]odel loading took)\s*"
        r"([\d.]+)\s*GiB(?:\s+memory)?"
    ),
    "kv_cache_gb": re.compile(
        r"(?:Available KV cache memory|GPU KV cache size \(memory\))[:\s]*"
        r"([\d.]+)\s*GiB"),
    "gpu_blocks": re.compile(r"#?\s*GPU blocks?[:\s]*([\d,]+)"),
    "kv_cache_tokens": re.compile(r"GPU KV cache size[:\s]*([\d,]+)\s*tokens"),
    "max_concurrency": re.compile(
        r"[Mm]aximum concurrency for[\s\d,]+tokens per request[:\s]*([\d.]+)x"),
}


def parse_server_log(path: str) -> dict:
    """Extract exact weight/KV figures from a captured vLLM stdout log."""
    out: dict = {}
    try:
        with open(path, errors="ignore") as f:
            text = f.read()
    except OSError:
        return out
    for key, pat in _LOG_PATTERNS.items():
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).replace(",", "")
        out[key] = float(raw) if "." in raw else int(raw)
    return out


# ---- top level ---------------------------------------------------------------
def collect(base_url: str, model: str | None = None, server_log: str = "",
            compute_from_hf: bool = True, trust_remote_code: bool = False) -> dict:
    """Best-effort snapshot of the served model's capacity and configuration.

    Order of precedence for the two memory footprints: an explicit server log
    (exact, what vLLM computed) wins; otherwise they are computed from the HF
    config and the live KV cache configuration. Every field is optional.
    """
    logged = parse_server_log(server_log) if server_log else {}
    info: dict = {
        "model": model,
        "vllm_version": _server_version(base_url),
        "max_model_len": _max_model_len(base_url, model),
    }
    info.update(_cache_config(base_url))

    if "weight_gb" in logged:
        info["worker_weight_gb"] = logged["weight_gb"]
    if "kv_cache_gb" in logged:
        info["worker_kv_cache_gb"] = logged["kv_cache_gb"]
    if "max_concurrency" in logged:
        info["max_concurrency"] = logged["max_concurrency"]
    if logged.get("gpu_blocks") and not info.get("gpu_blocks"):
        info["gpu_blocks"] = logged["gpu_blocks"]
        if (info.get("block_size") and info.get("total_kv_tokens") is None
                and not info.get("hybrid_cache")):
            info["total_kv_tokens"] = logged["gpu_blocks"] * info["block_size"]
            info["total_kv_tokens_source"] = "gpu_blocks_x_block_size"
    # The startup line is already group-aware and is authoritative for both
    # ordinary and hybrid models. It must override a naive metric derivation.
    if logged.get("kv_cache_tokens"):
        info["total_kv_tokens"] = logged["kv_cache_tokens"]
        info["total_kv_tokens_source"] = "vllm_startup_log"

    if compute_from_hf and model:
        if info.get("kv_cache_gb") is None and not info.get("hybrid_cache"):
            info["kv_cache_gb"] = compute_kv_cache_gb(
                model, info.get("gpu_blocks"), info.get("block_size"),
                info.get("kv_cache_dtype"), trust_remote_code)
        info["weight_gb"] = compute_weight_gb(model)
    return info
