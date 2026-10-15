# Baseline result sets

These directories are the measurements taken before the RDMA restart. They are
kept for comparison and are never resumed into or appended to. The new campaign
writes to `results/` with methodology `steady-exact-v4`; the harness refuses to
mix rows across methodologies, so a stray `OUT_DIR` cannot splice them.

The git tag `pre-rdma-campaign-2026-10-15` pins the repository exactly as it was
before these directories moved here (paths `results/`, `results-variance/`,
`results-rdma/`).

| Directory | Dates (2026) | NCCL transport | Links | Rows | Repeats | Purpose |
|---|---|---|---|---|---|---|
| `pmbs26-tcp/` | Jul 16 - Jul 24 | TCP sockets (`NCCL_IB_DISABLE=1`) | 1 cable, `enp1s0f0np0` | 224 (28 curves x 8 loads) | 1 | PMBS26 submission campaign |
| `pmbs26-tcp-variance/` | Aug 1 - Aug 2 | TCP sockets | 1 cable | 42 | 3, at 5 hand-picked points | Post-hoc run-to-run spread for the PMBS rebuttal |
| `rdma-pilot-2026-10/` | Oct 12 - Oct 15 | RDMA / RoCEv2 (`NCCL_IB_DISABLE=0`) | 2 cables, 4 interfaces | 112 (14 curves x 8 loads) | 1 | First RDMA campaign, on three NVFP4/BF16 pairs; the Qwen3-Next 80B pair is re-measured with replicates in the new campaign, the two Nemotron pairs are dropped |

Common to all three: methodology `steady-exact-v3`, image
`nvcr.io/nvidia/vllm@sha256:98494fda...cb75b27` (NGC `26.04-py3`, vLLM
`0.19.0+6bc3197f.nv26.04`), a 1500 s measurement window with 120 s / 180 s
minimum warmup / cooldown, and concurrencies 1-128.

## Interconnect reference points

Two-node Ray/NCCL microbenchmarks recorded beside the sweeps:

| Run | Transport | all-reduce busbw @ 1 GiB | send/recv busbw @ 1 GiB |
|---|---|---|---|
| `pmbs26-tcp/allreduce_ray_*` (3 runs) | TCP | 1.99 - 2.22 GB/s | n/a |
| `pmbs26-tcp-variance/interconnect_extra_*` (2 runs) | TCP | 2.02 - 2.21 GB/s | 2.86 - 3.25 GB/s |
| `rdma-pilot-2026-10/allreduce_ray_*` | RDMA, 4 ports | 23.56 GB/s | n/a |

## Known limitations

- `pmbs26-tcp/` spans eight harness commits and was sealed with
  `--completed-only --allow-source-match-commits`. Four planned combos have no
  complete curve (`llama3.3-70b-fp8/{single,pp2}`, `llama3.3-70b/{tp2,pp2}`).
  The old `models.yaml` notes attribute the 70B single/pp2 gaps to the
  20-request floor at c=1 in a 1500 s window.
- One repeat per point. `pmbs26-tcp-variance/` repeated only points chosen
  after looking at the margins. Its spread estimates are valid for those points
  but cannot be quoted as a campaign-wide noise floor. The new campaign
  replicates every point instead (see the README section on repeats).
- `rdma-pilot-2026-10/` attempted 16 curves and completed 14. The two gaps
  are `nemotron-super-49b-bf16` single and pp2: a launch was made for each (a
  serving policy exists), but neither produced a row, and the server logs were
  not kept. The likely cause is the 20-request release floor at c=1: tp2,
  which halves each node's weight reads, reached only 25 requests in 1500 s,
  and single/pp2 read all 99.8 GB per token, so they would see ~15. Single may
  also have run out of memory at load. The new matrix drops this model.
- Server logs were never committed, so `scripts/validate_campaign.py verify`
  cannot be rerun on a fresh clone.

## Comparing against the new campaign

Models present in both `pmbs26-tcp/` and the new matrix, under the same labels
and pinned revisions: `llama3.1-8b`, `llama3.1-8b-fp8`, `llama3.1-8b-nvfp4`,
`nemotron3-super-120b-a12b-nvfp4`. From `rdma-pilot-2026-10/`, the new matrix
keeps `qwen3-next-80b-nvfp4` and `qwen3-next-80b-bf16`. `scripts/run_job.sh`
writes both comparisons under `results/figs/stats/`; by hand:

```bash
python scripts/compare_campaigns.py \
  --new results/index.csv \
  --baseline baselines/pmbs26-tcp/index.csv \
  --md results/compare_vs_pmbs26_tcp.md
python scripts/compare_campaigns.py \
  --new results/index.csv \
  --baseline baselines/rdma-pilot-2026-10/index.csv \
  --md results/compare_vs_rdma_pilot.md
```

Against the pilot, transport and cabling are the same, so a difference
reflects the image (vLLM 0.19 to 0.29), the window rule, and replication.

The new campaign also changes the vLLM image (26.09, vLLM 0.29), the
measurement window, and the repeat design. A new-vs-baseline ratio therefore
measures transport *and* software together. To attribute a difference to RDMA
alone, use the transport ablation described in the top-level README, which
reruns a subset over TCP with the same image and harness.

## Raw telemetry

The `*.raw.jsonl`, `*.power.jsonl`, and `*.vllm_metrics.jsonl` files here were
committed before the raw-data policy changed. They are still tracked so nothing
is lost, but new campaigns never commit raw telemetry (see `.gitignore`). To
shrink the repository, pack them with `scripts/pack_raw.sh`, store the archive
outside git, and only then consider a history rewrite.
