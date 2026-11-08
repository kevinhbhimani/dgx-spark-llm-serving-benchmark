# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 36.9 tok/s (CV 0.7%, 95%CI +/-2.38) | 0.138 req/s (CV 12.9%, 95%CI +/-0.159) | 0.138 req/s (CV 12.9%, 95%CI +/-0.159) | 25.5 ms (CV 0.3%, 95%CI +/-0.772) | 26.1 ms (CV 1.5%, 95%CI +/-3.49) | 165 ms (CV 62.9%, 95%CI +/-934) | 1.25 J/tok (CV 1.0%, 95%CI +/-0.116) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 125 tok/s (CV 1.0%, 95%CI +/-11.6) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 28.8 ms (CV 0.8%, 95%CI +/-1.97) | 27.5 ms (CV 1.1%, 95%CI +/-2.84) | 475 ms (CV 0.3%, 95%CI +/-11.2) | 0.427 J/tok (CV 0.3%, 95%CI +/-0.00995) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
