# Add a DGX Spark to the benchmark cluster

Step 1 supports exactly two DGX Spark GB10 systems. `configs/cluster.example.yaml`
defines the YAML format. Its hostnames and IPv4 addresses are documentation
examples and require replacement before use.

## Configure the host

The sysadmin completes the [administrator prerequisites](cluster-setup.md#administrator-prerequisites).
Repository environment setup and experimental runs use the benchmark account.

1. Complete firmware, operating system, cabling, and fabric setup using the
   NVIDIA [Connect Two Sparks playbook](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-two-sparks).
2. Assign each fabric interface the IPv4 address and prefix listed for that
   node and rail in the site-specific cluster YAML.
3. Set the local short hostname so `hostname -s` matches the node's configured
   `hostname` value.
4. Configure `ssh_target` for head-to-worker SSH access with the existing
   strict host-key policy. SSH is used for the read-only worker system
   snapshot.
5. Install an interpreter with the PyYAML version in `requirements.lock` on
   both hosts. Configure `TOPOLOGY_PYTHON` when the interpreter is not
   `python3`.

The benchmark does not write Netplan or `/etc/hosts` files. The host must have
the configured addresses, routes, and link state before a benchmark command
starts.

## Add the node to the two-node YAML

Each system has a stable ID, short hostname, SSH target, and one interface per
shared rail. Interface names can differ between systems. Every peer pair must
use distinct host addresses in the same IPv4 subnet. Rail subnets must not
overlap.

Topology entrypoints default `CLUSTER_CONFIG` to
`<repo-root>/configs/cluster.yaml` on each host. Set it only when using a
different local path; the canonical topology digest must match:

```bash
python3 scripts/cluster_topology.py validate configs/cluster.yaml
scripts/verify_connectx.sh
```

Run `scripts/verify_connectx.sh` on each host. It checks the local addresses,
routes, route sources, link states, peer probes, and active RDMA mapping when
`NCCL_IB_DISABLE=0`.

## Assign head and worker roles

The YAML `head_node` value selects the Ray head and campaign client. Start Ray
on the two matching hosts:

```bash
# On the host whose hostname matches head_node:
scripts/start_ray.sh head

# On the other host:
scripts/start_ray.sh worker
```

Changing `head_node` swaps the operational roles and changes the canonical
topology digest. Use a new campaign output path after a topology change.

## Check the cluster

Run the following commands on the selected head after Ray starts on both
hosts:

```bash
docker exec vllm-spark ray status
PLAN_ONLY=1 scripts/run_job.sh
```

`PLAN_ONLY=1` checks campaign configuration without link probes, container
changes, or GPU work. Follow the [cluster setup guide](cluster-setup.md) for
software setup, cache requirements, hardware checks, and campaign commands.
