"""Load generation engines: closed loop fixed concurrency and open loop Poisson."""
from __future__ import annotations
import asyncio
import random
import time
import aiohttp
from .client import send_request


async def _closed_loop(cfg, workload, session, stop_at, records):
    async def worker():
        while time.perf_counter() < stop_at:
            spec = workload.sample()
            records.append(await send_request(session, cfg, spec))
    await asyncio.gather(*[asyncio.create_task(worker())
                           for _ in range(cfg.concurrency)])


async def _open_loop(cfg, workload, session, stop_at, records):
    rng = random.Random(cfg.seed + 7)
    sem = asyncio.Semaphore(cfg.max_inflight)
    inflight = set()

    async def launch(spec):
        async with sem:
            records.append(await send_request(session, cfg, spec))

    while time.perf_counter() < stop_at:
        t = asyncio.create_task(launch(workload.sample()))
        inflight.add(t)
        t.add_done_callback(inflight.discard)
        await asyncio.sleep(rng.expovariate(cfg.qps))
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
