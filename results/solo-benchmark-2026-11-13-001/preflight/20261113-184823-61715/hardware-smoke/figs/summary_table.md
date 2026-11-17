| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 6 † | 38 ± 4 | 0.15 | 237/836 | 25 ± 3 | 6.6 | 0.15 | 1.106 |
| Single Spark | 4 | 30 | 15 † | 128 ± 3 | 0.50 | 465/1340 | 27 ± 1 | 8.2 | 0.50 | 0.377 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
