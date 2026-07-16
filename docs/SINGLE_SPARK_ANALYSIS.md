# Single-Spark serving analysis

Analysis of Kevin's single-node benchmark results from commit
[`7249671`](https://github.com/kevinhbhimani/dgx-spark-llm-serving-benchmark/tree/7249671d14d9d02749446d8fb10910f5e0bc0f90)
("Nemotron and Llama results"). Source: `results/index.csv` (144 rows, 600 s measurement window).

## Setup

| Item | Value |
| --- | --- |
| Hardware | One DGX Spark node (GB10, ~128 GB unified memory) |
| Server | vLLM via `scripts/launch_server.sh single` |
| Workload | `configs/experiment.yaml` — mixed short + long context (up to 8192 input + 512 output tokens) |
| Load | Closed-loop concurrency sweep (1 → 128) |
| SLOs | TTFT ≤ 3.0 s, mean ITL ≤ 0.10 s (used for goodput) |

Models tested on **single** Spark (7 of 9 in the matrix):

| Label | Model | Notes |
| --- | --- | --- |
| nemotron3-nano-30b-a3b-nvfp4 | NVIDIA Nemotron 3 Nano 30B A3B NVFP4 | NVIDIA-optimized MoE |
| qwen3-8b | Qwen/Qwen3-8B | Dense 8B baseline |
| qwen3-vl-8b | Qwen/Qwen3-VL-8B-Instruct | Multimodal 8B baseline |
| qwen3-30b-a3b | Qwen/Qwen3-30B-A3B | MoE counterpart to 32B |
| nemotron3-super-120b-a12b-nvfp4 | NVIDIA Nemotron 3 Super 120B A12B NVFP4 | Large NVIDIA MoE |
| llama3.3-70b-nvfp4 | nvidia/Llama-3.3-70B-Instruct-NVFP4 | Quantized 70B |
| qwen3-32b | Qwen/Qwen3-32B | Dense 32B (also in original `configs/models.yaml`) |

`llama3.3-70b` (bf16) is dual-node only and has no single-node rows.

## Headline findings

### 1. Nemotron Nano NVFP4 is the clear single-Spark winner

| Model | Peak tok/s | @ concurrency | TTFT p50 | KV cache max |
| --- | --- | --- | --- | --- |
| **nemotron3-nano-30b-a3b-nvfp4** | **206** | 32 | 1.9 s | 2.6% |
| qwen3-vl-8b | 109 | 32 | 3.0 s | 26.9% |
| qwen3-8b | 107 | 32 | 3.0 s | 26.2% |
| qwen3-30b-a3b | 102 | 16 | 1.7 s | 22.0% |
| nemotron3-super-120b-a12b-nvfp4 | 84 | 64 | 5.1 s | 53.5% |
| llama3.3-70b-nvfp4 | 54 | 64 | 10.3 s | 70.2% |
| qwen3-32b | 25 | 64 | 84.4 s | **100%** |

At a fair comparison point (concurrency 16), Nemotron Nano delivers **157 tok/s** vs **~98 tok/s** for the 8B baselines and only **24 tok/s** for Qwen3-32B.

**Takeaway:** For this long-context serving workload on one Spark, NVIDIA's NVFP4 Nemotron Nano is ~**8× faster** than dense Qwen3-32B and ~**1.6× faster** than Qwen3-8B, with far more KV headroom.

### 2. Qwen3-32B is KV-cache bound, not broken

Qwen3-32B throughput plateaus around 24 tok/s while KV cache climbs to 100%. TTFT explodes from 0.9 s at c=1 to 84 s at c=64. The model loads and serves, but the default workload (8192-token prompts) exhausts KV on one node.

**Takeaway:** Qwen3-32B is a poor single-Spark choice for long-context chat at `max_model_len=8704`. It may still be useful for shorter prompts or on dual-node (`tp2` peak was 51 tok/s in this dataset — still well below Nemotron Nano's 301 tok/s on `tp2`).

### 3. Quantization path matters more than parameter count

Nemotron Super 120B NVFP4 (120B total, 12B active) reaches **84 tok/s** on one node — higher than Llama 3.3 70B NVFP4 (**54 tok/s**) despite being a larger model. NVFP4 + MoE sparsity lets vLLM serve a flagship-class model on one GB10.

Llama 3.3 70B bf16 has no single-node data (expected — ~140 GB weights exceed one node).

### 4. Goodput is strict under the default SLOs

With TTFT ≤ 3.0 s and mean ITL ≤ 100 ms, only Nemotron Nano shows meaningful goodput at c=16 (0.41 rps). Other models meet TTFT but violate the 100 ms ITL target on long outputs — their raw throughput is fine, but they fail the chat responsiveness SLO.

**Takeaway:** If the UD chatbot needs snappy streaming, either relax ITL SLO for long-context buckets or prioritize Nemotron Nano / smaller models.

### 5. Energy efficiency follows throughput

At peak throughput (single node, power via `nvidia_smi`):

| Model | tok/W | J/token |
| --- | --- | --- |
| nemotron3-nano-30b-a3b-nvfp4 | 4.48 | 0.22 |
| qwen3-30b-a3b | 1.92 | 0.52 |
| qwen3-8b | 1.61 | 0.62 |
| qwen3-32b | 0.35 | 2.88 |

Nemotron Nano is ~**13× more energy-efficient per token** than Qwen3-32B at peak load.

## Dual-Spark preview (tp2 rows in same index)

The same commit includes early `tp2` sweeps. Peak tok/s:

| Model | single peak | tp2 peak |
| --- | --- | --- |
| nemotron3-nano-30b-a3b-nvfp4 | 206 | **301** |
| qwen3-8b | 107 | 163 |
| qwen3-32b | 25 | 51 |
| llama3.3-70b (bf16) | — | 17 |

Nemotron Nano benefits from dual-node scaling (+46%). Qwen3-32B improves but remains far behind. Full dual-Spark analysis should wait for Hoedus internet + completed `tp2`/`pp2` sweeps.

## Connection to model survey (BenchmarkSurvey)

The separate `BenchmarkSurvey/` work ranks models by **task quality** (documents, code, math, etc.) and flags Spark feasibility using **active-parameter serving memory**. That survey's top 10 (Qwen3.5-397B, Kimi K2.x, DeepSeek-V3.2, etc.) score highest on benchmarks but need **400–800+ GB HuggingFace checkpoints** — they cannot be downloaded or stored on current Spark disk (~80 GB free).

Kevin's `configs/models.yaml` at this commit instead targets the **paper's hardware story**: NVIDIA NVFP4/Nemotron optimized models vs open Qwen/Llama baselines that actually fit on Spark today. That is why no survey top-10 model appears in the matrix — it is a **deployability** constraint, not a dismissal of the survey.

| Survey recommendation | Why not in this run |
| --- | --- |
| Qwen3.5-397B-A17B | ~807 GB HF download; MoE checkpoint too large for disk |
| Kimi K2.5 / K2.6 | ~595 GB HF download each |
| DeepSeek-V3.2 | ~690 GB HF download |
| GLM-4.7 | ~717 GB HF download |
| Qwen3-32B | In matrix, but KV-bound on long-context workload (this analysis) |
| nemotron-super-49b-fp8 (team yaml) | Replaced by nemotron3-super-120b-a12b-nvfp4 in Kevin's matrix |

## Recommendations

**For the paper (single Spark, section 5.1):**
- Lead with **Nemotron Nano NVFP4** as the optimized path: best throughput, KV headroom, energy, and partial goodput.
- Use **Qwen3-8B** as the neutral open baseline.
- Show **Qwen3-32B** as a cautionary long-context case (KV saturation on one node).
- Include **Nemotron Super 120B NVFP4** as evidence that large MoE + NVFP4 serves on one GB10.

**For dual-Spark (next phase):**
- Prioritize Nemotron Nano and Qwen3-8B `tp2`/`pp2` comparisons (already partially collected).
- Run `llama3.3-70b` bf16 on `tp2`/`pp2` as the large dense dual-node case.
- De-prioritize Qwen3-32B unless testing shorter `max_model_len`.

**For UD chatbot model selection:**
- If deployment is **one Spark**, Nemotron Nano NVFP4 is the strongest serving pick from this data.
- Survey top-10 models remain worth tracking for **quality**, but need either admin storage, a quantized HF release, or dual-node + offline staging before serving benchmarks.

## Reproduce plots

```bash
# Check out the results commit
git checkout 7249671d14d9d02749446d8fb10910f5e0bc0f90

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python plot_results.py --index results/index.csv --out figs --format both --table
```

Key figures: `model_comparison`, throughput scaling, latency-throughput, capacity, and `summary_table.md`.
