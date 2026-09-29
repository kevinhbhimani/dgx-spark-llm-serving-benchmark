
# NCCL Testing
## Tracking the things I do to setup and test NCCL

After setting up the network, we must install, configure, and test NCCL. NCCL is a library created by nvidia that provides communication primitives for GPUs and networking

### Dependencies
```bash
apt update
apt install libnccl2 libnccl-dev #NCCL
apt install openmpi-bin libopenmpi-dev #MPI
```

### Clone `nccl-tests` and build

```bash
git clone https://github.com/NVIDIA/nccl-tests.git
cd nccl-tests
make
```
