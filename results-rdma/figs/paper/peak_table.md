| Model | Prec. | Config | Peak tok/s | @c | tok/s/node | TTFT@1 (ms) | Pooled ITL@1 (ms) | J/tok@peak |
|---|---|---|--:|--:|--:|--:|--:|--:|
| nemotron-super-49b-bf16 | BF16 | Dual Spark, TP=2 | 53 | 64 | 26 | 920 | 217 | 2.45 |
| nemotron-super-49b-nvfp4 | NVFP4 | Dual Spark, TP=2 | 179 | 128 | 90 | 365 | 75 | 0.74 |
| nemotron-puzzle-75b-nvfp4 | NVFP4 | Dual Spark, TP=2 | 293 | 128 | 146 | 256 | 37 | 0.41 |
