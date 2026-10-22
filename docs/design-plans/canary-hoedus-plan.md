# Design Plan: Diagnose Hoedus Failure During the Canary

**Status:** Static diagnosis and test plan. No hardware run or implementation change was performed.

## Observed Behavior in the Repository

The campaign canary fetches one model group, mirrors it when the group includes `tp2` or `pp2`, launches each model/configuration pair once, validates the strict streaming contract, then purges the group ([`scripts/run_job.sh`](../../scripts/run_job.sh#L407), [`docs/campaign-runbook.md`](../campaign-runbook.md#L192)). `CANARY_ONLY=1` skips `run_all.sh` and the measurement sweep ([`scripts/run_models.sh`](../../scripts/run_models.sh#L946)).

After a canary passes, `run_models.sh` calls `teardown()`, waits for the API endpoint to go down, and returns. This branch returns before the five-second sleep used after the normal measurement sweep ([`scripts/run_models.sh`](../../scripts/run_models.sh#L946), [`scripts/run_models.sh`](../../scripts/run_models.sh#L964)). For distributed runs, `teardown()` stops the `vllm serve` process in the Ray container and waits for the launcher process. It does not verify that Hoedus has returned to a measured GPU-memory or temperature baseline.

The experiment's `cooldown_s: 30` is part of the continuous benchmark request phase. The runner continues replacement requests while measurement-started requests remain in flight. It is not an idle wait between canary launches ([`configs/experiment.yaml`](../../configs/experiment.yaml#L25), [`bench/runner.py`](../../bench/runner.py#L215)). The canary skips that runner, so this setting does not delay canary-to-canary transitions.

The checked-in `results/` tree contains no `server_*.log` or `run_job_*.log` crash trace. This inspection cannot identify whether “Hoedus crashes” means the host reboots, becomes unreachable, loses its GPU, loses its Ray worker, or only stops serving.

Preserve failure isolation during the canary: continue checking independent model/configuration cases after a local canary failure when teardown and shared cluster/cache state remain healthy, and retain each outcome. The canary remains a gate before measurement. Aggregate its failures and do not start the measurement pass unless the existing explicit `ALLOW_CANARY_FAILURES=1` override is set. A host-level or shared-cluster failure stops further trials because later outcomes would not be independent or safe to collect.

## Diagnosis

The limited post-teardown wait hypothesis is consistent with the code: the canary has no fixed post-teardown sleep, while the normal measurement path sleeps five seconds after its sweep. The code does not establish a thermal cause. The same timing difference could expose asynchronous worker teardown, stale CUDA processes, residual GPU memory, host-memory pressure, Ray state, storage pressure, a model/configuration failure, or a GPU/driver fault.

A canary sends one short strict streaming request. It exercises model startup, distributed worker initialization, the first forward pass, and teardown. It does not exercise sustained load or the full concurrency matrix. A full benchmark uses a longer lifetime and a different sequence of model, configuration, and server transitions. A passing benchmark on another sequence does not isolate cooldown as the cause.

NVIDIA documents Xid reports in the operating-system kernel or event log and states that an Xid can indicate hardware, driver, or user-application problems. An Xid code alone does not establish root cause. Capture kernel records with their timestamps and full context, then interpret them against the matching driver documentation. See [NVIDIA Xid introduction](https://docs.nvidia.com/deploy/xid-errors/introduction.html) and [NVIDIA SMI documentation](https://docs.nvidia.com/deploy/nvidia-smi/index.html).

## Evidence Collection

First classify the failure:

- **Host failure:** Hoedus is unreachable or rebooted. Record last SSH/ping success, console state, boot ID, uptime, and previous-boot kernel logs.
- **GPU/driver failure:** host remains available, but `nvidia-smi` or CUDA fails. Record Xid/kernel messages and driver/GPU identity.
- **Ray failure:** host and GPU remain healthy, but the expected Ray node disappears or its raylet fails.
- **Serving failure:** Ray remains healthy, but the vLLM process, worker actors, or API endpoint exits.
- **Resource failure:** kernel OOM/cgroup kill, memory exhaustion, filesystem exhaustion, or a growing process/GPU-memory footprint appears.

For each attempt, preserve UTC timestamps and correlate these observations on both nodes: model label and pinned revision, serving config, image/runtime fingerprint, canary launch/readiness/request/teardown times, endpoint status, Ray node identity and status, host available memory, filesystem space, GPU memory, power, temperature, utilization, process list, and relevant kernel/journal records. Record measurements immediately before launch, after readiness, after the canary request, after teardown, and at each inter-launch wait boundary.

## Controlled Cooldown Test

Use a model/configuration pair that reproduces the failure. Keep model revision, image, Ray state, cache state, canary request, and all launch options fixed. Change only the post-teardown idle interval. Compare the current no-fixed-wait canary branch with measured waits such as 5, 15, 30, and 60 seconds. Randomize or alternate trial order and repeat each condition enough to estimate whether the failure is reproducible. Stop the trial after a hard host failure and preserve the evidence before recovery.

At each interval, record GPU temperature, GPU memory, host memory, Ray worker/process state, and endpoint state. A longer interval that reduces failures is evidence for a timing dependency, not necessarily thermal causality. Thermal evidence requires failure risk to track elevated thermal measurements and recovery toward baseline as temperature falls. If process or memory state settles while temperature remains similar, investigate cleanup or resource reclamation. If failures track a model/configuration regardless of interval, investigate model, topology, runtime, and driver compatibility.

Do not use `cooldown_s` as the fix. It changes request-phase semantics and does not execute in `CANARY_ONLY`. If evidence supports an idle interval, add a separately named post-teardown canary delay with a default calibrated from the trials. Keep it outside all performance windows. Keep normal benchmark inter-launch behavior unchanged unless separate evidence requires a change.

## Acceptance Criteria for Diagnosis

- The failure is classified by host, GPU/driver, Ray, serving, or resource evidence.
- The failing model/configuration and sequence are reproducible or the current evidence is explicitly reported as insufficient.
- The cooldown comparison records temperature, memory, process/Ray state, and failure outcomes at every interval.
- The report distinguishes timing association from thermal causation and names evidence that would falsify the cooldown hypothesis.
- Any proposed code change is limited to the demonstrated cause and does not alter benchmark measurement semantics.
