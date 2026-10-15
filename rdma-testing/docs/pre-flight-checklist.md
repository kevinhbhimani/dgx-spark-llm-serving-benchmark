# Pre-flight checklist

Run in order. Each step must pass before the next.

## 1. Netplan

Both nodes, `/etc/netplan/40-cx7.yaml`:

Diya:
```yaml
network:
  version: 2
  ethernets:
    enp1s0f0np0:    {addresses: [10.10.10.1/24], dhcp4: no, mtu: 4096}
    enP2p1s0f0np0:  {addresses: [10.10.11.1/24], dhcp4: no, mtu: 4096}
    enp1s0f1np1:    {addresses: [10.10.12.1/24], dhcp4: no, mtu: 4096}
    enP2p1s0f1np1:  {addresses: [10.10.13.1/24], dhcp4: no, mtu: 4096}
```

Hoedus:
```yaml
network:
  version: 2
  ethernets:
    enp1s0f0np0:    {addresses: [10.10.10.2/24], dhcp4: no, mtu: 4096}
    enP2p1s0f0np0:  {addresses: [10.10.11.2/24], dhcp4: no, mtu: 4096}
    enp1s0f1np1:    {addresses: [10.10.12.2/24], dhcp4: no, mtu: 4096}
    enP2p1s0f1np1:  {addresses: [10.10.13.2/24], dhcp4: no, mtu: 4096}
```

```bash
sudo chmod 600 /etc/netplan/40-cx7.yaml
sudo netplan apply
```

## 2. /etc/hosts

Both nodes:
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

## 3. Python venv

Diya only:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`run_job.sh` auto-detects `.venv/bin/python` and runs
`scripts/check_environment.py` as a prep step.

## 4. Environment

Both nodes. Source once per shell session, and always before
`scripts/start_ray.sh` (the Ray containers inherit `CX_IFACE`,
`NCCL_IB_DISABLE`, and the `NCCL_DEBUG` transport logging from it):
```bash
source rdma-dual-cable.env
```

The campaign keeps one model at a time in the cache (largest ~163 GB). If
the internal NVMe has less than ~200 GB free on either node, set `HF_CACHE`
in `rdma-dual-cable.env` to a larger drive.

## 5. Link state

Both nodes:
```bash
ip -br link show enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1
```
All four `UP`.

## 6. Ping

Diya:
```bash
ping -c 3 10.10.10.2
ping -c 3 10.10.11.2
ping -c 3 10.10.12.2
ping -c 3 10.10.13.2
```
All four under 2 ms.

## 7. RDMA devices

Both nodes:
```bash
ibdev2netdev
```
Four devices.

## 8. RDMA perftest

Hoedus:
```bash
cd ~/fsaii/perftest-suite-dgx-spark-cluster
./perftest-suite.sh host --run-name dual-200Gb-Oct-8/run-001 --modes isolation,concurrent
```

Diya:
```bash
cd ~/fsaii/perftest-suite-dgx-spark-cluster
./perftest-suite.sh client --run-name dual-200Gb-Oct-8/run-001 --modes isolation,concurrent
```

Check `perftest-results/dual-200Gb-Oct-8/run-001/host/results.csv`. Each device's
isolation bandwidth should be a stable fraction of its `ibv_devinfo` link rate,
and the concurrent aggregate should approach the sum of the isolation rates.
`ib_write_lat` under 2 us.

## 9. NCCL all-reduce

Diya:
```bash
NCCL_DEBUG=INFO bash scripts/interconnect_microbench.sh
```

`NCCL INFO NET/IB : Using` must list four devices. Bus bandwidth at 1 GiB near
100 GB/s. If fewer than four devices appear:
```bash
export NCCL_IB_HCA="rocep1s0f0,roceP2p1s0f0,rocep1s0f1,roceP2p1s0f1"
```
Rerun step 9 after exporting.

## 10. Ray cluster

Diya:
```bash
scripts/start_ray.sh head
```

Hoedus:
```bash
scripts/start_ray.sh worker
```

Diya, verify:
```bash
docker exec vllm-spark ray status
```
Two nodes `Active`.

## 11. Ray all-reduce

Diya:
```bash
bash scripts/ray_allreduce_bench.sh
```

`results/allreduce_ray_*.csv` bus bandwidth at 1 GiB must reflect four ports.

## 12. Disk

Both nodes:
```bash
df -h ~/.cache/huggingface
```
At least ~200 GB free. `scripts/run_job.sh` downloads, mirrors, measures, and
purges one model at a time, so nothing has to be pre-downloaded.

## 13. Plan and canary pass

Diya, from a clean commit:
```bash
PLAN_ONLY=1 scripts/run_job.sh
STOP_AFTER_CANARY=1 scripts/run_job.sh
```

The canary pass fetches each model, launches it under every config it lists
(strict stream contract, client tokenizer, NCCL transport check), and purges
it. It lists every failure at the end. Fix them, commit, and rerun until it
passes; a passed canary is remembered in `results/.canary_passed`.

## 14. Campaign

All models, all configs, all concurrencies, 3 replicates each. `--skip-done`
is active, so a rerun resumes and skips finished models without downloading
them. The process must survive SSH disconnects; run it under `tmux`.
On Diya:
```bash
source rdma-dual-cable.env
OUT_DIR="results" bash scripts/run_job.sh
```
