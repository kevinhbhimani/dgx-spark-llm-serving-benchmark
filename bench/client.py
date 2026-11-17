"""A single streaming request to the vLLM OpenAI compatible server.

TTFT is the time from request start to the first returned output token id.
Requests ask vLLM to include delta ``token_ids`` and final usage in the stream;
this keeps token counts exact even when decoded text is empty or one HTTP chunk
contains several tokens. Output length is forced to exactly ``output_len`` by
setting ``max_tokens`` and ``min_tokens`` equal with ``ignore_eos`` true, and a
stream is successful only when its token-id count and final usage both match.
Paper runs also require server-reported prompt usage to match the input bucket.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
import aiohttp
from scripts.failure_diagnostics import redact_diagnostic


@dataclass
class RequestRecord:
    config_label: str
    input_len: int
    output_len: int
    t_start: float = 0.0
    t_first_token: float = 0.0
    t_end: float = 0.0
    output_tokens: int = 0
    token_times: list = field(default_factory=list)
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    finish_reason: str = ""
    stream_done: bool = False
    success: bool = False
    error: str = ""

    @property
    def ttft(self):
        return self.t_first_token - self.t_start if self.success else float("nan")

    @property
    def e2e(self):
        return self.t_end - self.t_start if self.success else float("nan")

    @property
    def mean_itl(self):
        if len(self.token_times) < 2:
            return float("nan")
        gaps = self.inter_token_latencies
        return sum(gaps) / len(gaps)

    @property
    def inter_token_latencies(self):
        if len(self.token_times) < 2:
            return []
        return [b - a for a, b in zip(self.token_times, self.token_times[1:])]

    def to_json(self):
        return {
            "config_label": self.config_label,
            "input_len": self.input_len,
            "output_len": self.output_len,
            "output_tokens": self.output_tokens,
            "t_start": self.t_start,
            "t_first_token": self.t_first_token,
            "t_end": self.t_end,
            "token_times": self.token_times,
            "usage_input_tokens": self.usage_input_tokens,
            "usage_output_tokens": self.usage_output_tokens,
            "finish_reason": self.finish_reason,
            "stream_done": self.stream_done,
            "ttft": self.ttft,
            "e2e": self.e2e,
            "mean_itl": self.mean_itl,
            "inter_token_latencies": self.inter_token_latencies,
            "success": self.success,
            "error": self.error,
        }


def _first_choice(chunk):
    choices = chunk.get("choices") or []
    return choices[0] if choices else {}


def _extract_token_ids(chunk):
    """Return this stream chunk's delta token ids, or None if absent.

    vLLM's completions response puts them on the choice. The alternate lookups
    make the parser tolerant of chat-compatible response shapes without making
    decoded text the token-counting fallback again.
    """
    choice = _first_choice(chunk)
    value = choice.get("token_ids")
    if value is None:
        value = (choice.get("delta") or {}).get("token_ids")
    if value is None:
        value = chunk.get("token_ids")
    return value if isinstance(value, list) else None


def _extract_usage_tokens(chunk, field):
    usage = chunk.get("usage") or {}
    value = usage.get(field)
    # Do not truncate fractional counts or accept booleans as token counts.
    # Usage is our independent cross-check on the streamed token IDs.
    return value if type(value) is int and value >= 0 else None


def _extract_usage_input_tokens(chunk):
    return _extract_usage_tokens(chunk, "prompt_tokens")


def _extract_usage_output_tokens(chunk):
    return _extract_usage_tokens(chunk, "completion_tokens")


def _extract_finish_reason(chunk):
    value = _first_choice(chunk).get("finish_reason")
    return str(value) if value is not None else ""


async def send_request(session, cfg, spec, t_start=None) -> RequestRecord:
    """Send one streaming request. t_start, when given, backdates the record to
    the request's intended arrival time so client-side queueing (the open
    loop's max_inflight gate) counts toward TTFT and e2e latency."""
    rec = RequestRecord(cfg.config_label, spec.input_len, spec.output_len)
    url = cfg.base_url.rstrip("/") + cfg.endpoint
    body = {
        "model": cfg.model,
        "prompt": spec.prompt,
        "max_tokens": spec.output_len,
        "min_tokens": spec.output_len,
        "ignore_eos": True,
        "temperature": 0.0,
        "stream": True,
        # vLLM extensions. Counting decoded content chunks is not exact: tokens
        # can decode to empty text and a streamed delta can contain >1 token.
        "return_token_ids": True,
        "stream_options": {"include_usage": True},
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=cfg.request_timeout_s)
    rec.t_start = t_start if t_start is not None else time.perf_counter()
    try:
        async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                rec.error = f"http {resp.status}: {(await resp.text())[:200]}"
                rec.t_end = time.perf_counter()
                return rec
            done = False
            saw_token_ids = False
            parse_errors = 0
            async for raw in resp.content:
                for line in raw.decode("utf-8", "ignore").splitlines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue
                    if chunk.get("error"):
                        rec.error = f"stream error: {str(chunk['error'])[:200]}"
                    usage_tokens = _extract_usage_output_tokens(chunk)
                    if usage_tokens is not None:
                        rec.usage_output_tokens = usage_tokens
                    input_tokens = _extract_usage_input_tokens(chunk)
                    if input_tokens is not None:
                        rec.usage_input_tokens = input_tokens
                    finish_reason = _extract_finish_reason(chunk)
                    if finish_reason:
                        rec.finish_reason = finish_reason
                    token_ids = _extract_token_ids(chunk)
                    if token_ids is not None:
                        saw_token_ids = True
                    if token_ids:
                        now = time.perf_counter()
                        if rec.output_tokens == 0:
                            rec.t_first_token = now
                        rec.output_tokens += len(token_ids)
                        # Several ids delivered in one network chunk arrived as a
                        # burst from the client's point of view.
                        rec.token_times.extend([now] * len(token_ids))
                if done:
                    break
        rec.t_end = time.perf_counter()
        rec.stream_done = done
        if not rec.error and parse_errors:
            rec.error = f"{parse_errors} malformed SSE JSON chunk(s)"
        if not rec.error and not done:
            rec.error = "stream ended without [DONE]"
        if not rec.error and not saw_token_ids:
            rec.error = "server did not return delta token_ids"
        if not rec.error and rec.usage_output_tokens is None:
            rec.error = "server did not return final completion token usage"
        if (not rec.error and getattr(cfg, "require_provenance", False)
                and rec.usage_input_tokens is None):
            rec.error = "server did not return final prompt token usage"
        if (not rec.error
                and (cfg.use_token_ids or getattr(cfg, "require_provenance", False))
                and rec.usage_input_tokens is not None
                and rec.usage_input_tokens != spec.input_len):
            rec.error = ("prompt token usage does not match requested input length: "
                         f"{rec.usage_input_tokens} != {spec.input_len}")
        if (not rec.error
                and rec.usage_output_tokens != rec.output_tokens):
            rec.error = ("stream token-id count does not match final usage: "
                         f"{rec.output_tokens} != {rec.usage_output_tokens}")
        if not rec.error and rec.output_tokens != spec.output_len:
            rec.error = ("incomplete generation: expected "
                         f"{spec.output_len} tokens, received {rec.output_tokens}")
        if not rec.error and rec.finish_reason != "length":
            rec.error = ("unexpected finish_reason: "
                         f"{rec.finish_reason or 'missing'}")
        rec.success = not rec.error
    except Exception as e:  # network, timeout, parse
        rec.t_end = time.perf_counter()
        rec.error = f"{type(e).__name__}: {redact_diagnostic(str(e), (cfg.api_key,))}"
    return rec
