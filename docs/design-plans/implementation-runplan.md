# Sequential Agent Runplan

## Objective

Complete the benchmark cleanup as one ordered effort. Follow the design plans in sequence, preserve publication methodology and existing result artifacts, and leave the repository in a state where a campaign can be diagnosed and reproduced from its own output directory.

This runplan coordinates the four workstreams in this folder. It does not authorize changes to benchmark workloads, model revisions, serving policies, or measurement semantics. Use the referenced workstream plans as the detailed design contracts and their prompts as task instructions.

## Non-Negotiable Standards

Academic and scientific rigor is of the utmost importance. Treat the work as reviewable research infrastructure. Make claims only to the degree supported by repository evidence, primary technical documentation, or observed measurements. Label established facts, code-derived findings, empirical results, inferences, hypotheses, and unresolved questions distinctly. Preserve enough methods, conditions, timestamps, provenance, and artifacts for another researcher to check material conclusions. Seek evidence that could disconfirm the current explanation. Do not claim a cause, validation result, or hardware behavior that was not demonstrated.

Make the fewest code and configuration changes that satisfy every stated constraint and preserve the benchmark's academic rigor. Follow existing code and repository conventions. Reuse existing paths and mechanisms. Add an abstraction only when it removes actual duplication and makes the code easier to understand. Do not add speculative options, wrappers, layers, generalized frameworks, or convenience behavior. Minimality does not permit skipping necessary validation, failure handling, provenance, or compatibility checks.

Before each change, identify the requirement and evidence that justify it. Before completion, review each changed line for necessity and verify that no workload, model revision, serving policy, measurement boundary, inclusion rule, or publication claim changed without explicit justification and validation.

Preserve the existing continue-on-test-failure behavior. When one model/configuration/replicate or optional report fails, record the failure and continue to the next independent item if the cluster, cache, and result state remain safe. Complete safe teardown and cleanup for that item before proceeding. Never mark a failed or incomplete item as passed. After safe work finishes, summarize the failures and leave the campaign status partial or failed so it can be diagnosed and resumed. Fail fast only when a shared prerequisite, cluster health, resource preflight, provenance check, or result integrity problem makes further work unsafe or scientifically invalid. The canary remains a pre-measurement gate: check independent canary items and aggregate failures, but do not begin measurements after a failed canary unless the existing explicit override is set.

## Run Order

### 0. [x] Establish the baseline and collect local canary evidence

Start with read-only inspection of the current checkout and available crash evidence. Follow the evidence-collection portion of [canary-hoedus-plan.md](canary-hoedus-plan.md), but do not run another canary yet.

- Record the current Git state and preserve all pre-existing user changes, especially files under `results/` and named result directories.
- Trace the current canary and benchmark paths, including startup, readiness, requests, teardown, cache purge, and waits.
- Search available logs and host records for the reported failure. Classify it only when evidence supports a host, GPU/driver, Ray, serving, or resource failure.
- Record missing evidence and the access required to obtain it. Do not infer a root cause from missing logs.

**Gate:** Continue without a hardware rerun. If a code defect is already demonstrated by static evidence, record it for the relevant implementation stage. Keep a controlled canary trial as the final stage after instrumentation is ready.

**Evidence:** The active branch is `fall-cleaning`. The checkout contains pre-existing deletions under `results-rdma-v4/` and `results-rdma-v4-paper/`, plus untracked design documents and a `results/` tree. No `server_*.log` or `run_job_*.log` file was found under the local `results/` or named result directories. The canary branch tears down the server and returns without the five-second post-sweep wait; the benchmark `cooldown_s` runs during request generation and does not run in `CANARY_ONLY`. No local evidence classifies the reported Hoedus failure. Host journal, GPU, Ray, and process records are unavailable in this checkout. A hardware trial remains deferred until the authorized operator confirms that Hoedus is available for recovery.

### 1. [x] Implement model acquisition and cache ownership

Follow [model-cache-plan.md](model-cache-plan.md) and [model-cache-prompt.md](model-cache-prompt.md).

- Establish one explicit Hugging Face cache root in the invoking user's writable home directory.
- Make JIT acquisition, cache validation, tokenizer/config access, dual-node mirroring, and purge agree on that root and its permissions.
- Remove redundant predownload work only after tracing callers and reproducing the NVIDIA warning's source.
- Preserve pinned revisions, cold-cache behavior, and download/purge boundaries outside measurement windows.

**Gate:** A reviewer can trace one canonical cache path through host and container processes. Relevant unit or integration tests pass. Hardware cold-cache behavior is reported as unverified until actually exercised.

**Evidence:** Host and container probes use the invoking UID/GID and the canonical `$HF_CACHE` path mounted at `/hf-cache`; Hub, module, mirror, and purge paths use the same root. Single-node campaigns skip predownload and allow pinned-revision JIT acquisition. Dual-node campaigns acquire once, mirror, and serve offline. Cache access and path tests pass. Shell syntax and Python compilation pass. Hugging Face, Ray, and the serving image are unavailable in this checkout, so cold-cache downloads, worker mirroring, and container ownership remain unverified.

### 2. [x] Establish per-campaign result directories

Follow [campaign-results-plan.md](campaign-results-plan.md) and [campaign-results-prompt.md](campaign-results-prompt.md).

- Keep `OUT_DIR` as the only user-facing way to choose a campaign name and output location. Resolve the exact `results/<campaign-name>-<UTC-date>/` path once before hardware work. Do not add a separate `CAMPAIGN_NAME` setting; an example explicit path is `OUT_DIR=results/rdma-v4-paper-2026-10-22`.
- Pass the exact resolved `OUT_DIR` through every producer and consumer, including plotting and validation.
- Preserve explicit paths for existing campaigns, explicit resume, and legacy indexes. Do not move or rewrite old result data.
- Verify plan-only, collision, and resume behavior before allowing a hardware run.

**Gate:** A non-hardware path test demonstrates that a new campaign writes beneath one dated directory, a collision fails closed, resume reuses the selected directory, and plotting resolves artifacts from an explicit campaign or index path.

**Evidence:** `campaign_output.py` resolves explicit paths or `results/benchmark-<UTC-date>`, and `run_job.sh` passes the resolved absolute path to child processes. Implicit collisions require a matching identity marker; explicit legacy paths remain available. Plotting, validation, comparison, audit, and variance-report tools accept campaign or index paths and place default reports under the selected campaign. Nine focused path tests pass, including UTC rollover, no-create plan-only, collision, resume, and paths resolved from a different working directory. Existing result paths and the pre-existing `results/` tree were not moved or rewritten.

### 3. [x] Implement failure-oriented logging and telemetry

Follow [observability-plan.md](observability-plan.md) and [observability-prompt.md](observability-prompt.md).

- Reduce repeated routine output while preserving concise stage events and actionable failure context.
- Retain the per-launch server evidence needed by NCCL transport validation until an equivalent durable record is proven.
- Preserve existing power semantics and add temperature with explicit units, source, timestamps, node/GPU identity, coverage, and missing-value handling where supported.
- Write diagnostic and telemetry artifacts inside the resolved campaign directory.

**Evidence:**

- `run_all.sh` records failed points, checks the expected serving model, and
  continues when shared state is healthy. It returns a partial failure status
  after safe points finish. It stops the sweep when the endpoint is unhealthy
  or serves a different model.
- `run_job.sh` checks Ray between legs, stops on invalid resume state and cache
  purge failure, and continues to independent work after recoverable leg
  failures. Optional reports are aggregated as failures.
- `run_models.sh` records the failed stage, return code, model revision, node
  scope, output directory, and server log. It checks shared Ray state before
  launching more work for Ray-backed configurations.
- Acquisition, sizing, and streaming failure details are bounded and redact
  credentials. Per-launch server logs remain available for NCCL checks.
- Temperature uses telemetry schema version 2 with raw sensor identity,
  shared timestamps, window-only summaries, and explicit missing status.
- The local test environment uses an isolated Python 3.12.3 virtual environment
  installed from `requirements.lock`. `scripts/check_environment.py` validated
  45 exact distributions and `pip check` passed. Campaign validation fixtures
  use the DGX Spark `gpu_util_default: 0.8`; the over-budget case uses a live
  value of `0.9`.
  Additional tests verify that `GPU_UTIL=0.85` changes both the planned expected
  value and the model launch value, and that invalid values fail before launch.
- `GPU_UTIL=0.85 PLAN_ONLY=1` with an explicit `/tmp` output path validated the
  19 model groups and 53 launches for a one-concurrency plan. It printed the
  resolved 0.85 target, did not create the output directory, and performed no
  hardware operations.
- Full test discovery ran 227 tests and passed.
- Shell syntax, Python compilation, and `git diff --check` passed. The model
  revision matrix and locked requirements are unchanged. Experiment and smoke
  YAML changes only remove the old output-directory field. No target-hardware
  temperature reading was collected. Temperature is not a release gate.

**Gate:** Software failure propagation and telemetry behavior have focused
coverage. GB10 temperature accuracy, sensor availability, and sample behavior
remain unverified on the pinned image.

### 4. [ ] Complete the canary diagnosis and controlled comparison

Return to [canary-hoedus-plan.md](canary-hoedus-plan.md) and [canary-hoedus-prompt.md](canary-hoedus-prompt.md) after the cache, campaign paths, and diagnostics are ready.

- Use the newly retained campaign-specific evidence to classify the failure and test competing explanations.
- Reproduce only when the host is available for recovery and the operator has confirmed that a canary trial is safe.
- Keep the model revision, image, Ray state, cache state, request, and launch options fixed. Change only the post-teardown idle interval.
- Record predeclared outcomes and host/GPU/Ray/process state at consistent boundaries. Alternate or randomize trial order when practical.
- Stop after the first host-level failure and preserve evidence before recovery. Do not run a full benchmark campaign as part of this comparison.
- Treat an association with longer waits as a timing dependency. Claim thermal causation only if temperature measurements and outcome patterns support it while plausible alternatives are addressed.

**Gate:** Recommend a code change only when evidence isolates a cause. If no cause is established, report the uncertainty and the smallest additional observation needed. Do not use `cooldown_s` as an inter-run fix; it belongs to benchmark request semantics and is not executed by `CANARY_ONLY`.

**Current evidence:** The local validation host reports `x86_64` and is not a
DGX Spark GB10 node. No target host, GPU, Ray, or canary logs were available.
No controlled trial or hardware diagnosis was performed. The failure remains
unclassified, and no cooldown code change is justified by local evidence.

### 5. [ ] Integration review and final validation

**Current status:** Local software integration and regression validation are
complete. This stage remains open because the pre-existing deletions leave no
working-tree copies of the named `results-rdma-v4/` artifacts. Their 11 tracked
Git versions parse as CSV or JSON, and the deletion state remains unchanged.
Stage 4 also remains open because the DGX Spark GB10 target systems are not
available from this host.

After the four workstreams:

- Review shared edits to `scripts/run_job.sh`, `scripts/run_models.sh`, and benchmark artifact writers for conflicting path, cache, logging, and lifecycle assumptions.
- Run repository tests and static validation relevant to the changed code. The locked test environment passes all 227 tests. Report commands and results; do not claim hardware validation that was not performed.
- Inspect generated plans and campaign outputs for accidental writes outside the selected campaign directory.
- Verify old result directories remain readable and unmodified. Preserve the pre-existing working-tree deletions; do not restore them as part of this work.
- Review model/revision pins, result hashes, workload definitions, serving fingerprints, transport checks, power coverage, and measurement-window semantics.
- Verify failure isolation: an isolated failure does not block later independent tests, shared validity or safety failures do stop work, and the final status does not describe partial results as complete.
- Update operational documentation to describe the final invocation, cache path, result layout, failure artifacts, and canary procedure.

**Local validation record:** The locked Python environment validator passed with
45 exact distributions. Full test discovery passed 227 tests. Shell syntax,
Python compilation, whitespace validation, and plan-only validation passed.
The plan-only command resolved 19 model groups and 53 launches with
`GPU_UTIL=0.85` without creating its `/tmp` output path. The actual validation
host is x86_64, so these results do not validate GB10 sensors, RDMA, serving,
or the controlled canary.

## Coordination Rules

- Work serially in the order above. Do not launch parallel implementation agents for these workstreams.
- Keep one integration branch or worktree for the sequential effort. Preserve the starting Git state and do not clean pre-existing user changes.
- When a shared file changes in multiple stages, integrate each stage before beginning the next and review the combined diff.
- Keep design decisions and unresolved questions in the relevant plan. Do not silently change a plan contract to fit an implementation shortcut.
- Do not suppress diagnostics merely to reduce log volume. Every removed message must be redundant or replaced by a more useful durable record.

## Final Report

Provide a concise report with the completed stages, changed files and why each change was necessary, relevant test commands and results, hardware experiments actually performed, evidence-based canary conclusion, assumptions, unmet requirements, and remaining risks. Separate observed results from hypotheses and state which checks remain unverified. Identify any new abstraction and explain the concrete duplication it removes and how it improves readability. If no abstraction was added, state that.
