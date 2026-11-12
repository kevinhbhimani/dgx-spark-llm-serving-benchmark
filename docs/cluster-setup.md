# Two-Spark cluster setup

The benchmark uses two DGX Spark GB10 systems. The default topology file is
`configs/cluster.yaml`. `configs/cluster.example.yaml` shows the topology
schema with documentation-only hostnames and reserved example addresses. Set
`CLUSTER_CONFIG` to select another topology file. The selected head and worker
roles come from `head_node` in the selected file.

## Topology file

The file has exactly two nodes. Each node has a stable ID, a short hostname,
an SSH target, and one interface entry for every shared rail. `hostname -s`
must match the configured hostname on each system. Interface names may differ
between systems. The two addresses on each rail must use the same IPv4 subnet.

Topology entrypoints default `CLUSTER_CONFIG` to
`<repo-root>/configs/cluster.yaml`. Set it on both systems only when the
topology file has a different path. The files may use different local paths;
their canonical topology digests must match. Put an override in each
checkout's `.env` or export it for each command:

```bash
export CLUSTER_CONFIG=/path/to/site/cluster.yaml
```

Validate the YAML on each system:

```bash
python3 scripts/cluster_topology.py validate configs/cluster.yaml
```

The loader checks two-node scope, node and rail IDs, hostnames, SSH targets,
interface names, address and prefix pairs, duplicate YAML keys, and the Ray
port. It prints the canonical topology digest. The digest excludes the local
file path.

The loader does not configure Netplan, DNS, routes, SSH, or firmware. Configure
the declared IPv4 addresses and prefixes on the listed interfaces before
starting Ray. Keep the interfaces up and route each declared peer address
through its paired interface. `ssh_target` must be reachable from the selected
head for the read-only worker snapshot. SSH uses strict host-key checking.

## Administrator prerequisites

The sysadmin provisions each host before the benchmark account uses this
repository:

- Install the GPU driver, Docker Engine, and NVIDIA Container Toolkit. Configure
  GPU access through Docker for the benchmark account.
- Grant that account access to the host Docker daemon, including container
  creation with host networking, host IPC, `IPC_LOCK`, unlimited memlock, and
  `/dev/infiniband` devices for RDMA. Group-based Docker access grants control
  of a privileged daemon. Sudo access for the account is not required.
- Configure firmware, RDMA, interfaces, routes, hostnames, and SSH service.
- Install Python 3.12 with venv support, Git, and the host tools used by the
  fabric checks (`ip`, `ping`, `rdma`, and `ibdev2netdev`).
- Provide user-writable checkout, cache, temporary, and output directories on
  each node. Existing files owned by another account require an administrator
  to correct permissions or a new user-owned location.
- For postmortem diagnostics, grant system journal read access if site policy
  permits it. Restricted journals and pstore require administrator evidence
  collection; the script records available evidence as the invoking account.

Log in again after group changes. Run `docker info` as the benchmark account
to check daemon access. The repository does not invoke sudo or reconfigure the
host during experimental runs. Driver recovery, kernel module reloads, and
reboots remain administrator tasks. Rootless Docker support is unverified.

## Host and software requirements

Apply firmware and system updates using the DGX Dashboard and NVIDIA's
[Connect Two Sparks playbook](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-two-sparks).
The benchmark does not change host network configuration.

Both hosts need an interpreter with the PyYAML version in `requirements.lock`
before `scripts/start_ray.sh` runs. Set `TOPOLOGY_PYTHON` when that interpreter
is not `python3`:

```bash
export TOPOLOGY_PYTHON=/path/to/python-with-pyyaml
```

The selected head also needs the locked benchmark client environment:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
PYTHONNOUSERSITE=1 .venv/bin/python scripts/check_environment.py
```

Each system has its own Hugging Face cache. The default is
`$HOME/.cache/huggingface-$UID`. Set `HF_CACHE` in each system's `.env` when the
default path lacks space or is not writable. The campaign acquires pinned
snapshots on the head and mirrors dual-node model files to the worker over Ray.
Only the selected head needs `HF_TOKEN` for gated model downloads.

## Verify the local fabric

Run the verifier on both systems. It checks each configured local address and
prefix, link state, route interface, route source address, peer reachability,
and requested RDMA mapping. `NCCL_IB_DISABLE=1` skips the RDMA port check and
retains the address, route, link, and peer checks.

```bash
scripts/verify_connectx.sh
```

`NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`, and `NCCL_NET_PLUGIN` remain per-host
environment settings. `NCCL_IB_HCA` accepts device names with an optional port,
such as `mlx5_0:1,mlx5_1:1`. Each selected device must map to a configured
interface with an active RDMA link, and the allowlist must cover every
configured interface. The runtime fingerprint records effective settings
from both containers.

Optional verbs-level tests can use the separate
[perftest-suite-dgx-spark-cluster](https://github.com/dmihovch/perftest-suite-dgx-spark-cluster)
repository. Record the host, client, rail, transfer size, and command with
those results.

## Start Ray

Run the head command on the system whose hostname matches `head_node`. Run the
worker command on the other system. Both systems must use matching topology
digests and a matching repository revision.

```bash
# On the configured head:
scripts/start_ray.sh head

# On the configured worker:
scripts/start_ray.sh worker
```

The worker joins `HEAD_IP:RAY_PORT` using the rendezvous rail address from the
YAML. `scripts/start_ray.sh stop` identifies the local node by hostname and
does not require an active fabric link.

Check Ray on the head:

```bash
docker exec vllm-spark ray status
```

Distributed serving, cache mirroring, and microbenchmarks verify the two Ray
GPU nodes, topology digest, per-node socket interfaces, and shared runtime
settings before use.

## Run a hardware-free plan

Run the plan on the configured head after installing the client environment:

```bash
PLAN_ONLY=1 scripts/run_job.sh
```

`PLAN_ONLY=1` validates campaign inputs without checking links, starting
containers, or running GPU work. A successful plan does not establish network
or Ray readiness.

## Run readiness and campaigns

Run local hardware checks on both hosts. Run client, canary, campaign, and
benchmark commands on the selected head:

```bash
scripts/hardware_smoke.sh
scripts/ray_allreduce_bench.sh
scripts/interconnect_extra_bench.sh
scripts/run_job.sh
```

The hardware smoke uses the selected head's GPU. The Ray microbenchmarks use
both Ray nodes. `run_job.sh` records the YAML bytes and both topology digests
under `OUT_DIR/results/inputs/`. A topology edit changes the campaign identity.
Use a fresh `OUT_DIR` after changing the selected head or any topology field.

See the [campaign runbook](campaign-runbook.md) for cache flow, canaries,
resume behavior, and campaign verification.
