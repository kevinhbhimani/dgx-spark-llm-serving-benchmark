| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 6 † | 38 ± 1 | 0.15 | 233/829 | 26 ± 1 | 6.6 | 0.15 | 0.977 |
| Single Spark | 4 | 30 | 15 † | 127 ± 4 | 0.50 | 460/1323 | 27 ± 2 | 8.3 | 0.50 | 0.358 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
