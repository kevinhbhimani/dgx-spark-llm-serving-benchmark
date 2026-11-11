# Campaign comparison

| | new | baseline |
|---|---|---|
| index | /home/kbhimani@win.udel.edu/dgx-spark-llm-serving-benchmark/results/RDMA-Betelgeuse-Suite-benchmark-2026-10-30-001/results/index.csv | /home/kbhimani@win.udel.edu/dgx-spark-llm-serving-benchmark/baselines/pmbs26-tcp/index.csv |
| rows | 918 | 224 |
| replicates per point | 3 | 1 |
| methodology | steady-exact-v4 | steady-exact-v3 |
| vLLM | 0.29.0+5013de39.dev | 0.19.0+6bc3197f.nv26.04.48680843 |
| image | nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2 | nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27 |
| window (s) | 600.0 | 1500.0 |
| NCCL transport | RDMA, 4 interface(s) | TCP sockets, 1 interface(s) |

Ratios are new / baseline. ± is the new campaign's 95% Student-t half-width across replicates. A ratio reflects every provenance difference above, not only the transport.

| model | config | loads | ITL p50 @c_low (ms) new | base | ratio | peak tok/s new (c) | base (c) | ratio | max goodput new | base |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| llama3.1-8b | pp2 | 6 | 74.6 ± 0.3 (c=1) | 76.4 | 0.98x | 204 ± 6 (64) | 197 (64) | 1.04x | 0.320 | 0.325 |
| llama3.1-8b | single | 6 | 73.5 ± 0.3 (c=1) | 73.3 | 1.00x | 131 ± 2 (32) | 130 (32) | 1.01x | 0.181 | 0.281 |
| llama3.1-8b | tp2 | 6 | 38.1 ± 0.1 (c=1) | 48.3 | 0.79x | 218 ± 1 (32) | 130 (32) | 1.68x | 0.729 | 0.167 |
| llama3.1-8b-fp8 | pp2 | 6 | 42.2 ± 0.2 (c=1) | 42.5 | 0.99x | 439 ± 23 (64) | 410 (64) | 1.07x | 0.854 | 1.303 |
| llama3.1-8b-fp8 | single | 6 | 41.4 ± 0.2 (c=1) | 39.9 | 1.04x | 321 ± 2 (64) | 323 (64) | 0.99x | 0.785 | 0.821 |
| llama3.1-8b-fp8 | tp2 | 6 | 22.6 ± 0.1 (c=1) | 28.8 | 0.79x | 466 ± 5 (64) | 197 (64) | 2.37x | 1.564 | 0.389 |
| llama3.1-8b-nvfp4 | pp2 | 6 | 26.9 ± 0.3 (c=1) | 29.9 | 0.90x | 522 ± 1 (64) | 487 (64) | 1.07x | 1.624 | 1.629 |
| llama3.1-8b-nvfp4 | single | 6 | 26.3 ± 0.8 (c=1) | 26.0 | 1.01x | 390 ± 1 (64) | 390 (64) | 1.00x | 1.330 | 1.349 |
| llama3.1-8b-nvfp4 | tp2 | 6 | 15.2 ± 0.2 (c=1) | 24.2 | 0.63x | 529 ± 2 (64) | 205 (64) | 2.58x | 1.852 | 0.436 |
| nemotron3-super-120b-a12b-nvfp4 | pp2 | 6 | 68.0 ± 0.7 (c=1) | 73.5 | 0.93x | 36 ± 1 (16) | 149 (64) | 0.24x | 0.054 | 0.051 |
| nemotron3-super-120b-a12b-nvfp4 | single | 6 | 66.6 ± 0.6 (c=1) | 67.8 | 0.98x | 35 ± 0 (32) | 113 (64) | 0.31x | 0.045 | 0.047 |
| nemotron3-super-120b-a12b-nvfp4 | tp2 | 6 | 54.9 ± 0.3 (c=1) | 59.5 | 0.92x | 47 ± 1 (32) | 101 (64) | 0.46x | 0.140 | 0.057 |
