# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 36.9 tok/s (CV 0.3%, 95%CI +/-0.953) | 0.138 req/s (CV 12.9%, 95%CI +/-0.159) | 0.138 req/s (CV 12.9%, 95%CI +/-0.159) | 25.6 ms (CV 0.3%, 95%CI +/-0.802) | 26.1 ms (CV 0.4%, 95%CI +/-0.83) | 162 ms (CV 61.9%, 95%CI +/-899) | 1.12 J/tok (CV 2.6%, 95%CI +/-0.263) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 125 tok/s (CV 0.5%, 95%CI +/-5.29) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 28.9 ms (CV 0.2%, 95%CI +/-0.395) | 27.8 ms (CV 0.4%, 95%CI +/-0.91) | 456 ms (CV 0.9%, 95%CI +/-35.2) | 0.389 J/tok (CV 1.0%, 95%CI +/-0.0342) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
