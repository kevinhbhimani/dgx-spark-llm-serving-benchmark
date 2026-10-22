# Design Plan: Failure-Oriented Logs and Hardware Telemetry

**Status:** Implemented in software. GB10 temperature validation remains open.

## Problem and Repository Evidence

Power sampling already exists in `bench/monitors.py`. `bench/run_benchmark.py` writes timestamped power samples, summary fields, sample counts, and coverage. The current `nvidia-smi` backend measures GPU-device power, not whole-node or wall power. The monitor does not query temperature in `bench/`, `scripts/`, or `configs/`.

Server stdout/stderr is retained per launch and used by `scripts/run_models.sh` to check NCCL transport. Repeated preflight and wrapper output increases routine log volume. Any reduction must retain the transport proof and failure evidence.

## Logging Contract

Keep routine campaign output to one concise event per model acquisition, launch, readiness, measurement completion, and purge. Each event includes a stable stage name, model label, revision prefix, node scope, and outcome. Disable download progress bars and per-file success messages in automated campaign mode.

For failures, record the stage, model ID and revision, node, cache path, exception class or exit code, relevant error text, bounded log excerpt, artifact path, and corrective action. Do not log Hugging Face credentials or authorization headers. Preserve exit status and do not mark incomplete work successful.

Keep per-test failure handling separate from campaign-level status. A failed independent test is logged and recorded, then the runner continues when shared prerequisites remain healthy. Summarize failures after safe work finishes and leave the campaign visibly partial or failed for resume. Stop immediately for a cluster, resource, provenance, or artifact-integrity failure that makes later measurements unsafe or invalid. Canary failures remain a pre-measurement gate under the existing override policy.

Keep the per-launch server log while the NCCL audit parses it. Reduce this artifact only after the transport line, server identity, and needed failure diagnostics are captured and validated elsewhere. Do not broadly redirect stderr or change third-party logging levels without identifying the noisy source.

## Power and Temperature

Extend the current per-sample power query to include GPU temperature in Celsius where the target driver supports it. Keep power's existing meaning and release coverage checks unchanged. Add temperature using the same sample timestamp and GPU/node identity. Extend the worker Ray sampler with the same fields.

For the measurement window, record mean and maximum temperature, sample count, maximum sample gap, source, units, and per-node coverage. Store raw temperature observations in the existing telemetry artifact where the schema permits. Missing and unsupported readings remain null with explicit coverage. Never substitute zero, repeat stale readings, infer temperature from power, or label GPU-device readings as whole-system temperature.

Do not make temperature a release gate until the GB10 sensor fields, accuracy, sampling interval, and missing-value behavior are validated on the pinned image and driver. Preserve existing power coverage rules.

## Reproducibility and Schema

Version any result schema change. Preserve the current workload, request window, energy integration, `power_scope`, per-node power coverage, serving fingerprints, runtime identity, transport audit, and artifact hashes. Keep operational model-download duration outside performance and energy windows.

## Acceptance Criteria

- Routine logs have fewer repeated status and preflight messages while retaining one diagnostic server log per launch until NCCL evidence has a replacement.
- Failures identify their phase, model, revision, node, path, cause, and retained evidence without secrets.
- Power values retain their existing interpretation and release thresholds.
- Head and worker temperature data are included when supported. Nulls and coverage accurately represent missing sensors or samples.
- Temperature summaries use only samples inside the request measurement window and include units and source.
- Existing measurement semantics, indexes, transport auditing, and publication validation continue to operate.

## Implementation Record

`scripts/run_all.sh` records a failed repeat/concurrency point and checks that
the expected model remains served. It continues to later points only while that
shared serving state is healthy. It reports all isolated point failures and
returns a nonzero status after safe points finish. `scripts/run_models.sh`
logs each failed launch stage, return code, model revision, node scope, output
path, and server log. It continues to independent launches only while shared
Ray state remains healthy. `scripts/run_job.sh` aggregates failed measurement
legs, checks Ray before each leg, and stops on invalid resume state, Ray loss,
or cache purge failure. Optional reports use the same failure aggregation.

`scripts/failure_diagnostics.py` bounds failure text to one line, removes
explicit secrets, authorization values, bearer tokens, and token query values.
The snapshot downloader, cache-size query, and streaming client apply it to
failure details while preserving failure status. Automated snapshot downloads
disable progress bars. Per-launch server logs remain available to the transport
audit.

Head and worker `nvidia-smi` queries include GPU index, UUID, power, and GPU
temperature. Raw power artifacts use telemetry schema version 2 and include
temperature, sensor identity, node identity, and the shared sample timestamp.
Summary temperature is restricted to valid readings inside the request
measurement window. It records mean, maximum, count, maximum observed sample
gap, source, units, and per-node status. Missing and unsupported readings are
null. Temperature does not affect power integration or release validation.

Focused coverage includes failure continuation and shared-server stop behavior,
credential redaction and retained exit status, temperature parsing and missing
values, worker script syntax, power-energy preservation, and window selection.
The locked Python 3.12.3 environment passed `scripts/check_environment.py` with
45 exact distributions and a clean `pip check`. Full test discovery passed all
227 tests. Tests cover the GB10 campaign's run-wide GPU utilization default of
0.8, the `GPU_UTIL` override through planning and launch selection, and rejection
of invalid values before launch. A plan-only run with `GPU_UTIL=0.85` printed the
resolved target without creating its output directory or starting hardware
work. The local host is x86_64, not a DGX Spark GB10. Sensor fields, sampling
interval, and missing-value behavior remain unverified on the pinned GB10 image
and driver.
