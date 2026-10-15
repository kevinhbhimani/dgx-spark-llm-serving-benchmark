"""Load generation engines with steady-state measurement boundaries.

Closed-loop measurement does not open until the configured warmup has elapsed
*and* every worker has completed its initial request. After the nominal cooldown,
both engines keep offering replacement load until every request that started in
the measurement window has finished. Only then do remaining cooldown requests
drain. This prevents first-wave and drain-down bias in latency percentiles.

With ``min_window_requests`` set, the closed-loop window is a minimum: while
fewer than that many requests have started inside it, its end moves out in
fixed ``window_extension_step_s`` steps, up to ``max_measure_s`` (the MLPerf
"minimum duration and minimum query count" rule). The decision depends only on
how many requests have started, never on their latencies, so it cannot bias the
latency percentiles; under steady load it does not bias the windowed rates.
"""
from __future__ import annotations
import asyncio
import math
import random
import time
import aiohttp
from .client import RequestRecord, send_request

# A dead server fails requests in microseconds; without a pause between retries
# the closed loop hammers it at thousands of requests per second (one real run
# logged 1.16M connection-refused records in a 600s window).
FAILURE_BACKOFF_S = 0.5
# Abort the phase after this many failures with no success in between, rather
# than spending the whole window measuring a server that stopped answering and
# then writing a summary row for it.
CONSECUTIVE_FAILURE_LIMIT = 50


async def _closed_loop(cfg, workload, session, phase_start, records):
    consecutive_failures = 0
    desired_window_start = phase_start + cfg.warmup_s
    initial_remaining = cfg.concurrency
    measurement_pending = 0
    min_requests = getattr(cfg, "min_window_requests", 0) or 0
    step_s = getattr(cfg, "window_extension_step_s", 0.0) or 0.0
    max_measure_s = getattr(cfg, "max_measure_s", 0.0) or 0.0
    state = {
        "window_start": None,
        "window_end": None,
        "nominal_stop": None,
        "load_stop": None,
        # Requests whose arrival fell inside [window_start, window_end).
        "window_requests_started": 0,
        "window_extensions": 0,
        # No further extension decisions once set (immediately when the
        # minimum-sample rule is disabled).
        "window_final": min_requests <= 0,
    }

    def settle_window(now):
        """Extend the window past ``now`` while its sample floor is unmet.

        Called before any arrival at or after the current window end is
        classified, and before a worker considers stopping. Arrivals are
        classified right after their timestamp is read, with no await in
        between, so every arrival before window_end has already been counted
        and the floor test below is exact for [window_start, window_end).
        """
        if state["window_start"] is None or state["window_final"]:
            return
        while now >= state["window_end"]:
            if state["window_requests_started"] >= min_requests:
                state["window_final"] = True
                return
            steps = state["window_extensions"] + 1
            length = cfg.measure_s + steps * step_s
            if length > max_measure_s + 1e-9:
                # Cap reached with the floor unmet: the window closes here and
                # the release quality gate rejects the point.
                state["window_final"] = True
                return
            state["window_extensions"] = steps
            state["window_end"] = state["window_start"] + length
            state["nominal_stop"] = state["window_end"] + cfg.cooldown_s

    async def worker():
        nonlocal consecutive_failures, initial_remaining, measurement_pending
        first = True
        while True:
            now = time.perf_counter()
            settle_window(now)
            nominal_stop = state["nominal_stop"]
            if (nominal_stop is not None and now >= nominal_stop
                    and measurement_pending == 0):
                if state["load_stop"] is None:
                    state["load_stop"] = now
                break
            spec = workload.sample()
            arrival = time.perf_counter()
            settle_window(arrival)
            is_measurement = (state["window_start"] is not None
                              and state["window_start"] <= arrival
                              < state["window_end"])
            if is_measurement:
                measurement_pending += 1
                state["window_requests_started"] += 1
            rec = await send_request(session, cfg, spec, t_start=arrival)
            records.append(rec)
            if first:
                first = False
                initial_remaining -= 1
                if initial_remaining == 0:
                    # If the first wave outlives the configured warmup, begin only
                    # after it is gone. Otherwise retain the configured boundary.
                    window_start = max(desired_window_start, time.perf_counter())
                    state["window_start"] = window_start
                    state["window_end"] = window_start + cfg.measure_s
                    state["nominal_stop"] = (state["window_end"]
                                               + cfg.cooldown_s)
            if is_measurement:
                measurement_pending -= 1
            if rec.success:
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                raise RuntimeError(
                    f"aborting phase: {consecutive_failures} consecutive "
                    f"request failures, last error: {rec.error}")
            await asyncio.sleep(FAILURE_BACKOFF_S)

    tasks = [asyncio.create_task(worker()) for _ in range(cfg.concurrency)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if state["window_start"] is None:
        raise RuntimeError("closed-loop workers ended before warmup completed")
    state["load_stop"] = state["load_stop"] or time.perf_counter()
    return state


async def _open_loop(cfg, workload, session, phase_start, records):
    rng = random.Random(cfg.seed + 7)
    inflight = set()
    window_start = phase_start + cfg.warmup_s
    window_end = window_start + cfg.measure_s
    nominal_stop = window_end + cfg.cooldown_s
    measurement_pending = 0
    measurement_done = asyncio.Event()
    measurement_done.set()
    consecutive_failures = 0
    fatal_error = None

    async def launch(spec, arrival, is_measurement):
        nonlocal measurement_pending, consecutive_failures, fatal_error
        try:
            rec = await send_request(session, cfg, spec, t_start=arrival)
            records.append(rec)
            if rec.success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                    fatal_error = RuntimeError(
                        f"aborting phase: {consecutive_failures} consecutive "
                        f"request failures, last error: {rec.error}"
                    )
        finally:
            if is_measurement:
                measurement_pending -= 1
                if measurement_pending == 0:
                    measurement_done.set()

    def schedule(arrival):
        nonlocal measurement_pending, consecutive_failures, fatal_error
        is_measurement = window_start <= arrival < window_end
        spec = workload.sample()
        # Bound *all* outstanding HTTP requests, not just the subset that has
        # acquired a semaphore. When the client-side limit is full, record the
        # offered arrival as a failed attempt instead of building an unbounded
        # task queue or blocking the arrival clock (coordinated omission).
        if len(inflight) >= cfg.max_inflight:
            now = time.perf_counter()
            rec = RequestRecord(cfg.config_label, spec.input_len,
                                spec.output_len, t_start=arrival, t_end=now,
                                error="client max_inflight exceeded")
            records.append(rec)
            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                fatal_error = RuntimeError(
                    f"aborting phase: {consecutive_failures} consecutive "
                    "request failures, last error: client max_inflight exceeded"
                )
            return
        if is_measurement:
            if measurement_pending == 0:
                measurement_done.clear()
            measurement_pending += 1
        t = asyncio.create_task(launch(spec, arrival, is_measurement))
        inflight.add(t)
        t.add_done_callback(inflight.discard)

    # Schedule arrivals against absolute times: sleeping one interarrival gap at
    # a time accumulates event-loop overhead and drops the achieved rate below
    # the target qps. If the loop falls behind, arrivals catch up in a burst,
    # preserving the average rate.
    next_arrival = phase_start + rng.expovariate(cfg.qps)
    while next_arrival < nominal_stop:
        if fatal_error is not None:
            break
        delay = next_arrival - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        schedule(next_arrival)
        next_arrival += rng.expovariate(cfg.qps)

    # Cooldown is a minimum. Continue Poisson arrivals while any measurement
    # request (including one queued behind max_inflight) remains unfinished.
    while measurement_pending > 0 and fatal_error is None:
        delay = next_arrival - time.perf_counter()
        if delay > 0:
            try:
                await asyncio.wait_for(measurement_done.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
        if measurement_pending == 0:
            break
        schedule(next_arrival)
        next_arrival += rng.expovariate(cfg.qps)
    load_stop = time.perf_counter()
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)
    if fatal_error is not None:
        raise fatal_error
    return {
        "window_start": window_start,
        "window_end": window_end,
        "nominal_stop": nominal_stop,
        "load_stop": load_stop,
    }


async def run_phase(cfg, workload):
    durations = (cfg.warmup_s, cfg.measure_s, cfg.cooldown_s)
    if (any(isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in durations)
            or cfg.warmup_s < 0 or cfg.measure_s <= 0
            or cfg.cooldown_s < 0):
        raise ValueError(
            "warmup/cooldown must be finite and non-negative and measure positive"
        )
    if (isinstance(cfg.request_timeout_s, bool)
            or not isinstance(cfg.request_timeout_s, (int, float))
            or not math.isfinite(cfg.request_timeout_s)
            or cfg.request_timeout_s <= 0):
        raise ValueError("request_timeout_s must be finite and positive")
    if cfg.mode == "closed":
        if (isinstance(cfg.concurrency, bool)
                or not isinstance(cfg.concurrency, int)
                or cfg.concurrency < 1):
            raise ValueError("closed-loop concurrency must be a positive integer")
        if getattr(cfg, "min_window_requests", 0):
            step = getattr(cfg, "window_extension_step_s", 0.0)
            cap = getattr(cfg, "max_measure_s", 0.0)
            if (not isinstance(step, (int, float)) or not math.isfinite(step)
                    or step <= 0 or not isinstance(cap, (int, float))
                    or not math.isfinite(cap) or cap < cfg.measure_s):
                raise ValueError(
                    "window extension needs a positive window_extension_step_s "
                    "and max_measure_s >= measure_s"
                )
    if cfg.mode == "open":
        if getattr(cfg, "min_window_requests", 0):
            raise ValueError(
                "min_window_requests is implemented for closed-loop sweeps only"
            )
        if (isinstance(cfg.qps, bool)
                or not isinstance(cfg.qps, (int, float))
                or not math.isfinite(cfg.qps) or cfg.qps <= 0):
            raise ValueError("open-loop qps must be finite and positive")
        if (isinstance(cfg.max_inflight, bool)
                or not isinstance(cfg.max_inflight, int)
                or cfg.max_inflight < 1):
            raise ValueError("open-loop max_inflight must be a positive integer")
    records = []
    connector = aiohttp.TCPConnector(limit=0)   # server controls admission
    async with aiohttp.ClientSession(connector=connector) as session:
        phase_start = time.perf_counter()
        if cfg.mode == "closed":
            state = await _closed_loop(cfg, workload, session, phase_start, records)
        elif cfg.mode == "open":
            state = await _open_loop(cfg, workload, session, phase_start, records)
        else:
            raise ValueError(f"unknown mode {cfg.mode}")
    state["phase_start"] = phase_start
    state["actual_end"] = time.perf_counter()
    return records, state
