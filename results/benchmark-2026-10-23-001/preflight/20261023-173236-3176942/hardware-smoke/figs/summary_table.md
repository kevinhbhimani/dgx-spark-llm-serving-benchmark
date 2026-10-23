| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 6 † | 37 ± 1 | 0.14 | 162/826 | 26 ± 1 | 6.7 | 0.14 | 1.121 |
| Single Spark | 4 | 30 | 15 † | 125 ± 5 | 0.50 | 456/1315 | 28 ± 1 | 8.5 | 0.50 | 0.389 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
