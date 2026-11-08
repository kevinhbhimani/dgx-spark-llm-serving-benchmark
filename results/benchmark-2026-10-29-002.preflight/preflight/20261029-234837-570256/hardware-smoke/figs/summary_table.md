| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 5 † | 37 ± 0 | 0.12 | 93/857 | 26 ± 0 | 6.6 | 0.12 | 1.251 |
| Single Spark | 4 | 30 | 15 † | 123 ± 3 | 0.50 | 476/1377 | 28 ± 1 | 8.5 | 0.50 | 0.430 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
