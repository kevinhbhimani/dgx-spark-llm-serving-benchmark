# Required Permissions

Run `sudo bash rdma-testing/scripts/setup-benchmark-user.sh [USERNAME]` on both nodes.

## The only group: `docker`

`/var/run/docker.sock` is `root:docker 0660`. Every script in this repo calls `docker` directly — `run`, `exec`, `rm`, `cp`, `logs`, `inspect`. Without membership you'd be prefixing everything with `sudo`.

## Everything else is `sudo` as needed

| Operation | Command |
|-----------|---------|
| GPU queries | `sudo nvidia-smi` |
| RDMA device check | `sudo ibv_devinfo` |
| MTU change (one-time) | `sudo ip link set dev enp1s0f0np0 mtu 4096` |
| RDMA firmware (one-time) | `sudo mlxconfig`, `sudo mlxfwmanager` |
| Page cache drop | `echo 3 \| sudo tee /proc/sys/vm/drop_caches` |
| Install RDMA tools | `sudo apt install infiniband-diags perftest ibverbs-utils` |

None of these need permanent groups or sudoers entries — they're one-off or rare.