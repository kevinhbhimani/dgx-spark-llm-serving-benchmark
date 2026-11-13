#!/usr/bin/env bash
# Shared container defaults for benchmark runs and offline bundle builds.
# Container settings. A mutable monthly tag alone is not sufficient provenance
# for a paper rerun, so the image is pinned by digest. This is NGC 26.09-py3
# (vLLM 0.29.0, CUDA 13.4.1, NCCL 2.31.2, HPC-X 2.51), required by the newer
# checkpoints in configs/models.yaml. The baselines/ campaigns used 26.04-py3
# (vLLM 0.19.0): nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27
# The image digest pins the platform stack. start_ray.sh installs the pinned
# Ray runtime using scripts/ray-runtime-constraints.txt, which also fixes its
# Python dependency versions to the latest full runtime recorded in this repo.
# Update the image, Ray version, and Ray constraints together.
export VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2}"
export RAY_VERSION="${RAY_VERSION:-2.56.1}"
