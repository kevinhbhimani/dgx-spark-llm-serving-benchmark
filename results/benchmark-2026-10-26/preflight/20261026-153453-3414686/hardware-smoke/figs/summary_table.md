| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 5 † | 36 ± 5 | 0.12 | 92/873 | 27 ± 2 | 6.7 | 0.12 | 1.274 |
| Single Spark | 4 | 30 | 14 † | 120 ± 30 | 0.48 | 466/1373 | 29 ± 13 | 8.7 | 0.48 | 0.429 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
