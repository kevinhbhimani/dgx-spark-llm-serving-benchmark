# Test Failure Report: 2026-10-23

## Test run

- Command: `MPLCONFIGDIR=/tmp/dgx-spark-mplconfig .venv/bin/python -m unittest discover -s tests`
- Initial result: 231 tests ran, 3 failed, 0 errors.
- All three failures are in `tests/test_rdma_readiness.py`.
- The failing tests use temporary command mocks. The run did not start a GPU server or a real Ray cluster.

## Failures

### `test_gpu_memory_fraction_and_scheduler_limit_reach_all_serving_modes`

Location: `tests/test_rdma_readiness.py:296`

The test expects `launch_server.sh` to return 0 for the `single`, `tp2`, and `pp2` launch modes after checking `--gpu-memory-utilization 0.85` and `--max-num-seqs 4`. The first launch returns 1 with `server command exited before the API became ready`.

The test's fake `docker` command records the arguments and exits 0. The test does not mock `curl`. `launch_server.sh` polls `$BASE_URL/v1/models` and returns failure when the mocked server command exits before the endpoint responds (`scripts/launch_server.sh:155-172`).

**Proposed fix:** Host a loopback readiness endpoint that returns success for `/v1/models` only when the modeled server is ready. Keep the mocked serving command alive through the readiness response, then let it exit cleanly. Assert that the readiness request occurred and retain the existing assertions for the forwarded flags.

### `test_direct_launch_defaults_gpu_memory_fraction_to_80_percent`

Location: `tests/test_rdma_readiness.py:330`

The test expects a direct launch with an empty `GPU_UTIL` value to return 0 and pass `--gpu-memory-utilization 0.80`. The test fails with the same `server command exited before the API became ready` message.

The test uses the same immediate-success fake `docker` command and has no `/v1/models` readiness mock. `launch_server.sh` requires an API readiness response before releasing its resource lock (`scripts/launch_server.sh:157-164`).

**Proposed fix:** Use the loopback readiness fixture described for the previous test. Keep the default-value assertion and verify that the launcher checks readiness before releasing the lock.

### `test_start_ray_rejects_missing_rail_before_removing_container`

Location: `tests/test_rdma_readiness.py:231`

The test expects `start_ray.sh head` to report a missing active RDMA rail and avoid removing a container. The test receives `[resources] UNKNOWN: cannot inspect local Docker containers` instead.

The shared test setup makes every `docker` invocation write to the call log and exit 99 (`tests/test_rdma_readiness.py:104`). `start_ray.sh` now checks resource availability before role-specific RDMA checks (`scripts/start_ray.sh:22-26`). The resource manager runs `docker ps --format {{.Names}}` and treats a failed listing as an unknown state (`scripts/resource_manager.py:64-75`). The resource guard fails closed before the missing-rail check runs.

**Proposed fix:** Model an idle running Ray container. Return its name from `docker ps`, return `true` from `docker inspect`, return a header with no processes from `docker top`, and report active Ray nodes as idle. Record potentially destructive commands such as `docker rm` and `docker run`, then assert that neither ran before the missing-rail error. Keep the assertion for the RDMA error text.

## Changes applied

- The launch tests start a loopback HTTP server on an ephemeral port. The launcher's real `curl -sf --max-time 1 <BASE_URL>/v1/models` request receives HTTP 503 before readiness and HTTP 200 after the modeled API becomes ready.
- The mocked serving process stays alive through the successful readiness request. Tests verify that the launcher holds its resource lock before readiness, releases it after HTTP 200, and remains attached to the serving process until the test stops it.
- `test_direct_launch_rejects_server_exit_before_api_readiness` verifies that a successful server-command exit without API readiness returns failure and does not take the successful readiness handoff.
- The RDMA test now models an idle running Ray container, successful local and worker resource checks, and a missing second RDMA rail. It verifies that the script issues read-only state checks and does not run `docker rm` or `docker run`.
- The production readiness check and fail-closed resource check remain enabled.

## Validation after changes

- `MPLCONFIGDIR=/tmp/dgx-spark-mplconfig .venv/bin/python -m unittest tests.test_rdma_readiness` passed 19 tests.
- `MPLCONFIGDIR=/tmp/dgx-spark-mplconfig .venv/bin/python -m unittest discover -s tests` passed 232 tests with 0 failures and 0 errors.
- Both test commands ran with loopback networking available. The launcher tests used the real `curl` executable and a local HTTP server.
- `bash -n scripts/launch_server.sh scripts/start_ray.sh`, Python compilation of `tests/test_rdma_readiness.py`, and `git diff --check` for the changed test and report passed.
