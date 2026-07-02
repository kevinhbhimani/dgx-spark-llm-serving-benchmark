"""Background monitors: a pluggable power sampler and a vLLM metrics scraper.

All timestamps use time.perf_counter so they align with the request records and
the measurement window. Power sampling is off by default. Validate the backend
on the hardware before trusting energy numbers, since GB10 power telemetry
varies by platform and firmware.
"""
from __future__ import annotations
import re
import time
import threading
import subprocess
import urllib.request


class PowerSampler:
    def __init__(self, backend="none", interval_ms=100, sysfs_path="", rails=None):
        self.backend = backend
        self.interval = interval_ms / 1000.0
        self.sysfs_path = sysfs_path
        self.rails = rails or []
        self._samples = []          # list of (t_perf, watts)
        self._proc = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self.backend == "none":
            return
        target = {
            "nvidia_smi": self._run_nvidia_smi,
            "sysfs": self._run_sysfs,
            "tegrastats": self._run_tegrastats,
        }.get(self.backend)
        if target is None:
            raise ValueError(f"unknown power backend {self.backend}")
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _add(self, watts):
        self._samples.append((time.perf_counter(), watts))

    def _run_nvidia_smi(self):
        ms = int(self.interval * 1000)
        cmd = ["nvidia-smi", "--query-gpu=power.draw",
               "--format=csv,noheader,nounits", "-lms", str(ms)]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        except FileNotFoundError:
            return
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            try:
                self._add(float(line.strip().split(",")[0]))
            except ValueError:
                continue

    def _run_sysfs(self):
        # power files report microwatts; change the divisor if your rail differs
        while not self._stop.is_set():
            try:
                with open(self.sysfs_path) as f:
                    self._add(float(f.read().strip()) / 1e6)
            except Exception:
                pass
            time.sleep(self.interval)

    def _run_tegrastats(self):
        ms = int(self.interval * 1000)
        try:
            self._proc = subprocess.Popen(["tegrastats", "--interval", str(ms)],
                                          stdout=subprocess.PIPE, text=True)
        except FileNotFoundError:
            return
        pat = re.compile(r"([A-Za-z0-9_]+)\s+(\d+)mW")
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            total, found = 0.0, False
            for name, mw in pat.findall(line):
                if not self.rails or any(r.lower() in name.lower() for r in self.rails):
                    total += float(mw) / 1000.0
                    found = True
            if found:
                self._add(total)

    def energy_joules(self, t0, t1):
        pts = [(t, w) for (t, w) in self._samples if t0 <= t <= t1]
        if len(pts) < 2:
            return float("nan")
        e = 0.0
        for (ta, wa), (tb, wb) in zip(pts, pts[1:]):
            e += (wa + wb) / 2.0 * (tb - ta)
        return e

    def mean_power(self, t0, t1):
        vals = [w for (t, w) in self._samples if t0 <= t <= t1]
        return sum(vals) / len(vals) if vals else float("nan")


class VLLMMetricsScraper:
    # Gauges: instantaneous occupancy/queue state, summarized as min/mean/max.
    GAUGE_KEYS = [
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
        "vllm:kv_cache_usage_perc",
    ]
    # Counters: monotonic totals of server-side work. Summarized as the delta
    # over the measurement window, which -- unlike the client's completed-in-
    # window aggregates -- counts tokens the server actually processed during the
    # window even when few requests finish inside it. This makes server-side
    # throughput robust to a measurement window shorter than request duration.
    COUNTER_KEYS = [
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
        "vllm:prompt_tokens_cached_total",
        "vllm:num_preemptions_total",
    ]
    KEYS = GAUGE_KEYS + COUNTER_KEYS
    METRIC_ALIASES = {
        # Older vLLM builds exposed this as gpu_cache_usage_perc. Keep summaries
        # stable by normalizing it to the current index/summary field.
        "vllm:gpu_cache_usage_perc": "vllm:kv_cache_usage_perc",
    }
    SCRAPE_KEYS = KEYS + list(METRIC_ALIASES)

    def __init__(self, base_url, interval_s=1.0):
        self.url = base_url.rstrip("/") + "/metrics"
        self.interval = interval_s
        self._series = {k: [] for k in self.KEYS}
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self):
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=2.0) as r:
                    text = r.read().decode("utf-8", "ignore")
                now = time.perf_counter()
                self._parse(text, now)
            except Exception:
                pass
            time.sleep(self.interval)

    def _parse(self, text, now):
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            for raw_key in self.SCRAPE_KEYS:
                if line.startswith(raw_key):
                    key = self.METRIC_ALIASES.get(raw_key, raw_key)
                    try:
                        self._series[key].append((now, float(line.rsplit(" ", 1)[1])))
                    except (ValueError, IndexError):
                        pass

    def summary(self, t0, t1):
        out = {}
        for k in self.GAUGE_KEYS:
            vals = [v for (t, v) in self._series[k] if t0 <= t <= t1]
            out[k] = ({"min": min(vals), "mean": sum(vals) / len(vals), "max": max(vals)}
                      if vals else {"min": None, "mean": None, "max": None})
        for k in self.COUNTER_KEYS:
            out[k] = self._counter_delta(self._series[k], t0, t1)
        return out

    @staticmethod
    def _counter_delta(pts, t0, t1):
        """Work a monotonic counter accumulated across the [t0, t1] window.

        Baselines on the last sample at or before the window start (so the full
        window is covered, not just the gap between the first and last in-window
        scrapes) and ends on the last sample at or before the window end.
        """
        base = None
        for t, v in pts:
            if t <= t0:
                base = (t, v)
            else:
                break
        end = None
        for t, v in pts:
            if t <= t1:
                end = (t, v)
            else:
                break
        if base is None:
            base = next(((t, v) for t, v in pts if t0 <= t <= t1), None)
        if base is None or end is None or end[0] <= base[0]:
            return {"delta": None, "rate_per_s": None}
        delta = end[1] - base[1]
        span = end[0] - base[0]
        return {"delta": delta, "rate_per_s": (delta / span) if span > 0 else None}
