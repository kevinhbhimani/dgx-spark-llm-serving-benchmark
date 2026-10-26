| Config | HF weight (GiB) | HF KV (GiB) | Worker weight (GiB) | Worker KV (GiB) | KV tokens | GPU blocks | Block | KV dtype | GPU mem util | max_model_len | max conc. | Hybrid cache | KV token source | vLLM |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | -- | -- | 17.9 | 74.9 | 7,792,566 | 6,267 | 4176 | fp8_e4m3 | 0.80 | 8,704 | 895.3 | no | vllm_startup_log | 0.29.0+5013de39.dev |

HF weight/KV cache columns are optional full-model estimates. Worker weight/KV columns come from the vLLM startup log and describe the actual per-worker serving process when a server log was captured.
