# NVFP4 / BF16 model selection for 1–2 node DGX Spark benchmarks

Source: `rdma-testing/docs/models-to-test.csv`.

## Constraints

- DGX Spark node: 128 GB Grace unified memory, 102.4 GB effective at `--gpu-memory-utilization 0.80`.
- Serving configs: `single` (1 node, TP=1 PP=1), `tp2` (2 nodes, TP=2 PP=1), `pp2` (2 nodes, TP=1 PP=2).
- Workload: `max_model_len` 8704 (8192 input + 512 output).
- Every selected model has a pre-quantized NVFP4 checkpoint on Hugging Face.
- Models requiring 3 or more nodes at NVFP4 precision are excluded.

## Models with both NVFP4 and BF16

### nemotron-super-49b (nvidia/Llama-3_3-Nemotron-Super-49B-v1_5, Dense 49.9B)

| Precision | Repo | Weight (GB) | Nodes | Configs |
|-----------|------|-------------|-------|---------|
| NVFP4 | `nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4` | 31.1 | 1 | single, tp2, pp2 |
| BF16 | `nvidia/Llama-3_3-Nemotron-Super-49B-v1_5` | 99.8 | 1 | single, tp2, pp2 |

- Architecture: `DeciLMForCausalLM`. Requires `trust_remote_code: true`.
- BF16 weight 99.8 GB + KV cache ~1.6 GB (8704 tokens) = 101.4 GB. Under 102.4 GB budget.
- NAS heterogeneous blocks produce uneven PP splits in compute.

### nemotron-puzzle-75b (nvidia/NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B, MoE 78.3B total / 9.0B active)

| Precision | Repo | Weight (GB) | Nodes | Configs |
|-----------|------|-------------|-------|---------|
| NVFP4 | `nvidia/NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B-NVFP4` | 53.5 | 1 | single, tp2, pp2 |
| BF16 | `nvidia/NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B-BF16` | 156.6 | 2 | tp2, pp2 |

- Architecture: `NemotronHPuzzleForCausalLM`. Requires `trust_remote_code: true`.
- BF16 entry omits `single` because 156.6 GB exceeds one node.
- The related Nemotron-3-Nano NVFP4 checkpoint crashes on GB10 during CUDA graph capture. Workaround: `VLLM_USE_FLASHINFER_MOE_FP4=0`. Apply to this model if the same crash occurs.

### qwen3-next-80b (Qwen3-Next-80B-A3B-Instruct, MoE 80.0B total / 3.0B active)

| Precision | Repo | Weight (GB) | Nodes | Configs |
|-----------|------|-------------|-------|---------|
| NVFP4 | `nvidia/Qwen3-Next-80B-A3B-Instruct-NVFP4` | 46.3 | 1 | single, tp2, pp2 |
| BF16 | `Qwen/Qwen3-Next-80B-A3B-Instruct` | 160.0 | 2 | tp2, pp2 |

- Architecture: `Qwen3NextForCausalLM`. Requires `trust_remote_code: true`.
- BF16 entry omits `single` because 160.0 GB exceeds one node.
- Weight sizes are estimated. The BF16 repo publishes no safetensors index metadata. Verify actual memory consumption at launch.

## Models excluded

### NVFP4-only (no BF16 repo, excluded to limit download size)

| Model | NVFP4 (GB) | Reason |
|-------|-----------|--------|
| `nvidia/DeepSeek-V4-Flash-NVFP4` | 168.2 | No BF16 repo |
| `nvidia/MiniMax-M2.5-NVFP4` | 139.9 | No BF16 or FP8 repo |
| `nvidia/Qwen3-235B-A22B-Instruct-2507-NVFP4` | 139.2 | BF16 needs 5 nodes |

### Require 3 or more nodes at NVFP4

| Model | NVFP4 (GB) | Min nodes |
|-------|-----------|-----------|
| `nvidia/Qwen3-Coder-480B-A35B-Instruct-NVFP4` | 272.9 | 3 |
| `nvidia/Llama-3.1-405B-Instruct-NVFP4` | 234.3 | 3 |
| `nvidia/GLM-4.7-NVFP4` | 229.9 | 3 |
| `nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4` | 320.1 | 4 |

## KV cache precision

NVFP4 checkpoints carry `kv_cache_quant_algo` in `hf_quant_config.json`. NVIDIA ModelOpt NVFP4 artifacts set this to FP8. vLLM auto-applies it. BF16 entries use unquantized BF16 KV cache.

The existing Llama-3.1-8B ladder has the same split: `llama3.1-8b-fp8` uses FP8 KV cache, `llama3.1-8b` uses BF16 KV cache.

Verify the `kv_cache_quant_algo` field in each NVFP4 checkpoint's `hf_quant_config.json` before running. If a checkpoint specifies a different KV cache dtype, record it in the results.

## RDMA

Set `NCCL_IB_DISABLE=0` in the environment before starting Ray. `scripts/launch_server.sh` reads this variable.

## Revision pinning

Revisions pinned to HEAD of each repo as of 2026-10-08:

| Model | Revision |
|-------|----------|
| `nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4` | `f0caaafd5152e07527c7a14e04aa67823107529f` |
| `nvidia/Llama-3_3-Nemotron-Super-49B-v1_5` | `420ba7d28211abf116b8b103ab700d92619daf98` |
| `nvidia/NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B-NVFP4` | `1d370e47fbc56d1019a471c2339663cdbbb5236f` |
| `nvidia/NVIDIA-Nemotron-Labs-3-Puzzle-75B-A9B-BF16` | `8fe5546888e9bd03fdbf52d808adebdfca901b52` |
| `nvidia/Qwen3-Next-80B-A3B-Instruct-NVFP4` | `8fb2682f136cf94d932a498f18cb1e428832a912` |
| `Qwen/Qwen3-Next-80B-A3B-Instruct` | `9c7f2fbe84465e40164a94cc16cd30b6999b0cc7` |

Pre-download before running:
```bash
scripts/predownload.sh all
scripts/mirror_worker_cache.sh all
```

## Discrepancies

| Discrepancy | Models affected | Action |
|-------------|----------------|--------|
| KV cache: FP8 (NVFP4) vs BF16 (BF16) | All pairs | Matches existing ladder pattern. Record actual `kv_cache_quant_algo` per checkpoint. |
| NVFP4 has `single`, BF16 does not | nemotron-puzzle-75b, qwen3-next-80b | BF16 weight exceeds one node. Matches llama3.3-70b pattern. |
| Estimated weight sizes | qwen3-next-80b | Verify at launch. |
| CUDA graph capture crash risk | nemotron-puzzle-75b | Set `VLLM_USE_FLASHINFER_MOE_FP4=0` if crash occurs. |
| Uneven PP splits | nemotron-super-49b | Affects TP-vs-PP comparison shape. |