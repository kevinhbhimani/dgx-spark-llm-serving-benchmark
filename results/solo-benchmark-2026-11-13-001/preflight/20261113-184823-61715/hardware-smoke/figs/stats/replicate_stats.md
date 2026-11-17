# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 38 tok/s (CV 1.2%, 95%CI +/-4.13) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 24.8 ms (CV 1.3%, 95%CI +/-2.8) | 25.2 ms (CV 1.2%, 95%CI +/-2.8) | 237 ms (CV 0.0%, 95%CI +/-0.654) | 1.11 J/tok (CV 1.9%, 95%CI +/-0.189) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 128 tok/s (CV 0.3%, 95%CI +/-3.39) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 28.1 ms (CV 0.2%, 95%CI +/-0.445) | 26.8 ms (CV 0.3%, 95%CI +/-0.731) | 465 ms (CV 0.4%, 95%CI +/-15.8) | 0.377 J/tok (CV 0.6%, 95%CI +/-0.0192) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
