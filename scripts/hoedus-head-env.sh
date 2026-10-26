# Source this file to assign Hoedus the head role and Diya the worker role.
export DIYA_HOST=cisc-hoedus
export DIYA_IP=10.10.10.2
export HOEDUS_HOST=cisc-diya
export HOEDUS_IP=10.10.10.1
export HEAD_HOST="$DIYA_HOST"
export HEAD_IP="$DIYA_IP"
export WORKER_POWER_NODE_IP="$HOEDUS_IP"
