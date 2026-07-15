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
        # One output line becomes one sample. On a multi-GPU node nvidia-smi
        # emits one line per GPU per tick, which this would average rather than
        # sum; correct on DGX Spark, where each node has a single GB10.
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


# Runs inside the serving container on the head node. Pins a tiny actor to the
# worker node through the shared Ray cluster (the only remote-execution channel:
# there is no SSH path from head to worker), streams the worker GPU's power
# draw one watts value per line, and exits when stdin reaches EOF.
_WORKER_POWER_SCRIPT = r"""
import subprocess, sys, threading, time
import ray

ip, interval_ms = sys.argv[1], int(sys.argv[2])
ray.init(address="auto", log_to_driver=False)


@ray.remote(num_cpus=0, resources={f"node:{ip}": 0.001})
class Reader:
    def __init__(self, ms):
        self.latest = None
        threading.Thread(target=self._pump, args=(ms,), daemon=True).start()

    def _pump(self, ms):
        p = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=power.draw",
             "--format=csv,noheader,nounits", "-lms", str(ms)],
            stdout=subprocess.PIPE, text=True)
        for line in p.stdout:
            try:
                self.latest = float(line.strip().split(",")[0])
            except ValueError:
                pass

    def read(self):
        return self.latest


reader = Reader.remote(interval_ms)
stop = threading.Event()


def _watch_stdin():
    sys.stdin.read()          # EOF when the head-side sampler closes stdin
    stop.set()


threading.Thread(target=_watch_stdin, daemon=True).start()
while not stop.is_set():
    try:
        watts = ray.get(reader.read.remote(), timeout=10)
        if watts is not None:
            print(watts, flush=True)
    except Exception:
        pass
    time.sleep(interval_ms / 1000.0)
ray.kill(reader)
"""


class WorkerPowerSampler(PowerSampler):
    """Sample the worker node's GPU power through the shared Ray cluster.

    A helper process is docker-exec'ed into the head node's serving container;
    it pins an actor to the worker node (Ray's node:<ip> custom resource), polls
    nvidia-smi there, and prints one watts value per line. Each line is
    timestamped on receipt with the head's perf_counter, so the samples share
    the request records' clock; staleness is bounded by the sampling interval,
    which is negligible against a multi-hundred-second window.

    Best effort by design: if Ray, the container, or the worker is unreachable,
    the sampler yields no samples and mean_power/energy_joules return NaN, which
    metrics.aggregate reports as head-node-only power rather than failing the
    run.
    """

    def __init__(self, container, node_ip, interval_ms=1000):
        super().__init__(backend="none", interval_ms=interval_ms)
        self.container = container
        self.node_ip = node_ip

    def start(self):
        cmd = ["docker", "exec", "-i", self.container, "python3", "-u", "-c",
               _WORKER_POWER_SCRIPT, self.node_ip,
               str(int(self.interval * 1000))]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True)
        except FileNotFoundError:
            print(f"[bench] WARNING: docker not found; worker power for "
                  f"{self.node_ip} will not be sampled")
            return
        self._thread = threading.Thread(target=self._consume, daemon=True)
        self._thread.start()

    def _consume(self):
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            try:
                self._add(float(line.strip()))
            except ValueError:
                continue

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.stdin.close()   # EOF stops the remote script cleanly
            except Exception:
                pass
            try:
                self._proc.wait(timeout=15)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def sample_count(self):
        return len(self._samples)


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
    SCRAPE_KEYS = set(KEYS + list(METRIC_ALIASES))
    _SAMPLE_RE = re.compile(
        r"^([^\s{]+)(?:\{[^}]*\})?\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+)"
        r"(?:[eE][-+]?\d+)?|[-+]?Inf|NaN)\s*$"
    )

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
            self.scrape_once()
            time.sleep(self.interval)

    def scrape_once(self):
        try:
            with urllib.request.urlopen(self.url, timeout=2.0) as r:
                text = r.read().decode("utf-8", "ignore")
            now = time.perf_counter()
            self._parse(text, now)
        except Exception:
            pass

    def _parse(self, text, now):
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            sample = self._parse_sample(line)
            if sample is None:
                continue
            raw_key, value = sample
            if raw_key not in self.SCRAPE_KEYS:
                continue
            key = self.METRIC_ALIASES.get(raw_key, raw_key)
            self._series[key].append((now, value))

    @classmethod
    def _parse_sample(cls, line):
        m = cls._SAMPLE_RE.match(line.strip())
        if not m:
            return None
        try:
            return m.group(1), float(m.group(2))
        except ValueError:
            return None

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
        if delta < 0:
            return {"delta": None, "rate_per_s": None}
        span = end[0] - base[0]
        return {"delta": delta, "rate_per_s": (delta / span) if span > 0 else None}
