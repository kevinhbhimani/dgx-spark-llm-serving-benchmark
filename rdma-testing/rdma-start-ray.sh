#!/usr/bin/env bash
#
#

if [ ! -n "$1" ]; then
	echo "Usage $0 <start_ray.sh option>"
	exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

NCCL_IB_DISABLE=0 NCCL_DEBUG=INFO bash $SCRIPT_DIR/../scripts/start_ray.sh $1
