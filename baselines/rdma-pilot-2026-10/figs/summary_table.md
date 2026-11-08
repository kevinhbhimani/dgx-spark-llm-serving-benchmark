| Model | Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| nemotron-super-49b-nvfp4 | Single Spark | 1 | 1500 | 42 † | 7 | 0.03 | 439/4194 | 136 | 35.0 | 0.00 | 6.306 |
| nemotron-super-49b-nvfp4 | Single Spark | 2 | 1500 | 79 † | 13 | 0.05 | 739/4623 | 139 | 37.5 | 0.00 | 3.484 |
| nemotron-super-49b-nvfp4 | Single Spark | 4 | 1500 | 143 | 25 | 0.10 | 2293/5613 | 143 | 41.1 | 0.00 | 1.996 |
| nemotron-super-49b-nvfp4 | Single Spark | 8 | 1500 | 252 | 43 | 0.17 | 2381/4903 | 149 | 46.9 | 0.00 | 1.282 |
| nemotron-super-49b-nvfp4 | Single Spark | 16 | 1500 | 398 | 68 | 0.27 | 2467/5403 | 163 | 59.8 | 0.00 | 0.913 |
| nemotron-super-49b-nvfp4 | Single Spark | 32 | 1500 | 550 | 95 | 0.37 | 2992/7526 | 196 | 86.3 | 0.00 | 0.712 |
| nemotron-super-49b-nvfp4 | Single Spark | 64 | 1500 | 682 | 118 | 0.45 | 3289/6541 | 262 | 139.1 | 0.00 | 0.630 |
| nemotron-super-49b-nvfp4 | Single Spark | 128 | 1500 | 774 | 132 | 0.52 | 4682/9081 | 1119 | 247.4 | 0.00 | 0.605 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 1 | 1500 | 75 † | 13 | 0.05 | 365/3430 | 75 | 19.4 | 0.04 | 7.291 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 2 | 1500 | 137 | 23 | 0.09 | 448/3558 | 77 | 21.3 | 0.07 | 3.998 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 4 | 1500 | 245 | 42 | 0.16 | 979/4214 | 80 | 24.4 | 0.13 | 2.367 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 8 | 1500 | 383 | 66 | 0.26 | 1771/3606 | 92 | 30.5 | 0.00 | 1.553 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 16 | 1500 | 564 | 97 | 0.38 | 1853/3949 | 106 | 42.1 | 0.00 | 1.126 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 32 | 1500 | 781 | 134 | 0.52 | 2002/5725 | 121 | 61.2 | 0.00 | 0.882 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 64 | 1500 | 927 | 159 | 0.62 | 2107/4433 | 169 | 102.6 | 0.00 | 0.785 |
| nemotron-super-49b-nvfp4 | Dual Spark, TP=2 | 128 | 1500 | 1050 | 179 | 0.70 | 2728/6512 | 880 | 182.5 | 0.00 | 0.738 |
| nemotron-super-49b-nvfp4 | Dual Spark, PP=2 | 1 | 1500 | 40 † | 7 | 0.03 | 442/3385 | 140 | 36.0 | 0.00 | 8.931 |
| nemotron-super-49b-nvfp4 | Dual Spark, PP=2 | 2 | 1500 | 79 † | 13 | 0.05 | 610/3571 | 142 | 37.8 | 0.00 | 4.757 |
| nemotron-super-49b-nvfp4 | Dual Spark, PP=2 | 4 | 1500 | 140 | 24 | 0.09 | 1791/4092 | 147 | 42.7 | 0.00 | 2.955 |
| nemotron-super-49b-nvfp4 | Dual Spark, PP=2 | 8 | 1500 | 223 | 38 | 0.15 | 1862/3586 | 181 | 53.0 | 0.00 | 2.234 |
| nemotron-super-49b-nvfp4 | Dual Spark, PP=2 | 16 | 1500 | 381 | 65 | 0.25 | 1884/3748 | 192 | 63.0 | 0.00 | 1.412 |
| nemotron-super-49b-nvfp4 | Dual Spark, PP=2 | 32 | 1500 | 577 | 100 | 0.38 | 2239/5211 | 214 | 81.9 | 0.00 | 0.966 |
| nemotron-super-49b-nvfp4 | Dual Spark, PP=2 | 64 | 1500 | 769 | 135 | 0.51 | 2117/4376 | 263 | 122.7 | 0.00 | 0.763 |
| nemotron-super-49b-nvfp4 | Dual Spark, PP=2 | 128 | 1500 | 946 | 163 | 0.63 | 2258/5479 | 376 | 201.8 | 0.00 | 0.670 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 1 | 1500 | 25 † | 4 | 0.02 | 920/7214 | 217 | 56.3 | 0.00 | 19.285 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 2 | 1500 | 48 † | 8 | 0.03 | 1105/7713 | 229 | 62.3 | 0.00 | 9.385 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 4 | 1500 | 88 † | 15 | 0.06 | 3072/9203 | 232 | 67.5 | 0.00 | 5.525 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 8 | 1500 | 151 | 25 | 0.10 | 3998/7953 | 248 | 78.7 | 0.00 | 3.670 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 16 | 1500 | 231 | 40 | 0.15 | 4478/8894 | 269 | 103.2 | 0.00 | 2.768 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 32 | 1500 | 293 | 51 | 0.20 | 6348/14745 | 316 | 161.5 | 0.00 | 2.382 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 64 | 1500 | 300 | 53 | 0.20 | 6999/14005 | 401 | 317.2 | 0.00 | 2.448 |
| nemotron-super-49b-bf16 | Dual Spark, TP=2 | 128 | 1500 | 275 | 46 | 0.18 | 18401/51370 | 3521 | 695.2 | 0.00 | 2.776 |
| nemotron-puzzle-75b-nvfp4 | Single Spark | 1 | 1500 | 101 | 17 | 0.07 | 360/2408 | 55 | 14.3 | 0.07 | 2.246 |
| nemotron-puzzle-75b-nvfp4 | Single Spark | 2 | 1500 | 186 | 32 | 0.12 | 469/2601 | 56 | 15.8 | 0.12 | 1.302 |
| nemotron-puzzle-75b-nvfp4 | Single Spark | 4 | 1500 | 306 | 52 | 0.20 | 1247/3423 | 64 | 19.5 | 0.17 | 0.874 |
| nemotron-puzzle-75b-nvfp4 | Single Spark | 8 | 1500 | 456 | 78 | 0.30 | 1527/2896 | 80 | 25.8 | 0.30 | 0.629 |
| nemotron-puzzle-75b-nvfp4 | Single Spark | 16 | 1500 | 634 | 108 | 0.42 | 1561/3137 | 103 | 37.6 | 0.00 | 0.503 |
| nemotron-puzzle-75b-nvfp4 | Single Spark | 32 | 1500 | 820 | 141 | 0.55 | 1846/4591 | 143 | 58.1 | 0.00 | 0.426 |
| nemotron-puzzle-75b-nvfp4 | Single Spark | 64 | 1500 | 1008 | 173 | 0.67 | 2075/4156 | 229 | 94.6 | 0.00 | 0.371 |
| nemotron-puzzle-75b-nvfp4 | Single Spark | 128 | 1500 | 1179 | 201 | 0.79 | 3006/5627 | 725 | 162.4 | 0.00 | 0.340 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 1 | 1500 | 148 | 25 | 0.10 | 256/1783 | 37 | 9.8 | 0.10 | 3.281 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 2 | 1500 | 264 | 45 | 0.18 | 298/1886 | 40 | 11.1 | 0.18 | 1.909 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 4 | 1500 | 434 | 74 | 0.29 | 786/2380 | 44 | 13.7 | 0.29 | 1.229 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 8 | 1500 | 657 | 112 | 0.44 | 1029/2110 | 53 | 18.0 | 0.44 | 0.875 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 16 | 1500 | 891 | 152 | 0.59 | 1070/2327 | 71 | 26.8 | 0.09 | 0.685 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 32 | 1500 | 1166 | 199 | 0.78 | 1202/3224 | 97 | 41.1 | 0.00 | 0.553 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 64 | 1500 | 1472 | 252 | 0.98 | 1276/2462 | 132 | 64.8 | 0.00 | 0.468 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, TP=2 | 128 | 1500 | 1710 | 293 | 1.14 | 1715/3705 | 519 | 112.0 | 0.00 | 0.410 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, PP=2 | 1 | 1500 | 95 † | 16 | 0.06 | 368/1565 | 59 | 15.4 | 0.06 | 3.293 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, PP=2 | 2 | 1500 | 176 | 30 | 0.12 | 432/1708 | 62 | 16.8 | 0.12 | 1.875 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, PP=2 | 4 | 1500 | 298 | 51 | 0.20 | 1013/2058 | 70 | 20.1 | 0.20 | 1.272 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, PP=2 | 8 | 1500 | 509 | 87 | 0.34 | 1006/1790 | 74 | 23.3 | 0.34 | 0.972 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, PP=2 | 16 | 1500 | 719 | 123 | 0.48 | 1038/2646 | 98 | 33.2 | 0.00 | 0.713 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, PP=2 | 32 | 1500 | 1053 | 178 | 0.70 | 1225/2983 | 120 | 45.5 | 0.00 | 0.537 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, PP=2 | 64 | 1500 | 1340 | 229 | 0.89 | 1233/2172 | 162 | 71.2 | 0.00 | 0.444 |
| nemotron-puzzle-75b-nvfp4 | Dual Spark, PP=2 | 128 | 1500 | 1543 | 267 | 1.03 | 1403/3208 | 305 | 123.3 | 0.00 | 0.344 |
| nemotron-puzzle-75b-bf16 | Dual Spark, TP=2 | 1 | 1500 | 104 | 18 | 0.07 | 437/2821 | 53 | 13.9 | 0.07 | 4.244 |
| nemotron-puzzle-75b-bf16 | Dual Spark, TP=2 | 2 | 1500 | 185 | 32 | 0.12 | 489/3022 | 55 | 15.8 | 0.12 | 2.259 |
| nemotron-puzzle-75b-bf16 | Dual Spark, TP=2 | 4 | 1500 | 288 | 49 | 0.19 | 1293/3701 | 67 | 20.6 | 0.15 | 1.571 |
| nemotron-puzzle-75b-bf16 | Dual Spark, TP=2 | 8 | 1500 | 409 | 70 | 0.27 | 1666/3228 | 86 | 28.7 | 0.01 | 1.192 |
| nemotron-puzzle-75b-bf16 | Dual Spark, TP=2 | 16 | 1500 | 544 | 93 | 0.36 | 1751/3631 | 118 | 43.9 | 0.00 | 0.970 |
| nemotron-puzzle-75b-bf16 | Dual Spark, TP=2 | 32 | 1500 | 691 | 119 | 0.46 | 2125/5253 | 165 | 68.9 | 0.00 | 0.809 |
| nemotron-puzzle-75b-bf16 | Dual Spark, TP=2 | 64 | 1500 | 848 | 146 | 0.57 | 2229/4270 | 224 | 112.3 | 0.00 | 0.751 |
| nemotron-puzzle-75b-bf16 | Dual Spark, TP=2 | 128 | 1500 | 977 | 167 | 0.65 | 3063/6436 | 908 | 196.1 | 0.00 | 0.662 |
| nemotron-puzzle-75b-bf16 | Dual Spark, PP=2 | 1 | 1500 | 64 † | 11 | 0.04 | 763/2838 | 88 | 23.1 | 0.04 | 4.924 |
| nemotron-puzzle-75b-bf16 | Dual Spark, PP=2 | 2 | 1500 | 112 | 19 | 0.07 | 852/3000 | 96 | 26.6 | 0.03 | 2.734 |
| nemotron-puzzle-75b-bf16 | Dual Spark, PP=2 | 4 | 1500 | 175 | 30 | 0.12 | 1882/3536 | 119 | 34.4 | 0.00 | 1.930 |
| nemotron-puzzle-75b-bf16 | Dual Spark, PP=2 | 8 | 1500 | 277 | 47 | 0.18 | 1892/3045 | 142 | 42.9 | 0.00 | 1.511 |
| nemotron-puzzle-75b-bf16 | Dual Spark, PP=2 | 16 | 1500 | 388 | 66 | 0.26 | 1933/3598 | 189 | 60.4 | 0.00 | 1.148 |
| nemotron-puzzle-75b-bf16 | Dual Spark, PP=2 | 32 | 1500 | 566 | 98 | 0.38 | 2499/4723 | 220 | 83.0 | 0.00 | 0.842 |
| nemotron-puzzle-75b-bf16 | Dual Spark, PP=2 | 64 | 1500 | 778 | 135 | 0.52 | 2366/4002 | 268 | 115.9 | 0.00 | 0.685 |
| nemotron-puzzle-75b-bf16 | Dual Spark, PP=2 | 128 | 1500 | 878 | 151 | 0.59 | 2639/5176 | 614 | 212.4 | 0.00 | 0.583 |
| qwen3-next-80b-nvfp4 | Single Spark | 1 | 1500 | 170 | 29 | 0.11 | 249/1779 | 32 | 8.5 | 0.11 | 1.538 |
| qwen3-next-80b-nvfp4 | Single Spark | 2 | 1500 | 308 | 53 | 0.21 | 310/1906 | 33 | 9.4 | 0.21 | 0.853 |
| qwen3-next-80b-nvfp4 | Single Spark | 4 | 1500 | 480 | 82 | 0.32 | 913/2542 | 40 | 12.4 | 0.32 | 0.576 |
| qwen3-next-80b-nvfp4 | Single Spark | 8 | 1500 | 697 | 119 | 0.46 | 1098/2169 | 49 | 16.9 | 0.46 | 0.418 |
| qwen3-next-80b-nvfp4 | Single Spark | 16 | 1500 | 953 | 163 | 0.64 | 1167/2426 | 62 | 25.0 | 0.62 | 0.316 |
| qwen3-next-80b-nvfp4 | Single Spark | 32 | 1500 | 1188 | 203 | 0.79 | 1440/3621 | 83 | 40.4 | 0.00 | 0.267 |
| qwen3-next-80b-nvfp4 | Single Spark | 64 | 1500 | 1278 | 219 | 0.85 | 1791/3569 | 194 | 74.8 | 0.00 | 0.248 |
| qwen3-next-80b-nvfp4 | Dual Spark, TP=2 | 1 | 1500 | 214 | 37 | 0.14 | 213/1330 | 25 | 6.7 | 0.14 | 2.349 |
| qwen3-next-80b-nvfp4 | Dual Spark, TP=2 | 2 | 1500 | 384 | 66 | 0.26 | 236/1538 | 27 | 7.6 | 0.26 | 1.288 |
| qwen3-next-80b-nvfp4 | Dual Spark, TP=2 | 4 | 1500 | 618 | 106 | 0.41 | 710/1657 | 31 | 9.6 | 0.41 | 0.823 |
| qwen3-next-80b-nvfp4 | Dual Spark, TP=2 | 8 | 1500 | 927 | 158 | 0.62 | 757/1464 | 37 | 12.8 | 0.62 | 0.576 |
| qwen3-next-80b-nvfp4 | Dual Spark, TP=2 | 16 | 1500 | 1260 | 216 | 0.84 | 811/1789 | 47 | 18.9 | 0.84 | 0.428 |
| qwen3-next-80b-nvfp4 | Dual Spark, TP=2 | 32 | 1500 | 1603 | 274 | 1.07 | 944/2452 | 62 | 30.0 | 0.00 | 0.351 |
| qwen3-next-80b-nvfp4 | Dual Spark, TP=2 | 64 | 1500 | 1753 | 300 | 1.17 | 1100/2265 | 90 | 54.5 | 0.00 | 0.329 |
| qwen3-next-80b-nvfp4 | Dual Spark, TP=2 | 128 | 1500 | 1819 | 310 | 1.21 | 1733/3463 | 486 | 105.2 | 0.00 | 0.310 |
| qwen3-next-80b-nvfp4 | Dual Spark, PP=2 | 1 | 1500 | 158 | 27 | 0.11 | 250/1121 | 35 | 9.2 | 0.11 | 2.160 |
| qwen3-next-80b-nvfp4 | Dual Spark, PP=2 | 2 | 1500 | 288 | 49 | 0.19 | 288/1192 | 37 | 10.2 | 0.19 | 1.198 |
| qwen3-next-80b-nvfp4 | Dual Spark, PP=2 | 4 | 1500 | 471 | 80 | 0.31 | 718/1387 | 44 | 12.7 | 0.31 | 0.794 |
| qwen3-next-80b-nvfp4 | Dual Spark, PP=2 | 8 | 1500 | 752 | 128 | 0.50 | 720/1213 | 54 | 15.7 | 0.50 | 0.662 |
| qwen3-next-80b-nvfp4 | Dual Spark, PP=2 | 16 | 1500 | 997 | 170 | 0.66 | 745/1449 | 73 | 24.2 | 0.66 | 0.515 |
| qwen3-next-80b-nvfp4 | Dual Spark, PP=2 | 32 | 1500 | 1480 | 254 | 0.99 | 840/1893 | 85 | 32.0 | 0.00 | 0.365 |
| qwen3-next-80b-nvfp4 | Dual Spark, PP=2 | 64 | 1500 | 1823 | 312 | 1.22 | 955/1626 | 110 | 52.5 | 0.00 | 0.299 |
| qwen3-next-80b-nvfp4 | Dual Spark, PP=2 | 128 | 1500 | 1954 | 336 | 1.30 | 1171/2199 | 312 | 97.8 | 0.00 | 0.271 |
| qwen3-next-80b-bf16 | Dual Spark, TP=2 | 1 | 1500 | 217 | 37 | 0.14 | 310/1699 | 24 | 6.6 | 0.14 | 1.792 |
| qwen3-next-80b-bf16 | Dual Spark, TP=2 | 2 | 1500 | 348 | 59 | 0.23 | 337/2016 | 29 | 8.4 | 0.23 | 1.085 |
| qwen3-next-80b-bf16 | Dual Spark, TP=2 | 4 | 1500 | 486 | 83 | 0.32 | 807/2203 | 39 | 12.2 | 0.32 | 0.820 |
| qwen3-next-80b-bf16 | Dual Spark, TP=2 | 8 | 1500 | 659 | 112 | 0.44 | 1047/1983 | 53 | 18.0 | 0.44 | 0.646 |
| qwen3-next-80b-bf16 | Dual Spark, TP=2 | 16 | 1500 | 856 | 146 | 0.57 | 1145/2307 | 73 | 27.9 | 0.00 | 0.531 |
| qwen3-next-80b-bf16 | Dual Spark, TP=2 | 32 | 1500 | 1051 | 179 | 0.70 | 1423/3500 | 104 | 45.7 | 0.00 | 0.464 |
| qwen3-next-80b-bf16 | Dual Spark, TP=2 | 64 | 1500 | 1141 | 196 | 0.76 | 1718/3185 | 142 | 83.8 | 0.00 | 0.447 |
| qwen3-next-80b-bf16 | Dual Spark, TP=2 | 128 | 1500 | 1129 | 194 | 0.75 | 2584/5535 | 780 | 169.1 | 0.00 | 0.479 |
| qwen3-next-80b-bf16 | Dual Spark, PP=2 | 1 | 1500 | 134 | 23 | 0.09 | 469/1699 | 41 | 10.8 | 0.09 | 2.178 |
| qwen3-next-80b-bf16 | Dual Spark, PP=2 | 2 | 1500 | 217 | 37 | 0.14 | 510/1792 | 49 | 13.4 | 0.14 | 1.336 |
| qwen3-next-80b-bf16 | Dual Spark, PP=2 | 4 | 1500 | 302 | 52 | 0.20 | 1139/2157 | 68 | 19.7 | 0.20 | 1.017 |
| qwen3-next-80b-bf16 | Dual Spark, PP=2 | 8 | 1500 | 407 | 70 | 0.27 | 1190/1871 | 101 | 28.9 | 0.02 | 0.909 |
| qwen3-next-80b-bf16 | Dual Spark, PP=2 | 16 | 1500 | 519 | 89 | 0.35 | 1211/2294 | 153 | 46.9 | 0.00 | 0.748 |
| qwen3-next-80b-bf16 | Dual Spark, PP=2 | 32 | 1500 | 782 | 134 | 0.52 | 1576/3004 | 182 | 60.9 | 0.00 | 0.545 |
| qwen3-next-80b-bf16 | Dual Spark, PP=2 | 64 | 1500 | 997 | 173 | 0.66 | 1608/2722 | 224 | 95.0 | 0.00 | 0.462 |
| qwen3-next-80b-bf16 | Dual Spark, PP=2 | 128 | 1500 | 1098 | 191 | 0.73 | 2066/3799 | 601 | 172.7 | 0.00 | 0.437 |

† fewer than 100 requests; p99 is small-sample noise.
