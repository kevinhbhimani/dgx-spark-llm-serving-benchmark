| Config | Load | Win (s) | n | Tok/s | Req/s | TTFT p50/p99 (ms) | ITL p50 (ms) | E2E p50 (s) | Goodput (r/s) | GPU energy (J/tok) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Single Spark | 1 | 40 | 6 † | 37 ± 0 | 0.15 | 244/853 | 26 ± 2 | 6.8 | 0.15 | 1.252 |
| Single Spark | 4 | 30 | 15 † | 125 ± 4 | 0.50 | 475/1373 | 27 ± 1 | 8.4 | 0.50 | 0.426 |

Values are means over up to 2 independent server launches; ± is the 95% Student-t half-width of the mean.
† fewer than 100 requests; p99 is small-sample noise.
