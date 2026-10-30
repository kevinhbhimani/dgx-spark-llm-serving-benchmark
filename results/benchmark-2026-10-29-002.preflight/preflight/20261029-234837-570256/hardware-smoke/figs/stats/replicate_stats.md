# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 37 tok/s (CV 0.0%, 95%CI +/-0) | 0.125 req/s (CV 0.0%, 95%CI +/-0) | 0.125 req/s (CV 0.0%, 95%CI +/-0) | 25.4 ms (CV 0.3%, 95%CI +/-0.679) | 26.1 ms (CV 0.0%, 95%CI +/-0.0997) | 93.1 ms (CV 2.2%, 95%CI +/-18) | 1.25 J/tok (CV 0.4%, 95%CI +/-0.0499) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 123 tok/s (CV 0.3%, 95%CI +/-2.96) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 29.1 ms (CV 0.4%, 95%CI +/-1.01) | 27.8 ms (CV 0.2%, 95%CI +/-0.539) | 476 ms (CV 0.1%, 95%CI +/-6.32) | 0.43 J/tok (CV 0.7%, 95%CI +/-0.0266) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
