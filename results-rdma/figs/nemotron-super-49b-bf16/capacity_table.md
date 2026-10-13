| Config | HF weight (GiB) | HF KV (GiB) | Worker weight (GiB) | Worker KV (GiB) | KV tokens | GPU blocks | Block | KV dtype | GPU mem util | max_model_len | max conc. | Hybrid cache | KV token source | vLLM |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Dual Spark, TP=2 | -- | -- | 46.5 | 47.6 | 508,624 | 31,789 | 16 | auto | 0.80 | 8,704 | 58.4 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |

HF weight/KV cache columns are optional full-model estimates. Worker weight/KV columns come from the vLLM startup log and describe the actual per-worker serving process when a server log was captured.
