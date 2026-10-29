# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 36.2 tok/s (CV 1.1%, 95%CI +/-3.49) | 0.125 req/s (CV 0.0%, 95%CI +/-0) | 0.125 req/s (CV 0.0%, 95%CI +/-0) | 25.9 ms (CV 0.9%, 95%CI +/-2.16) | 26.7 ms (CV 1.0%, 95%CI +/-2.46) | 91.5 ms (CV 0.7%, 95%CI +/-5.47) | 1.19 J/tok (CV 7.2%, 95%CI +/-0.772) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 123 tok/s (CV 0.0%, 95%CI +/-0.212) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 29.3 ms (CV 0.1%, 95%CI +/-0.15) | 28 ms (CV 0.2%, 95%CI +/-0.543) | 478 ms (CV 0.5%, 95%CI +/-22) | 0.402 J/tok (CV 7.3%, 95%CI +/-0.264) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
