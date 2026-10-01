# TODO

### rdma-start-ray.sh

The script is not robust in the sense that if you move it, or the script it calls, it won't find it and fail

### runner.sh

rename it so it is more clear what it does, get rid of the hardcoded --notes option, should probably improve it so that if the experiment fails, we stop the script, find some way to make sure the client/host are in sync, maybe via an ssh command?
