| Model | Config | HF weight (GiB) | HF KV (GiB) | Worker weight (GiB) | Worker KV (GiB) | KV tokens | GPU blocks | Block | KV dtype | GPU mem util | max_model_len | max conc. | Hybrid cache | KV token source | vLLM |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| qwen3-8b | Single Spark | -- | -- | 15.3 | 77.7 | 565,488 | 35,343 | 16 | auto | 0.80 | 8,704 | 65.0 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| qwen3-8b | Dual Spark, TP=2 | -- | -- | 7.6 | 86.2 | 1,255,680 | 78,480 | 16 | auto | 0.80 | 8,704 | 144.3 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| qwen3-8b | Dual Spark, PP=2 | -- | -- | 7.6 | 86.1 | 1,253,680 | 78,355 | 16 | auto | 0.80 | 8,704 | 144.0 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| qwen3-32b | Single Spark | -- | -- | 61.0 | 30.8 | 126,320 | 7,895 | 16 | auto | 0.80 | 8,704 | 14.5 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| qwen3-32b | Dual Spark, TP=2 | -- | -- | 30.6 | 63.1 | 512,704 | 32,044 | 16 | auto | 0.80 | 8,704 | 58.9 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| qwen3-32b | Dual Spark, PP=2 | -- | -- | 30.5 | 63.3 | 511,888 | 31,993 | 16 | auto | 0.80 | 8,704 | 58.8 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| qwen3-30b-a3b | Single Spark | -- | -- | 56.9 | 35.9 | 392,176 | 24,511 | 16 | auto | 0.80 | 8,704 | 45.1 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| qwen3-30b-a3b | Dual Spark, TP=2 | -- | -- | 28.5 | 64.9 | 1,417,184 | 88,574 | 16 | auto | 0.80 | 8,704 | 162.8 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| qwen3-30b-a3b | Dual Spark, PP=2 | -- | -- | 29.0 | 65.1 | 1,412,432 | 88,277 | 16 | auto | 0.80 | 8,704 | 162.3 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.3-70b-fp8 | Dual Spark, TP=2 | -- | -- | 33.9 | 58.6 | 383,840 | 23,990 | 16 | auto | 0.80 | 8,704 | 44.1 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.3-70b-nvfp4 | Single Spark | -- | -- | 39.9 | 51.1 | 335,232 | 20,952 | 16 | fp8_e4m3 | 0.80 | 8,704 | 38.5 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.3-70b-nvfp4 | Dual Spark, TP=2 | -- | -- | 19.9 | 72.9 | 955,280 | 59,705 | 16 | fp8_e4m3 | 0.80 | 8,704 | 109.8 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.3-70b-nvfp4 | Dual Spark, PP=2 | -- | -- | 20.0 | 72.8 | 954,832 | 59,677 | 16 | fp8_e4m3 | 0.80 | 8,704 | 109.7 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| nemotron3-nano-30b-a3b-nvfp4 | Single Spark | -- | -- | 18.6 | 74.4 | 5,199,120 | 6,228 | 4176 | fp8_e4m3 | 0.80 | 8,704 | 889.7 | yes | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| nemotron3-nano-30b-a3b-nvfp4 | Dual Spark, TP=2 | -- | -- | 9.8 | 84.4 | 11,730,384 | 14,049 | 4176 | fp8_e4m3 | 0.80 | 8,704 | 2007.0 | yes | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| nemotron3-nano-30b-a3b-nvfp4 | Dual Spark, PP=2 | -- | -- | 9.7 | 83.7 | 11,701,152 | 14,014 | 4176 | fp8_e4m3 | 0.80 | 8,704 | 2002.0 | yes | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| nemotron3-super-120b-a12b-nvfp4 | Single Spark | -- | -- | 69.5 | 24.0 | 1,048,320 | 757 | 8320 | fp8_e4m3 | 0.80 | 8,704 | 108.1 | yes | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| nemotron3-super-120b-a12b-nvfp4 | Dual Spark, TP=2 | -- | -- | 36.6 | 56.4 | 4,925,440 | 3,556 | 8320 | fp8_e4m3 | 0.80 | 8,704 | 508.0 | yes | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| nemotron3-super-120b-a12b-nvfp4 | Dual Spark, PP=2 | -- | -- | 35.6 | 57.6 | 5,033,600 | 3,632 | 8320 | fp8_e4m3 | 0.80 | 8,704 | 518.9 | yes | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b | Single Spark | -- | -- | 15.0 | 78.2 | 640,608 | 40,038 | 16 | auto | 0.80 | 8,704 | 73.6 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b | Dual Spark, TP=2 | -- | -- | 7.5 | 86.5 | 1,414,096 | 88,381 | 16 | auto | 0.80 | 8,704 | 162.5 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b | Dual Spark, PP=2 | -- | -- | 7.5 | 86.8 | 1,415,120 | 88,445 | 16 | auto | 0.80 | 8,704 | 162.6 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b-fp8 | Single Spark | -- | -- | 8.5 | 84.7 | 1,387,888 | 86,743 | 16 | fp8_e4m3 | 0.80 | 8,704 | 159.4 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b-fp8 | Dual Spark, TP=2 | -- | -- | 4.3 | 89.7 | 2,935,040 | 183,440 | 16 | fp8_e4m3 | 0.80 | 8,704 | 337.2 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b-fp8 | Dual Spark, PP=2 | -- | -- | 4.3 | 90.0 | 2,942,928 | 183,933 | 16 | fp8_e4m3 | 0.80 | 8,704 | 338.1 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b-nvfp4 | Single Spark | -- | -- | 5.7 | 87.7 | 1,436,352 | 89,772 | 16 | fp8_e4m3 | 0.80 | 8,704 | 165.0 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b-nvfp4 | Dual Spark, TP=2 | -- | -- | 2.9 | 91.2 | 2,987,536 | 186,721 | 16 | fp8_e4m3 | 0.80 | 8,704 | 343.2 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |
| llama3.1-8b-nvfp4 | Dual Spark, PP=2 | -- | -- | 2.8 | 91.3 | 2,993,120 | 187,070 | 16 | fp8_e4m3 | 0.80 | 8,704 | 343.9 | no | vllm_startup_log | 0.19.0+6bc3197f.nv26.04.48680843 |

HF weight/KV cache columns are optional full-model estimates. Worker weight/KV columns come from the vLLM startup log and describe the actual per-worker serving process when a server log was captured.
