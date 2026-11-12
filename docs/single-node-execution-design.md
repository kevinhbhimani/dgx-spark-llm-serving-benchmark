# Single-node execution through the existing campaign scripts

## Status and source baseline

- Status: implementation completed. Hardware acceptance remains pending.
- Inspected branch: `arbitrary-nodes`.
- Inspected commit: `6d19429bdcad1d77eee2a9bd329a8b1dcfe4acb4`.
- Inspection date: 2026-11-10.
- Implementation branch: `folding-in-solo`.
- Initial working-tree state: untracked `docs/single-node-execution-design.md` and `solo-node-testing/`. Both were preserved.
- Implementation changed entrypoints, local campaign provenance, schema validation, and solo documentation. Hardware tests, benchmark campaigns, model downloads, and peer connections were not performed.
- Focused verification: `.venv/bin/python -m unittest tests.test_solo_execution tests.test_run_job_contract tests.test_run_preflight_then_benchmark` passed 26 tests.
- Full verification: `.venv/bin/python -m unittest discover -s tests` passed 296 tests.
- Shell verification: `bash -n` passed for all 16 changed shell scripts.
- Python verification: `.venv/bin/python -m compileall -q bench scripts` passed. No repository type-checker configuration or `mypy`/`pyright` executable was available.
- Diff verification: `git diff --check` passed.

## Required behavior

Add one public environment variable, `SINGLE_NODE_ONLY`, to the existing entrypoints. Set it to `1` only when explicitly requesting single-node testing. Leave it unset for the current campaign behavior.

```bash
# Run local readiness checks, local smoke, local canaries, and local benchmarks.
SINGLE_NODE_ONLY=1 scripts/run_preflight_then_benchmark.sh

# Preserve the current campaign behavior without setting a mode variable.
scripts/run_preflight_then_benchmark.sh
```

The solo path must run on the invoking machine with no peer configured, reachable, or attached. It must also run when a peer is attached or when the invoking machine is the worker in an existing cluster YAML. Solo execution must not contact, measure, purge, stop, or modify that peer.

“No networking” means no inter-node dependency or traffic initiated by these scripts. The existing HTTP benchmark client still uses local loopback. Image acquisition and model acquisition retain their existing Internet/cache requirements. This feature does not provide an air-gapped acquisition workflow or Docker network isolation.

Keep the experiment YAML, model revisions, single-node eligibility, workload buckets, seeds, measurement windows, concurrency defaults, independent repetitions, serving policy defaults, quality gates, and power/metrics requirements unchanged. Exclude only model/config combinations and operations that require another node.

Hardware acceptance is separate from the implementation. Do not run GPU workloads, download models, change networking, commit, or publish unless the human requests those actions.

## Current dependency map

Paths and symbols below refer to the inspected commit.

| Source | Current behavior | Required change |
| --- | --- | --- |
| [`run_preflight_then_benchmark.sh`](../scripts/run_preflight_then_benchmark.sh) | Resolves one output directory, invokes preflight, then forces a full measurement run after preflight success. | Resolve and export the mode before either child; preserve serial failure propagation. |
| [`connectx_env.sh`](../scripts/connectx_env.sh) | Loads `.env`, parses two-node topology before exposing common image/cache/locking helpers, derives network variables, and rejects conflicting overrides. | Make common settings available without topology parsing in solo mode. |
| [`cluster_topology.py`](../scripts/cluster_topology.py), `ClusterTopology` | Represents exactly two nodes and selects the local node by hostname. | Keep this schema and its validation unchanged. Solo execution bypasses it. |
| [`preflight.sh`](../scripts/preflight.sh) | Requires head role and live links before output checks; runs ConnectX checks, standalone smoke, campaign gates/canaries, then purges both caches. | Retain local stages and local cleanup; explicitly skip peer stages. |
| [`run_job.sh`](../scripts/run_job.sh) | Forces `SINGLE_CONTAINER=ray`; checks Ray throughout canaries/measurements; obtains dual runtime identity; runs all-reduce/send-receive; seals saved topology. | Use standalone serving, local identity, local resource checks, and solo artifact validation. |
| [`campaign_schedule.py`](../scripts/campaign_schedule.py), `build_groups` | Already filters configs, omits empty groups, and marks groups that need a worker mirror. | Reuse filtering with effective configs restricted to `single`. |
| [`hardware_smoke.sh`](../scripts/hardware_smoke.sh) | Runs single-node serving and measurement, but requires head identity and live links; uses resource scope `auto`. | Retain smoke coverage without cluster requirements. |
| [`predownload.sh`](../scripts/predownload.sh) | Requires head role and validates links before acquisition. | Retain local cache, space, permission, revision, and acquisition checks. |
| [`launch_server.sh`](../scripts/launch_server.sh), [`run_models.sh`](../scripts/run_models.sh), [`run_all.sh`](../scripts/run_all.sh) | Require head role and links even for `single`; serving policies encode topology fields. | Permit standalone solo execution; retain all local serving and measurement gates. |
| [`purge_model_cache.sh`](../scripts/purge_model_cache.sh) | Detects Ray, can infer worker purge, uses `auto` resource checks, and validates links even for a local throwaway purge. | Select an unconditional local purge path in solo mode. |
| [`connectx_env.sh`](../scripts/connectx_env.sh), `resource_check_once` | `auto` can inspect the worker through an existing Ray container. | Resolve `auto` to `none` in solo mode; reject explicit `required`. |
| [`system_snapshot.py`](../scripts/system_snapshot.py), `main` | Requires a cluster YAML and snapshots the worker through SSH. | Add a local-only snapshot path that never reads topology or invokes SSH. |
| [`release_contracts.py`](../bench/release_contracts.py) | `serving-v2` binds even single-host runtime facts to cluster identity. | Add an explicit solo policy schema without fabricated peer fields. |
| [`validate_campaign.py`](../scripts/validate_campaign.py), `verify_campaign` | Requires nonempty network policy fields; `serving-v2` requires saved cluster YAML. All-reduce artifacts are already conditional on verified dual rows. | Validate exact solo selection and a local execution record; retain dual validation. |

Setting only `CAMPAIGN_CONFIGS=single` leaves the topology, Ray, cleanup, and runtime gates active. Setting `NCCL_IB_DISABLE=1` selects TCP transport for distributed execution and does not enable solo execution.

## Configuration contract

### Parsing and propagation

- Accept exactly `0` or `1`; resolve an unset value to `0` internally. Only `SINGLE_NODE_ONLY=1` enables solo execution. An unset value or `0` preserves current dual-enabled behavior; callers do not need to set the variable for that behavior. Reject empty strings, `false`, `true`, and other values with a named diagnostic before probing or mutating resources.
- Load `scripts/load_env.sh` before resolving the default. Keep its exported-environment precedence over `.env`.
- Export the resolved value to all child scripts. Do not independently default a child back to dual mode.
- Check environment-loader failures explicitly in callers that lack `set -e`, including `run_job.sh`. A failed mode/topology load must terminate before later commands or cleanup traps.
- Put parsing in `connectx_env.sh` immediately after `load_env.sh`, before any topology read. The serial runner can source `load_env.sh`, validate/export this same flag, and then proceed without sourcing the hardware environment.
- Keep `.env` guard behavior. Tests must cover inherited `DGX_SPARK_ENV_LOADED` and a mode already exported by the parent.
- Do not add another user-facing mode flag, command-line switch, topology schema, or required solo YAML.

### Selection

| Mode and requested configs | Effective behavior |
| --- | --- |
| Dual enabled, configs unset | Current full matrix. |
| Dual enabled, explicit configs | Current subset behavior. |
| Solo, configs unset | All existing model entries supporting `single`, with only `single` selected. |
| Solo, explicit `single` | Single-node subset, preserving requested labels. |
| Solo, any explicit `tp2` or `pp2` | Configuration error before downloads, locks, cleanup traps, or serving. |
| Solo, labels selecting no single-capable entry | Empty-selection error before hardware operations. |
| Solo, mixed single-capable and dual-only labels | Run eligible single combinations; print excluded dual-only labels. Unknown labels remain errors. |

Preflight currently clears `CAMPAIGN_LABELS` and `CAMPAIGN_CONFIGS` for broad readiness coverage. Preserve that behavior for readiness stages, but preserve `SINGLE_NODE_ONLY=1` so both nested jobs still resolve to a single-only matrix. Validate contradictory caller configs in the serial runner before starting preflight. Direct `run_job.sh` must validate them too.

For standalone entrypoints, reject `tp2`/`pp2` whenever solo mode is active. `predownload.sh all` may acquire only single-eligible entries in solo mode; a direct request for a dual config must fail. Do not make excluded dual-only entries single-capable by changing model YAML.

## Environment and ownership design

### Keep the existing shared entrypoint

Retain `connectx_env.sh` as the source used by current scripts. Guard its topology initialization and derived-variable conflict checks behind dual mode. Leave shared container, Hugging Face cache, cache-access, lock, and local resource helper definitions available in both modes. Do not duplicate these definitions into a second solo script.

In solo mode:

- Do not open `CLUSTER_CONFIG`, invoke `cluster_topology.py`, select a configured head, inspect ConnectX links, check routes, ping, or require RDMA tooling.
- Determine the local hostname using the same normalization as existing hostname handling. Keep local hostname separate from configured cluster node IDs.
- Use `127.0.0.1` for internal local endpoint/thermal/diagnostic address arguments where existing code requires an address. Record it as a loopback endpoint, never as a measured fabric address.
- Do not synthesize `WORKER_IP`, worker node IDs, topology hashes, or ConnectX interfaces. Guard consumers instead of supplying placeholder cluster facts.
- Ignore inherited cluster-derived exports for solo topology selection and provenance. They must not affect solo identity, resume decisions, or container launch arguments.
- Construct solo container environment arguments without NCCL/GLOO interface allowlists or RDMA selectors inherited from a cluster shell. Record the effective solo environment. Leave the parent's environment unchanged outside the script process.
- Derive a distinct solo container name from the configured base, appending `-solo` once. The default base `vllm-spark` becomes `vllm-spark-solo`; an existing `.env` base remains usable with the one mode flag. Export an internal initialization marker so repeated sourcing and child entrypoints retain the resolved name. This marker is not a public option. Continue to honor the smoke script's subsequent temporary-container override without adding another suffix in its children.
- If the resulting solo container name already belongs to a Ray runtime, refuse before any `docker rm` or process kill. Do not remove or reuse that Ray container. Check this in both the orchestrator and direct launcher path.

Introduce one shared `validate_execution_environment` shell function to replace repeated head-role-plus-link-validation call pairs. Its dual branch performs the existing head-role and live-link checks. Its solo branch validates local execution identity without cluster access. Preserve callers that deliberately omit live-link checks for status-only or plan-only operations; these paths must not gain hardware checks.

Keep `require_local_connectx_ip`, `validate_local_topology_links`, `require_connectx_rdma`, `validate_cluster_runtime`, and `validate_local_ray_container` strict. Do not turn these cluster-specific functions into success-returning stubs. Explicit cluster-only entrypoints, including Ray start, cache mirroring, ConnectX verification, and interconnect microbenchmarks, must reject solo mode before side effects.

### Locks, idle checks, and cleanup

- Preserve the host-scoped lock path and descriptor inheritance. Solo and dual runs on the same physical host must contend for the same lock.
- In `resource_check_once`, map `auto` to `none` before Docker/Ray worker discovery when solo mode is active. Reject `required` with a configuration error in solo mode. Retain local busy/unknown/timeout handling.
- Replace solo-reachable hardcoded `required` calls in `run_job.sh` with mode-selected scopes, including prep, failed legs, canary failure recovery, and signal handling.
- Keep local GPU/process checks active. An attached peer doing unrelated work must not block solo readiness. A busy local GPU must block it.
- Use standalone serving in solo mode regardless of stale `SINGLE_CONTAINER=ray` settings in `.env` or the parent. Direct solo launch/model entrypoints must enforce the same effective value.
- Track whether the current command launched a serving process/container. Cleanup may remove only that owned container or process. Status-only queries and early failures must not remove a pre-existing Ray container.
- `purge_model_cache.sh` must select local throwaway-container cleanup without Ray detection in solo mode. Default `PURGE_WORKER` to `0`; reject explicit `PURGE_WORKER=1` before cleanup. Use resource scope `none`.
- Keep existing selected-repository protection and local purge failure handling. Preserve preflight's all-configured-model local purge scope; omit the worker operation and the two-node retry path. Report the local purge outcome accurately.

## Stage behavior

| Stage | Dual enabled | Solo |
| --- | --- | --- |
| Input/options validation | Existing rules | Existing rules plus mode/config consistency |
| Output write probes | Run | Run |
| Locked Python client environment | Run | Run |
| Config/workload validation and plan | Full or requested matrix | Exact single-only matrix |
| Read-only system snapshot | Head and worker | Local host/container only |
| Head-role, route, ping, link, RDMA checks | Run | `SKIP: dual-node checks disabled (SINGLE_NODE_ONLY=1)` |
| Single-node hardware smoke | Run | Run with all existing local checks |
| Ray health and per-node runtime match | Run | Skip; single-host runtime collection remains required |
| All-reduce and send/receive | Run | Skip |
| Model acquisition | Selected groups | Single-eligible selected groups only |
| Cache mirror | Groups needing dual | Skip |
| Strict stream/model/tokenizer canaries | Existing selected configs | Single configs only |
| Independent measurement launches | Existing selected configs | Standalone single configs only |
| Power, metrics, thermal telemetry | Current node scope | Local node only |
| Group/final preflight cache purge | Current head/worker behavior | Local only |
| Grid, artifact, source, provenance verification | Current checks | Exact selected single grid and local execution record |
| Result plots/tables, trends, replicate statistics | Run | Run for single rows |
| Interconnect figure and NCCL transport audit | Run | Explicit skip |
| Baseline comparisons | Existing available baselines | Existing same-model/config matching, using single rows |
| Sealed manifest | Run | Run with solo scope and all required local artifacts |

Use explicit skip messages for omitted stages in both preflight summaries and job logs. Skips must not set the failure flag or claim a passed interconnect check. Local failures retain existing failure/exit behavior and prevent the serial runner from starting the measurement job.

Do not skip an entire useful stage because its current initialization requires a peer. Split the initialization requirements for hardware smoke, downloads, local cache access, local memory checks, telemetry, and local purge as described above.

## Planning and completeness

Add optional `--labels` and `--configs` selection arguments to `validate_campaign.py plan` and `verify`, matching the existing scheduler's space-separated selection semantics. These are internal consistency plumbing for existing `CAMPAIGN_*` controls. Keep their default behavior unchanged.

Add a typed selection function in `scripts/campaign/plan.py` that takes validated `ModelSpec` records and optional label/config selections and returns selected model records with filtered config tuples. Validate unknown selections and empty results. Reuse this selection rule from the scheduler and validator. Avoid a second independent filtering implementation.

In `run_job.sh`:

1. Resolve and validate mode and requested selection before hardware operations.
2. Derive effective configs. Solo resolves to `single`.
3. Pass the effective label/config selection to the scheduler, plan validator, result verifier, and manifest builder.
4. Preserve grouping by model repository, single-config order, repetition counts, and concurrency order.
5. Run canaries and resume checks over that same selected schedule.

Solo verification must require every selected `(model_label, single, concurrency, repeat_id)` point, and reject unexpected dual points. Do not implement solo completion with `--completed-only`: that option derives combinations from existing rows and can accept a campaign with an entirely missing selected model/config combination.

Keep the existing `--completed-only` behavior for existing callers. For solo jobs use the explicit selected grid without that flag, including when `CAMPAIGN_LABELS` is set. Record selected labels/configs and exclusions in plan output and the final manifest. Excluded combinations are intentional selection metadata; missing selected combinations are failures.

## Runtime facts, serving policy, and resume identity

### Local execution record

For solo jobs, save `results/inputs/local_execution.json` under the resolved campaign directory. Define its content as:

```json
{
  "schema_version": 1,
  "execution_scope": "single-node",
  "hostname": "<normalized actual local hostname>",
  "loopback_address": "127.0.0.1",
  "serving_container": "standalone"
}
```

The hostname placeholder above is a specification placeholder. Populate it from the host at runtime. Serialize canonical identity bytes with sorted keys and compact JSON separators, and compute `execution_sha256` from those bytes. Use the existing immutable-input installation rules from `run_job.sh`: reject symlinks/non-regular files, retain identical bytes, fail on conflicting bytes, and install atomically without overwriting an existing input.

Include this record's digest, solo mode, effective selection, standalone choice, image, and existing applicable workload/serving inputs in `CAMPAIGN_IDENTITY` and canary identity. Do not read/hash `CLUSTER_CONFIG` or include inherited peer settings for solo identity. Verify the saved local record against the selected execution scope and local hostname at finalization. Preserve the existing saved-cluster-input verification for dual campaigns.

Keep the existing required GPU/driver identity collection in `run_models.sh` for single-host execution. Bind its canonical runtime facts to `execution_sha256`, hostname, standalone choice, image identity, and actual GPU facts. Preserve legitimately unavailable optional telemetry as unavailable. Do not substitute values for required GPU identity failures.

Use this same local runtime collection for the solo job readiness/canary identity. Extract the existing single-host GPU probe into one typed Python module if needed to share the actual collection between job prep and model execution. Do not hash an empty runtime or treat a successful smoke as a replacement for runtime identity.

### Solo serving policy

Introduce `serving-v3` for the standalone solo path only. Continue producing and validating current `serving-v2` policies for dual-enabled execution, including its single rows. Preserve `serving-v1` readers.

Define the v3 fingerprint fields from `SERVING_V2_FINGERPRINT_FIELDS` as follows:

- Retain every field except `ray_version`, `connectx_iface`, `nccl_ib_disable`, `head_ip`, `worker_ip`, `topology_sha256`, `head_node_id`, `worker_node_id`, `head_cx_iface`, `worker_cx_iface`, `nccl_ib_hca`, `nccl_ib_gid_index`, and `nccl_net_plugin`.
- Add `schema`, `execution_scope`, `execution_sha256`, `hostname`, and `serving_container` to the hashed fields.
- Require `schema=serving-v3`, `config=single`, `execution_scope=single-node`, and `serving_container=standalone`.
- Retain the existing runtime object, canonical runtime digest, runtime scope `single-host`, and artifact fingerprint checks. `cluster_runtime` remains the existing compatibility name for the runtime digest; its solo value must equal the local runtime hash.
- Require `runtime_facts.hostname`, `runtime_facts.execution_sha256`, and `runtime_facts.serving_container` to match policy values. Validate GPU identity fields using the existing single-host requirements.
- Require a digest-pinned image, immutable model/tokenizer revisions, and the existing quality/provenance fields. Require local-only power collection for solo rows.
- Omit the removed cluster-policy fields from v3 JSON. Reject dual config labels and contradictory peer/topology fields in a v3 policy.

Dispatch schema validation before enforcing the current common legacy fields or constructing legacy hash input in `load_serving_policy`. Dispatch `serving_policy_fingerprint` by schema while preserving existing v2 canonical bytes and hashes. Use the same v3 field definition for policy writing, fingerprinting, loading, and campaign verification.

Audit `run_models.sh` policy generation, its human-readable run note, thermal/diagnostic arguments, and all code reading policy fields. A solo path must not expand unset worker variables under `set -u`. Retain the existing single-config rejection of worker power overrides in `run_all.sh`.

### Verification and manifest

- In `validate_campaign.py`, choose required/global policy fields by schema. Solo policy validation requires the saved local execution record and its digest instead of cluster YAML.
- Require one consistent execution record, standalone choice, schema, and applicable global policy across solo rows. Keep per-model runtime, workload, observable-artifact, and repetition checks.
- Match the local execution record to runtime facts, policy digest, and campaign identity metadata. Reject mixed v2/v3 campaigns and cross-host resume.
- Keep v1/v2 completeness, topology, transport, and artifact requirements intact. Existing all-reduce gating already depends on verified dual rows.
- Add manifest schema version `3` for solo manifests, with `execution` containing the local record and digest, and campaign selection/exclusions. Include `local_execution.json` in the sealed artifact inventory. Keep versions `1` and `2` readable and unchanged for their existing paths.
- Mode changes in an existing `OUT_DIR` must fail before acquisition or cache purge. A dual canary marker cannot satisfy a solo canary, and the reverse cannot satisfy a dual canary.
- A changed peer YAML, disconnected link, or stale exported peer address must not invalidate solo resume. A changed local GPU/runtime, source, workload, selected matrix, or serving policy must retain existing mismatch protection.

## Snapshots, diagnostics, and reporting

Add `--local-only` to `system_snapshot.py`. Make `--cluster-config` conditional: it remains required for the current dual snapshot path and is unnecessary for `--local-only`. Pass this option from solo `preflight.sh` and `run_job.sh`.

The local snapshot must contain one actual host record, local container/image facts, timestamp, and `execution_scope=single-node`. Record peer collection as skipped by configuration without creating an unreachable-worker error record. Local read-only network inventory may remain, but it cannot be a success gate or issue peer traffic. Snapshot collection remains best-effort as in the current orchestrators.

Keep thermal collection and failure diagnostics local. Existing single-config paths already omit worker arguments; ensure solo address/identity fields are available without topology. Do not invoke SSH, Ray remote tasks, or remote GPU power samplers from solo error handling.

Skip `plot_interconnect.py` and `audit_nccl_transport.py` invocations in solo finalization. Keep single-node plots, tables, trends, and replicate statistics. Check these consumers with a single-only synthetic fixture; add a narrow adjustment only if a consumer requires missing dual rows. Baseline comparison text must preserve the actual single-config match and recorded experimental differences.

## Luna implementation sequence

1. Read `AGENTS.md` and the coding standard. Recheck branch/revision and working-tree state. Preserve unrelated files and raw measurements.
2. Implement mode parsing and guarded topology initialization in `connectx_env.sh`; retain shared local settings/locks and strict cluster helpers. Add the shared execution validation and resource-scope behavior.
3. Update the serial runner and solo-reachable preflight, download, launcher, model runner, sweep, smoke, and purge call sites. Guard cluster-only entrypoints. Cover signal/error paths and container ownership.
4. Implement typed shared selection in `scripts/campaign/plan.py`; use it in scheduling and exact verification. Preserve current defaults.
5. Implement immutable local execution input, shared local runtime collection, v3 policy/fingerprinting, schema-dispatched validation, and solo manifest metadata. Add any new runtime module to `bench/provenance.py`'s source fingerprint list and test that editing it changes the fingerprint.
6. Branch job prep, repeated Ray checks, acquisition/mirror, canary identity, signal recovery, and finalization by mode. Preserve broad preflight coverage within the effective mode.
7. Add local-only snapshots, local diagnostic arguments, and explicit skip reporting. Verify single-only report consumers with fixtures.
8. Update `README.md` and `docs/campaign-runbook.md` with the one-variable commands, default behavior, local loopback/acquisition requirements, selected matrix, local purge scope, and resume restrictions. Do not rewrite unrelated cluster instructions.
9. Run hardware-free tests and shell syntax checks. Report checks actually run and hardware validation still pending. Do not commit to satisfy the existing clean-source runtime gate without human authorization.

Use full type annotations for new/modified Python functions and explicit typed records for new selection/runtime structures. Do not spread existing untyped `dict` annotations into new APIs. Keep shell variable naming consistent with existing exported interfaces. Do not add a new configuration framework, external dependency, solo campaign wrapper, or automatic fallback from a failed dual run.

## Required hardware-free tests

Use temporary repositories/directories, explicit synthetic fixtures, and command stubs. Stubs must log invocations and fail on forbidden operations. Successful tests establish control-flow and artifact-contract behavior; they do not establish GPU readiness or performance.

| Test area | Required assertions |
| --- | --- |
| Mode parsing | Unset or `0` selects dual; only `1` selects solo; empty/malformed values fail early; exported environment overrides `.env`; nested children preserve mode. |
| Disconnected solo | Missing/nonexistent/malformed `CLUSTER_CONFIG`, hostname absent from cluster, missing `ip`/`rdma`, and down links do not block local paths. Assert topology parser, route lookup, ping, SSH, Ray status/tasks, and interconnect scripts are never called. |
| Attached solo | Existing Ray container and hostile inherited cluster exports do not trigger worker discovery, runtime matching, remote power, mirror, or worker purge. Configured/default base names resolve to a distinct solo container exactly once across nested scripts; smoke overrides survive. A collision at the resulting solo name fails without removal. Local busy GPU still blocks work. |
| Selection | Mixed model eligibility produces only single steps; shared repositories remain grouped; no dual downloads/mirroring; unknown/empty selection fails; contradictory explicit dual configs fail; repeat/concurrency order is unchanged. |
| Preflight | Local write checks, environment plan, smoke, local canaries, and local cleanup execute; dual stages report SKIP; local stage/purge failures fail preflight; serial runner launches measurements only after success. |
| Direct entrypoints | Solo `single` launcher/models/sweep/smoke/download/purge work with stubs and no topology; solo explicit dual/worker operations fail before mutations. |
| Resource and signals | Shared lock contention remains; solo auto scope remains local with Ray present; required worker scope fails; failures and signals perform owned local teardown only and preserve failure status. |
| Serving contracts | v3 valid fixture passes; missing/forged local execution/runtime fields, inconsistent hashes, dual config, peer fields, and non-pinned revisions/images fail; changing hashed solo fields changes fingerprint; old v1/v2 fixtures retain their validation behavior. |
| Exact completion | A complete selected single rectangle passes; missing an entire eligible model, one repetition, or one concurrency fails; unexpected dual rows and mixed schemas fail. Do not use completed-only verification to satisfy these tests. |
| Resume and sealing | Mode/canary/output identity changes fail; cross-host or local runtime mismatch fails; peer configuration changes do not affect solo identity; conflicting/symlink local input fails; solo manifest includes local input and required artifacts without dual artifacts. |
| Observability/reporting | Snapshot is one local node with zero SSH calls; thermal/diagnostic/power arguments remain local; synthetic single-only data produces local reports; interconnect reports are skipped; quality gates still reject missing required power/metrics. |
| Dual regression | Default mode retains strict topology, head-role/link validation, Ray, dual runtime match, mirror/purge, all-reduce/send-receive, transport audit, and saved-topology checks. Existing tests remain enabled. |

Extend existing tests in `test_run_preflight_then_benchmark.py`, `test_preflight_hardening.py`, `test_run_job_contract.py`, `test_campaign_tools.py`, `test_validate_campaign.py`, `test_release_readiness.py`, `test_campaign_output.py`, `test_system_snapshot.py`, `test_failure_diagnostics.py`, and `test_measurement_integrity.py` where their fixtures cover the changed behavior. Use a focused new solo test module for end-to-end stub orchestration if needed. Do not replace behavior assertions with source-text matches alone.

Run `bash -n` on changed shell scripts. Use the repository's `.venv/bin/python -m unittest discover -s tests -v` when its environment is available. Report unavailable dependencies/tools instead of installing or changing dependencies. No repository strict type-checker configuration was identified during inspection; apply annotations and report the type-checker/compiler checks actually available and run.

## Hardware acceptance, pending separate authorization

These are future operator checks. Do not execute them as part of design authoring or hardware-free implementation validation.

1. On a disconnected Spark with the pinned image/cache prerequisites, run `SINGLE_NODE_ONLY=1 scripts/run_preflight_then_benchmark.sh` into a fresh output directory. Confirm local smoke, canaries, all selected single points, quality checks, reports, and sealing finish without peer traffic or prerequisites.
2. Repeat with a peer attached and a Ray runtime already present. Confirm solo execution leaves peer caches/processes untouched and preserves the pre-existing local Ray container. Confirm local resource contention is still enforced.
3. Interrupt a solo launch, verify owned local teardown and local-only purge, then resume the same solo campaign. Verify completed points are reused only under matching identity.
4. Run the existing dual workflow with default mode in an authorized cluster validation session. Verify the existing dual checks and selected matrix still execute.

Record source/working-tree state, commands, resolved mode/selection, model/image revisions, runtime/driver facts, timestamps, failures, and deviations for any authorized hardware validation. Do not present stub results as measurements.

## Assumptions, unmet requirements, and scope limits

- Assumption: solo execution targets the existing one-GPU single-node workload and the invoking machine, including a machine formerly configured as a worker.
- Assumption: “no networking” excludes inter-node operations; local HTTP and existing image/model acquisition remain permitted requirements.
- Assumption: dual behavior remains the default for compatibility.
- Hardware acceptance and measured performance remain pending. This document records the design and implementation contract.
- No coding-standard conflicts were identified. Schema branching is required to preserve existing provenance validation while representing execution without a peer.
- No unrelated source defect is asserted by this design. The existing two-node topology schema is a scope boundary and remains unchanged.
- Preserve pre-existing `solo-node-testing/` artifacts. Their contents were not used as benchmark evidence for this design.
