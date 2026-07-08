"""Load generation engines: closed loop fixed concurrency and open loop Poisson."""
from __future__ import annotations
import asyncio
import random
import time
import aiohttp
from .client import send_request

# A dead server fails requests in microseconds; without a pause between retries
# the closed loop hammers it at thousands of requests per second (one real run
# logged 1.16M connection-refused records in a 600s window).
FAILURE_BACKOFF_S = 0.5
# Abort the phase after this many failures with no success in between, rather
# than spending the whole window measuring a server that stopped answering and
# then writing a summary row for it.
CONSECUTIVE_FAILURE_LIMIT = 50


async def _closed_loop(cfg, workload, session, stop_at, records):
    consecutive_failures = 0

    async def worker():
        nonlocal consecutive_failures
        while time.perf_counter() < stop_at:
            spec = workload.sample()
            rec = await send_request(session, cfg, spec)
            records.append(rec)
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


async def _open_loop(cfg, workload, session, stop_at, records):
    rng = random.Random(cfg.seed + 7)
    sem = asyncio.Semaphore(cfg.max_inflight)
    inflight = set()

    async def launch(spec, arrival):
        async with sem:
            # t_start is the intended arrival time, so time a request spends
            # queued at the client (max_inflight) counts toward its latency
            # rather than being silently dropped (coordinated omission).
            records.append(await send_request(session, cfg, spec,
                                              t_start=arrival))

    # Schedule arrivals against absolute times: sleeping one interarrival gap at
    # a time accumulates event-loop overhead and drops the achieved rate below
    # the target qps. If the loop falls behind, arrivals catch up in a burst,
    # preserving the average rate.
    next_arrival = time.perf_counter() + rng.expovariate(cfg.qps)
    while next_arrival < stop_at:
        delay = next_arrival - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        t = asyncio.create_task(launch(workload.sample(), next_arrival))
        inflight.add(t)
        t.add_done_callback(inflight.discard)
        next_arrival += rng.expovariate(cfg.qps)
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


async def run_phase(cfg, workload, duration_s):
    if duration_s <= 0:
        raise ValueError("phase duration must be positive")
    if cfg.mode == "closed" and cfg.concurrency < 1:
        raise ValueError("closed-loop concurrency must be at least 1")
    if cfg.mode == "open":
        if cfg.qps <= 0:
            raise ValueError("open-loop qps must be positive")
        if cfg.max_inflight < 1:
            raise ValueError("open-loop max_inflight must be at least 1")
    records = []
    connector = aiohttp.TCPConnector(limit=0)   # server controls admission
    async with aiohttp.ClientSession(connector=connector) as session:
        stop_at = time.perf_counter() + duration_s
        if cfg.mode == "closed":
            await _closed_loop(cfg, workload, session, stop_at, records)
        elif cfg.mode == "open":
            await _open_loop(cfg, workload, session, stop_at, records)
        else:
            raise ValueError(f"unknown mode {cfg.mode}")
    return records
