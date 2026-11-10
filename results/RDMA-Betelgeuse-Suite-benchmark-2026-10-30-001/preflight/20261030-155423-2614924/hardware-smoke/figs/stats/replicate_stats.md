# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 36.4 tok/s (CV 0.9%, 95%CI +/-3.02) | 0.125 req/s (CV 0.0%, 95%CI +/-0) | 0.125 req/s (CV 0.0%, 95%CI +/-0) | 25.6 ms (CV 0.7%, 95%CI +/-1.62) | 26.5 ms (CV 0.9%, 95%CI +/-2.03) | 95.2 ms (CV 2.2%, 95%CI +/-19.1) | 1.21 J/tok (CV 3.7%, 95%CI +/-0.401) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 123 tok/s (CV 0.2%, 95%CI +/-1.69) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 29.3 ms (CV 0.1%, 95%CI +/-0.323) | 28 ms (CV 0.3%, 95%CI +/-0.778) | 476 ms (CV 0.1%, 95%CI +/-5.22) | 0.405 J/tok (CV 7.4%, 95%CI +/-0.27) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
