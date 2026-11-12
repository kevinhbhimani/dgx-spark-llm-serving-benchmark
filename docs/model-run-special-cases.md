# Model run special cases

This guide records model-specific launch requirements in [configs/models.yaml](../configs/models.yaml) and shared settings in [scripts/launch_server.sh](../scripts/launch_server.sh). The pinned container is NVIDIA NGC vLLM 26.09 with vLLM 0.29.0, as set in [scripts/connectx_env.sh](../scripts/connectx_env.sh). Recheck each entry after changing a model revision or the container image.

## Model-specific requirements

| Checkpoint | Run requirement or matrix policy | Documentation |
| --- | --- | --- |
| `meta-llama/Llama-3.1-8B-Instruct` | The Hugging Face repository is gated. Accept Meta's Llama 3.1 terms and provide `HF_TOKEN` to the head node before model download. | [Meta Llama 3.1 model page](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct) |
| `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` | The NVIDIA 26.09 sequence-limit note names Nemotron Nano V3 and Nemotron Super V3 NVFP4. It does not name the Lightning checkpoint. The current matrix leaves `max_num_seqs` unset for Lightning. Check high-concurrency behavior before applying a cap to this variant. | [NVIDIA vLLM 26.09 known issues](https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/rel-26-09.html#known-issues), [Nemotron 3.5 Lightning model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4) |
| `nvidia/Gemma-4-26B-A4B-NVFP4` | The pinned vLLM 0.29.0 image raises `NotImplementedError` for the `FLASHINFER_CUTLASS` NVFP4 MoE path when TP2 needs gated `w1`/`w3` padding. The matrix enables `single` and `pp2` and omits `tp2`. Recheck TP2 with a canary after changing the image. | [Gemma 4 26B NVFP4 model card](https://huggingface.co/nvidia/Gemma-4-26B-A4B-NVFP4), [vLLM Gemma NVFP4 TP2 issue](https://github.com/vllm-project/vllm/issues/42516), [current model matrix](../configs/models.yaml) |
| `nvidia/Qwen3-235B-A22B-Instruct-2507-NVFP4`, `nvidia/Mistral-Medium-3.5-128B-NVFP4`, and `Qwen/Qwen3-Next-80B-A3B-Instruct` | The matrix lists only `tp2` and `pp2` for these checkpoints. Preserve those placement restrictions when selecting a serving config. | [current model matrix](../configs/models.yaml), [Qwen3-Next model card](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct), [Qwen3-235B NVFP4 model card](https://huggingface.co/nvidia/Qwen3-235B-A22B-Instruct-2507-NVFP4), [Mistral Medium NVFP4 model card](https://huggingface.co/nvidia/Mistral-Medium-3.5-128B-NVFP4) |
| `nvidia/Qwen3-Next-80B-A3B-Instruct-NVFP4` and `Qwen/Qwen3-Next-80B-A3B-Instruct` | The matrix sets `trust_remote_code: true` for both variants. The BF16 variant is dual-node only. | [Qwen3-Next model card](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct), [current model matrix](../configs/models.yaml) |

The `configs` list in the model matrix is the allowed placement set for each checkpoint. The launcher reads `max_num_seqs` from that matrix, passes it as `--max-num-seqs`, and includes it in the serving fingerprint.

## Shared launch settings

| Setting | Reason | Documentation |
| --- | --- | --- |
| `FLASHINFER_AUTOTUNE=0` | The pinned vLLM 0.29.0 build can hang during PP2 startup when pipeline stages autotune different kernels. The launcher passes `--no-enable-flashinfer-autotune` by default for every model and placement. Keep the setting until a replacement image passes the PP2 canary. | [vLLM PP-aware autotune change](https://github.com/vllm-project/vllm/pull/57197), [RDMA readiness notes](rdma-readiness.md#publication-checks-and-interpretation) |
| `--language-model-only` | The launcher uses a text-only workload and passes this option for all models. vLLM defines the option as disabling multimodal inputs. | [vLLM 0.29 CLI reference](https://docs.vllm.ai/en/v0.29.0/cli/serve/), [launcher](../scripts/launch_server.sh) |
| `TORCHINDUCTOR_AUTOGRAD_CACHE=0` | The pinned PyTorch build can fail while serializing AOTAutograd graph caches during on-the-fly FP8 quantization. The launcher keeps this cache disabled. The prequantized matrix entries leave `quant` unset. | [launcher](../scripts/launch_server.sh), [model matrix](../configs/models.yaml) |
| `HF_HUB_OFFLINE=1` for `tp2` and `pp2` | Dual-node serving uses the pinned snapshot mirrored to both node caches. `run_models.sh` acquires and mirrors selected models before launch. | [campaign runbook](campaign-runbook.md), [model runner](../scripts/run_models.sh) |

## Applying a run requirement

Put per-checkpoint settings such as `trust_remote_code` and `max_num_seqs` in `configs/models.yaml`. The model runner forwards these settings and fingerprints them in the serving policy. An alternate model YAML must carry the same required fields for each included checkpoint.

Use a fresh `OUT_DIR` after changing a serving setting. Resume checks reject rows whose serving fingerprint differs from the requested policy. Run a canary for every listed placement after changing a model revision or container image.
