| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 5 † | 36 ± 3 | 0.12 | 95/858 | 26 ± 2 | 6.6 | 0.12 | 1.211 |
| Single Spark | 4 | 30 | 15 † | 123 ± 2 | 0.50 | 476/1371 | 28 ± 1 | 8.6 | 0.50 | 0.405 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
