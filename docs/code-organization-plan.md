# Design Plan: Benchmark Execution and Validation Boundaries

**Status:** Proposed

## Objective

Reduce coupling between benchmark execution, campaign verification, and dependency validation. Keep the existing command-line interfaces, result formats, fingerprint rules, and release gates stable.

This plan covers the large benchmark and campaign-validation modules and duplicated validation contracts. Campaign artifact layout, plotting, cache scripts, model configuration, and benchmark methodology are out of scope.

## Repository Evidence

- [`bench/run_benchmark.py`](../../bench/run_benchmark.py) has 1,652 lines. Its `main()` function is 415 lines and coordinates client setup, monitoring, load generation, provenance, result checks, and artifact publication. The module also owns index field definitions, index-row validation, source and environment fingerprints, serving-policy loading, and index append logic.
- [`scripts/validate_campaign.py`](../../scripts/validate_campaign.py) has 1,963 lines. `verify_campaign()` is 482 lines and verifies the expected grid, environment manifest, serving policies, summaries, logs, all-reduce artifacts, and campaign manifest.
- `validate_campaign.py` imports `bench.run_benchmark` and calls its private functions for serving-policy, provenance, observability, quality, and source-fingerprint checks. The verifier therefore depends on implementation details of the benchmark entry point.
- `scripts/check_environment.py` and `scripts/validate_campaign.py` each normalize distribution names and parse exact `name==version` dependency pins. `bench/run_benchmark.py` has another distribution-name normalizer for its client-environment record.
- `bench/run_benchmark.py` fingerprints every top-level `bench/*.py` file. The campaign manifest records source files from top-level `bench/` and `scripts/` globs. New modules must be included in both inventories.

## Safety Constraints

- Keep the `index.csv` header, field order, row meanings, summary JSON, serving-policy JSON, and campaign-manifest schema unchanged.
- Keep runtime release checks and final campaign verification as separate gates. Share pure validation rules while retaining checks at both lifecycle points.
- Extract one cohesive responsibility at a time. Do not split modules to meet a line-count target, add forwarding wrappers, or combine this work with broad formatting or naming changes.
- Keep dependency validation standard-library-only. Do not add runtime packages for parsing or typing.
- Preserve the benchmark source fingerprint's coverage. Every new measurement-affecting module must be hashed and listed in the sealed source inventory.
- Do not edit, move, regenerate, or delete campaign artifacts as part of this work.
- Run the refactor between campaigns. A changed benchmark source fingerprint makes existing partial runs incompatible with new measurements. Use a fresh `OUT_DIR` for measurements made by the refactored source.
- Keep existing direct commands working, including `python -m bench.run_benchmark` and `python scripts/validate_campaign.py plan|verify`.

## Proposed Work Order

### 1. Record the existing contracts

Inventory the current public commands, exit behavior, dependency-lock error cases, fingerprint inputs, and serialized field sets. Use existing fixtures and representative saved campaign inputs as read-only references. Do not normalize error messages or data fields during extraction.

### 2. Consolidate dependency-lock parsing

Create one small standard-library module for distribution-name normalization and exact-pin parsing. Use it from the environment checker, benchmark environment snapshot, and campaign verifier. Keep environment-specific checks, manifest serialization, and user-facing diagnostics in their current command owners. Confirm a shared import works when each script is run directly from the repository root.

### 3. Establish public release-contract APIs

Move shared serving-policy parsing and the pure provenance, observability, and result-quality rules out of the benchmark CLI module. Expose typed functions and constants from cohesive contract modules. Have both the live runner and campaign verifier call those public contracts. Keep caller-specific context checks in their existing flows. Remove the verifier's calls to underscore-prefixed benchmark functions. Keep a rule in its current owner if extraction would add indirection without removing duplication or coupling.

### 4. Separate benchmark execution from artifact handling

After the shared contracts are in place, move index schema definitions, index-row validation, artifact metadata, and index persistence into a focused artifact module if they form a coherent boundary. Move environment, source, experiment, and deployment fingerprint logic into the provenance module from step 3. Leave `run_benchmark.py` responsible for argument parsing and sequencing workload, monitor, runner, validation, and publication operations. Make these extractions separately reviewable and preserve the command's failure and cleanup behavior at each step.

### 5. Separate campaign verification by responsibility

Keep `validate_campaign.py` as the command-line entry point and campaign verification coordinator. Extract model/experiment YAML validation, expected-grid construction, and manifest operations only where each group forms a cohesive boundary. Extract summary and serving-policy cross-checks only where the shared release contracts do not already own the rule. Keep ordering and fail-closed behavior visible in the coordinator. Preserve atomic writes and failure behavior.

Place new modules directly under `bench/` or `scripts/`, or update the fingerprint and manifest inventories to recurse into any new subdirectories. Do not leave a new module outside both source inventories.

## Validation Gates

- Dependency-lock fixtures produce the same normalized names, pins, duplicate detection, and rejection behavior through all three callers.
- Existing unit tests cover valid and invalid index rows, summary quality, serving-policy validation, environment manifests, and campaign manifests without hardware access.
- Before and after extraction, representative `plan` output, CLI arguments, exit status, index header, JSON key sets, and fingerprint serialization match. Fingerprint values may change because source files changed; the fingerprint algorithm and coverage must remain correct.
- The campaign verifier accepts and rejects the same temporary campaign fixtures as before the refactor. Manifest generation uses temporary directories and does not touch existing results.
- New source modules appear in both the benchmark fingerprint and the campaign manifest source inventory.
- `run_job.sh` rejects an uncommitted refactor source tree before hardware work. The final implementation is committed before a campaign starts.
- No GPU, Ray, model download, or cluster operation is required for this refactor.

## Completion Criteria

- `run_benchmark.py` coordinates a run and delegates index, fingerprint, and release-contract rules to cohesive modules.
- `validate_campaign.py` coordinates verification and uses public shared contracts instead of private runner functions.
- The environment checker and campaign verifier share one dependency-lock parser and name normalizer.
- Existing command-line interfaces and artifact schemas remain unchanged.
- No campaign artifacts or measurement methodology change.
- New and changed functions have explicit parameter and return annotations. The refactor does not include a repository-wide annotation cleanup or unrelated formatting pass.
- Each extraction has a clear owner and removes a dependency, duplicated rule, or mixed responsibility. No module exists only to forward calls.

## Assumptions and Open Decisions

- The current source fingerprint behavior is intentional. Refactored source creates a new fingerprint and requires a fresh output directory for new measurements.
- The shared dependency-contract module location depends on direct-script import behavior. Select the smallest location that both command-line tools can import without runtime dependencies or duplicated path-loading code.
- Existing campaigns remain immutable. If a campaign requires verification against its original source fingerprint, run the verifier from that recorded source revision.


WHEN YOU HAVE FINISHED, APPLY the skill docs-writing and update all documentation to reflect the state and workflow of the system
