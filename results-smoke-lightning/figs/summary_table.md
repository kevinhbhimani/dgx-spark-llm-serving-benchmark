| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 30 | 9 † | 72 ± 1 | 0.30 | 184/1154 | 12 ± 0 | 3.3 | 0.30 | 0.611 |
| Single Spark | 128 | 30 | 48 † | 470 ± 19 | 1.60 | 1272/2675 | 353 ± 0 | 75.1 | 0.00 | 0.157 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
