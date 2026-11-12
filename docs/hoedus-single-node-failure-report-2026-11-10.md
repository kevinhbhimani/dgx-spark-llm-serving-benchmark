# Hoedus single-node failure report: 2026-11-10

## Finding

The hardware smoke test and all 15 campaign canaries failed during page-cache preparation. `scripts/flush_page_cache.sh:18` expanded an unset `LOCAL_NODE_ID` under `set -u`:

```text
scripts/flush_page_cache.sh: line 18: LOCAL_NODE_ID: unbound variable
```

Solo initialization explicitly unsets `LOCAL_NODE_ID` and exports `LOCAL_HOSTNAME`. The cache-flush script still requires the cluster variable. The failure occurs before `sync`, the privileged cache flush, thermal capture, and server launch. The saved run contains no benchmark measurement CSV or JSONL files.

Sources: [solo initialization](../scripts/connectx_env.sh), [cache preparation](../scripts/flush_page_cache.sh), [model orchestration](../scripts/run_models.sh), [hardware smoke log archive](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/logs/hardware-smoke.log.zip), [campaign log archive](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/campaign/logs/run_job_20261110-173025.log.zip).

## Scope and provenance

| Field | Recorded value |
| --- | --- |
| Branch reviewed | `folding-in-solo` |
| Review revision | `7f658c4c9d1ec776dd5eda33d68b56548c7f7297` |
| Recorded experiment revision | `290873e856386d1a857f8e04b94ac17f78eeb177` |
| Experiment source state | Campaign log reports `prep OK: clean committed benchmark source tree` |
| Results directory | `results/benchmark-2026-11-10-001` |
| Preflight directory | `preflight/20261110-223018-62540` |
| Host and execution mode | `hoedus`, `SINGLE_NODE_ONLY=1`, standalone serving, loopback `127.0.0.1` |
| GPU and driver | NVIDIA GB10, `580.178.04` |
| OS and kernel | Ubuntu 24.04.5 LTS, `7.0.0-1019-nvidia`, aarch64 |
| Client environment | Python 3.12.3, 45 exact distributions; recorded environment validation and `pip check` passed |
| Serving image | `nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2` |
| Selected configuration | `single`, 15 model entries, GPU utilization target `0.90` |
| Planned full measurement grid | Concurrencies `1 4 8 16 32 64`, 3 independent repetitions, 45 server launches, 270 measurement windows |
| Forced preflight canary settings | `CANARY_FIRST=1`, `STOP_AFTER_CANARY=1`, `ALLOW_CANARY_FAILURES=0`, `PURGE_WEIGHTS=1` |

The measurement grid describes the plan. No measurement windows completed in the saved artifacts. Model revisions and per-model serving settings are retained in the [campaign results directory](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/campaign/results/).

Sources: [campaign identity](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/campaign/results/campaign_run.json), [client environment](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/campaign/results/client_environment.json), [system snapshot](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/logs/system_snapshot.json), [plan archive](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/logs/plan.log.zip).

## Recorded sequence

| Stage | Observation |
| --- | --- |
| Initial snapshot | Captured at `2026-11-10T22:30:18.596184+00:00`; peer collection was skipped because solo mode was enabled. |
| Output and plan checks | Output directories were writable. Campaign options and the client environment passed validation. Four dual-only model entries were excluded. |
| Hardware smoke | Selected `llama3.1-8b-nvfp4`, GPU utilization `0.90`, 30-second windows, concurrencies `1 4`, and 2 repetitions. Cache access, pinned-snapshot validation, and client tokenizer checks passed. The initial canary failed at page-cache preparation. |
| Campaign preparation | Source cleanliness, client environment, campaign definition, local resource availability, and local runtime identity checks passed. Ray, worker mirroring, all-reduce, and send/receive checks were skipped for solo mode. |
| Campaign canaries | Each of the 15 models completed pinned acquisition and reached page-cache preparation. Every attempt reported `stage=memory-preparation rc=1`. |
| Canary termination | The campaign log reports `canary pass FAILED for 15 item(s)` at `2026-11-10 19:07:48`. The first canary began at `17:30:26`, an elapsed interval of 1 hour, 37 minutes, 22 seconds in the log's clock. |
| Cleanup | Per-model purge output follows failed canaries. The final preflight purge log records local cache cleanup. |

The campaign log's timestamps have no timezone suffix. Its `17:30:25` start corresponds to the campaign identity's `22:30:25` UTC creation time, consistent with America/New_York on November 10. The shell command originally entered by the operator and the outer serial runner's final console output are absent from the saved logs.

The 15 failed campaign entries were:

- `qwen3.6-35b-a3b-fp8`
- `llama3.1-8b`
- `llama3.1-8b-fp8`
- `llama3.1-8b-nvfp4`
- `qwen3.6-27b`
- `qwen3.6-27b-fp8`
- `qwen3.6-27b-nvfp4`
- `qwen3.6-35b-a3b`
- `qwen3.6-35b-a3b-nvfp4`
- `gemma4-31b-it-nvfp4`
- `gemma4-26b-a4b-nvfp4`
- `nemotron3.5-lightning-30b-a3b-nvfp4`
- `gpt-oss-120b`
- `nemotron3-super-120b-a12b-nvfp4`
- `qwen3-next-80b-nvfp4`

Sources: [hardware smoke archive](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/logs/hardware-smoke.log.zip), [campaign archive](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/campaign/logs/run_job_20261110-173025.log.zip), [final purge archive](../results/benchmark-2026-11-10-001/preflight/20261110-223018-62540/logs/purge-models.log.zip).

## Failure mechanism and propagation

1. `flush_page_cache.sh:2` enables `set -euo pipefail` and line 5 sources `connectx_env.sh`.
2. `connectx_env.sh:10-27` initializes solo execution. Line 21 unsets `LOCAL_NODE_ID`; lines 11-18 establish and export `LOCAL_HOSTNAME`.
3. `flush_page_cache.sh:50` calls `flush_local_page_cache`. Its first command, at line 18, expands `$LOCAL_NODE_ID` in a log message. Bash terminates with an unbound-variable error before reading memory statistics or dropping caches.
4. `run_models.sh:1212-1216` labels the operation `memory-preparation`, captures the failure in the allocated server log, and returns failure. The server launcher is invoked later, at line 1241, and is never reached for these attempts.
5. `preflight.sh:84-105` records a failed stage and continues to later checks. Its hardware smoke failure therefore does not suppress the campaign canary stage.
6. The campaign continues through independent model canaries, acquires and purges their snapshots, and reports all 15 failures at the end.
7. `preflight.sh:182-207` makes any recorded stage failure produce exit 1. `run_preflight_then_benchmark.sh` starts the full benchmark only after preflight returns success. The saved failures require that gate to reject the run if it was invoked through the serial runner.

Commit `40e57923b23581020902b7d2d7ebdabc1a00766f` introduced the solo initialization that unsets the cluster identity. `flush_page_cache.sh` retained its unconditional cluster-variable references. The four scripts involved in the immediate failure, `connectx_env.sh`, `flush_page_cache.sh`, `run_models.sh`, and `preflight.sh`, have no differences between the recorded experiment revision and the reviewed revision.

## Non-GUI mode and diagnostic limits

- The user's description and review commit identify this as the non-GUI run. The inspected artifacts do not establish the active display-manager service or systemd boot target.
- The failing expansion depends on solo initialization and Bash nounset behavior. It has no GUI-state check. The evidence establishes a solo-path code defect; it does not establish that disabling the GUI caused the failure.
- Host GPU inventory succeeded. The run did not reach serving-container GPU initialization, model loading, API readiness, inference, or power measurement. Their behavior remains untested by this run.
- Purge output includes `The NVIDIA Driver was not detected` and a shared-memory warning. The standalone purge command in `scripts/purge_model_cache.sh:134-139` runs the image without GPU flags or host IPC. These messages occur in cache-cleanup containers after the page-cache failure and do not establish a serving failure.
- Campaign guidance to fix `models.yaml/image` is the aggregate canary error message. Every recorded model failure identifies `memory-preparation` and the same unset variable.
- The 16 allocated server logs contain only the unbound-variable error. Their filenames do not establish that a server process started.

## Correction and verification required before another run

These actions are recommendations. This review changes no executable code or benchmark settings.

- Update local cache-flush log messages to use the solo hostname when solo mode is active and the topology node identity for cluster execution. The current references occur at lines 18, 22, and 25 of `flush_page_cache.sh`. Exporting `LOCAL_NODE_ID` before launch cannot bypass the defect because solo initialization unsets it.
- Add a regression test that executes the real cache-flush entrypoint with `SINGLE_NODE_ONLY=1`, no cluster identity, and stubbed cache-flush commands. Assert local completion and absence of worker contact. Keep actual cache writes and GPU workloads outside the test.
- Preserve the page-cache preparation gate. Bypassing it changes the measurement procedure.
- After the correction and regression check, run the hardware smoke test before authorizing another all-model canary or full benchmark. The current evidence cannot predict later serving or measurement failures.

## Review checks and changes

- Restored 23 log archives with `python3 scripts/archive_logs.py unzip results/benchmark-2026-11-10-001`.
- Compared all 23 restored logs byte-for-byte with their ZIP entries; all matched. Original ZIP archives were preserved.
- Counted 15 campaign failure records and 15 campaign server logs with the same error. The hardware smoke contributed one additional server log with that error.
- Searched the preflight tree for `.csv`, `.jsonl`, and `manifest.json` artifacts; none were present.
- Inspected source control history, recorded campaign identity, environment validation, snapshots, and failure propagation. No benchmark, GPU workload, model download, hardware change, or test suite was run during this review.
- The initial working tree was clean. This report is the only tracked-content addition; restored logs are ignored files.
- Scope assumption: the requested run is `results/benchmark-2026-11-10-001`, added by the branch-tip commit titled `hoedus solo in no gui mode failed`.
- Unmet requirements and instruction conflicts: none. Out-of-scope issues: none identified beyond the untested runtime stages listed above.
