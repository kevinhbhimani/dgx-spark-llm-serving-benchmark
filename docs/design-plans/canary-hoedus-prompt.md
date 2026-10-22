# Agent Prompt: Diagnose the Hoedus Canary Failure

Follow [canary-hoedus-plan.md](canary-hoedus-plan.md). This assignment begins as a diagnosis. Do not assume that insufficient cooldown or temperature caused the crash, and do not change benchmark behavior before evidence identifies a cause.

Academic and scientific rigor is of the utmost importance. Distinguish repository facts, operator reports, direct observations, inferences, and hypotheses. Record conditions and timestamps needed to reproduce the diagnosis, seek disconfirming evidence, and state uncertainty directly. Do not claim a causal or thermal explanation from correlation alone. Make no code change unless evidence demonstrates a defect and the change is the smallest one that addresses it. Add no abstraction unless it removes real duplication and makes the code easier to understand.

## Diagnosis Tasks

1. Inspect the canary and full-benchmark control paths, teardown behavior, logs, existing result artifacts, and campaign runbook. Identify the exact differences between them and state which facts are code-derived versus reported.
2. Classify “Hoedus crashes” using evidence: host reboot/unreachable, GPU/driver failure, Ray worker/node failure, vLLM-only failure, or host/container resource exhaustion. Collect UTC timestamps and correlate model/revision, configuration, runtime fingerprint, launch/readiness/request/teardown events, endpoint and Ray state, host memory, disk space, GPU memory/utilization/power/temperature, processes, and kernel/journal records where available.
3. Preserve raw diagnostic evidence and redact credentials. If evidence is missing, state exactly which artifact, host access, timestamp, or operator observation is needed. Do not manufacture a root cause from absence of logs.
4. Test alternative explanations including asynchronous teardown, stale GPU processes, residual GPU/host memory, Ray cleanup, disk pressure, model/configuration compatibility, and driver/hardware faults.
5. Distinguish a timing association from thermal causation. A longer wait that improves outcomes is insufficient by itself to claim temperature caused the failures.
6. Preserve canary failure isolation: continue through independent model/configuration checks after a local failure only while teardown and shared host/cluster state remain healthy. Aggregate and report failures; do not start the measurement pass when the canary gate fails unless the existing explicit override applies.

## Controlled Trial Boundary

Use existing logs and read-only host evidence first. If reproducing the failure requires another canary, only run it when the target host is available for recovery and the authorized operator agrees it is safe to proceed. Keep model revision, image, Ray state, cache state, request, and launch arguments fixed; change only the post-teardown idle interval. Record predeclared outcomes and boundary measurements for no fixed wait and the planned intervals. Alternate or randomize interval order when practical. Stop after the first host-level failure; preserve evidence before any recovery. Do not run a full campaign.

Do not use or modify the benchmark `cooldown_s` setting as a canary fix. It operates during the request phase and is skipped by `CANARY_ONLY`. If the controlled evidence supports an idle interval, propose a distinct post-teardown canary setting, specify how it was calibrated, and keep it outside measured windows. Do not implement it as part of a diagnosis-only task unless the user separately authorizes that implementation.

## Completion Report

Report the failure classification, observed evidence with timestamps, competing hypotheses and confidence, controlled-trial design and outcomes if performed, evidence that would falsify the cooldown hypothesis, and the next smallest justified action. State clearly if hardware access or logs were unavailable. Make no unsupported causal claim and make no code changes without a demonstrated cause and a separate implementation assignment.
