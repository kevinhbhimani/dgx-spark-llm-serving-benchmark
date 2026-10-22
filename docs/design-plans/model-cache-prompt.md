# Agent Prompt: Implement the Model Cache Lifecycle Plan

Implement the approved scope in [model-cache-plan.md](model-cache-plan.md). Begin by checking the current repository state and tracing the active campaign commands. The plan is the design contract; use repository behavior and the pinned runtime as evidence where the plan identifies open questions.

Academic and scientific rigor is of the utmost importance. Separate code-derived facts, observed behavior, inference, and unresolved hypotheses. Verify pinned-runtime behavior rather than assuming it. Make only the smallest changes that satisfy the plan and preserve reproducibility. Follow existing conventions; add an abstraction only when it removes real duplication and makes the code clearer. Do not add speculative options or indirection.

## Scope

- Replace redundant whole-group predownload and duplicate cache preflight behavior with selected-model JIT acquisition where supported.
- Place the Hub cache under the invoking host user's writable home directory by default. Define one canonical `HF_HUB_CACHE` and propagate it to all relevant host and container processes.
- Keep `HF_HUB_DISABLE_SHARED_BLOBS=1` active and make cache validation, mirroring, tokenizer/config loading, and purge use the same explicit cache root.
- Ensure cache files created in containers remain usable by the host user. Prefer matching UID/GID when supported; otherwise use the narrowest verified permission mechanism. Do not create a second fallback cache.
- Preserve dual-node acquisition and transfer behavior until cold-cache evidence proves it is safe to remove.
- Preserve failure isolation: record a per-model acquisition/cache failure and continue to a later independent model group only when shared cache and cluster state remain safe. Complete cleanup before proceeding and retain the campaign failure status.
- Identify the actual source of the NVIDIA warning before removing its trigger. Do not suppress stderr to make output appear clean.
- Preserve pinned revisions, download-before-measurement semantics, safe selected-revision cleanup, and actionable failure diagnostics.

## Work Boundaries

This workstream owns cache acquisition, cache paths and permissions, cache validation, mirroring, and purge. Avoid changing result directory resolution or power/temperature telemetry. If shared files need edits, make the smallest compatible changes and document overlap for integration with the campaign-results and observability workstreams. Use a separate worktree when several agents work in parallel.

Do not move, delete, rewrite, or adopt the pre-existing modified result artifacts in this checkout. Do not change model revisions, workloads, serving policies, or measurement windows. Preserve NCCL transport evidence and fail-closed validation.

## Required Method

1. Trace actual callers and inspect the pinned Hub/vLLM runtime behavior. Separate verified repository facts from assumptions.
2. Record how the NVIDIA warning is reproduced and which process emits it before modifying that path.
3. Implement the smallest changes that satisfy the plan. Remove dead helpers only after checking all callers and documentation.
4. Update relevant operational documentation and error messages.
5. Add or update focused tests for cache root propagation, host/container permissions, repo-local cache layouts, pinned snapshot completeness, mirror errors, purge safety, and warning-source behavior. Run the relevant tests and report exact commands and results.

## Completion Report

Report changed files, behavior, tests and outcomes, remaining hardware validation, assumptions, requirements not fully met, and any conflicts with coding standards. Do not claim a cold-cache hardware path passed unless it was run and observed.
