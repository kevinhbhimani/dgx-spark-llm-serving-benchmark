# Troubleshooting

Each entry names a symptom and its fix. Configuration fields and environment
variables are described in [reference.md](reference.md).

- `ModuleNotFoundError` for `pandas`, `aiohttp`, or `yaml`: activate the Python
  3.12+ virtual environment, reinstall `requirements.lock`, and rerun
  `python scripts/check_environment.py` with `PYTHONNOUSERSITE=1`.
- `[topology] hostname -s '<name>' matches 0 configured nodes`: the local host
  is not listed in the topology file. Set `SINGLE_NODE_ONLY=1` for one-node
  runs, or add the host to `configs/cluster.yaml` or to the file named by
  `CLUSTER_CONFIG`.
- `Connection refused` or model resolution fails: confirm vLLM is running and
  `curl http://127.0.0.1:8000/v1/models` works from the benchmark client.
- A direct `launch_server.sh` dual-node launch reports a missing revision: run
  `scripts/predownload.sh <config>` while Hub access is available, then mirror
  the checked snapshot to the worker. `run_models.sh` performs these steps for
  its selected model automatically.
- `/v1/models` works but the readiness probe fails: inspect the server log and
  try the same two-token streaming completion manually. The benchmark requires
  vLLM's delta `token_ids`, final usage, `finish_reason: length`, and `[DONE]`;
  a proxy or server that strips those fields is not compatible with exact-token
  accounting.
- Smoke test works but real runs are slow or fail: the real config uses much
  longer prompts and outputs. Reduce buckets temporarily or increase
  `request_timeout_s`.
- vLLM returns `400 Bad Request` with a maximum context length message: make
  sure `MAXLEN`/`--max-model-len` is at least the largest `input_len +
  output_len` in the workload. The default experiment needs `8704`.
- `goodput=0.00` with successful requests: the run completed, but the measured
  TTFT or mean ITL did not meet `ttft_slo_s`/`itl_slo_s`. Check `goodput_itl2_rps`
  (the looser `itl_slo_s2` tier) for a less strict service target, or loosen the
  SLOs. On bandwidth-bound hardware, dense models can floor above a strict ITL
  even unloaded, so a strict `goodput_rps` of 0 across the sweep is expected, not
  a bug.
- `PyTorch was not found` from Transformers on the benchmark client is expected
  and harmless: `requirements.txt` intentionally does not install PyTorch
  because the harness only loads tokenizers and model configs locally. vLLM and
  PyTorch run inside the serving container.
- `index schema mismatch` on a run or `Expected N fields` in plotting: the index
  columns changed between versions and a new run tried to append to an old
  `index.csv`. Write to a fresh `OUT_DIR` (or move the old index aside) and plot
  the old and new indexes separately.
- `required provenance is incomplete` under `configs/experiment.yaml`: use
  `run_models.sh` so the exact 40-hex revisions and genuine 64-hex launch-policy
  fingerprint are forwarded together. A manual server needs the same evidence;
  do not use an all-zero, random, or otherwise fabricated hash. For plumbing
  diagnostics that make no paper claim, use `configs/smoke.yaml`.
- `required power coverage is incomplete` or required vLLM metrics are missing:
  the full config deliberately did not write a paper row. Check `nvidia-smi`,
  `/metrics`, both Ray nodes, and the worker-power actor, then rerun the point.
  Use a separate diagnostic YAML with the corresponding `require_*` field false
  only when incomplete observability is intentional.
- An on-the-fly FP8 launch fails while serializing an AOTAutograd graph cache
  (`PicklingError`): confirm `TORCHINDUCTOR_AUTOGRAD_CACHE=0` reaches the vLLM
  and Ray containers. This disables the problematic disk cache, not graph
  compilation, and is the uniform default for all configs.
- `run_models.sh` skips a model with a timeout or "launcher exited": that model
  failed to come up (often OOM). Check the newest matching
  `OUT_DIR/logs/server_<label>_<config>_*.log` (logs are never overwritten);
  set a lower `GPU_UTIL` for the campaign, add a `quant`, or move it to the
  dual-node configs if it does not fit on one GB10. The
  orchestrator continues to the next model rather than aborting the whole
  sweep.
- `weight_gb`/`kv_cache_gb` blank in the index: the model config or Hub metadata
  could not be fetched, including when a gated repository lacks `HF_TOKEN`.
  HTTP-derived launch fields such as `gpu_blocks` and `block_size` still
  populate. For a hybrid attention/Mamba cache, `total_kv_tokens` is left blank
  unless vLLM exposes a group-aware token capacity or `server_log` supplies its
  startup value; reporting `gpu_blocks * block_size` there would be wrong.
- Tokenizer download fails: set `HF_TOKEN`, log in with the Hugging Face CLI, or
  set `tokenizer` to a local tokenizer path.
- HF cache access fails: check the reported path, effective UID:GID, directory
  owner, and mode. Set `HF_CACHE` in each node's `.env` to a user-owned path
  with enough space. The probe does not change directory ownership.
- Docker cannot access the GPU: verify `docker run --gpus all ... nvidia-smi`
  works with your container runtime.
- Dual-node launch says Ray is not active: run `scripts/start_ray.sh head` on
  the head node and `scripts/start_ray.sh worker` on the worker node, then check
  `docker exec vllm-spark ray status`.
- `tp2`/`pp2` launch dies with `HFValidationError: Repo id must be in the form
  ...` quoting a `/hf-cache/hub/...` path, raised by a
  `RayWorkerWrapper` at the worker's IP: the model's weights are not in the
  worker node's HF cache (each node has its own; acquisition runs on the head).
  `run_models.sh` mirrors automatically; a direct `launch_server.sh` requires
  `scripts/mirror_worker_cache.sh tp2|pp2|all` from the head before relaunch.
- `pp2` launch stops logging during `[AutoTuner]: Tuning ...` and the server
  log then shows only `No available shared memory broadcast block found in 60
  seconds` once a minute until the readiness timeout: the two pipeline stages
  deadlocked in FlashInfer autotuning. This failure was observed during the
  October 2026 canary on the Nemotron 3 Super NVFP4 checkpoint, which is no
  longer in the current matrix. vLLM 0.29.0 synchronizes per-tactic
  timings, the cache broadcast, and a barrier over the world group. When the
  stages tune different kernels, one stage waits on a collective the other
  never joins. The
  signature is one stage logging `Autotuning process ends` while the other
  stops mid-tuning, and neither logging `Graph capturing finished` afterward.
  Upstream fixed this in vllm-project/vllm#57197 (first released in v0.32.0;
  the cache-hit variant in #57635). Keep `FLASHINFER_AUTOTUNE=0` (the default)
  on the pinned 26.09 image. The vLLM `[shutdown]` and `SIGTERM received`
  lines at the end of such a log are run_models.sh stopping the server, not
  a host shutdown.
- `tp2`/`pp2` launch passes the cache preflight but hangs during model load
  with no engine output: NCCL cannot finish the RDMA handshake. Run
  `ibdev2netdev` on both nodes and confirm one UP device per port. Run
  `scripts/verify_connectx.sh` and confirm per-subnet latency under 2 ms.
  Restart Ray with `NCCL_DEBUG=INFO` and read the `NET/IB : Using ...` line
  in the launcher output to see which devices NCCL selected. Set
  `NCCL_IB_HCA` to the full device list when some are missing.
- A campaign run over the TCP fallback (`NCCL_IB_DISABLE=1`) fails the
  allreduce gate: the default floor of 16 GB/s at 1 GiB is calibrated for
  RDMA. Set `ALLREDUCE_MIN_1G_BUSBW_GBPS=1.8` and
  `ALLREDUCE_MAX_8B_LATENCY_S=0.005` for the fallback campaign.
- `PREFLIGHT FAILED` for local memory, or `low free memory` before a tp2/pp2
  launch: both preflights already drop clean file pages from the Hugging Face
  cache without applying anonymous-memory pressure. A remaining
  shortage may be unrelated clean cache (retry with `DROP_CACHES=1`), another
  process on that node (check `ps aux --sort=-rss`), or a `gpu_util` too high
  for its baseline usage (`util x MemTotal` must fit in `MemFree`).
- `tp2`/`pp2` launch fails with `ray.exceptions.OutOfMemoryError: Task was
  killed due to the node running low on memory` naming one node's IP: Ray's
  node memory monitor killed a worker, almost always stale page cache on
  Grace unified memory rather than a real leak (the reported "top memory
  users" list won't account for most of the usage). Restart Ray on both nodes
  with `DROP_CACHES=1` (see [Two-node runs](reference.md#two-node-runs)) before
  relaunching.
- Same `ray.exceptions.OutOfMemoryError`, but it happens later, right after
  `Available KV cache memory: ...` logs during model load rather than at Ray
  startup: the model exceeds available unified memory. The campaign defaults to
  `0.80`; set a lower `GPU_UTIL` for the campaign or lower `gpu_util_default`
  in `configs/models.yaml` for a persistent change.
- Dual-node performance looks like management-network traffic: run
  `scripts/verify_connectx.sh` on each host, check the declared rail routes in
  `CLUSTER_CONFIG`, and watch interface byte counters during a `tp2` run.
- No energy figure is generated: rerun with a non-`none` power backend. The
  plotting script skips energy when the index has no power columns.
- `plot_results.py` reports `Expected N fields` (and the schema versions match):
  an older `index.csv` row was likely missing a trailing newline before another
  run appended, concatenating two rows. New runs guard against this; split the
  concatenated row or start a fresh `OUT_DIR`.
- The canary pass stops the job: every failure is listed at the end of the
  pass. Fix the entry or image (a model card's minimum vLLM version is the
  usual cause), commit, and rerun; the marker `OUT_DIR/.canary_passed` makes
  later runs skip a pass that already succeeded. `ALLOW_CANARY_FAILURES=1`
  measures the models that passed.
- `NCCL used NET/Socket ... although NCCL_IB_DISABLE=0`: the RoCE handshake
  failed and NCCL fell back to TCP, so the launch was refused rather than
  measured as RDMA. Check `ibdev2netdev` on both nodes and the
  `NET/IB : Using` line in the server log, then restart Ray. A warning about
  missing NCCL channel lines means Ray was started without `NCCL_DEBUG=INFO`
  (restart Ray on both nodes without overriding `NCCL_DEBUG`).
- `purge` refuses with `serving processes still running`: a vLLM server or
  Ray serving worker is still alive on that node. Stop it
  (`docker exec vllm-spark pkill -f 'vllm serve'`) and rerun
  `MODEL_LABELS=... scripts/purge_model_cache.sh`.
- `predownload` reports too little space: it reserves the exact remaining
  bytes of the pinned snapshot plus a 10 GiB free-space buffer by default.
  Set `PREDOWNLOAD_MIN_FREE_GB` to adjust the buffer or point `HF_CACHE` at a
  larger drive.
- The cache has shared blobs left from earlier runs: start Ray on both nodes,
  review `DRY_RUN=1 scripts/purge_model_cache.sh`, then run
  `scripts/purge_model_cache.sh` to remove detached revisions, incomplete
  downloads, and unreferenced shared blobs. Pruning is enabled by default. Set
  `PURGE_CACHE_GARBAGE=0` to skip pruning while deleting selected snapshots.
