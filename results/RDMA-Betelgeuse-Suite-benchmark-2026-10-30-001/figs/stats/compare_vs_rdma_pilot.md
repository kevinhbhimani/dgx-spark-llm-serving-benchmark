# Campaign comparison

| | new | baseline |
|---|---|---|
| index | /home/kbhimani@win.udel.edu/dgx-spark-llm-serving-benchmark/results/RDMA-Betelgeuse-Suite-benchmark-2026-10-30-001/results/index.csv | /home/kbhimani@win.udel.edu/dgx-spark-llm-serving-benchmark/baselines/rdma-pilot-2026-10/index.csv |
| rows | 918 | 111 |
| replicates per point | 3 | 1 |
| methodology | steady-exact-v4 | steady-exact-v3 |
| vLLM | 0.29.0+5013de39.dev | 0.19.0+6bc3197f.nv26.04.48680843 |
| image | nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2 | nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27 |
| window (s) | 600.0 | 1500.0 |
| NCCL transport | RDMA, 4 interface(s) | RDMA, 4 interface(s) |

Ratios are new / baseline. ± is the new campaign's 95% Student-t half-width across replicates. A ratio reflects every provenance difference above, not only the transport.

| model | config | loads | ITL p50 @c_low (ms) new | base | ratio | peak tok/s new (c) | base (c) | ratio | max goodput new | base |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| qwen3-next-80b-bf16 | pp2 | 6 | 39.6 ± 0.0 (c=1) | 40.9 | 0.97x | 177 ± 5 (64) | 173 (64) | 1.02x | 0.272 | 0.201 |
| qwen3-next-80b-bf16 | tp2 | 6 | 24.1 ± 0.2 (c=1) | 24.5 | 0.98x | 215 ± 9 (64) | 196 (64) | 1.10x | 0.427 | 0.439 |
| qwen3-next-80b-nvfp4 | pp2 | 6 | 26.4 ± 0.2 (c=1) | 35.5 | 0.74x | 363 ± 12 (64) | 312 (64) | 1.16x | 0.757 | 0.661 |
| qwen3-next-80b-nvfp4 | single | 6 | 26.7 ± 0.1 (c=1) | 32.3 | 0.82x | 284 ± 3 (64) | 219 (64) | 1.30x | 0.691 | 0.624 |
| qwen3-next-80b-nvfp4 | tp2 | 6 | 17.9 ± 0.0 (c=1) | 25.5 | 0.70x | 386 ± 6 (64) | 300 (64) | 1.29x | 1.245 | 0.840 |
