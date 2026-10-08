# Changes to apply after dual-cable RDMA verification

Every item below is gated on successful completion of the verification
sequence in `dual-cable-campaign-setup.md`. None of these changes are safe to
apply before the full 8-step sequence passes.

## `scripts/connectx_env.sh`

- Line 18: change default `CX_IFACE` from `enp1s0f0np0` to the four-interface
  list if dual-cable becomes the standard configuration. Keep the single
  default if single-cable remains the primary path.
- Line 39: change default `NCCL_IB_DISABLE` from `1` to `0` if RDMA becomes
  the standard transport. The current default (`1`, TCP sockets) is the safe
  fallback.

## `.env`

- Add `CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"`.
- Add `NCCL_IB_DISABLE=0`.
- These overrides make the dual-cable RDMA configuration the default for
  anyone who clones and sources `.env`. Keep them commented out with a note
  if single-cable TCP remains the documented baseline.

## `README.md`

- Add a "Dual-cable RDMA setup" section referencing
  `rdma-testing/docs/dual-cable-campaign-setup.md`.
- Update the troubleshooting section: the false-OOM page-cache issue on Grace
  unified memory still applies. RDMA adds a new failure mode: NCCL hangs if
  one cable is unplugged or one port is down. Document the `NCCL_DEBUG=INFO`
  diagnostic step.

## `rdma-testing/scripts/rdma-start-ray.sh`

- Line 12: remove the `#This is jank, should change it` comment.
- Add `CX_IFACE` passthrough if the wrapper should enforce a specific
  interface list independent of the caller's environment.
- Consider merging into `scripts/start_ray.sh` as an `--rdma` flag instead of
  maintaining a separate wrapper.

## `rdma-testing/scripts/ic_shim.sh`

- Line 5: `HOST0=cisc-diya HOST1=cisc-hoedus`. Replace with `$DIYA_HOST` and
  `$HOEDUS_HOST` from `connectx_env.sh`, or source `connectx_env.sh` and use
  the variables directly.

## `scripts/run_job.sh`

- Line 231: the `ray_ok()` check runs `docker exec "$VLLM_CONTAINER" ray status`.
  After a dual-cable campaign, the single leg tears down the Ray container.
  The existing comment on line 358 documents this. Add a note that restarting
  Ray for a resumed dual-cable leg requires the full `CX_IFACE` and
  `NCCL_IB_DISABLE` exports on both nodes.

## `scripts/verify_connectx.sh`

- Add a ping check for all four subnets (currently pings only `$HOEDUS_HOST`,
  which resolves to `10.10.10.2` via `/etc/hosts`). Add explicit pings to
  `10.10.11.2`, `10.10.12.2`, `10.10.13.2` when `CX_IFACE` contains more
  than one interface.

## `rdma-testing/docs/rdma-testing.md`

- The netplan configs in this file show only two interfaces (one cable).
  Add the four-interface configs from `dual-cable-campaign-setup.md`, or
  reference that document.
- The `ib_write_lat` / `ib_read_lat` / `ib_write_bw` / `ib_read_bw` examples
  use a single device (`-d rocep1s0f0`). Add dual-device examples or
  reference `rdma-testing/ib-test-scripts/`.

## `rdma-testing/ib-test-scripts/`

- `client_dual_ib_test.sh` and its variants hardcode `rocep1s0f0` and
  `roceP2p1s0f0` (two devices from one cable). Extend to four devices or
  auto-discover from `/sys/class/infiniband/` like the perftest suite does.

## `configs/models.yaml`

- Revisions pinned 2026-10-08. NVFP4-only models excluded to limit download
  size to ~547 GB. No further action.

## `results/` directory

- The existing `index.csv` and allreduce artifacts were produced with
  `connectx_iface=enp1s0f0np0` and `nccl_ib_disable=1`. The dual-cable RDMA
  campaign produces rows with `connectx_iface=<four-interface list>` and
  `nccl_ib_disable=0`. These are different serving-policy fingerprints.
  Existing rows are not invalidated. New rows append to a fresh `OUT_DIR` or
  coexist in the same index under different fingerprints.