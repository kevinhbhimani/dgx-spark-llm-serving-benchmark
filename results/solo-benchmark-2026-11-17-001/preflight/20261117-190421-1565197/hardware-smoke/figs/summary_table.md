| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 6 † | 38 ± 0 | 0.15 | 238/839 | 25 ± 1 | 6.6 | 0.15 | 1.106 |
| Single Spark | 4 | 30 | 15 † | 126 ± 3 | 0.50 | 464/1339 | 27 ± 1 | 8.4 | 0.50 | 0.382 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
