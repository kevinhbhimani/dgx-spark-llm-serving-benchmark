# Campaign comparison

| | new | baseline |
|---|---|---|
| index | /home/dmihovch/dgx-spark-llm-serving-benchmark/results/solo-benchmark-2026-11-13-001/preflight/20261113-184823-61715/hardware-smoke/results/index.csv | /home/dmihovch/dgx-spark-llm-serving-benchmark/baselines/pmbs26-tcp/index.csv |
| rows | 4 | 224 |
| replicates per point | 2 | 1 |
| methodology | steady-exact-v4 | steady-exact-v3 |
| vLLM | 0.29.0+5013de39.dev | 0.19.0+6bc3197f.nv26.04.48680843 |
| image | nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2 | nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27 |
| window (s) | 30.0 | 1500.0 |
| NCCL transport | unknown | TCP sockets, 1 interface(s) |

Ratios are new / baseline. ± is the new campaign's 95% Student-t half-width across replicates. Each ratio reflects the provenance differences above, including transport.

| model | config | loads | ITL p50 @c_low (ms) new | base | ratio | peak tok/s new (c) | base (c) | ratio | max goodput new | base |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| llama3.1-8b-nvfp4 | single | 2 | 25.2 ± 2.8 (c=1) | 26.0 | 0.97x | 128 ± 3 (4) | 126 (4) | 1.02x | 0.500 | 0.492 |
