# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 36 tok/s (CV 1.6%, 95%CI +/-5.08) | 0.125 req/s (CV 0.0%, 95%CI +/-0) | 0.125 req/s (CV 0.0%, 95%CI +/-0) | 25.8 ms (CV 2.1%, 95%CI +/-4.76) | 26.7 ms (CV 0.8%, 95%CI +/-1.81) | 92.5 ms (CV 2.9%, 95%CI +/-24.4) | 1.27 J/tok (CV 1.8%, 95%CI +/-0.202) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 120 tok/s (CV 2.8%, 95%CI +/-29.9) | 0.483 req/s (CV 4.9%, 95%CI +/-0.212) | 0.483 req/s (CV 4.9%, 95%CI +/-0.212) | 30.3 ms (CV 5.0%, 95%CI +/-13.6) | 29 ms (CV 5.2%, 95%CI +/-13.5) | 466 ms (CV 3.9%, 95%CI +/-163) | 0.429 J/tok (CV 1.0%, 95%CI +/-0.0368) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
