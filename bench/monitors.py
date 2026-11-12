"""Background monitors: a pluggable power sampler and a vLLM metrics scraper.

All timestamps use time.perf_counter so they align with the request records and
the measurement window. Power sampling is off by default. Validate the backend
on the hardware before trusting energy numbers, since GB10 power telemetry
varies by platform and firmware.
"""
from __future__ import annotations
import math
import re
import time
import threading
import subprocess
from typing import Any
import urllib.request


def _optional_float(value: str | float | None) -> float | None:
    """Parse a finite sensor value, treating driver placeholders as missing."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_nvidia_smi_row(line: str) -> dict[str, Any] | None:
    """Parse one index, UUID, power, temperature query row."""
    fields = [field.strip() for field in line.strip().split(",")]
    if (len(fields) < 3 or not fields[0].isdigit()
            or not fields[1] or fields[1].casefold() in {"n/a", "unknown"}):
        return None
    return {
        "gpu_index": fields[0] or None,
        "gpu_uuid": fields[1] or None,
        "watts": _optional_float(fields[2]),
        "temperature_c": _optional_float(fields[3]) if len(fields) > 3 else None,
    }


class PowerSampler:
    def __init__(self, backend="none", interval_ms=100, sysfs_path="", rails=None):
        self.backend = backend
        self.interval = interval_ms / 1000.0
        self.sysfs_path = sysfs_path
        self.rails = rails or []
        self._samples = []          # list of (t_perf, watts)
        self._temperature_samples = []
        self._sample_records = []
        self.temperature_backend = backend
        self._proc = None
        self._proc_lock = threading.Lock()
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
        self._terminate_proc()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        # Covers a target that was entering its spawn critical section exactly
        # as stop began. _spawn_proc checks the event while holding the same
        # lock, so after this second call no child can appear later.
        self._terminate_proc()

    def _spawn_proc(self, cmd):
        with self._proc_lock:
            if self._stop.is_set():
                return None
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, text=True
                )
            except FileNotFoundError:
                return None
            return self._proc

    def _terminate_proc(self):
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=3.0)
                except Exception:
                    pass

    def _add(
        self,
        watts: str | float | None,
        temperature_c: str | float | None = None,
        gpu_index: str | None = None,
        gpu_uuid: str | None = None,
    ) -> bool:
        # Sensor backends occasionally report NaN/Inf while initializing.  A
        # non-finite point would poison the entire trapezoidal integral while
        # still looking like a present sample to observability checks.
        watts_value = _optional_float(watts)
        temperature_value = _optional_float(temperature_c)
        if watts_value is None and temperature_value is None:
            return False
        timestamp = time.perf_counter()
        if watts_value is not None:
            self._samples.append((timestamp, watts_value))
        if temperature_value is not None:
            self._temperature_samples.append(
                (timestamp, temperature_value, gpu_index, gpu_uuid)
            )
        self._sample_records.append({
            "t_perf": timestamp,
            "watts": watts_value,
            "temperature_c": temperature_value,
            "gpu_index": gpu_index,
            "gpu_uuid": gpu_uuid,
        })
        return True

    def _run_nvidia_smi(self):
        # One output line becomes one sample. On a multi-GPU node nvidia-smi
        # emits one line per GPU per tick, which this would average rather than
        # sum; correct on DGX Spark, where each node has a single GB10.
        ms = int(self.interval * 1000)
        cmd = ["nvidia-smi", "--query-gpu=index,uuid,power.draw,temperature.gpu",
               "--format=csv,noheader,nounits", "-lms", str(ms)]
        proc = self._spawn_proc(cmd)
        if proc is None:
            return
        for line in proc.stdout:
            if self._stop.is_set():
                break
            sample = _parse_nvidia_smi_row(line)
            if sample is None:
                continue
            self._add(**sample)

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
        proc = self._spawn_proc(["tegrastats", "--interval", str(ms)])
        if proc is None:
            return
        pat = re.compile(r"([A-Za-z0-9_]+)\s+(\d+)mW")
        for line in proc.stdout:
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
        pts = sorted(self._samples)
        if t1 <= t0 or len(pts) < 2:
            return float("nan")

        def at(boundary):
            before = next(((t, w) for t, w in reversed(pts) if t <= boundary),
                          None)
            after = next(((t, w) for t, w in pts if t >= boundary), None)
            if before is None or after is None:
                return None
            if after[0] == before[0]:
                return before[1]
            alpha = (boundary - before[0]) / (after[0] - before[0])
            return before[1] + alpha * (after[1] - before[1])

        w0, w1 = at(t0), at(t1)
        # Do not silently extrapolate beyond sampler coverage. Normal benchmark
        # windows are bracketed because sampling starts before warmup and stops
        # only after cooldown/drain.
        if w0 is None or w1 is None:
            return float("nan")
        window_pts = [(t0, w0)]
        window_pts.extend((t, w) for t, w in pts if t0 < t < t1)
        window_pts.append((t1, w1))
        e = 0.0
        for (ta, wa), (tb, wb) in zip(window_pts, window_pts[1:]):
            e += (wa + wb) / 2.0 * (tb - ta)
        return e

    def mean_power(self, t0, t1):
        energy = self.energy_joules(t0, t1)
        return energy / (t1 - t0) if t1 > t0 else float("nan")

    def sample_count(self, t0=None, t1=None):
        if t0 is None or t1 is None:
            return len(self._samples)
        return sum(1 for t, _ in self._samples if t0 <= t <= t1)

    def samples(self):
        """Return a stable copy for durable raw-observability artifacts."""
        return list(self._samples)

    def sample_records(self) -> list[dict[str, Any]]:
        """Return power and temperature observations with shared timestamps."""
        return [dict(record) for record in self._sample_records]

    def temperature_summary(self, t0: float, t1: float) -> dict[str, Any]:
        """Summarize valid per-GPU temperature readings inside a window."""
        if self.temperature_backend == "none":
            status = "disabled"
        elif self.temperature_backend != "nvidia_smi":
            status = "unsupported_backend"
        else:
            status = "sampled" if any(
                t0 <= timestamp <= t1
                for timestamp, _, _, _ in self._temperature_samples
            ) else "no_valid_samples"

        grouped: dict[tuple[str | None, str | None], list[tuple[float, float]]] = {}
        for timestamp, value, gpu_index, gpu_uuid in self._temperature_samples:
            if t0 <= timestamp <= t1:
                grouped.setdefault((gpu_index, gpu_uuid), []).append((timestamp, value))

        gpu_summaries = []
        gaps = []
        values = []
        for (gpu_index, gpu_uuid), points in sorted(
            grouped.items(), key=lambda item: (item[0][0] or "", item[0][1] or "")
        ):
            points.sort()
            gpu_values = [value for _, value in points]
            gpu_gaps = [b[0] - a[0] for a, b in zip(points, points[1:])]
            gaps.extend(gpu_gaps)
            values.extend(gpu_values)
            gpu_summaries.append({
                "gpu_index": gpu_index,
                "gpu_uuid": gpu_uuid,
                "mean_c": sum(gpu_values) / len(gpu_values),
                "max_c": max(gpu_values),
                "sample_count": len(gpu_values),
                "max_gap_s": max(gpu_gaps) if gpu_gaps else None,
            })

        return {
            "source": "nvidia_smi" if self.temperature_backend == "nvidia_smi"
            else self.temperature_backend,
            "scope": "gpu",
            "units": "C",
            "status": status,
            "mean_c": sum(values) / len(values) if values else None,
            "max_c": max(values) if values else None,
            "sample_count": len(values),
            "max_gap_s": max(gaps) if gaps else None,
            "gpus": gpu_summaries,
        }

    def wait_for_samples(self, minimum=2, timeout_s=10.0):
        """Wait for live telemetry before committing to a long release point."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.sample_count() >= minimum:
                return True
            if self._thread is not None and not self._thread.is_alive():
                break
            time.sleep(min(0.1, max(0.01, self.interval)))
        return self.sample_count() >= minimum

    def coverage(self, t0, t1):
        """Return in-window count and the largest bracketing sample gap."""
        pts = sorted(self._samples)
        before = next(((t, w) for t, w in reversed(pts) if t <= t0), None)
        after = next(((t, w) for t, w in pts if t >= t1), None)
        count = sum(1 for t, _ in pts if t0 <= t <= t1)
        if before is None or after is None:
            return {"sample_count": count, "max_gap_s": float("nan")}
        covered = [before]
        covered.extend((t, w) for t, w in pts if t0 < t < t1)
        covered.append(after)
        max_gap = max((b[0] - a[0] for a, b in zip(covered, covered[1:])),
                      default=0.0)
        return {"sample_count": count, "max_gap_s": max_gap}


# Runs inside the serving container on the head node. Pins a tiny actor to the
# worker node through the shared Ray cluster (the only remote-execution channel:
# there is no SSH path from head to worker), streams the worker GPU's power
# draw one watts value per line, and exits when stdin reaches EOF.
_WORKER_POWER_SCRIPT = r"""
import math, subprocess, sys, threading, time
import ray

ip, interval_ms = sys.argv[1], int(sys.argv[2])
ray.init(address="auto", log_to_driver=False)


@ray.remote(num_cpus=0, resources={f"node:{ip}": 0.001})
class Reader:
    def __init__(self, ms):
        self.latest = None
        self.sequence = 0
        self.proc = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._pump, args=(ms,), daemon=True)
        self.thread.start()

    def _pump(self, ms):
        if self.stop_event.is_set():
            return
        self.proc = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=index,uuid,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits", "-lms", str(ms)],
            stdout=subprocess.PIPE, text=True)
        try:
            for line in self.proc.stdout:
                if self.stop_event.is_set():
                    break
                fields = [field.strip() for field in line.strip().split(",")]
                if (len(fields) < 4 or not fields[0].isdigit()
                        or not fields[1]):
                    continue
                try:
                    watts = float(fields[2])
                    if not math.isfinite(watts):
                        watts = None
                except ValueError:
                    watts = None
                try:
                    temperature_c = float(fields[3]) if len(fields) > 3 else None
                    if temperature_c is not None and not math.isfinite(temperature_c):
                        temperature_c = None
                except ValueError:
                    temperature_c = None
                if watts is None and temperature_c is None:
                    continue
                self.sequence += 1
                # Keep the observation timestamp on the worker. Monotonic
                # clocks are not comparable across hosts, so read() computes
                # sample age before returning it to the head-side helper.
                self.latest = (
                    self.sequence, time.monotonic(), watts, temperature_c,
                    fields[0] or None, fields[1] or None,
                )
        finally:
            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)

    def read(self):
        if self.latest is None:
            return None
        sequence, observed_at, watts, temperature_c, gpu_index, gpu_uuid = self.latest
        return (sequence, time.monotonic() - observed_at, watts, temperature_c,
                gpu_index, gpu_uuid)

    def identity(self):
        node_id = ray.get_runtime_context().get_node_id()
        return ray.util.get_node_ip_address(), str(node_id)

    def stop(self):
        self.stop_event.set()
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self.thread.join(timeout=5)
        return True


reader = Reader.remote(interval_ms)
identity_ip, identity_node_id = ray.get(reader.identity.remote(), timeout=10)
print("IDENTITY", identity_ip, identity_node_id, flush=True)
stop = threading.Event()
last_sequence = 0
max_sample_age_s = max(2.0, 3.0 * interval_ms / 1000.0)


def _watch_stdin():
    sys.stdin.read()          # EOF when the head-side sampler closes stdin
    stop.set()


threading.Thread(target=_watch_stdin, daemon=True).start()
try:
    while not stop.is_set():
        try:
            sample = ray.get(reader.read.remote(), timeout=2)
            if sample is not None:
                sequence, age_s, watts, temperature_c, gpu_index, gpu_uuid = sample
                # Emit each source observation once. If nvidia-smi exits or
                # stalls, its cached last value cannot masquerade as fresh data.
                if sequence > last_sequence:
                    last_sequence = sequence
                    if 0.0 <= age_s <= max_sample_age_s:
                        values = (sequence, age_s, watts, temperature_c,
                                  gpu_index, gpu_uuid)
                        print("SAMPLE", *values, sep="\t", flush=True)
        except Exception:
            pass
        time.sleep(interval_ms / 1000.0)
finally:
    try:
        ray.get(reader.stop.remote(), timeout=25)
    except Exception:
        pass
    ray.kill(reader)
"""


class WorkerPowerSampler(PowerSampler):
    """Sample the worker node's GPU power through the shared Ray cluster.

    A helper process is docker-exec'ed into the head node's serving container;
    it pins an actor to the worker node (Ray's node:<ip> custom resource), polls
    nvidia-smi there, and prints a source sequence, source age, and watts. Each
    source observation is accepted at most once and only while fresh, then is
    timestamped on receipt with the head's perf_counter so it aligns with the
    request records' clock. A stopped remote producer therefore creates a gap
    instead of replaying its final value as fabricated coverage.

    If Ray, the container, or the worker is unreachable, the sampler yields no
    samples and mean_power/energy_joules return NaN. Exploratory summaries can
    retain that missing value; release configurations reject incomplete power
    coverage before writing a paper result.
    """

    def __init__(self, container, node_ip, interval_ms=1000):
        super().__init__(backend="none", interval_ms=interval_ms)
        self.temperature_backend = "nvidia_smi"
        self.container = container
        self.node_ip = node_ip
        self._last_sequence = 0
        self._max_sample_age_s = max(2.0, 3.0 * self.interval)
        self.identity_ip = ""
        self.identity_node_id = ""

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

    def _consume_line(self, line):
        """Accept one fresh worker observation and reject replayed/stale data."""
        if line.startswith("IDENTITY "):
            identity = line.strip().split()
            if len(identity) != 3:
                return False
            self.identity_ip = identity[1]
            self.identity_node_id = identity[2]
            return True
        parts = line.strip().split("\t")
        if len(parts) == 7 and parts[0] == "SAMPLE":
            try:
                sequence = int(parts[1])
                age_s = float(parts[2])
            except ValueError:
                return False
            watts = _optional_float(parts[3])
            temperature_c = _optional_float(parts[4])
            gpu_index = None if parts[5] == "None" else parts[5]
            gpu_uuid = None if parts[6] == "None" else parts[6]
        else:
            # Keep the prior helper protocol readable for diagnostic fixtures.
            legacy_parts = line.strip().split()
            if len(legacy_parts) != 3:
                return False
            try:
                sequence = int(legacy_parts[0])
                age_s = float(legacy_parts[1])
            except ValueError:
                return False
            watts = _optional_float(legacy_parts[2])
            temperature_c = None
            gpu_index = gpu_uuid = None
        if sequence <= self._last_sequence:
            return False
        # Advance even for a stale observation so replaying it later cannot
        # make an old source value look fresh merely because head time advanced.
        self._last_sequence = sequence
        if not math.isfinite(age_s) or not 0.0 <= age_s <= self._max_sample_age_s:
            return False
        before = len(self._samples)
        accepted = self._add(watts, temperature_c, gpu_index, gpu_uuid)
        return accepted or len(self._samples) > before

    def wait_for_identity(self, timeout_s=10.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.identity_ip and self.identity_node_id:
                return True
            if self._proc is not None and self._proc.poll() is not None:
                break
            time.sleep(0.05)
        return bool(self.identity_ip and self.identity_node_id)

    def _consume(self):
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            self._consume_line(line)

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.stdin.close()   # EOF stops the remote script cleanly
            except Exception:
                pass
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        self._proc.kill()
                        self._proc.wait(timeout=3)
                    except Exception:
                        pass
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

class VLLMMetricsScraper:
    # Gauges: instantaneous occupancy/queue state, summarized as min/mean/max.
    GAUGE_KEYS = [
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
        "vllm:kv_cache_usage_perc",
    ]
    # Counters: monotonic totals of server-side work, summarized as the delta
    # over the measurement window. This includes server-processed tokens from
    # requests that do not complete inside the window.
    COUNTER_KEYS = [
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
        "vllm:prompt_tokens_cached_total",
        "vllm:num_preemptions_total",
    ]
    KEYS = GAUGE_KEYS + COUNTER_KEYS
    # Every series copied into release index columns is mandatory except cached
    # prompt tokens, which some vLLM builds omit when prefix caching is disabled.
    # Keeping this policy next to the scrape list prevents the strict validator
    # and index schema from drifting apart.
    REQUIRED_RELEASE_STATS = {
        "vllm:num_requests_running": "mean",
        "vllm:num_requests_waiting": "mean",
        "vllm:kv_cache_usage_perc": "mean",
        "vllm:prompt_tokens_total": "delta",
        "vllm:generation_tokens_total": "delta",
        "vllm:num_preemptions_total": "delta",
    }
    OPTIONAL_RELEASE_KEYS = {"vllm:prompt_tokens_cached_total"}
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
        self._label_sets = {k: set() for k in self.KEYS}
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
            # Prometheus permits NaN and +/-Inf, but those values mean the
            # instrument is unavailable for these vLLM gauges/counters.  Do
            # not let them satisfy the benchmark's data-availability checks.
            if not math.isfinite(value):
                continue
            key = self.METRIC_ALIASES.get(raw_key, raw_key)
            metric_token = line.strip().split(None, 1)[0]
            label_text = metric_token[len(raw_key):]
            self._label_sets[key].add(label_text)
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
            pts = sorted((t, v) for (t, v) in self._series[k] if t0 <= t <= t1)
            vals = [v for _, v in pts]
            gaps = [b[0] - a[0] for a, b in zip(pts, pts[1:])]
            out[k] = ({
                "min": min(vals), "mean": sum(vals) / len(vals), "max": max(vals),
                "sample_count": len(vals),
                "sample_span_s": (pts[-1][0] - pts[0][0]) if len(pts) >= 2 else 0.0,
                "max_gap_s": max(gaps, default=0.0),
                "series_count": len(self._label_sets[k]),
            } if vals else {
                "min": None, "mean": None, "max": None,
                "sample_count": 0, "sample_span_s": None, "max_gap_s": None,
                "series_count": len(self._label_sets[k]),
            })
        for k in self.COUNTER_KEYS:
            out[k] = self._counter_delta(self._series[k], t0, t1)
            out[k]["series_count"] = len(self._label_sets[k])
        return out

    def wait_for_release_metrics(self, timeout_s=10.0):
        """Require every release metric to exist as one unambiguous series."""
        deadline = time.monotonic() + timeout_s
        required = set(self.REQUIRED_RELEASE_STATS)
        while time.monotonic() < deadline:
            self.scrape_once()
            if all(self._series[name] for name in required):
                ambiguous = {
                    name: len(self._label_sets[name])
                    for name in required if len(self._label_sets[name]) != 1
                }
                if ambiguous:
                    raise RuntimeError(
                        "required vLLM metrics expose ambiguous label series: "
                        + ", ".join(f"{name}={count}"
                                    for name, count in sorted(ambiguous.items()))
                    )
                return True
            time.sleep(min(0.25, self.interval))
        return False

    def raw_samples(self):
        """Return timestamp/value samples and observed label identities."""
        return {
            key: {
                "labels": sorted(self._label_sets[key]),
                "samples": list(self._series[key]),
            }
            for key in self.KEYS
        }

    @staticmethod
    def _counter_delta(pts, t0, t1):
        """Work a monotonic counter accumulated across the [t0, t1] window.

        Baselines on the last sample at or before the window start and ends on
        the last sample at or before the window end.
        """
        # scrape_once() can be called concurrently with the background scrape
        # (the benchmark does this to seed a baseline), so do not depend on
        # append order here.
        pts = sorted(pts)
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
            return {"delta": None, "rate_per_s": None, "sample_count": 0,
                    "sample_span_s": None, "max_gap_s": None}
        # Checking only end < base misses a reset that recovers past the old
        # baseline before window end.  Any observed decrease makes the whole
        # interval unusable as one monotonic counter delta.
        covered = [(t, v) for t, v in pts if base[0] <= t <= end[0]]
        if any(vb < va for (_, va), (_, vb) in zip(covered, covered[1:])):
            return {"delta": None, "rate_per_s": None,
                    "sample_count": len(covered),
                    "sample_span_s": end[0] - base[0],
                    "max_gap_s": max((b[0] - a[0]
                                      for a, b in zip(covered, covered[1:])),
                                     default=0.0)}
        delta = end[1] - base[1]
        span = end[0] - base[0]
        return {
            "delta": delta,
            "rate_per_s": (delta / span) if span > 0 else None,
            "sample_count": len(covered),
            "sample_span_s": span,
            "max_gap_s": max((b[0] - a[0]
                              for a, b in zip(covered, covered[1:])),
                             default=0.0),
        }
