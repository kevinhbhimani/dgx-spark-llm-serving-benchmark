| Model | Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 1 | 1500 | 148 | 25 | 0.10 | 256/1783 | 37 | 9.8 | 0.10 | 3.281 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 2 | 1500 | 264 | 45 | 0.18 | 298/1886 | 40 | 11.1 | 0.18 | 1.909 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 4 | 1500 | 434 | 74 | 0.29 | 786/2380 | 44 | 13.7 | 0.29 | 1.229 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 8 | 1500 | 657 | 112 | 0.44 | 1029/2110 | 53 | 18.0 | 0.44 | 0.875 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 16 | 1500 | 891 | 152 | 0.59 | 1070/2327 | 71 | 26.8 | 0.09 | 0.685 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 32 | 1500 | 1166 | 199 | 0.78 | 1202/3224 | 97 | 41.1 | 0.00 | 0.553 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 64 | 1500 | 1472 | 252 | 0.98 | 1276/2462 | 132 | 64.8 | 0.00 | 0.468 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 128 | 1500 | 1710 | 293 | 1.14 | 1715/3705 | 519 | 112.0 | 0.00 | 0.410 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 1 | 1500 | 25 † | 4 | 0.02 | 920/7214 | 217 | 56.3 | 0.00 | 19.285 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 2 | 1500 | 48 † | 8 | 0.03 | 1105/7713 | 229 | 62.3 | 0.00 | 9.385 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 4 | 1500 | 88 † | 15 | 0.06 | 3072/9203 | 232 | 67.5 | 0.00 | 5.525 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 8 | 1500 | 151 | 25 | 0.10 | 3998/7953 | 248 | 78.7 | 0.00 | 3.670 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 16 | 1500 | 231 | 40 | 0.15 | 4478/8894 | 269 | 103.2 | 0.00 | 2.768 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 32 | 1500 | 293 | 51 | 0.20 | 6348/14745 | 316 | 161.5 | 0.00 | 2.382 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 64 | 1500 | 300 | 53 | 0.20 | 6999/14005 | 401 | 317.2 | 0.00 | 2.448 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 128 | 1500 | 275 | 46 | 0.18 | 18401/51370 | 3521 | 695.2 | 0.00 | 2.776 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 1 | 1500 | 75 † | 13 | 0.05 | 365/3430 | 75 | 19.4 | 0.04 | 7.291 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 2 | 1500 | 137 | 23 | 0.09 | 448/3558 | 77 | 21.3 | 0.07 | 3.998 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 4 | 1500 | 245 | 42 | 0.16 | 979/4214 | 80 | 24.4 | 0.13 | 2.367 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 8 | 1500 | 383 | 66 | 0.26 | 1771/3606 | 92 | 30.5 | 0.00 | 1.553 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 16 | 1500 | 564 | 97 | 0.38 | 1853/3949 | 106 | 42.1 | 0.00 | 1.126 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 32 | 1500 | 781 | 134 | 0.52 | 2002/5725 | 121 | 61.2 | 0.00 | 0.882 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 64 | 1500 | 927 | 159 | 0.62 | 2107/4433 | 169 | 102.6 | 0.00 | 0.785 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 128 | 1500 | 1050 | 179 | 0.70 | 2728/6512 | 880 | 182.5 | 0.00 | 0.738 |

† fewer than 100 requests; p99 is small-sample noise.
