"""A single streaming request to the vLLM OpenAI compatible server.

TTFT is the time from request start to the first content bearing chunk. Token
count uses one streamed chunk per token, which is the vLLM streaming behavior
under greedy decoding. Output length is forced to exactly output_len by setting
max_tokens and min_tokens equal with ignore_eos true.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
import aiohttp


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
            "ttft": self.ttft,
            "e2e": self.e2e,
            "mean_itl": self.mean_itl,
            "inter_token_latencies": self.inter_token_latencies,
            "success": self.success,
            "error": self.error,
        }


def _extract_text(chunk):
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    c0 = choices[0]
    if "text" in c0:                       # completions endpoint
        return c0.get("text", "")
    return (c0.get("delta", {}) or {}).get("content", "") or ""   # chat endpoint


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
            async for raw in resp.content:
                line = raw.decode("utf-8", "ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if _extract_text(chunk):
                    now = time.perf_counter()
                    if rec.output_tokens == 0:
                        rec.t_first_token = now
                    rec.output_tokens += 1
                    rec.token_times.append(now)
        rec.t_end = time.perf_counter()
        rec.success = rec.output_tokens > 0
        if not rec.success and not rec.error:
            rec.error = "no tokens returned"
    except Exception as e:  # network, timeout, parse
        rec.t_end = time.perf_counter()
        rec.error = f"{type(e).__name__}: {e}"
    return rec
