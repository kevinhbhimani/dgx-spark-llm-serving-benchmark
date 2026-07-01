"""Workload generation: token length controlled prompts and trace loading."""
from __future__ import annotations
import json
import random
from dataclasses import dataclass


@dataclass
class RequestSpec:
    input_len: int
    output_len: int
    prompt: object        # str or list[int]


class Workload:
    def __init__(self, cfg, tokenizer=None):
        self.cfg = cfg
        self.tok = tokenizer
        self.rng = random.Random(cfg.seed)
        self._base_ids = self._build_base_ids()
        self.specs = self._load_trace(cfg.trace_path) if cfg.trace_path else None
        if self.specs is not None and not self.specs:
            raise ValueError(f"trace is empty: {cfg.trace_path}")
        if self.specs is None:
            self._validate_buckets()

    def _validate_buckets(self):
        if not self.cfg.buckets:
            raise ValueError("at least one workload bucket is required")
        if any(b.input_len <= 0 or b.output_len <= 0 for b in self.cfg.buckets):
            raise ValueError("bucket input_len and output_len must be positive")
        if sum(b.weight for b in self.cfg.buckets) <= 0:
            raise ValueError("at least one workload bucket weight must be positive")

    def _build_base_ids(self):
        if self.tok is None:
            return None
        max_input = max((b.input_len for b in self.cfg.buckets), default=8192)
        target_tokens = max(max_input * 4, 32768)
        ids = []
        i = 0
        chunk_segments = 512
        while len(ids) < target_tokens:
            text = " ".join(
                f"benchmark_segment_{j}_latency_cache_{(j * 17) % 997}"
                for j in range(i, i + chunk_segments)
            )
            ids.extend(self.tok(text, add_special_tokens=False)["input_ids"])
            i += chunk_segments
        return ids

    def _make_prompt(self, input_len):
        if self.tok is not None and self._base_ids:
            ids = self._base_ids
            if len(ids) < input_len:
                ids = ids * ((input_len // len(ids)) + 1)
            start_max = max(0, len(ids) - input_len)
            start = self.rng.randint(0, start_max) if start_max else 0
            prompt_ids = ids[start:start + input_len]
            return prompt_ids if self.cfg.use_token_ids else self.tok.decode(prompt_ids)
        # fallback without a tokenizer, length is approximate not token exact
        return ("word " * input_len).strip()

    def _load_trace(self, path):
        specs = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                in_len = int(rec.get("input_len", 0))
                out_len = int(rec.get("output_len", rec.get("max_tokens", 128)))
                if "prompt" in rec and not self.cfg.use_token_ids:
                    prompt = rec["prompt"]
                    in_len = in_len or len(prompt.split())
                else:
                    if in_len <= 0:
                        raise ValueError(f"trace record missing input_len: {path}")
                    prompt = self._make_prompt(in_len)
                if in_len <= 0 or out_len <= 0:
                    raise ValueError(
                        f"trace records require positive input/output lengths: {path}"
                    )
                specs.append(RequestSpec(in_len, out_len, prompt))
        return specs

    def sample(self) -> RequestSpec:
        if self.specs is not None:
            return self.rng.choice(self.specs)
        buckets = self.cfg.buckets
        weights = [b.weight for b in buckets]
        b = self.rng.choices(buckets, weights=weights, k=1)[0]
        return RequestSpec(b.input_len, b.output_len, self._make_prompt(b.input_len))
