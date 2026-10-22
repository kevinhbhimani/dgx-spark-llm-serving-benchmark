# Design Plan: On-Demand Model Acquisition and Cache Ownership

**Status:** Implementation complete. Hardware cold-cache and worker-mirror validation remain open.

## Problem and Repository Evidence

The current campaign launcher calls `predownload.sh` to check and download a selected model group. It mirrors model files to Hoedus for dual-node runs and purges the group after measurement ([`scripts/run_job.sh`](../../scripts/run_job.sh#L368), [`scripts/run_job.sh`](../../scripts/run_job.sh#L444)). `run_models.sh` performs another cache check. `launch_server.sh` defaults serving to offline mode and validates both node caches for distributed configurations ([`scripts/run_models.sh`](../../scripts/run_models.sh#L213), [`scripts/launch_server.sh`](../../scripts/launch_server.sh#L81), [`scripts/launch_server.sh`](../../scripts/launch_server.sh#L171)). The checked-in flow therefore differs from a pure vLLM JIT download flow.

`connectx_env.sh` defaults host `HF_CACHE` to `$HOME/.cache/huggingface`, then mounts it at `/root/.cache/huggingface` in containers. The purge utility hard-codes `/root/.cache/huggingface/hub`. `bench/run_benchmark.py` falls back to `~/.cache/huggingface-client` after a cache permission error. The fallback creates a second tokenizer/config cache instead of fixing permissions on the selected cache.

The repo sets `HF_HUB_DISABLE_SHARED_BLOBS=1`. Hugging Face documents this as disabling the cache-wide shared blob store while retaining the configured cache root. `HF_HUB_CACHE` identifies the repository-cache directory. See [Hugging Face environment variables](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables) and the [cache guide](https://huggingface.co/docs/huggingface_hub/guides/manage-cache).

The reported NVIDIA driver warning has not been reproduced. The current download Docker calls do not request GPU devices. Its source must be identified before changing or suppressing the output.

## Design

1. Acquire only the pinned model revision needed by the next incomplete campaign group.
2. Let vLLM use its normal Hub integration for a single-node cold-cache launch when Hub access and credentials are available. Keep the download before readiness and outside the measured request window. Record the resolved revision and acquisition duration as operational metadata.
3. Preserve head-to-worker transfer for dual-node runs while the worker lacks Hub access. Acquire the selected revision on the head, mirror it to Hoedus, validate both caches, then serve offline. Remove mirroring only after a cold-cache dual-node run proves worker Hub access works.
4. Retire `predownload.sh` from the normal JIT path. Keep a download-only utility only if an offline-warming workflow still needs it. Remove unused size, DNS, worker-count, or compatibility branches after auditing callers and docs.
5. Keep one canonical cache root. On the host, default to `$HOME/.cache/huggingface`. Derive `HF_HUB_CACHE` once from `HF_CACHE` and pass it explicitly. In containers, mount the host cache at a stable path such as `/hf-cache` and set `HF_HOME=/hf-cache` and `HF_HUB_CACHE=/hf-cache/hub`. Do not rely on `/root/.cache` defaults.
6. Ensure cache files created in containers remain readable, writable, and removable by the host user. Prefer matching host UID/GID when the pinned image supports it. Otherwise use a narrow, verified group or ACL policy. Do not silently fall back to `huggingface-client/hub`, create root-owned cache files, or recursively change ownership outside the selected cache.
7. Check host and container create/read/delete access with a small probe before downloading. Fail early with host path, container path, effective UID/GID, and the operation that failed.
8. Validate the resolved pinned snapshot using the pinned Hugging Face runtime's supported APIs. Confirm all indexed model shards exist. Treat tokenizer-only repositories separately. Detect incomplete downloads without assuming a cache-wide blob store or symlink-only snapshots.
9. Remove duplicate cache checks while preserving a final gate before distributed serving. Purge only selected model revisions after every scheduled configuration and repeat completes and serving processes stop.

### Failure isolation

Record an acquisition, validation, mirror, or purge failure with its phase and evidence. Continue to a later independent model group only when the failure has not left shared cache, cluster, or result state unsafe. Complete safe cleanup for the failed item before proceeding. Never represent a failed cache operation as a successful measurement or silently redirect to a second cache. Fail fast when a shared prerequisite is unhealthy or continuing could contaminate results. Preserve the campaign failure summary and resume behavior.

## Acceptance Criteria

- A single-node cold-cache launch downloads only the selected pinned revision through the serving stack and reports readiness only after model loading.
- Dual-node cold-cache launch works with worker Hub access disabled by downloading on the head, mirroring, checking both nodes, and serving offline.
- Host and container Hub processes use the same configured repository cache. Repository-local storage remains enabled through `HF_HUB_DISABLE_SHARED_BLOBS=1`.
- The host user can read, write, and remove files created by the container. Cache permission errors identify the path and UID/GID instead of silently switching caches.
- The warning source is traced to a process or container entrypoint. The fix removes the triggering device discovery from standalone download/check work; it does not hide stderr.
- Cache validation rejects a wrong revision, incomplete download, or missing weight shard. Purge removes only selected repos after serving stops.
- Download and purge stay outside request measurement windows. Model revisions and performance methodology stay unchanged.

## Open Evidence

- Which test command currently produces the reported JIT behavior?
- Does the pinned serving image run its Hub download path under the current offline environment?
- Can the pinned image write the mounted home cache as the host UID/GID?
- Which cache entry path and incomplete-download marker are used by the pinned Hugging Face version with shared blobs disabled?
- What exact process produces the NVIDIA warning?

## Implementation Record

The cache path, UID/GID probes, pinned snapshot acquisition, worker mirror,
tokenizer metadata lookup, and purge scripts now use one configured cache root.
Single-node campaign groups use vLLM's pinned-revision online acquisition
path. Dual-node groups acquire on the head, mirror to the worker, and serve
offline. Cache access and path unit tests pass. Hardware cold-cache behavior,
container ownership on the target image, and the reported NVIDIA warning
remain unverified.
