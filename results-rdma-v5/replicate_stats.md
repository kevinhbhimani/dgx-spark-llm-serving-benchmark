# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 37.5 tok/s (CV 0.2%, 95%CI +/-0.794) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 25.1 ms (CV 0.3%, 95%CI +/-0.724) | 25.5 ms (CV 0.4%, 95%CI +/-0.852) | 233 ms (CV 1.3%, 95%CI +/-28) | 0.977 J/tok (CV 2.9%, 95%CI +/-0.256) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 127 tok/s (CV 0.3%, 95%CI +/-3.81) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 28.4 ms (CV 0.5%, 95%CI +/-1.32) | 27.2 ms (CV 0.6%, 95%CI +/-1.59) | 460 ms (CV 1.3%, 95%CI +/-53.5) | 0.358 J/tok (CV 8.9%, 95%CI +/-0.287) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
