# Agent guide: implement configurable two-Spark topology

Status: implementation handoff. This document specifies work to perform in a later coding task. It does not report an implementation or a hardware result. Read [the two-Spark design](two-spark-topology-design.md) and [AGENTS.md](../AGENTS.md) before editing code.

## Instruction precedence and scope

- Implement Step 1: exactly two DGX Sparks with arbitrary configured hostnames, IPv4 fabric addresses, prefixes, and per-node interface names. Either physical node can be the Ray head and benchmark client.
- Use one versioned YAML file on both hosts. The file contains both node records. The local record is selected by the host's short hostname. The YAML path is supplied through `CLUSTER_CONFIG` on both hosts.
- The design document's `load_env.sh` table row mentions selecting a local node ID. Its later command contract and the user's final choice specify hostname-based selection with one file. Do not require `CLUSTER_NODE_ID` in the implementation.
- Preserve the current `single`, `tp2`, and `pp2` experiment semantics, result metrics, model matrix, power coverage rules, transport audit, and historical artifacts.
- Keep host network provisioning outside the code. The YAML declares expected Netplan and link state; validation checks actual state. Do not write Netplan, `/etc/hosts`, firmware settings, or routing tables.
- Do not begin Ray, GPU, model-download, benchmark, or network-changing operations while implementing unless the human separately authorizes them. Do not commit or publish the implementation without authorization.
- Do not expand this work into an arbitrary-node scheduler. Step 1 must reject any topology with other than two nodes.

## Read the current call paths before editing

The source was surveyed at Git commit `517f1a608dd9052758f033dd01d4e00ac7f71daf`. Reinspect these files in the implementation checkout because the working tree may have changed:

| Path | Read for |
| --- | --- |
| [scripts/load_env.sh](../scripts/load_env.sh), [scripts/connectx_env.sh](../scripts/connectx_env.sh) | `.env` precedence, exported defaults, local IP checks, RDMA checks, resource locks, Ray worker resource check. |
| [scripts/start_ray.sh](../scripts/start_ray.sh), [scripts/verify_connectx.sh](../scripts/verify_connectx.sh) | Container creation, `ray start` arguments, last-octet peer calculation, route and ping checks. |
| [scripts/launch_server.sh](../scripts/launch_server.sh), [scripts/mirror_worker_cache.sh](../scripts/mirror_worker_cache.sh), [scripts/mirror_worker_cache.py](../scripts/mirror_worker_cache.py) | Ray node membership, pinned cache checks, worker placement, offline model transfer. |
| [scripts/purge_model_cache.sh](../scripts/purge_model_cache.sh), [scripts/purge_model_cache_container.py](../scripts/purge_model_cache_container.py), [scripts/resource_manager.py](../scripts/resource_manager.py) | Worker cache deletion and idle-resource checks. |
| [scripts/ray_allreduce_bench.sh](../scripts/ray_allreduce_bench.sh), [scripts/ray_allreduce_bench.py](../scripts/ray_allreduce_bench.py), [scripts/interconnect_extra_bench.sh](../scripts/interconnect_extra_bench.sh), [scripts/ray_interconnect_bench.py](../scripts/ray_interconnect_bench.py) | Rank placement, per-process NCCL/Gloo interface overrides, artifact IDs, transport logs. |
| [scripts/cluster_runtime_fingerprint.py](../scripts/cluster_runtime_fingerprint.py), [scripts/system_snapshot.py](../scripts/system_snapshot.py) | Per-node software/hardware facts, equal-runtime comparison, remote SSH snapshot. |
| [scripts/run_job.sh](../scripts/run_job.sh), [scripts/run_models.sh](../scripts/run_models.sh), [scripts/run_all.sh](../scripts/run_all.sh), [scripts/preflight.sh](../scripts/preflight.sh) | Campaign sequence, resume identity, serving policy, worker power target, setup snapshots. |
| [utils/campaign_output.py](../utils/campaign_output.py), [scripts/campaign/manifest.py](../scripts/campaign/manifest.py), [scripts/validate_campaign.py](../scripts/validate_campaign.py) | Campaign `OUT_DIR` identity, sealed artifact manifest, result verification and all-reduce fingerprint reproduction. |
| [bench/release_contracts.py](../bench/release_contracts.py), [bench/provenance.py](../bench/provenance.py), [bench/run_benchmark.py](../bench/run_benchmark.py), [bench/metrics.py](../bench/metrics.py) | `serving-v1` validation, source hash, per-point resume hash, power coverage. |
| [tests/test_rdma_readiness.py](../tests/test_rdma_readiness.py), [tests/test_run_all_failure_isolation.py](../tests/test_run_all_failure_isolation.py), [tests/test_release_readiness.py](../tests/test_release_readiness.py), [tests/test_validate_campaign.py](../tests/test_validate_campaign.py) | Existing mock patterns and historical-policy fixtures. |

Search the active source and documentation for `DIYA_`, `HOEDUS_`, `cisc-diya`, `cisc-hoedus`, `10.10.10`, `CX_IFACE`, `EXPECTED_HEAD`, `EXPECTED_WORKER`, `head_ip`, and `worker_ip`. Exclude `results/`, `baselines/`, and archived diagnostic output when identifying code to modify. Preserve historical measurements and baseline labels.

## Invariants to retain throughout the change

1. `head_node` identifies the Ray head, benchmark client, model acquisition node, and local power sampler. The other configured node is the worker and the remote power sampler target.
2. The Ray `NodeManagerAddress` for each node equals that node's address on `rendezvous_rail`. Ray task placement uses `node:<that-address>`.
3. Each node's `NCCL_SOCKET_IFNAME` and `GLOO_SOCKET_IFNAME` list contains its own configured Linux interface names, in deterministic rail order. The two lists may differ.
4. Each host loads its topology file before local Ray startup. After both nodes join, compare their canonical topology SHA-256 values before distributed work. Their containers expose that digest and their own node ID. A changed host file with an old Ray container is a mismatch.
5. `NCCL_IB_DISABLE` remains the shared RDMA/TCP policy. An `NCCL_IB_HCA` or GID selection can be local to a node; the runtime fingerprint records the effective value on each node.
6. The existing two-GPU and two-power-sampler release requirements remain attached to `tp2` and `pp2`. `single` continues to use the selected head only.
7. A locally detectable topology error fails before creating or replacing a Ray or serving container. A mismatch detected after Ray joins blocks cache transfer, cache deletion, microbenchmarks, serving, and measurement rows. Read-only validation and an existing resource lock may precede an error.
8. A campaign never resumes a canary, microbenchmark artifact, or point from a different topology. Historical artifacts remain byte-for-byte unchanged and readable.

## Phase 1: typed YAML model and canonical identity

Use the field names and example in [the design's configuration contract](two-spark-topology-design.md#configuration-contract). Keep the model small: one cluster record, two node records, and each node's rail records. Prefer frozen, typed records or a comparably explicit typed structure. Give all new function parameters and returns concrete annotations. Do not use a generic dictionary as the validated internal representation.

Implement one loader in `scripts/` that has these operations:

| Operation | Input | Output |
| --- | --- | --- |
| Parse | YAML file bytes | Validated topology value. |
| Select local node | Validated topology, observed `hostname -s` | Exactly one node record and its role. |
| Derive role values | Validated topology | Head and worker IDs, hostnames, rendezvous addresses, Ray port. |
| Derive local values | Validated topology, local node record | Local rendezvous address, ordered interface names, ordered rail/peer pairs. |
| Serialize | Validated topology | Stable canonical JSON bytes and SHA-256. |
| CLI validation | File path | Human-readable, read-only validation result or a nonzero error. |
| CLI shell transfer | File path, local hostname | Validated fields in a fixed, non-evaluated record format. |

The Python loader must read the file once per invocation. Require a readable regular file, a mapping at the root, known keys, `schema_version` equal to integer `1`, exactly two node mappings, and an existing `head_node` ID. Reject duplicate YAML keys. `yaml.safe_load` alone accepts duplicate keys, so add a duplicate-key check to its safe loader. Reject booleans where an integer is required and reject coerced YAML values where a string is required.

Validate node IDs and rail IDs as stable slugs. Validate hostnames against the observed short-hostname contract. Require unique node IDs and hostnames after the chosen hostname normalization. Validate `ssh_target` as a single SSH destination, consistent with the safe host handling in `system_snapshot.py`. Validate interface names as one Linux netdevice token that cannot become a shell option, a comma-separated second interface, or a control character. Respect the Linux interface name length limit. Reject unknown keys instead of silently ignoring misspellings.

Parse each `ipv4` with `ipaddress.IPv4Interface`. Store normalized address and prefix. Reject duplicate addresses, loopback, multicast, unspecified, and any endpoint that cannot be a host on its declared subnet. Require at least one rail and the same set of rail IDs on both nodes. Require a different non-overlapping IPv4 network per rail and both endpoints in the same network with the same prefix. Require `rendezvous_rail` to be present on both nodes. Validate `ray_port` as an integer from 1 through 65535. Do not derive peers by changing an octet.

Canonical serialization uses only validated values: schema version, `head_node`, `rendezvous_rail`, `ray_port`, and the complete node records. Sort nodes by ID and interfaces by rail ID. Serialize with stable JSON keys and separators, then SHA-256 those exact bytes. Do not hash the host-local YAML path, comments, whitespace, or YAML key order. Keep a separate SHA-256 of the original file bytes for artifact provenance. A role swap, hostname, address, prefix, interface, rail, SSH target, or port change must change the canonical digest.

Do not put credentials, Hugging Face tokens, cache paths, model labels, or benchmark settings in this topology model. `requirements.lock` already pins `PyYAML==6.0.3`. `start_ray.sh` runs on both hosts, so the selected host Python interpreter needs that module before any container starts. Report the interpreter and import failure clearly; do not install packages as a side effect of YAML loading.

## Phase 2: shell integration and precedence

Keep `scripts/load_env.sh` responsible for the existing `.env` precedence. Read `CLUSTER_CONFIG` after that load. Require an explicit nonempty path for topology-consuming entrypoints. Resolve relative paths against the repository root so the same command works from another current directory. The two hosts may use different absolute paths to files with the same canonical content.

Have `scripts/connectx_env.sh` call the typed loader before it exports topology values. Derive and export `HEAD_NODE_ID`, `WORKER_NODE_ID`, `LOCAL_NODE_ID`, `HEAD_HOST`, `WORKER_HOST`, `HEAD_IP`, `WORKER_IP`, `LOCAL_IP`, `RAY_PORT`, local `CX_IFACE`, and `TOPOLOGY_SHA256`. `HEAD_IP`, `WORKER_IP`, and `LOCAL_IP` come from `rendezvous_rail`. `VLLM_HOST_IP` equals `LOCAL_IP` on both Ray nodes. `NCCL_SOCKET_IFNAME` and `GLOO_SOCKET_IFNAME` use the local `CX_IFACE` list. Preserve image, cache, Ray version, NCCL transport, and resource-lock defaults unless the topology requirement directly affects them.

Do not `eval` YAML-derived text or source a generated shell script. A safe shell boundary is: run the loader to a temporary file, check the producer's exit status, read fixed-order NUL-delimited fields, verify the expected record count, assign values, and remove the file. Avoid process substitution for the producer because a failed producer can leave `mapfile` successful with empty input. Validate field characters before shell output. Pass all resulting values as quoted arguments or array elements.

Reject a conflicting inherited topology variable, including `DIYA_*`, `HOEDUS_*`, `HEAD_IP`, `HEAD_HOST`, `CX_IFACE`, and `VLLM_HOST_IP`. Equal inherited values may pass; a different value must name the conflicting key and the YAML-derived value. Child scripts inherit derived exports and source `connectx_env.sh` again. Distinguish these known derived values from caller overrides so nested calls do not fail. Recompute on a new `CLUSTER_CONFIG` in the same shell; do not let the `DGX_SPARK_ENV_LOADED` guard freeze previously derived topology. Do not silently use a previous topology after a file change.

Split static loading from hardware validation. A `PLAN_ONLY=1` run can parse and identify the local hostname without ping, RDMA, Docker, or GPU work. State-changing entrypoints call explicit local validation before their first state change. `start_ray.sh stop` still identifies the local node when the fabric link is down and does not require peer reachability to remove the local container.

## Phase 3: local network validation and Ray startup

Replace the arithmetic peer discovery in `verify_connectx.sh`. For each sorted rail, read its local interface, local CIDR, and peer address from the validated topology. Check the configured address and prefix on that interface, link-up state, and a route to the peer whose device and source are the configured local interface and address. Run the existing interface-bound ping against that explicit peer address. Report the rail ID and both endpoints in every failure. Check each selected interface's active/link-up RDMA mapping when `NCCL_IB_DISABLE=0`. Keep the TCP mode's route and ping checks while skipping its RDMA gate.

Do not require `hostname` to resolve to a ConnectX address. The configured hostname identifies the local record; Ray uses explicit IPs. `ssh_target` supplies the independent snapshot destination. Preserve strict SSH host-key checking in `system_snapshot.py`.

In `start_ray.sh`, compare its `head` or `worker` argument with the automatically identified local node role before `docker rm -f` or `docker run`. Check local addresses, routes, requested RDMA state, and cache access at the existing preflight stage. Use `LOCAL_IP` for `ray start --node-ip-address` and container `VLLM_HOST_IP`; use `HEAD_IP:RAY_PORT` for the worker join address. Pass `TOPOLOGY_SHA256`, `LOCAL_NODE_ID`, and local network policy as non-secret container environment values. Ensure a new YAML file cannot be paired with a container started under an older digest.

Do not replace the pinned vLLM image, Ray dependency constraints, offline serving defaults, NCCL transport log settings, or cache ownership checks as part of the topology change.

## Phase 4: every two-node consumer

Replace the meaning of `HOEDUS_IP` with derived `WORKER_IP` at active call sites. Keep the current two-node APIs where they express the two-node experiment. Rename interface and environment fields where their meaning is a physical machine rather than a role. Review the following paths in this order:

1. `connectx_env.sh` passes head and worker IPs into `resource_manager.py`. Required worker checks must fail if Ray lacks the configured worker. Auto checks may retain their existing allow-missing behavior for local-only operations.
2. `mirror_worker_cache.sh` and `.py` use the derived worker for Ray placement. Their exact pinned snapshot and cache verification logic stays unchanged.
3. `purge_model_cache.sh` and its container script use the derived worker. Preserve dry-run behavior, serving-process refusal, and the one-node fallback when Ray is unavailable.
4. `launch_server.sh` uses the derived role addresses for Ray membership, pinned cache, and memory preflight. Require exactly the configured two Ray GPU nodes; reject an extra node. Preserve the current `single`, `tp2`, and `pp2` launch flags and the head-only API listener default.
5. `run_all.sh` derives `WORKER_POWER_NODE_IP` from `WORKER_IP`. A nonempty override must equal the YAML worker IP and differ from the head IP. Keep the current required two-node power coverage and head/worker fields in result rows.
6. `run_models.sh`, `run_job.sh`, `preflight.sh`, `predownload.sh`, and `hardware_smoke.sh` load the file through their existing environment chain. Head-only campaign and acquisition commands must reject execution from the worker before a download, cache purge, or benchmark starts. Update messages that currently instruct operators to use Diya or Hoedus.
7. `system_snapshot.py` uses the worker's `ssh_target` from the YAML. Add configured node IDs, roles, interface CIDRs, topology digest, and observed state to new snapshots. Preserve the explicit unavailable status when SSH cannot collect a remote snapshot.

`scripts/hoedus-head-env.sh` becomes a historical role-swap example. Remove it from the primary workflow and update [docs/hoedus-head.md](hoedus-head.md) to show a `head_node` change. Do not use that helper to set `DIYA_*` or `HOEDUS_*` during a topology-aware run.

## Phase 5: microbenchmarks and runtime evidence

`ray_allreduce_bench.py` and `ray_interconnect_bench.py` currently pass one `iface` argument from the head and overwrite `NCCL_SOCKET_IFNAME` and `GLOO_SOCKET_IFNAME` inside both Ray tasks. Remove that shared override. Each task uses the environment of its own Ray container, already set from that node's YAML entry. The driver may pass each rank its own expected IP and interface list for validation; it must not set the worker's network environment to the head's list. Rank 0 remains pinned to `HEAD_IP`; rank 1 remains pinned to `WORKER_IP`. Validate each task's observed Ray IP and interface environment against its assigned node record before timing. Preserve the existing GPU idle checks, two-rank world size, output CSV format, NCCL channel evidence, transport classification, and timing acceptance thresholds.

`cluster_runtime_fingerprint.py` currently compares the entire `runtime_env` object across both nodes. Split the comparison:

| Shared across nodes | Checked against each node's declared or effective local state |
| --- | --- |
| Pinned image identity, Ray and vLLM versions, torch/CUDA stack, common GPU and driver requirements already enforced by the script, `NCCL_IB_DISABLE`, and shared serving policy. | Ray `NodeManagerAddress`, `LOCAL_NODE_ID`, `TOPOLOGY_SHA256`, `NCCL_SOCKET_IFNAME`, `GLOO_SOCKET_IFNAME`, `NCCL_IB_HCA`, GID selection, and RDMA-to-netdevice mapping. |

Both containers must report the same topology digest as the head's current file. Their node IDs must map to distinct configured Ray IPs. Interface names need only match their own node's YAML record. An explicit RDMA device allowlist must map to the selected local interfaces; the YAML does not declare RDMA device names. Report unavailable observed RDMA mapping as unknown or failure according to the existing RDMA gate; do not infer it from a device name pattern.

Update both shell microbenchmark artifact fingerprints and `scripts/validate_campaign.py`'s `_expected_allreduce_fingerprint` together. Include the topology digest and the effective per-node network/runtime facts. The verifier must reproduce the writer's hash without a live Ray cluster. Historical CSVs keep their previous identity and validation path.

## Phase 6: campaign identity, resume, and artifact provenance

This phase is a single contract change. Update writers and validators in the same implementation branch and test the round trip before running any model.

1. Extend `run_job.sh`'s campaign identity from `utils/campaign_output.py` with `topology_sha256`. Keep the existing `campaign_run.json` equality check for implicit and explicit paths.
2. Copy the exact YAML bytes into the campaign input artifacts and record both the raw-file SHA-256 and canonical topology SHA-256. At finalization, compare the current file and the saved input, then include the saved input in the sealed manifest. Offline verification reads the saved input and does not require the original host path. If the YAML was stored outside Git, the saved bytes supply its provenance. Preserve the existing clean-source gate for code under `bench/`, `configs/`, `scripts/`, and `plotting/`; include changed `utils/` modules if they become part of campaign identity.
3. Add topology identity to the canary marker, serving policy, single and dual runtime identities, and both microbenchmark artifact identities. A topology edit must invalidate `--skip-done` reuse and a previous canary before an expensive action. Include the new topology loader and other measurement-affecting changed sources, including a changed campaign-output utility, in `bench/provenance.py`'s source fingerprint.
4. Define a new topology-aware serving-policy schema, such as `serving-v2`. Its canonical fingerprint input contains the existing launch fields, the topology digest, head/worker IDs and IPs, per-node interface lists, transport policy, and runtime-facts digest. Use one shared canonical serialization function for the new writer and validator; the current `serving-v1` newline serialization in `run_models.sh` and `bench/release_contracts.py` must remain readable for historical rows.
5. Update `scripts/validate_campaign.py` to check one topology digest across all new campaign rows and serving policies. Include the topology input in a new manifest contract while preserving schema-1 historical verification. Update its all-reduce artifact reproduction and global-policy consistency checks. Do not alter raw measurements, old manifests, or old policy files.
6. Preserve `bench/run_benchmark.py`'s deployment fingerprint semantics: it hashes a serving fingerprint plus observed live capacity. Feed it the new serving fingerprint; do not insert host paths or volatile Ray process IDs into per-point experiment identity.

An explicit `OUT_DIR` topology failure must leave existing index rows, canary markers, and artifacts untouched. A changed topology requires a fresh campaign output path. Plots and comparisons must identify different topology digests as different experimental conditions; do not pool their rows as one condition.

## Phase 7: tests and documentation

Use small synthetic fixtures, including the reserved-address example in the design. Do not present fixture timing or mocked command output as hardware measurements. Tests should exercise behavior and failure boundaries rather than duplicate loader implementation details.

| Test class | Required cases |
| --- | --- |
| YAML contract | Two valid nodes with different interface names and one, two, or four rails; role swap; reordered YAML with the same canonical hash; changed address/interface/role with a different hash; duplicate keys; unknown fields; bad scalar types; duplicate IDs/hosts/IPs; mismatched peer networks; missing rendezvous rail; three-node rejection. |
| Local selection | Either hostname selects its own record; unknown hostname fails; wrong local address or prefix fails before Docker; `stop` remains possible when a link is down. |
| Link verifier | For every rail, test the declared peer IP, bound interface, route device, route source, address, link state, and RDMA state. Test TCP mode's RDMA skip and its retained route checks. |
| Ray and cache | Wrong role fails before `docker rm`; missing or extra Ray node fails; topology digest mismatch fails; cache mirroring and purge select the configured worker. |
| Microbenchmarks | Different head/worker interface names survive into their own Ray tasks; rank/IP binding is exact; transport evidence still rejects wrong or mixed channels; verifier reproduces the artifact fingerprint. |
| Campaign | Implicit and explicit output directories reject topology changes; a changed topology invalidates canary and `--skip-done`; serving-v2 writer/reader agree; serving-v1 fixtures remain valid; manifest contains the saved YAML and hashes. |
| Power and single mode | Worker sampler targets the configured worker; `power_nodes=2` remains required for `tp2`/`pp2`; `single` uses only the selected head and remains distinct from dual runtime facts. |

Update the existing tests that mock `DIYA_IP`, `HOEDUS_IP`, `CX_IFACE`, `getent`, and last-octet peer behavior. Preserve any tests that verify legacy artifact parsing. Run the repository's available unit tests, Python compilation, and `bash -n` on changed shell scripts. Run a `PLAN_ONLY=1` command with a synthetic topology and mocked hostname only if it stays read-only. Do not run GPU or network benchmarks as part of a coding-only verification pass.

Update [docs/cluster-setup.md](cluster-setup.md), [docs/new-spark-setup.md](new-spark-setup.md), [docs/campaign-runbook.md](campaign-runbook.md), [docs/hoedus-head.md](hoedus-head.md), and [README.md](../README.md) only where commands or fixed-name assumptions change. Show `CLUSTER_CONFIG` on both Spark commands, describe how the same YAML is made available on each host, and state the required host network and SSH setup. Label any concrete Diya/Hoedus addresses as an example. Keep model, workload, and metric descriptions unchanged.

## Implementation completion checklist

- Every active fixed-name or fixed-IP reference has been replaced with a role-derived value, or is retained solely as labeled historical documentation or measurement data.
- Both hosts use the same canonical topology digest; each uses its own interface names and rendezvous address.
- Static and local validation fail before local state changes. A Ray topology mismatch fails before distributed work, with clear expected and observed values.
- Both head assignments pass mock-based startup, verification, Ray placement, cache, microbenchmark, and campaign identity tests.
- New serving-policy, microbenchmark, and campaign artifacts reproduce their own hashes. Historical artifact fixtures still validate.
- No benchmark inputs, serving policies, metric definitions, raw results, or baseline artifacts changed outside the topology requirement.
- Report checks actually run, assumptions, requirements still open, and any standards conflicts. Report hardware acceptance as unperformed until it is authorized and observed.

## Future-node boundary

Keep node IDs, rail IDs, and canonical topology serialization independent of the physical names. Retain the explicit two-node validator in Step 1. Do not generalize the two-rank microbenchmarks, `tp2`/`pp2` labels, one-worker cache transfer, head/worker power schema, or result validation to a third node in this implementation. A later switch-based design must define fabric membership and reachability before relaxing that validator.
