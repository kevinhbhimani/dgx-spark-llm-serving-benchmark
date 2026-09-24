#!/usr/bin/env bash
# Verify NCCL is using RDMA (NET/IB) not TCP (NET/Socket).
# Requires Ray up on both nodes. Run on the head node.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
source "$REPO/scripts/connectx_env.sh"

# Check the container's NCCL env
echo "=== NCCL env inside container ==="
docker exec "$VLLM_CONTAINER" bash -c 'env | grep ^NCCL | sort' 2>/dev/null || {
    echo "Container $VLLM_CONTAINER not running. Start Ray first."; exit 1
}

# Check if IB devices are visible inside the container
echo ""
echo "=== IB devices in container ==="
docker exec "$VLLM_CONTAINER" ibv_devinfo 2>/dev/null | grep -E "hca_id|link_layer|state" || {
    echo "No IB devices visible. Is RoCE configured on the host?"; exit 1
}

# Run a quick all-reduce with NCCL_DEBUG=INFO to reveal the transport
echo ""
echo "=== NCCL transport probe (look for NET/IB or NET/Socket) ==="
docker exec -i \
    -e NCCL_DEBUG=INFO -e NCCL_DEBUG_SUBSYS=INIT,NET \
    -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE" \
    "$VLLM_CONTAINER" python3 -u - "$HEAD_IP" "$HOEDUS_IP" "$CX_IFACE" <<'PY'
import os, sys, time
import ray

head, worker, iface = sys.argv[1], sys.argv[2], sys.argv[3]

def run(rank, world, head_ip, iface):
    import datetime, torch, torch.distributed as dist
    os.environ["NCCL_SOCKET_IFNAME"] = iface
    os.environ["GLOO_SOCKET_IFNAME"] = iface
    dist.init_process_group("nccl", init_method=f"tcp://{head_ip}:29520",
                            rank=rank, world_size=world,
                            timeout=datetime.timedelta(minutes=2))
    t = torch.ones(256, dtype=torch.float32, device="cuda:0")
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    dist.all_reduce(t)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - t0
    bw = (256 * 4) / elapsed / 1e9
    print(f"all_reduce 1KiB: {elapsed*1e6:.0f} us  ({bw:.2f} GB/s)", flush=True)
    dist.destroy_process_group()

ray.init(address="auto", log_to_driver=False)
refs = []
for rank, ip in enumerate([head, worker]):
    fn = ray.remote(num_cpus=0, num_gpus=1, resources={f"node:{ip}": 0.001})(run)
    refs.append(fn.remote(rank, 2, head, iface))
ray.get(refs, timeout=120)
print("done", flush=True)
PY

echo ""
echo "Look at the NCCL debug lines above:"
echo "  NET/IB : Using [0]mlx5_0/RoCE  → RDMA is active"
echo "  NET/Socket : Using [0]enp...    → fell back to TCP"