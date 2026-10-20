# Cluster setup and pre-flight

Two DGX Sparks: **Diya** (head, `10.10.10.1`) and **Hoedus** (worker,
`10.10.10.2`), joined by two QSFP cables, which give four ConnectX-7 interfaces
per node. The campaign requests NCCL over RDMA (RoCEv2) with all four interfaces
available. Confirm the transport in the NCCL logs and the devices actually used
before claiming four-interface striping. Run the steps in order; each must pass
before the next. See [RDMA readiness](rdma-readiness.md) for the software checks,
remaining hardware gates, and paper reporting limits.

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
10.10.10.1  Diya
10.10.10.2  Hoedus
```

Keep these hostnames bound only to the first subnet so rendezvous addresses are
unambiguous. Use IP addresses for the other subnets; remove older entries that
repeat `Diya` or `Hoedus` on them. Ray and the API server use the first subnet;
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

## 6. Client environment (Diya)

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
PYTHONNOUSERSITE=1 .venv/bin/python scripts/check_environment.py
```

Put `HF_TOKEN` (with the Llama 3.1 license accepted) in Diya's repo-local
`.env`; set its permissions to `600`. The campaign downloads one model at a
time to Diya and mirrors dual-node weights to Hoedus over Ray-managed TCP.
Each node has a separate Hugging Face cache. `HF_HUB_DISABLE_SHARED_BLOBS=1`
keeps new downloads in repository-local cache blobs. The largest snapshot is
about 163 GB, so keep about 200 GB free in each cache. Set `HF_CACHE` in each
node's `.env` to use a larger drive.

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

`start_ray.sh` installs Ray `RAY_VERSION` (2.56.1) into both containers with
the exact dependency constraints in `scripts/ray-runtime-constraints.txt`. Both
nodes need PyPI access at start. Update that file when changing `RAY_VERSION`.

## 8. RDMA gates (Diya)

```bash
OUT_DIR=results scripts/ray_allreduce_bench.sh        # fails below 16 GB/s at 1 GiB
OUT_DIR=results scripts/interconnect_extra_bench.sh   # all-reduce + send/recv, transport from NCCL's own log
```

`run_job.sh` runs both itself; running them here catches a link problem
before anything else. TCP sockets plateau near 2 GB/s and the four-port RDMA
pilot reached 23.6 GB/s, so the 16 GB/s floor separates them cleanly.

## 9. Hardware smoke test (Diya)

```bash
scripts/hardware_smoke.sh
SMOKE_LABEL=nemotron3.5-lightning-30b-a3b-nvfp4 SMOKE_GPU_UTIL=0.80 \
  SMOKE_CONCURRENCIES="1 128" OUT_DIR=results-smoke-lightning \
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

## 10. Plan, canary pass, campaign (Diya, inside tmux)

See the step-by-step [full campaign runbook](campaign-runbook.md) for software
checks, the one-model-at-a-time cache flow, canary preflight, and resume
commands.

```bash
PLAN_ONLY=1 scripts/run_job.sh           # hardware-free: matrix, windows, hours
STOP_AFTER_CANARY=1 scripts/run_job.sh   # every model x config launches once
scripts/run_job.sh                       # the campaign; rerun to resume
```

The canary pass lists every failure at the end; fix them, commit, and rerun
it. A passed canary is remembered in `results/.canary_passed`.

## TCP ablation (after the campaign)

Restart Ray on both nodes with `NCCL_IB_DISABLE=1`, then run the subset in
the top-level README (section "Transport evidence and the TCP ablation")
into `results-tcp-ablation`. `plotting/plot_interconnect.py results
results-tcp-ablation --out results/figs` overlays both transports.

## Rollback

```bash
sudo rm /etc/netplan/40-cx7.yaml && sudo netplan apply   # both nodes
scripts/start_ray.sh stop                                # both nodes
```
