# Things Dan needs sudo for:

## Packages/Tools
| Tool Name | Package Name | Install Command | Why? |
| --- | --- | --- | --- |
| `ibstat` | `infiniband-diags` | `apt install infiniband-diags` | Provides diagnostics about infiniband/RoCE/RDMA |

## General Commands
| Command | Why? |
| --- | --- |
| `ip link set dev <ifname> mtu 4096` | changes mtu (max transmission unit to 4096 bytes. currently set to 1024 (sept 17)) |
