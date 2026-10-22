# Cluster setup and pre-flight

Two DGX Spark GB10 systems: **Diya** (`cisc-diya`, head, `10.10.10.1`) and
**Hoedus** (`cisc-hoedus`, worker, `10.10.10.2`). Two QSFP cables connect the
systems and provide four ConnectX-7 interfaces per node. The campaign requests
NCCL over RDMA (RoCEv2) with all four interfaces available. Check NCCL logs
and device use before claiming four-interface striping.
Complete required setup and campaign gates before measurement. Optional
diagnostics are labeled. See [RDMA readiness](rdma-readiness.md) for software
checks, hardware gates, and paper reporting limits.

## Command scope by node

Run host configuration and interface checks on both nodes. Run the client,
readiness, benchmark, and campaign commands from Diya unless a row names a
different node. Commands launched on Diya can use Ray to run work on Hoedus.

| Task | Run command on | Nodes involved |
| --- | --- | --- |
| Firmware, Netplan, `/etc/hosts`, interface state, and `ibdev2netdev` checks | Both nodes | The local node only for each invocation |
| `scripts/verify_connectx.sh` | Diya only | Checks Diya's ConnectX routes and probes each Hoedus peer interface |
| Perftest suite | Diya client and Hoedus host | Both nodes, with separate client and server commands |
| Python environment, software checks, and `PLAN_ONLY=1` | Diya only | Local files and environment; no cluster work |
| `scripts/start_ray.sh head` / `worker` | Diya / Hoedus, respectively | Starts one Ray node on each machine |
| `ray status`, RDMA microbenchmarks, canary, and campaign | Diya only | Ray distributes two-node benchmarks and dual-node serving to Hoedus |
| `scripts/hardware_smoke.sh` | Diya only | Diya GPU only; it does not require Hoedus |
| `scripts/predownload.sh` | Diya only | Acquires pinned snapshots for dual-node serving or offline cache warming |
| `scripts/purge_model_cache.sh` | Diya only | Purges Diya and, when Ray is active, Hoedus caches |
| `scripts/start_ray.sh stop` | Both nodes | Stops the local Ray container on each machine |

## 0. Disable the DGX Dashboard update notifier

This removes the automatic update notification entry. Check for system updates
periodically at `localhost:11000`.

```bash
yes | sudo rm /etc/xdg/autostart/dgx-dashboard-notify.desktop
```

## 1. Firmware and system software (both nodes)

```bash
fwupdmgr get-upgrades && fwupdmgr update     # ConnectX and platform firmware
```

Apply system updates from the DGX Dashboard (`localhost:11000/updates`). Use
the same username on both nodes. NVIDIA's
[connect-two-sparks playbook](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-two-sparks)
covers the cabling.

## 2. Netplan (both nodes)

`/etc/netplan/40-cx7.yaml` on Diya (on Hoedus, replace each `.1/24` with `.2/24`):

```yaml
network:
  version: 2
  ethernets:
    enp1s0f0np0:    {addresses: [10.10.10.1/24], dhcp4: no, mtu: 4096}
    enP2p1s0f0np0:  {addresses: [10.10.11.1/24], dhcp4: no, mtu: 4096}
    enp1s0f1np1:    {addresses: [10.10.12.1/24], dhcp4: no, mtu: 4096}
    enP2p1s0f1np1:  {addresses: [10.10.13.1/24], dhcp4: no, mtu: 4096}
```

```bash
sudo chmod 600 /etc/netplan/40-cx7.yaml
sudo netplan apply
```

## 3. `/etc/hosts` (both nodes)

```
10.10.10.1  cisc-diya
10.10.10.2  cisc-hoedus
```

Keep these rendezvous names bound only to the first subnet so addresses are
unambiguous. Keep the system's local loopback entry. The verifier ignores
loopback addresses for Diya's local name and rejects additional non-loopback
addresses. Use IP addresses for the other subnets; remove entries that repeat
`cisc-diya` or `cisc-hoedus` on them. Ray and the API server use the first subnet;
all four interfaces are available to NCCL. The interface/RoCE correspondence
is documented in NVIDIA's
[ConnectX-7 networking guide](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html).

## 4. Links and RDMA devices

```bash
for iface in enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1; do
  ip -br link show dev "$iface"                  # all UP, both nodes
done
ibdev2netdev                                    # four RoCE devices, both nodes
scripts/verify_connectx.sh                      # on Diya: addresses, routes, per-interface ping
```

If NCCL later uses fewer than four devices, set
`NCCL_IB_HCA="rocep1s0f0,roceP2p1s0f0,rocep1s0f1,roceP2p1s0f1"` (names from
`ls /sys/class/infiniband/`).

## 5. Verbs-level bandwidth (optional, recommended once per cabling change)

Run the per-device perftest matrix from the separate
[perftest-suite-dgx-spark-cluster](https://github.com/dmihovch/perftest-suite-dgx-spark-cluster)
repository (host on Hoedus, client on Diya, `--modes isolation,concurrent`).
Record isolation and concurrent bandwidth per device, the aggregate bandwidth,
and `ib_write_lat`. Compare them with a healthy baseline for the same cabling,
MTU, firmware, and transfer sizes. The campaign's 16 GB/s all-reduce gate is an
empirical threshold for this cluster; it does not establish a theoretical link
capacity or prove that every rail carries traffic.

## 6. Client environment (Diya only)

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
PYTHONNOUSERSITE=1 .venv/bin/python scripts/check_environment.py
```

Put `HF_TOKEN` (with the Llama 3.1 license accepted) in Diya's repo-local
`.env`; set its permissions to `600`. Single-node launches can acquire pinned
revisions through vLLM. Dual-node serving acquires snapshots on Diya and mirrors
them to Hoedus over Ray-managed TCP. Each node has a separate host-user-writable
Hugging Face cache. The cache defaults to `$HOME/.cache/huggingface`; scripts
derive `HF_HUB_CACHE=$HF_CACHE/hub` and mount it at `/hf-cache` in containers.
`HF_HUB_DISABLE_SHARED_BLOBS=1` keeps new downloads in repository-local cache
blobs. The largest snapshot is about 163 GB, so keep about 200 GB free in each
cache. The cache directory must be writable by the account running the scripts.
If the default directory is owned by another account or has insufficient space,
set `HF_CACHE` in the repo-local `.env` on each node to a user-owned path with
about 200 GB free. For a home filesystem with enough space, use:

```bash
export HF_CACHE="$HOME/.cache/huggingface-$UID"
```

The host and pinned image check cache create/read/delete access before use. A
failure reports the effective UID:GID and, when available, the cache directory
owner and mode. The scripts do not change ownership. Cache setup and regular
benchmark commands do not require sudo. `DROP_CACHES=1` requires sudo and is
optional.

The campaign GPU-memory target defaults to `0.80` in
`configs/models.yaml`. Set `GPU_UTIL` on Diya to override the target for plan
validation, memory preflight, and every serving launch. Keep the same value
when resuming a campaign.

## 7. Ray cluster

`scripts/connectx_env.sh` defaults to this layout: all four interfaces,
`NCCL_IB_DISABLE=0`, and `NCCL_DEBUG=INFO` with `NCCL_DEBUG_SUBSYS=INIT,NET`
so every launch logs the transport its NCCL channels used. Nothing needs to be
sourced.

```bash
scripts/start_ray.sh head          # Diya
scripts/start_ray.sh worker        # Hoedus
docker exec vllm-spark ray status  # Diya: two nodes Active
```

Omit `DROP_CACHES=1` when the account does not have sudo access. Ray startup
does not need a page-cache flush.

`start_ray.sh` installs Ray `RAY_VERSION` (2.56.1) into both containers with
the exact dependency constraints in `scripts/ray-runtime-constraints.txt`. Both
nodes need PyPI access at start. Update that file when changing `RAY_VERSION`.

## 8. Optional RDMA diagnostics (launch on Diya, use both nodes)

```bash
export OUT_DIR=results/rdma-v4-paper-2026-10-22
scripts/ray_allreduce_bench.sh        # fails below 16 GB/s at 1 GiB
scripts/interconnect_extra_bench.sh   # all-reduce + send/recv, transport from NCCL's own log
```

These commands provide diagnostics before the campaign. `run_job.sh` always
runs a fresh all-reduce gate before the canary and before each campaign or
resume invocation. A manual all-reduce run therefore does not replace the
campaign gate. During `run_job.sh`, a send/recv failure is logged and does not
block later campaign work. The campaign can reuse a valid send/recv result that
matches the current runtime and source. TCP sockets plateau near 2 GB/s and the
four-port RDMA pilot reached 23.6 GB/s, so the 16 GB/s floor separates them
cleanly.

## 9. Hardware smoke test (Diya only, single node)

```bash
scripts/hardware_smoke.sh
SMOKE_LABEL=nemotron3.5-lightning-30b-a3b-nvfp4 SMOKE_GPU_UTIL=0.80 \
  SMOKE_CONCURRENCIES="1 128" OUT_DIR=results/smoke-lightning-2026-10-22 \
  scripts/hardware_smoke.sh
```

About 15 minutes, single node, its own container, port, and output directory:
it fetches a small model, runs the canary, measures two replicates on fresh
launches with the release gates on, then writes figures and replicate
statistics and purges the weights. It proves the image, harness, power and
metrics sampling, and plotting work together before the long run. The second
command runs the Lightning checkpoint at the campaign's top load, since the
canary cannot catch a failure that appears only under load (see
[RDMA readiness](rdma-readiness.md)).

## 10. Plan, canary pass, campaign (launch on Diya; campaign uses both nodes)

See the step-by-step [full campaign runbook](campaign-runbook.md) for software
checks, the one-model-at-a-time cache flow, canary preflight, and resume
commands.

```bash
export OUT_DIR=results/rdma-v4-paper-2026-10-22
PLAN_ONLY=1 scripts/run_job.sh
CANARY_FIRST=1 STOP_AFTER_CANARY=1 scripts/run_job.sh
scripts/run_job.sh
```

The plan validates the matrix and reports windows and estimated measurement
time without hardware operations. The canary launches every model/configuration
pair once and stops before measurements. It collects recoverable pair failures
and reports them; a failed canary does not proceed to measurements by default.
Fix failures, commit the changes, and rerun the canary. A passing canary is
remembered in `$OUT_DIR/.canary_passed` when the campaign identity matches,
including `GPU_UTIL`. Keep `OUT_DIR` and `GPU_UTIL` unchanged when resuming.

## TCP ablation (after the campaign)

Restart Ray on both nodes with `NCCL_IB_DISABLE=1`, then run the subset in
the top-level README (section "Transport evidence and the TCP ablation")
into `results/tcp-ablation-2026-10-22`. `plotting/plot_interconnect.py
results/rdma-v4-paper-2026-10-22 results/tcp-ablation-2026-10-22` overlays
both transports and writes the figure under the first campaign's `figs/stats/`.

## Rollback

```bash
sudo rm /etc/netplan/40-cx7.yaml && sudo netplan apply   # both nodes
scripts/start_ray.sh stop                                # both nodes
```
