# Two-Spark configurable topology: implementation design

Status: design only. No implementation or hardware validation is included in this document. The source survey used the working tree on 2026-10-29 at Git commit `517f1a608dd9052758f033dd01d4e00ac7f71daf`. That working tree contains pre-existing uncommitted changes. File references below describe the inspected working tree.

## Scope and acceptance target

Step 1 supports exactly two DGX Sparks. Either Spark can be the Ray head, benchmark client, and serving host. The other Spark is the Ray worker. Both may have arbitrary valid hostnames, ConnectX interface names, IPv4 addresses, and address prefixes. The two nodes may use different interface names for the same fabric rail.

One versioned YAML document describes both nodes. Both machines receive the same file. Each machine identifies its local node entry by its system hostname and verifies the declared interface addresses before startup. A change of machines, addresses, interfaces, or head assignment changes the YAML document. The experiment and model YAML files retain their existing workload and serving settings.

The implementation must cover Ray startup, link verification, model cache mirroring, cache cleanup, resource checks, distributed launch, microbenchmarks, power sampling, system snapshots, campaign identity, and resume validation. A campaign must reject a topology change rather than combine results from different physical configurations.

The YAML describes the **configured** network. Netplan, cabling, RDMA device state, hostname configuration, SSH access, and routing remain host configuration. The implementation verifies those conditions before using the hardware. It does not write Netplan or `/etc/hosts`.

The second step, operation with more than two Sparks through a switch, is outside this implementation. The schema uses a node list and rail IDs so a later version can add nodes without replacing the identity model. Step 1 explicitly rejects a document with any node count other than two.

## Current coupling in the repository

| Area | Current behavior | Required Step 1 change |
| --- | --- | --- |
| [scripts/connectx_env.sh](../scripts/connectx_env.sh) | Defaults `DIYA_HOST`, `HOEDUS_HOST`, their `10.10.10.x` addresses, `HEAD_IP`, and one shared `CX_IFACE` list. Resource checks use `HOEDUS_IP`. | Load validated topology; derive head, worker, and local values by node ID. Keep policy variables such as the pinned image and NCCL mode separate. |
| [scripts/load_env.sh](../scripts/load_env.sh) | Sources `.env` as shell code and preserves exported environment overrides. | Allow `.env` to select the topology file and local node ID. Reject conflicting legacy topology overrides after loading. |
| [scripts/start_ray.sh](../scripts/start_ray.sh) | `head` uses `HEAD_IP`; `worker` uses `HOEDUS_IP`; both containers receive the head's interface list if configured identically. | Check requested role against YAML; use the selected node's rendezvous IP and interface list; identify the loaded topology in each container. |
| [scripts/verify_connectx.sh](../scripts/verify_connectx.sh) | Assumes Diya/Hoedus and calculates each peer address by adding or subtracting one from the last octet. | Read the peer's declared address for each rail; verify local address, route, source, bound ping, and RDMA mapping per configured interface. |
| [scripts/launch_server.sh](../scripts/launch_server.sh), [scripts/mirror_worker_cache.sh](../scripts/mirror_worker_cache.sh), [scripts/purge_model_cache.sh](../scripts/purge_model_cache.sh), [scripts/resource_manager.py](../scripts/resource_manager.py) | Pin Ray work to the head and `HOEDUS_IP`; expect exactly two Ray nodes. | Use derived head and worker addresses; retain the two-node Ray membership check. |
| [scripts/ray_allreduce_bench.py](../scripts/ray_allreduce_bench.py), [scripts/ray_interconnect_bench.py](../scripts/ray_interconnect_bench.py) | Pass one interface list from the head to both Ray tasks. The all-reduce task overwrites each task's socket interface environment. | Give each task its own node's interface list; keep rank 0 on the head and rank 1 on the worker. |
| [scripts/cluster_runtime_fingerprint.py](../scripts/cluster_runtime_fingerprint.py) | Collects two nodes and requires equal runtime environment values, including socket interface lists. | Compare shared software and transport policy across nodes; verify each node's network settings against its own YAML entry. |
| [scripts/run_job.sh](../scripts/run_job.sh), [scripts/run_models.sh](../scripts/run_models.sh), [scripts/run_all.sh](../scripts/run_all.sh) | Put head IP, `HOEDUS_IP`, and a single `CX_IFACE` into campaign, canary, and serving identities. The dual-node power sampler targets `HOEDUS_IP`. | Derive values from topology and include the complete canonical topology identity in new campaign and serving fingerprints. |
| [scripts/system_snapshot.py](../scripts/system_snapshot.py), [scripts/preflight.sh](../scripts/preflight.sh) | Capture a local head and an SSH worker named by command arguments. | Record node IDs, roles, declared interfaces and addresses, and observed state for each node. Use the configured SSH target for the remote snapshot. |
| [bench/release_contracts.py](../bench/release_contracts.py), [scripts/validate_campaign.py](../scripts/validate_campaign.py) | Verify the current serving-policy serialization and require two power nodes for `tp2`/`pp2`. | Recognize a new topology-aware policy schema while still reading historical results. Keep the two-node power coverage rule for Step 1. |
| [docs/cluster-setup.md](cluster-setup.md), [docs/new-spark-setup.md](new-spark-setup.md), [docs/hoedus-head.md](hoedus-head.md), [docs/campaign-runbook.md](campaign-runbook.md), [README.md](../README.md) | Give Diya/Hoedus commands and fixed Netplan or `/etc/hosts` examples. | Document the YAML selection, host setup requirements, role swap, and topology-specific campaign outputs. |
| [tests/test_rdma_readiness.py](../tests/test_rdma_readiness.py), [tests/test_run_all_failure_isolation.py](../tests/test_run_all_failure_isolation.py) | Mock the current fixed addresses, hostname lookup, and interface list. | Use synthetic two-node topology fixtures with independent local interface names and addresses. |

`single`, `tp2`, and `pp2` remain the existing serving labels. `tp2` and `pp2` still mean two GPUs on two nodes in Step 1. The existing two-sampler energy fields and plots remain valid for those labels. An arbitrary-node energy schema belongs to Step 2.

## Configuration contract

Proposed file: `configs/cluster.yaml`. The following addresses are documentation examples from reserved address ranges. They are not measured or deployed settings.

```yaml
schema_version: 1
head_node: spark_a
rendezvous_rail: rail_0
ray_port: 6379
nodes:
  - id: spark_a
    hostname: lab-alpha
    ssh_target: lab-alpha
    interfaces:
      - rail: rail_0
        name: enp1s0f0np0
        ipv4: 192.0.2.1/30
      - rail: rail_1
        name: enp1s0f1np1
        ipv4: 198.51.100.1/30
  - id: spark_b
    hostname: lab-beta
    ssh_target: lab-beta
    interfaces:
      - rail: rail_0
        name: cx_primary
        ipv4: 192.0.2.2/30
      - rail: rail_1
        name: cx_secondary
        ipv4: 198.51.100.2/30
```

| Field | Meaning |
| --- | --- |
| `schema_version` | Selects the parser and canonical serialization contract. Step 1 accepts only `1`. |
| `head_node` | Stable node ID assigned the Ray head and benchmark client roles. The other ID is the worker. |
| `rendezvous_rail` | Rail whose per-node IPv4 addresses are used for Ray, vLLM host identity, torch rendezvous, and Ray task placement. |
| `ray_port` | TCP port for the Ray head. |
| `nodes[].id` | Stable key for roles, peer references, and provenance. It is independent of the machine hostname. |
| `nodes[].hostname` | Expected local `hostname -s` value for identity verification and reporting. |
| `nodes[].ssh_target` | Address or DNS name used by the existing remote system snapshot. SSH trust and credentials remain outside this YAML. |
| `nodes[].interfaces[].rail` | Logical fabric rail ID. The two entries with the same rail ID form one Step 1 peer pair. |
| `nodes[].interfaces[].name` | Linux network interface name on that node. Names can differ between nodes. |
| `nodes[].interfaces[].ipv4` | Address and prefix configured on that interface. The host address supplies the peer probe and, on `rendezvous_rail`, the Ray node address. |

The example uses two rails. The loader must accept one or more rails, including the current four-rail layout. The schema does not encode an odd/even address convention, a fixed subnet, a fixed interface spelling, or a host-specific role. `ssh_target` can use a management address; Ray and NCCL still use the configured fabric addresses and interfaces.

NCCL transport settings remain experiment policy. `NCCL_IB_DISABLE`, `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`, and `NCCL_NET_PLUGIN` retain their existing meaning. A node-specific `NCCL_IB_HCA` value may differ across machines because RDMA device names may differ. The configuration loader must distinguish those local values from the shared transport mode and record their effective values per node.

The file contains no secrets. `HF_TOKEN`, cache paths, container image, and workload settings remain in their existing configuration channels. Existing [configs/models.yaml](../configs/models.yaml) and [configs/experiment.yaml](../configs/experiment.yaml) retain their current meanings.

## Loading and command interface

1. Each entrypoint that uses topology requires `CLUSTER_CONFIG=/path/to/cluster.yaml`. A missing path or file fails; the old Diya/Hoedus defaults are not a fallback. `.env` may set this path on each machine. An explicit environment value keeps the existing `load_env.sh` precedence over `.env`.
2. A new typed Python loader reads YAML with `yaml.safe_load`, validates the full cluster, and produces a canonical topology object. It selects the local node by matching `hostname -s` to exactly one configured hostname. The same loader serves a `validate` command and the shell entrypoints. The `PyYAML==6.0.3` dependency is already pinned in `requirements.lock`; the worker host must have an interpreter with that dependency before `start_ray.sh` can load YAML.
3. Shell entrypoints source `connectx_env.sh` as they do now. That file loads `.env`, invokes the Python loader once, checks its exit status, and assigns validated values without `eval` or sourcing generated shell text. A length-delimited or NUL-delimited transfer prevents whitespace and shell metacharacters from changing assignments. Loader failure stops the entrypoint before Docker, Ray, or a benchmark starts.
4. The loader derives `HEAD_HOST`, `HEAD_IP`, `WORKER_HOST`, `WORKER_IP`, `LOCAL_IP`, and the local `CX_IFACE` list. `VLLM_HOST_IP`, `NCCL_SOCKET_IFNAME`, and `GLOO_SOCKET_IFNAME` use the selected node's values. `DIYA_*` and `HOEDUS_*` cease to be topology variables in active entrypoints.
5. Topology values come only from the YAML. An exported `HEAD_IP`, `HOEDUS_IP`, `DIYA_IP`, `CX_IFACE`, or similar legacy topology override that conflicts with the document fails with a migration message. This prevents a changed YAML from being silently overridden by `.env` or a parent shell. The loader revalidates when a different `CLUSTER_CONFIG` is selected in the same shell.
6. `start_ray.sh head` is valid only on the automatically identified `head_node`; `start_ray.sh worker` is valid only on the other node. The existing role arguments remain for command compatibility. The head-only orchestrators check that the local node is `head_node` before starting work.

Example invocation after host networking is configured:

```bash
# On lab-alpha:
CLUSTER_CONFIG=/path/to/cluster.yaml scripts/start_ray.sh head

# On lab-beta, with the same YAML content:
CLUSTER_CONFIG=/path/to/cluster.yaml scripts/start_ray.sh worker

# On the selected head:
CLUSTER_CONFIG=/path/to/cluster.yaml PLAN_ONLY=1 scripts/run_job.sh
```

The two machines may store the YAML at different paths. The canonical content, excluding the local path, must hash identically. A head swap changes `head_node` in the shared YAML and swaps the `start_ray.sh` roles. The role-specific [scripts/hoedus-head-env.sh](../scripts/hoedus-head-env.sh) then has no role in the active workflow.

## Validation and failure behavior

**Static validation, before hardware access:** Require a mapping with known keys and `schema_version: 1`. Reject duplicate YAML keys, unsupported types, empty strings, unknown node IDs, duplicate node IDs, duplicate hostnames, duplicate interface names within a node, duplicate rail IDs within a node, duplicate IPv4 addresses, missing peer rails, missing `rendezvous_rail`, and ports outside 1 through 65535. Parse addresses with `ipaddress.ip_interface`. Require two distinct usable unicast IPv4 host addresses in the same network for each peer rail. Require distinct rail networks so a route probe identifies the configured rail. Validate node IDs, hostnames, SSH targets, and interface names for their uses as command arguments and Ray resource labels. Reject YAML with more or fewer than two nodes in Step 1.

**Local validation, before starting or replacing containers:** Identify exactly one local node by matching `hostname -s` to a YAML entry. Zero matches fail; static validation already rejects duplicate configured hostnames. For each interface, check that the declared address and prefix are assigned locally, the link is up, and the expected peer address routes through that interface with the declared source address. Probe the declared peer address with `ping -I <local-interface>` on each rail. When RDMA is requested, check that each selected interface has an active, link-up RDMA port. Report the node ID, rail, interface, expected value, and observed value on failure. The peer address comes from the YAML, never from address arithmetic. `start_ray.sh stop` may identify the local entry by hostname without requiring an active fabric link.

**Cluster validation, after Ray startup:** Require exactly the two declared live Ray GPU node addresses. Check that Ray reports the declared rendezvous IP for each node and that tasks pinned by `node:<ip>` execute on the intended machine. Compare a canonical topology digest reported by both containers. Check the shared image, Ray version, and transport policy across both nodes. Compare each node's effective socket interface names and RDMA selection to its own YAML entry. A mismatch blocks cache transfer, microbenchmarks, serving, and campaign measurement.

Host setup remains explicit. The selected interfaces must already have their configured IPs. The remote snapshot requires SSH access to `ssh_target` with the existing strict host-key behavior. The design does not infer or edit a management route from the fabric YAML.

## Changes by execution path

1. **Ray and serving:** `start_ray.sh` uses `LOCAL_IP` for `--node-ip-address` and `VLLM_HOST_IP`; the worker joins `HEAD_IP:RAY_PORT`. Distributed serving checks exactly the configured head and worker and their GPU resources. `launch_server.sh` uses those derived addresses for pinned cache and memory preflight. The single-node path uses the selected head's local interface settings.
2. **Cache and resource operations:** `mirror_worker_cache.py`, `purge_model_cache_container.py`, and `resource_manager.py` receive the configured worker IP. Their existing two-node membership and idle-resource checks remain. Cache transfers continue to use Ray, with no new SSH transfer path.
3. **Network verification and microbenchmarks:** `verify_connectx.sh` runs local checks on both machines. The head command can report both sides' declared topology, while each machine verifies its own interface state. Ray microbenchmark tasks receive the interface list for their assigned node; neither task overwrites its environment with the other node's list. Transport evidence remains from each task's NCCL log. Artifact fingerprints include both nodes' effective rail assignments and transport policy.
4. **Campaign and measurement:** `run_job.sh` and `run_models.sh` use the selected head and worker identities in plan, canary, launch, and resume paths. `run_all.sh` derives the worker power target from `WORKER_IP`. A dual-node measurement still requires one head and one worker sampler. `WORKER_POWER_NODE_IP` may remain a diagnostic override only when it equals the YAML worker IP; a conflicting value fails before measurement.
5. **Snapshots and policy:** `system_snapshot.py` records both configured node IDs, roles, interface assignments, topology digest, and observed host/container facts. `cluster_runtime_fingerprint.py` records one fact row per declared node. The serving policy records the canonical topology digest and the effective per-node network settings. Raw historical artifacts remain unchanged.

## Provenance and compatibility

Canonical topology serialization must sort mapping keys and node/rail entries by stable IDs. It must include schema version, head assignment, rendezvous rail, Ray port, node IDs, hostnames, SSH targets, interface names, and interface CIDRs. Hash the canonical bytes with SHA-256. Do not hash local file paths; they may differ between machines while describing the same cluster. Preserve the original YAML beside a campaign's inputs and record its digest in the campaign manifest and system snapshot. Record observed interface addresses, routes, Ray membership, software versions, and effective NCCL settings separately from the declared YAML.

`run_job.sh` currently requires a clean source tree under `configs` and the benchmark code directories. A modified tracked `configs/cluster.yaml` therefore needs a source revision before a paper campaign. An external YAML path can remain outside the Git tree; the campaign must copy its exact bytes and digest into its input record before measurement. Report comparisons across topology digests as different experimental conditions.

Create a new serving-policy schema and fingerprint format for topology-aware runs. Update `run_models.sh`, `bench/release_contracts.py`, and `scripts/validate_campaign.py` together so writer and validators hash the same fields. Retain a read path for historical `serving-v1` policies and result indexes. Do not rewrite old policy files, manifests, index rows, or measurements. Include the topology digest in implicit output-directory identity, canary identity, microbenchmark artifact identity, and resume checks for explicit `OUT_DIR`. A changed head, IP, rail assignment, or interface name invalidates reuse of a canary or completed point. A runtime mismatch fails before a new result row is written.

The two-node `power_nodes=2` release requirement and current `tp2`/`pp2` labels remain in effect. The YAML does not change workload buckets, seeds, model revisions, GPU utilization, serving parallelism, metric definitions, or measurement windows.

## Step 1 acceptance criteria

- A valid two-node YAML with hostnames, subnets, and interface names unrelated to Diya/Hoedus automatically identifies the local node and produces the declared head, worker, and local values on each machine. Swapping `head_node` swaps the operational roles without renaming either physical node.
- Each rail probe targets the peer address declared for that rail. A wrong local address, route, source address, link state, or requested RDMA mapping fails before a serving or benchmark action.
- Ray startup refuses a role that conflicts with the identified local node ID. Distributed commands refuse missing, extra, or misidentified Ray GPU nodes and refuse a topology digest mismatch between containers.
- Cache mirroring, cache cleanup, resource checks, distributed launch, and both microbenchmarks target the configured worker. NCCL and Gloo use each task's local interface names when the two machines have different names.
- A `single`, `tp2`, and `pp2` targeted canary can run from either selected head after host setup. A required dual-node measurement records one power sampler per configured node.
- A topology edit changes campaign, canary, serving, and microbenchmark identities. Resume under an old identity fails before writing a result. Historical result files remain readable without mutation.
- A YAML with three nodes fails static validation with a two-node scope error. No Step 1 command silently ignores the third node.

## Implementation sequence

1. Add the versioned YAML schema, typed loader, canonical serializer, and example cluster file. Add parser tests for automatic local-node identification, valid asymmetric interface names, a head swap, malformed data, and a node count other than two.
2. Integrate the loader into `load_env.sh`/`connectx_env.sh`. Replace active `DIYA_*`/`HOEDUS_*` consumers with role-derived and local values. Add entrypoint tests that confirm wrong-node and conflicting-override failures before any state-changing command.
3. Update `start_ray.sh` and `verify_connectx.sh`; add mock-based tests for address, route, source, per-rail ping, RDMA mapping, and opposite head assignments.
4. Update Ray membership, cache, resource, launch, and microbenchmark paths. Test different interface names on the two nodes and reject an extra or missing Ray GPU node.
5. Update campaign identity, runtime fingerprint, serving-policy serialization, snapshots, and validators as one change. Test that a topology change invalidates canary/resume state and that historical policy fixtures still validate.
6. Update setup, role-swap, and campaign documentation. Replace machine-specific commands in the primary workflow with the two-node YAML selection. Keep the existing example topology labeled as an example.

Static checks for the implementation include the repository unit tests, `bash -n` on changed shell scripts, Python compilation, and a `PLAN_ONLY=1` run with a synthetic topology. These checks must not be reported as hardware measurements. An authorized hardware acceptance run would verify both head assignments, per-rail routes, the two Ray GPU nodes, cache mirroring, a targeted `tp2` and `pp2` canary, both power samplers, and NCCL transport logs. A full benchmark campaign is not required to establish topology configurability.

## Step 2 boundary

Stable node IDs, a node list, rail IDs, and a canonical topology digest carry forward. Step 2 must define switched-fabric membership and reachability, multi-node Ray placement, model distribution to every worker, multi-node power and temperature schemas, parallelism labels, and collective benchmark world sizes. Step 1 implements none of those multi-node policies. The explicit two-node validation prevents a larger YAML from entering code paths that still assume one worker or `power_nodes=2`.

## Assumptions and open decisions

- Each Spark has one Ray-visible GPU, matching the current runtime checks.
- The two Sparks have at least one common IPv4 fabric rail. Each shared rail uses one subnet with one configured address per Spark.
- The benchmark client and campaign orchestrator run on the selected Ray head, matching the current local power and cache behavior.
- `ssh_target` is reachable from the selected head for system snapshots. SSH availability does not gate all other runtime paths today; the implementation should preserve the snapshot's explicit unavailable status when SSH fails.
- The worker has a Python interpreter with the pinned YAML parser before Ray startup. The host setup guide must state how to provide it without downloading dependencies during a campaign.
- The topology file is passed to both machines through `CLUSTER_CONFIG`. Local node identification requires the system short hostname to match one YAML entry. Startup separately verifies the declared rendezvous address and every configured fabric interface.
