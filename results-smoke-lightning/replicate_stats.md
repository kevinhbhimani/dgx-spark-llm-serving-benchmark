# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| nemotron3.5-lightning-30b-a3b-nvfp4 | single | 1 | 2 | 71.9 tok/s (CV 0.1%, 95%CI +/-0.635) | 0.3 req/s (CV 0.0%, 95%CI +/-0) | 0.3 req/s (CV 0.0%, 95%CI +/-0) | 12.3 ms (CV 0.2%, 95%CI +/-0.267) | 12.2 ms (CV 0.3%, 95%CI +/-0.297) | 184 ms (CV 1.5%, 95%CI +/-24.5) | 0.611 J/tok (CV 0.4%, 95%CI +/-0.0193) |
| nemotron3.5-lightning-30b-a3b-nvfp4 | single | 128 | 2 | 470 tok/s (CV 0.5%, 95%CI +/-19.5) | 0 req/s (CV n/a, 95%CI +/-0) | 0 req/s (CV n/a, 95%CI +/-0) | 291 ms (CV 0.0%, 95%CI +/-0.121) | 353 ms (CV 0.0%, 95%CI +/-0.232) | 1.27e+03 ms (CV 0.0%, 95%CI +/-1.63) | 0.157 J/tok (CV 0.0%, 95%CI +/-0.000422) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
