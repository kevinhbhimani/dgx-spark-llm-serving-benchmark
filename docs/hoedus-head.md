# Select either Spark as the Ray head

The `head_node` field in `CLUSTER_CONFIG` assigns the Ray head and campaign
client roles. The other configured node is the worker. Node IDs and hostnames
remain stable when the head changes.

Set `head_node` to the selected node ID in the topology YAML. Use a file with
the same canonical topology digest on both systems. Topology entrypoints use
`<repo-root>/configs/cluster.yaml` by default; set `CLUSTER_CONFIG` to a local
override when using a different path.

On the system whose `hostname -s` matches the selected head, run:

```bash
scripts/start_ray.sh head
```

On the other system, run:

```bash
scripts/start_ray.sh worker
```

Run the plan or campaign on the selected head:

```bash
PLAN_ONLY=1 scripts/run_job.sh
scripts/run_job.sh
```

The plan validates files and options without checking host links or starting
hardware work. A topology change produces a new canonical digest and campaign
identity. Use a fresh `OUT_DIR` after changing `head_node`, a node address, a
rail, or an interface name.

`scripts/hoedus-head-env.sh` contains a historical fixed-role environment.
Active entrypoints reject its `DIYA_*` and `HOEDUS_*` overrides. Use
`CLUSTER_CONFIG` for current commands.
