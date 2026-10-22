# Agent Prompt: Implement Failure-Oriented Logs and Telemetry

Implement the approved scope in [observability-plan.md](observability-plan.md). Retain evidence needed for diagnosis and publication. Fewer log lines are useful only if the retained output explains what happened and where the detailed evidence is stored.

Academic and scientific rigor is of the utmost importance. Preserve measurement semantics, provenance, and diagnostic evidence. Separate measured behavior from expected behavior and inference. Make only the smallest changes that satisfy the plan; follow existing conventions and add an abstraction only when it removes real duplication and makes the code clearer. Do not add speculative telemetry frameworks, options, or indirection.

## Scope

- Reduce repeated routine preflight, per-file, and wrapper output. Keep a concise event for acquisition, launch, readiness, measurement completion, and purge.
- Preserve one server log per launch while NCCL transport validation reads it. Do not broadly discard stderr or lower third-party logger verbosity without identifying the source and retaining failure evidence.
- For failures, include stable stage, model and revision, node, path, exception or exit code, actionable error excerpt, artifact location, and next corrective action. Redact credentials and authorization headers. Preserve the original failure status.
- Preserve continue-on-test-failure behavior: log and retain an isolated test failure, then allow later independent work to continue when prerequisites remain healthy. Keep final campaign status visibly partial or failed. Fail fast only when continuing could compromise measurement validity, shared state, or safety.
- Extend existing power samples with GPU temperature in Celsius when the target driver exposes a valid field. Preserve power definitions, current release coverage, sample timing, and energy calculations.
- Collect head and worker observations through the existing collection pattern. Preserve node/GPU identity and a common timestamp. Summarize only valid samples in the request measurement window, including mean, maximum, count, maximum gap, sensor source, units, and per-node coverage.
- Represent missing or unsupported temperatures as null with explicit coverage. Do not use zero, stale-value carry-forward, power-based inference, or whole-node labels for GPU sensors.
- Do not make temperature a release gate until sensor behavior is validated on the pinned target image and driver. Version any required result schema change.

## Work Boundaries

This workstream owns log content and hardware telemetry. Do not alter cache lifecycle, model acquisition, results-directory policy, benchmark workload, measurement-window boundaries, energy integration, or existing power release thresholds. Coordinate shared edits to `scripts/run_job.sh`, `scripts/run_models.sh`, and benchmark result writers with other agents. Use a separate worktree for parallel work.

Do not move, delete, rewrite, or adopt the pre-existing modified result artifacts in this checkout. Preserve transport audits, fingerprints, artifact hashes, and fail-closed release validation.

## Required Method

1. Inventory existing log consumers and telemetry schema before changing output. Identify which server-log lines the NCCL audit requires.
2. Verify temperature field availability and semantics on the target stack or report hardware verification as outstanding. Do not infer GB10 sensor support from generic `nvidia-smi` documentation.
3. Implement bounded, structured failure context and concise routine output. Keep secrets out of logs and test redaction.
4. Add or update focused tests for log fields, redaction, exit-status propagation, temperature parsing, missing values, per-node coverage, measurement-window selection, and schema compatibility.
5. Run relevant tests and report exact commands and results. Do not claim hardware telemetry is validated unless it was measured on the target system.

## Completion Report

Report changed files, log/telemetry contract, tests and outcomes, what was verified on hardware, assumptions, requirements not fully met, and any conflicts with coding standards.
