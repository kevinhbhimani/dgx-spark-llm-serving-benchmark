# Design Plan: Per-Campaign Results Directories

**Status:** Implemented. See the implementation record below.

## Problem and Repository Evidence

Campaign artifacts currently go directly under `OUT_DIR`, which defaults to `results`. `run_job.sh` writes indexes, run logs, manifests, plots, transport reports, and comparisons below that path. `run_models.sh`, `run_all.sh`, `bench.run_benchmark`, smoke and microbenchmark scripts, campaign validation, and plotting tools also construct paths from `OUT_DIR` or default to `results/index.csv`.

The current checkout has existing named directories and an untracked `results/` tree. Those artifacts must remain in place and continue to work through explicit paths.

## Proposed Layout

New campaign artifacts use:

```text
results/<campaign-name>-<YYYY-MM-DD>/
```

Keep `OUT_DIR` as the only user-facing setting for choosing a campaign name and output location. Do not add a separate `CAMPAIGN_NAME` variable or another naming option. A new campaign's full target path is `results/<campaign-name>-<UTC-date>/`; callers can set, for example, `OUT_DIR=results/rdma-v4-paper-2026-10-22`. When `OUT_DIR` is omitted, resolve a documented default campaign path under `results/` using the UTC date. Resolve the date once when the campaign starts, then pass the exact resolved `OUT_DIR` through every child process. Do not let child scripts append another path component or compute their own date.

An explicitly supplied `OUT_DIR` is the complete target path. Preserve its current use for legacy directories, smoke tests, and resume. Resuming uses the same `OUT_DIR`, even after midnight UTC. A new campaign must not silently overwrite an existing directory; an existing path is resumable only when its campaign metadata and artifacts pass the compatibility checks. `PLAN_ONLY=1` prints the resolved path without creating it. If the default dated path already exists and is not a valid resume, fail before hardware work and require the caller to choose a fresh `OUT_DIR`.

Retain the contents currently stored within each campaign output directory: `index.csv`, summaries, raw request and telemetry files, serving policies, server logs, campaign manifest, run log, and `figs/`. Keep artifact references relative to the campaign directory where the current schema expects relative paths. Put generated figures, tables, variance reports, transport audits, and campaign comparisons under that campaign's `figs/` tree.

Preserve per-test failure isolation and resumability. A failed model/configuration/replicate remains visibly failed or incomplete and does not block later independent items when shared prerequisites are healthy. The campaign summary must report the failure, and final status must not call partial results complete. Keep successful completed items available to `--skip-done` so a rerun can fill only the gaps. Stop before further measurements when campaign identity, artifact integrity, or shared cluster validity cannot be trusted.

Plotting CLIs accept an explicit campaign directory or index path. Keep direct `--index` support for existing campaigns. Do not silently select the latest directory because the campaign identity must remain explicit for publication figures. Baseline inputs remain under `baselines/`; generated comparison reports belong under the selected campaign directory.

Do not automatically move, delete, or rewrite existing top-level `results/` files or existing named campaign directories.

## Refactor Scope

Trace and update the single `OUT_DIR` contract through `scripts/run_job.sh`, `scripts/run_models.sh`, `scripts/run_all.sh`, `bench/run_benchmark.py`, campaign manifest and validation commands, `scripts/ray_allreduce_bench.sh`, `scripts/interconnect_extra_bench.sh`, `scripts/hardware_smoke.sh`, comparison and statistics tools, and `plotting/plot_results.py`, `plotting/plot_trends.py`, and `plotting/plot_interconnect.py`.

This workstream changes output path resolution only. It does not change cache paths, model acquisition, benchmark timing, plot calculations, index schema, or campaign inclusion rules.

## Acceptance Criteria

- `OUT_DIR` remains the only way to select a campaign name or output location. Supplying `OUT_DIR=results/<campaign-name>-<UTC-date>` writes to that exact path; omitting it resolves the documented default dated path under `results/` before hardware work begins.
- Every child process uses the same resolved directory. No files for a new campaign are written directly under the `results/` root.
- `PLAN_ONLY=1` prints the exact directory and creates no result directory.
- Same-day path collisions fail closed unless the caller explicitly points `OUT_DIR` at a compatible existing campaign for resume.
- Resuming across dates reuses the exact original directory. Manifests identify the campaign name, date, and resolved output path.
- Plotting accepts a campaign path or an explicit index path and resolves relative artifacts independent of the shell's current directory.
- Existing output paths remain readable and no current result data is moved or deleted.

## Implementation Record

`campaign_output.py` resolves an explicit complete path or the default
`results/benchmark-<UTC-date>` and provides the identity guard used by
`run_job.sh`. The orchestrator resolves its path once and passes the absolute
value to child processes. Plan-only mode performs no directory creation.
Implicit path collisions require matching campaign metadata. Explicit legacy
paths retain their prior resume behavior. Plotters accept campaign directories
or index paths, resolve raw artifacts relative to the index, and default
figures and reports to the campaign's `figs/` tree. The campaign manifest now
records its name, date suffix, directory name, and resolved output path.

Six path tests pass. They cover UTC dates, explicit nested paths, resume across
dates, no-create plan-only behavior, collision handling, and plotting path
resolution from another working directory. Existing result files were not
moved or modified.
