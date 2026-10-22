# Benchmark Cleanup Design Plans

This folder contains four independent design plans and four copy-ready agent prompts. The plans cover model acquisition and cache ownership, campaign result directories, diagnostic telemetry and logs, and Hoedus canary stability.

To execute the work as one ordered effort, follow the [sequential agent runplan](implementation-runplan.md). It begins with read-only canary evidence collection and delays any controlled hardware trial until logging and campaign output paths are ready.

## Workstreams

| Workstream | Design plan | Agent prompt | Scope |
| --- | --- | --- | --- |
| Model acquisition | [Model cache lifecycle](model-cache-plan.md) | [Model cache implementation prompt](model-cache-prompt.md) | JIT downloads, cache roots and permissions, validation, mirroring, purge |
| Result layout | [Campaign results directories](campaign-results-plan.md) | [Results layout implementation prompt](campaign-results-prompt.md) | `results/<campaign-name>-<UTC-date>/`, `OUT_DIR` propagation, plotting |
| Observability | [Logs and telemetry](observability-plan.md) | [Observability implementation prompt](observability-prompt.md) | Concise failure-oriented logs, power and temperature artifacts |
| Canary stability | [Hoedus canary diagnosis](canary-hoedus-plan.md) | [Canary diagnostic prompt](canary-hoedus-prompt.md) | Crash classification, cooldown hypothesis, controlled evidence collection |

## Sequencing

Follow the sequential runplan in one worktree. It orders the cache, result
layout, observability, and canary stages because they share launcher and
benchmark files. Keep the canary diagnosis read-only until evidence supports a
code change.

The following contracts apply across workstreams:

- Apply the highest practical academic and scientific rigor. Separate verified facts, measurements, inferences, hypotheses, and unknowns. Support material claims with repository evidence, primary technical sources, or recorded observations; do not claim causation or validation without evidence.
- Make only the minimal changes needed to satisfy the plans and preserve publication standards. Follow existing conventions and add an abstraction only when it removes real duplication and makes the code clearer.
- Preserve failure isolation: record a failed test and continue to the next independent test when shared prerequisites remain healthy. Never count failed or incomplete tests as passing. Stop when continuing could waste resources, corrupt results, or invalidate measurements; report partial results as partial or failed after safe work finishes.
- Do not change model revisions, workload definitions, serving policy, measured-window boundaries, or existing publication claims.
- Resolve one campaign `OUT_DIR` and pass it through every process. Do not select a campaign or plot directory by “latest”.
- Keep `OUT_DIR` as the only user-facing campaign naming/path setting. Do not introduce a separate `CAMPAIGN_NAME` variable.
- Keep model files in a host-user-writable HF cache under the invoking user's home. Pass one explicit Hub cache path to every process.
- Keep downloads, mirror copies, and purge operations outside measured request windows.
- Preserve transport checks, artifact hashes, provenance, and fail-closed release validation.
- Do not move, delete, or rewrite existing result files. The current checkout has pre-existing changes in `results/`.
- Report assumptions, unmet requirements, and coding-standard conflicts after implementation.

The prior combined plan at [`design-jit-cache-observability-plan.md`](../design-jit-cache-observability-plan.md) has been split into these workstreams.
