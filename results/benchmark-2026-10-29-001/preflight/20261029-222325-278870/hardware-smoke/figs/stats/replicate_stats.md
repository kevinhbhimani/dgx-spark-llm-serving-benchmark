# Replicate statistics

## Per-arm spread across repeats

| model | config | c | repeats | throughput | strict goodput | loose goodput | median ITL | pooled ITL | median TTFT | energy/token |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1-8b-nvfp4 | single | 1 | 2 | 37.1 tok/s (CV 0.0%, 95%CI +/-0.159) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 0.15 req/s (CV 0.0%, 95%CI +/-0) | 25.5 ms (CV 0.8%, 95%CI +/-1.87) | 25.9 ms (CV 0.7%, 95%CI +/-1.53) | 244 ms (CV 2.4%, 95%CI +/-53) | 1.25 J/tok (CV 0.8%, 95%CI +/-0.093) |
| llama3.1-8b-nvfp4 | single | 4 | 2 | 125 tok/s (CV 0.3%, 95%CI +/-3.81) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 0.5 req/s (CV 0.0%, 95%CI +/-0) | 28.7 ms (CV 0.3%, 95%CI +/-0.738) | 27.4 ms (CV 0.5%, 95%CI +/-1.15) | 475 ms (CV 0.4%, 95%CI +/-15.4) | 0.426 J/tok (CV 0.5%, 95%CI +/-0.0188) |

## Every pairwise margin (Welch 95% CI)

(no rows)

## Ranking verdict: top arm separated from all others

(no rows)

## Sealed campaign value vs repeat range

(no rows)
