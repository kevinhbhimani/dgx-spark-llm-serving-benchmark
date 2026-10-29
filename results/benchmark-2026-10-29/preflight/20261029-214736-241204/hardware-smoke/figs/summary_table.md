| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 6 † | 37 ± 2 | 0.14 | 165/856 | 26 ± 3 | 6.7 | 0.14 | 1.247 |
| Single Spark | 4 | 30 | 15 † | 125 ± 12 | 0.50 | 475/1368 | 28 ± 3 | 8.4 | 0.50 | 0.427 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
