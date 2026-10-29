# Select either Spark as the Ray head

The `head_node` field in `CLUSTER_CONFIG` assigns the Ray head and campaign
client roles. The other configured node is the worker. Node IDs and hostnames
remain stable when the head changes.

Set `head_node` to the selected node ID in the site-specific YAML. Use a file
with the same canonical topology digest on both systems. Set `CLUSTER_CONFIG`
to the local path in each checkout or export it per command.

On the system whose `hostname -s` matches the selected head, run:

```bash
CLUSTER_CONFIG=/path/to/site/cluster.yaml scripts/start_ray.sh head
```

On the other system, run:

```bash
CLUSTER_CONFIG=/path/to/site/cluster.yaml scripts/start_ray.sh worker
```

Run the plan or campaign on the selected head:

```bash
CLUSTER_CONFIG=/path/to/site/cluster.yaml PLAN_ONLY=1 scripts/run_job.sh
CLUSTER_CONFIG=/path/to/site/cluster.yaml scripts/run_job.sh
```

The plan validates files and options without checking host links or starting
hardware work. A topology change produces a new canonical digest and campaign
identity. Use a fresh `OUT_DIR` after changing `head_node`, a node address, a
rail, or an interface name.

`scripts/hoedus-head-env.sh` contains a historical fixed-role environment.
Active entrypoints reject its `DIYA_*` and `HOEDUS_*` overrides. Use
`CLUSTER_CONFIG` for current commands.
