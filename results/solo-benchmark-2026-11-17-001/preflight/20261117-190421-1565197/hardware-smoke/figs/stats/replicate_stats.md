# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 37.8 tok/s (CV 0.1%, 95%CI +/-0.318) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 25 ms (CV 0.1%, 95%CI +/-0.293) | 25.3 ms (CV 0.2%, 95%CI +/-0.529) | 238 ms (CV 0.4%, 95%CI +/-8.88) | 1.11 J/tok (CV 0.7%, 95%CI +/-0.0728) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 126 tok/s (CV 0.2%, 95%CI +/-2.75) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 28.5 ms (CV 0.0%, 95%CI +/-0.00362) | 27.4 ms (CV 0.4%, 95%CI +/-0.975) | 464 ms (CV 0.7%, 95%CI +/-30.3) | 0.382 J/tok (CV 0.2%, 95%CI +/-0.00819) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
