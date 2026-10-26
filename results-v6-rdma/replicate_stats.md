# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 37.4 tok/s (CV 0.4%, 95%CI +/-1.43) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 25.2 ms (CV 0.6%, 95%CI +/-1.29) | 25.6 ms (CV 0.4%, 95%CI +/-0.985) | 231 ms (CV 1.7%, 95%CI +/-34.7) | 1.02 J/tok (CV 2.9%, 95%CI +/-0.271) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 126 tok/s (CV 0.4%, 95%CI +/-5.08) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 28.6 ms (CV 0.9%, 95%CI +/-2.19) | 27.3 ms (CV 0.6%, 95%CI +/-1.51) | 459 ms (CV 1.3%, 95%CI +/-52.3) | 0.376 J/tok (CV 2.5%, 95%CI +/-0.0844) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
