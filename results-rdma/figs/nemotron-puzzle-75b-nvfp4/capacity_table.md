| Config | HF weight (GiB) | HF KV (GiB) | Worker weight (GiB) | Worker KV (GiB) | KV tokens | GPU blocks | Block | KV dtype | GPU mem util | max_model_len | max conc. | Hybrid cache | KV token source | vLLM |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Dual Spark, TP=2 | -- | -- | 23.0 | 71.5 | 6,240,640 | 5,971 | 6272 | fp8_e4m3 | 0.80 | 8,704 | 853.0 | yes | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |

HF weight/KV cache columns are optional full-model estimates. Worker weight/KV columns come from the vLLM startup log and describe the actual per-worker serving process when a server log was captured.
