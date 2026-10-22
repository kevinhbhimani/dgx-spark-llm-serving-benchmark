# Agent Prompt: Implement Per-Campaign Results Directories

Implement the approved scope in [campaign-results-plan.md](campaign-results-plan.md). Trace every result-producing and result-consuming path before editing. Keep `OUT_DIR` as the only user-facing way to choose a campaign name and path. Do not add `CAMPAIGN_NAME` or a parallel naming setting. A caller can select a dated campaign directory with `OUT_DIR=results/<campaign-name>-<UTC-date>`.

Academic and scientific rigor is of the utmost importance. Establish path behavior from repository evidence and test observations; label assumptions and unresolved compatibility questions. Make only the smallest changes that satisfy the plan and preserve reproducibility. Follow existing conventions; add an abstraction only when it removes real duplication and makes the code clearer. Do not add speculative options or indirection.

## Scope

- Resolve a single campaign output directory once, before hardware work, and propagate that exact path to every child process.
- Add the planned default dated path behavior while preserving explicit `OUT_DIR` as the complete output path for naming, legacy runs, smoke tests, and explicit resume.
- Make `PLAN_ONLY=1` print the resolved path without creating the directory.
- Fail closed on an implicit naming collision before creating or changing campaign artifacts. Resume must reuse its original directory across date boundaries.
- Update launchers, benchmark writers, validators, microbench/smoke tools, comparison tools, and plotting CLIs to honor the same path contract.
- Let plotting use an explicit campaign directory or index path. Resolve artifact references relative to that campaign, independent of current working directory. Do not choose a directory by “latest”.
- Keep generated plots, comparison reports, and campaign-derived artifacts inside the chosen campaign's existing artifact structure. Preserve baseline inputs and old explicit paths.
- Preserve the current per-test failure isolation and `--skip-done` resume behavior. Record failed/incomplete tests, continue to later independent items when safe, and ensure final campaign status remains partial or failed rather than successful.

## Work Boundaries

This workstream owns result output path resolution, campaign artifact discovery, and plotting path compatibility. Do not change cache management, benchmark timing, measurement definitions, plot calculations, result inclusion rules, or index schema unless a versioned change is proven necessary. If a schema change is unavoidable, document the migration and keep old campaigns readable.

Do not move, delete, rewrite, or adopt existing result files. The current checkout contains pre-existing changes under `results/` and named result directories. Use a separate worktree when running parallel workstreams. Coordinate shared launcher edits with cache and observability agents.

## Required Method

1. Inventory every producer, consumer, default, and documentation reference to `OUT_DIR`, `results/index.csv`, and campaign artifact paths.
2. Implement one path resolution contract and pass the resulting path through the call graph without child-side date recomputation.
3. Update documentation and usage messages. Preserve old campaigns through explicit paths.
4. Add or update tests for default naming, UTC date resolution, plan-only behavior, collision handling, explicit resume, midnight resume, nested paths, plotting from a different working directory, and legacy index paths.
5. Run the relevant tests and report exact commands and results. Do not run a full hardware campaign as a path test.

## Completion Report

Report changed files, the final path contract, compatibility behavior, tests and outcomes, assumptions, requirements not fully met, and any conflicts with coding standards. Confirm that no existing result artifact was moved or modified.
