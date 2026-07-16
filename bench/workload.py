"""Workload generation: token length controlled prompts and trace loading."""
from __future__ import annotations
import json
import math
import random
from dataclasses import dataclass


# Reserve enough leading tokens for a cache-defeating request id. Even a small
# 16-token tag alphabet then supports 16**8 (>4 billion) distinct prefixes.
PREFIX_TAG_TOKENS = 8


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
        # distinct token values for the uniqueness tag; base_ids itself repeats
        # values heavily, which would let two counters produce the same tokens
        self._tag_vocab = sorted(set(self._base_ids)) if self._base_ids else []
        self._uid = 0
        self.specs = self._load_trace(cfg.trace_path) if cfg.trace_path else None
        self._trace_order = []
        self._trace_pos = 0
        if self.specs is not None and not self.specs:
            raise ValueError(f"trace is empty: {cfg.trace_path}")
        if self.specs is None:
            self._validate_buckets()
            # Smooth weighted round robin: unlike independent random choices,
            # every short contiguous slice stays close to the target mix. Seeded
            # tie ranks avoid making equal-weight buckets depend on YAML order.
            self._bucket_credit = [0.0] * len(cfg.buckets)
            order = list(range(len(cfg.buckets)))
            self.rng.shuffle(order)
            self._bucket_tie_rank = {idx: rank for rank, idx in enumerate(order)}

    def _validate_buckets(self):
        if not self.cfg.buckets:
            raise ValueError("at least one workload bucket is required")
        if any(b.input_len <= 0 or b.output_len <= 0 for b in self.cfg.buckets):
            raise ValueError("bucket input_len and output_len must be positive")
        if any(b.weight < 0 or not math.isfinite(b.weight) for b in self.cfg.buckets):
            raise ValueError("bucket weights must be finite and non-negative")
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
        # Every prompt must be unique from its first token: the random slices
        # below can repeat across requests (and across warmup vs measurement),
        # and with server-side prefix caching enabled that silently skipped up
        # to a third of the prefill work in some runs while models launched
        # with caching disabled did it all -- an unusable cross-model confound.
        # The launcher now disables prefix caching, and this tag makes the
        # workload itself cache-defeating: the leading tokens encode a
        # per-request counter in base len(tag_vocab), which breaks any
        # block-aligned prefix match at block 0. Eight tag positions avoid the
        # old two-token wrap at request 257 for tokenizers whose generated base
        # buffer happened to contain only 16 distinct ids. Deterministic given
        # the seed; the counter restarts per run, which is safe with server-side
        # caching disabled.
        self._uid += 1
        if self.tok is not None and self._base_ids:
            ids = self._base_ids
            if len(ids) < input_len:
                ids = ids * ((input_len // len(ids)) + 1)
            start_max = max(0, len(ids) - input_len)
            start = self.rng.randint(0, start_max) if start_max else 0
            prompt_ids = ids[start:start + input_len]
            vocab = self._tag_vocab
            k = min(PREFIX_TAG_TOKENS, input_len)
            capacity = len(vocab) ** k
            if self._uid >= capacity:
                raise RuntimeError(
                    f"request prefix id space exhausted after {capacity - 1} "
                    "unique prompts; increase PREFIX_TAG_TOKENS"
                )
            tag = [vocab[(self._uid // (len(vocab) ** j)) % len(vocab)]
                   for j in range(k)]
            prompt_ids = tag + prompt_ids[k:]
            return prompt_ids if self.cfg.use_token_ids else self.tok.decode(prompt_ids)
        # fallback without a tokenizer, length is approximate not token exact
        return (f"uniq{self._uid} " + "word " * max(0, input_len - 1)).strip()

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
            if not self.cfg.balanced_workload:
                return self.rng.choice(self.specs)
            if self._trace_pos >= len(self._trace_order):
                self._trace_order = list(range(len(self.specs)))
                self.rng.shuffle(self._trace_order)
                self._trace_pos = 0
            spec = self.specs[self._trace_order[self._trace_pos]]
            self._trace_pos += 1
            return spec
        buckets = self.cfg.buckets
        weights = [b.weight for b in buckets]
        if self.cfg.balanced_workload:
            total = sum(weights)
            for i, weight in enumerate(weights):
                self._bucket_credit[i] += weight
            idx = max(
                (i for i, weight in enumerate(weights) if weight > 0),
                key=lambda i: (self._bucket_credit[i],
                               -self._bucket_tie_rank[i]),
            )
            self._bucket_credit[idx] -= total
            b = buckets[idx]
        else:
            b = self.rng.choices(buckets, weights=weights, k=1)[0]
        return RequestSpec(b.input_len, b.output_len, self._make_prompt(b.input_len))
