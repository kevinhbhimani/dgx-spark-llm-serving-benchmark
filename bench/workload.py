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

    def _build_base_ids(self):
        if self.tok is None:
            return None
        max_input = max((b.input_len for b in self.cfg.buckets), default=8192)
        words = max(max_input * 4, 32768)
        text = " ".join(
            f"benchmark_segment_{i}_latency_cache_{(i * 17) % 997}"
            for i in range(words)
        )
        return self.tok(text, add_special_tokens=False)["input_ids"]

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
                    prompt = self._make_prompt(in_len)
                specs.append(RequestSpec(in_len, out_len, prompt))
        return specs

    def sample(self) -> RequestSpec:
        if self.specs is not None:
            return self.rng.choice(self.specs)
        buckets = self.cfg.buckets
        weights = [b.weight for b in buckets]
        b = self.rng.choices(buckets, weights=weights, k=1)[0]
        return RequestSpec(b.input_len, b.output_len, self._make_prompt(b.input_len))
