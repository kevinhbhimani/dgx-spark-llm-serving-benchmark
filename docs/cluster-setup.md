# Cluster setup and pre-flight

Two DGX Sparks: **Diya** (head, `10.10.10.1`) and **Hoedus** (worker,
`10.10.10.2`), joined by two QSFP cables, which give four ConnectX-7 interfaces
per node. Every dual-node measurement uses NCCL over RDMA (RoCEv2) across all
four. Run the steps in order; each must pass before the next.

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
10.10.11.1  Diya
10.10.11.2  Hoedus
10.10.12.1  Diya
10.10.12.2  Hoedus
10.10.13.1  Diya
10.10.13.2  Hoedus
```

Ray and the API server use the first subnet; NCCL stripes across all four.

## 4. Links and RDMA devices

```bash
ip -br link show enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1   # all UP, both nodes
ibdev2netdev                                                            # four RoCE devices, both nodes
for s in 10 11 12 13; do ping -c 3 10.10.$s.2; done                     # from Diya, < 2 ms
```

If NCCL later uses fewer than four devices, set
`NCCL_IB_HCA="rocep1s0f0,roceP2p1s0f0,rocep1s0f1,roceP2p1s0f1"` (names from
`ls /sys/class/infiniband/`).

## 5. Verbs-level bandwidth (optional, recommended once per cabling change)

Run the per-device perftest matrix from the separate
[perftest-suite-dgx-spark-cluster](https://github.com/dmihovch/perftest-suite-dgx-spark-cluster)
repository (host on Hoedus, client on Diya, `--modes isolation,concurrent`).
Each device's isolation bandwidth should be a stable fraction of its link
rate, the concurrent aggregate should sit at the fabric cap (200 Gb/s
unidirectional per node), and `ib_write_lat` should be under 2 µs.

## 6. Client environment (Diya)

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
PYTHONNOUSERSITE=1 .venv/bin/python scripts/check_environment.py
```

Put `HF_TOKEN` (with the Llama 3.1 license accepted) in a repo-local `.env`.
The campaign keeps one model at a time in each node's cache (largest
~163 GB), so ~200 GB free per node is enough; otherwise set `HF_CACHE` in
`.env` to a larger drive.

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

`start_ray.sh` installs Ray `RAY_VERSION` (2.56.1) into both containers, so
both nodes need PyPI access at start.

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
```

About 15 minutes, single node, its own container, port, and output directory:
it fetches a small model, runs the canary, measures two replicates on fresh
launches with the release gates on, then writes figures and replicate
statistics and purges the weights. It proves the image, harness, power and
metrics sampling, and plotting work together before the long run.

## 10. Plan, canary pass, campaign (Diya, inside tmux)

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
