# Run-to-run variance subset

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | median ITL | median TTFT |
|---|---|---|---|---|---|---|---|
| llama3.1-8b | single | 32 | 3 | 131 tok/s (CV 0.4%, 95%CI +/-1.27) | 0 req/s (CV n/a, 95%CI +/-0) | 141 ms (CV 0.7%, 95%CI +/-2.52) | 2.43e+03 ms (CV 0.7%, 95%CI +/-43.8) |
| llama3.1-8b | tp2 | 32 | 3 | 129 tok/s (CV 0.5%, 95%CI +/-1.64) | 0 req/s (CV n/a, 95%CI +/-0) | 112 ms (CV 1.3%, 95%CI +/-3.65) | 2.42e+03 ms (CV 2.2%, 95%CI +/-135) |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 8 | 3 | 187 tok/s (CV 1.3%, 95%CI +/-5.84) | 0.73 req/s (CV 1.4%, 95%CI +/-0.0248) | 36.4 ms (CV 1.5%, 95%CI +/-1.38) | 427 ms (CV 0.5%, 95%CI +/-5.35) |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 128 | 3 | 586 tok/s (CV 0.9%, 95%CI +/-13.6) | 0 req/s (CV n/a, 95%CI +/-0) | 159 ms (CV 1.0%, 95%CI +/-3.79) | 621 ms (CV 0.5%, 95%CI +/-7.97) |
| nemotron3-nano-30b-a3b-nvfp4 | single | 8 | 3 | 182 tok/s (CV 0.5%, 95%CI +/-2.36) | 0.711 req/s (CV 0.6%, 95%CI +/-0.00994) | 35.2 ms (CV 0.7%, 95%CI +/-0.639) | 603 ms (CV 0.8%, 95%CI +/-11.7) |
| nemotron3-nano-30b-a3b-nvfp4 | single | 128 | 3 | 512 tok/s (CV 0.6%, 95%CI +/-7.49) | 0 req/s (CV n/a, 95%CI +/-0) | 279 ms (CV 1.3%, 95%CI +/-9.19) | 1.18e+03 ms (CV 0.6%, 95%CI +/-18.7) |
| nemotron3-super-120b-a12b-nvfp4 | pp2 | 2 | 3 | 21.7 tok/s (CV 0.2%, 95%CI +/-0.0855) | 0.0727 req/s (CV 0.0%, 95%CI +/-0) | 84.8 ms (CV 0.3%, 95%CI +/-0.565) | 681 ms (CV 0.7%, 95%CI +/-11.7) |
| nemotron3-super-120b-a12b-nvfp4 | single | 2 | 3 | 22.7 tok/s (CV 0.4%, 95%CI +/-0.211) | 0.0624 req/s (CV 1.6%, 95%CI +/-0.00253) | 78.3 ms (CV 0.3%, 95%CI +/-0.646) | 718 ms (CV 0.3%, 95%CI +/-5.78) |
| nemotron3-super-120b-a12b-nvfp4 | tp2 | 2 | 3 | 23.7 tok/s (CV 5.1%, 95%CI +/-3.01) | 0.0658 req/s (CV 6.2%, 95%CI +/-0.0101) | 70.9 ms (CV 6.5%, 95%CI +/-11.5) | 781 ms (CV 2.6%, 95%CI +/-50.7) |
| qwen3-30b-a3b | pp2 | 8 | 3 | 90.7 tok/s (CV 2.5%, 95%CI +/-5.73) | 0.34 req/s (CV 2.2%, 95%CI +/-0.0188) | 75.8 ms (CV 5.1%, 95%CI +/-9.57) | 713 ms (CV 0.9%, 95%CI +/-16.5) |
| qwen3-30b-a3b | single | 8 | 3 | 85.3 tok/s (CV 0.5%, 95%CI +/-1.11) | 0.334 req/s (CV 0.3%, 95%CI +/-0.00253) | 77.7 ms (CV 0.3%, 95%CI +/-0.527) | 1.16e+03 ms (CV 1.6%, 95%CI +/-45.4) |
| qwen3-30b-a3b | tp2 | 8 | 3 | 85.6 tok/s (CV 2.6%, 95%CI +/-5.62) | 0.335 req/s (CV 2.6%, 95%CI +/-0.0216) | 68.2 ms (CV 3.6%, 95%CI +/-6.15) | 1.47e+03 ms (CV 0.7%, 95%CI +/-25.9) |
| qwen3-8b | single | 32 | 3 | 127 tok/s (CV 0.3%, 95%CI +/-0.944) | 0 req/s (CV n/a, 95%CI +/-0) | 151 ms (CV 0.2%, 95%CI +/-0.908) | 2.47e+03 ms (CV 0.3%, 95%CI +/-20.3) |
| qwen3-8b | tp2 | 32 | 3 | 119 tok/s (CV 1.0%, 95%CI +/-3.03) | 0 req/s (CV n/a, 95%CI +/-0) | 121 ms (CV 0.9%, 95%CI +/-2.75) | 2.66e+03 ms (CV 2.1%, 95%CI +/-137) |

## Every pairwise margin (Welch 95% CI)

| model | c | metric | pair | margin | 95% CI | separated |
|---|---|---|---|---|---|---|
| llama3.1-8b | 32 | median ITL | tp2 vs single | 28.7 ms | +/-3.28 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | 8 | median ITL | single vs pp2 | 1.19 ms | +/-1.52 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | 128 | median ITL | pp2 vs single | 120 ms | +/-9.94 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | median ITL | single vs pp2 | 6.54 ms | +/-0.635 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | median ITL | tp2 vs pp2 | 13.9 ms | +/-11.5 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | median ITL | tp2 vs single | 7.34 ms | +/-11.5 | NO |
| qwen3-30b-a3b | 8 | median ITL | pp2 vs single | 1.94 ms | +/-9.58 | NO |
| qwen3-30b-a3b | 8 | median ITL | tp2 vs pp2 | 7.65 ms | +/-8.41 | NO |
| qwen3-30b-a3b | 8 | median ITL | tp2 vs single | 9.59 ms | +/-6.17 | yes |
| qwen3-8b | 32 | median ITL | tp2 vs single | 29.4 ms | +/-2.9 | yes |
| llama3.1-8b | 32 | median TTFT | tp2 vs single | 4.22 ms | +/-142 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | 8 | median TTFT | pp2 vs single | 176 ms | +/-12.9 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | 128 | median TTFT | pp2 vs single | 559 ms | +/-20.3 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | median TTFT | pp2 vs single | 36.8 ms | +/-13 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | median TTFT | pp2 vs tp2 | 100 ms | +/-52 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | median TTFT | single vs tp2 | 63.2 ms | +/-51 | yes |
| qwen3-30b-a3b | 8 | median TTFT | pp2 vs single | 444 ms | +/-48.3 | yes |
| qwen3-30b-a3b | 8 | median TTFT | pp2 vs tp2 | 757 ms | +/-22.7 | yes |
| qwen3-30b-a3b | 8 | median TTFT | single vs tp2 | 313 ms | +/-38.7 | yes |
| qwen3-8b | 32 | median TTFT | single vs tp2 | 192 ms | +/-139 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | 8 | strict goodput | pp2 vs single | 0.0198 req/s | +/-0.0267 | NO |
| nemotron3-super-120b-a12b-nvfp4 | 2 | strict goodput | pp2 vs single | 0.0102 req/s | +/-0.00253 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | strict goodput | pp2 vs tp2 | 0.00689 req/s | +/-0.0101 | NO |
| nemotron3-super-120b-a12b-nvfp4 | 2 | strict goodput | tp2 vs single | 0.00333 req/s | +/-0.0104 | NO |
| qwen3-30b-a3b | 8 | strict goodput | pp2 vs single | 0.006 req/s | +/-0.019 | NO |
| qwen3-30b-a3b | 8 | strict goodput | pp2 vs tp2 | 0.00556 req/s | +/-0.0212 | NO |
| qwen3-30b-a3b | 8 | strict goodput | tp2 vs single | 0.000444 req/s | +/-0.0218 | NO |
| llama3.1-8b | 32 | throughput | single vs tp2 | 2.32 tok/s | +/-1.53 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | 8 | throughput | pp2 vs single | 4.93 tok/s | +/-6.3 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | 128 | throughput | pp2 vs single | 75 tok/s | +/-11.5 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | throughput | single vs pp2 | 0.96 tok/s | +/-0.227 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | throughput | tp2 vs pp2 | 2 tok/s | +/-3.01 | NO |
| nemotron3-super-120b-a12b-nvfp4 | 2 | throughput | tp2 vs single | 1.04 tok/s | +/-3.02 | NO |
| qwen3-30b-a3b | 8 | throughput | pp2 vs single | 5.41 tok/s | +/-5.84 | NO |
| qwen3-30b-a3b | 8 | throughput | pp2 vs tp2 | 5.14 tok/s | +/-5.94 | NO |
| qwen3-30b-a3b | 8 | throughput | tp2 vs single | 0.263 tok/s | +/-5.73 | NO |
| qwen3-8b | 32 | throughput | single vs tp2 | 8.34 tok/s | +/-3.18 | yes |

## Ranking verdict: top arm separated from all others

| model | c | metric | winner | arms | beats all others |
|---|---|---|---|---|---|
| llama3.1-8b | 32 | median ITL | tp2 | 2 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | 8 | median ITL | single | 2 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | 128 | median ITL | pp2 | 2 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | median ITL | tp2 | 3 | NO |
| qwen3-30b-a3b | 8 | median ITL | tp2 | 3 | NO |
| qwen3-8b | 32 | median ITL | tp2 | 2 | yes |
| llama3.1-8b | 32 | median TTFT | tp2 | 2 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | 8 | median TTFT | pp2 | 2 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | 128 | median TTFT | pp2 | 2 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | median TTFT | pp2 | 3 | yes |
| qwen3-30b-a3b | 8 | median TTFT | pp2 | 3 | yes |
| qwen3-8b | 32 | median TTFT | single | 2 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | 8 | strict goodput | pp2 | 2 | NO |
| nemotron3-super-120b-a12b-nvfp4 | 2 | strict goodput | pp2 | 3 | NO |
| qwen3-30b-a3b | 8 | strict goodput | pp2 | 3 | NO |
| llama3.1-8b | 32 | throughput | single | 2 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | 8 | throughput | pp2 | 2 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | 128 | throughput | pp2 | 2 | yes |
| nemotron3-super-120b-a12b-nvfp4 | 2 | throughput | tp2 | 3 | NO |
| qwen3-30b-a3b | 8 | throughput | pp2 | 3 | NO |
| qwen3-8b | 32 | throughput | single | 2 | yes |

## Sealed campaign value vs repeat range

| model | config | c | metric | campaign | repeat range | z | inside |
|---|---|---|---|---|---|---|---|
| llama3.1-8b | single | 32 | median ITL | 142.2 ms | 140.3..142.3 | +0.96 | yes |
| llama3.1-8b | single | 32 | median TTFT | 2453 ms | 2410..2445 | +1.37 | NO |
| llama3.1-8b | single | 32 | throughput | 130.1 tok/s | 130.4..131.4 | -1.59 | NO |
| llama3.1-8b | tp2 | 32 | median ITL | 113.5 ms | 110.8..113.5 | +0.71 | NO |
| llama3.1-8b | tp2 | 32 | median TTFT | 2359 ms | 2367..2474 | -1.20 | NO |
| llama3.1-8b | tp2 | 32 | throughput | 129.6 tok/s | 128.2..129.4 | +1.60 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 8 | median ITL | 36.69 ms | 35.8..36.91 | +0.56 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 8 | median TTFT | 434.2 ms | 425.6..429.5 | +3.31 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 8 | strict goodput | 0.728 req/s | 0.7193..0.7387 | -0.24 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 8 | throughput | 186.3 tok/s | 184.3..188.8 | -0.26 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 128 | median ITL | 187.9 ms | 157.8..160.8 | +18.76 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 128 | median TTFT | 677 ms | 618.2..624.3 | +17.56 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | pp2 | 128 | throughput | 520.1 tok/s | 580.4..591.2 | -12.09 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | single | 8 | median ITL | 35.21 ms | 34.91..35.39 | +0.05 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | single | 8 | median TTFT | 609.3 ms | 600.6..608.9 | +1.23 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | single | 8 | strict goodput | 0.7113 req/s | 0.7067..0.7147 | +0.17 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | single | 8 | throughput | 181.6 tok/s | 181..182.9 | -0.40 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | single | 128 | median ITL | 278.8 ms | 276.3..283.1 | -0.00 | yes |
| nemotron3-nano-30b-a3b-nvfp4 | single | 128 | median TTFT | 1188 ms | 1174..1188 | +1.15 | NO |
| nemotron3-nano-30b-a3b-nvfp4 | single | 128 | throughput | 508.3 tok/s | 508.1..513.7 | -1.05 | yes |
| nemotron3-super-120b-a12b-nvfp4 | pp2 | 2 | median ITL | 84.26 ms | 84.54..84.96 | -2.38 | NO |
| nemotron3-super-120b-a12b-nvfp4 | pp2 | 2 | median TTFT | 671.9 ms | 675.8..685 | -1.90 | NO |
| nemotron3-super-120b-a12b-nvfp4 | pp2 | 2 | strict goodput | 0.07267 req/s | 0.07267..0.07267 | n/a | yes |
| nemotron3-super-120b-a12b-nvfp4 | pp2 | 2 | throughput | 21.91 tok/s | 21.67..21.74 | +5.61 | NO |
| nemotron3-super-120b-a12b-nvfp4 | single | 2 | median ITL | 77.85 ms | 78.08..78.56 | -1.57 | NO |
| nemotron3-super-120b-a12b-nvfp4 | single | 2 | median TTFT | 720 ms | 716.2..720.3 | +0.99 | yes |
| nemotron3-super-120b-a12b-nvfp4 | single | 2 | strict goodput | 0.06333 req/s | 0.06133..0.06333 | +0.87 | yes |
| nemotron3-super-120b-a12b-nvfp4 | single | 2 | throughput | 22.95 tok/s | 22.58..22.75 | +3.23 | NO |
| nemotron3-super-120b-a12b-nvfp4 | tp2 | 2 | median ITL | 73.42 ms | 67.06..76.04 | +0.54 | yes |
| nemotron3-super-120b-a12b-nvfp4 | tp2 | 2 | median TTFT | 784 ms | 769.1..804.5 | +0.15 | yes |
| nemotron3-super-120b-a12b-nvfp4 | tp2 | 2 | strict goodput | 0.062 req/s | 0.06133..0.06933 | -0.93 | yes |
| nemotron3-super-120b-a12b-nvfp4 | tp2 | 2 | throughput | 22.91 tok/s | 22.39..24.78 | -0.66 | yes |
| qwen3-30b-a3b | pp2 | 8 | median ITL | 73.58 ms | 73.11..80.21 | -0.58 | yes |
| qwen3-30b-a3b | pp2 | 8 | median TTFT | 700.9 ms | 707.9..720.3 | -1.78 | NO |
| qwen3-30b-a3b | pp2 | 8 | strict goodput | 0.3427 req/s | 0.332..0.3467 | +0.29 | yes |
| qwen3-30b-a3b | pp2 | 8 | throughput | 92.03 tok/s | 88.13..92.53 | +0.57 | yes |
| qwen3-30b-a3b | single | 8 | median ITL | 77.51 ms | 77.51..77.92 | -1.07 | yes |
| qwen3-30b-a3b | single | 8 | median TTFT | 1163 ms | 1136..1168 | +0.32 | yes |
| qwen3-30b-a3b | single | 8 | strict goodput | 0.3347 req/s | 0.3333..0.3353 | +0.22 | yes |
| qwen3-30b-a3b | single | 8 | throughput | 85.8 tok/s | 84.85..85.74 | +1.08 | NO |
| qwen3-30b-a3b | tp2 | 8 | median ITL | 68.76 ms | 65.37..70.12 | +0.25 | yes |
| qwen3-30b-a3b | tp2 | 8 | median TTFT | 1448 ms | 1458..1478 | -2.09 | NO |
| qwen3-30b-a3b | tp2 | 8 | strict goodput | 0.3333 req/s | 0.328..0.3447 | -0.18 | yes |
| qwen3-30b-a3b | tp2 | 8 | throughput | 85.33 tok/s | 83.76..88.11 | -0.11 | yes |
| qwen3-8b | single | 32 | median ITL | 160.5 ms | 150.3..151 | +27.22 | NO |
| qwen3-8b | single | 32 | median TTFT | 2674 ms | 2462..2478 | +24.90 | NO |
| qwen3-8b | single | 32 | throughput | 119.3 tok/s | 126.8..127.6 | -20.89 | NO |
| qwen3-8b | tp2 | 32 | median ITL | 121.3 ms | 120.5..122.5 | +0.06 | yes |
| qwen3-8b | tp2 | 32 | median TTFT | 2689 ms | 2599..2698 | +0.47 | yes |
| qwen3-8b | tp2 | 32 | throughput | 119.3 tok/s | 118.2..120.3 | +0.33 | yes |
