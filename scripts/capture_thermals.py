#!/usr/bin/env python3
"""Write timestamped output from ``sensors`` for benchmark and canary runs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import FrameType
from typing import TextIO


DEFAULT_INTERVAL_SECONDS: float = 1.0
SENSOR_COMMAND_TIMEOUT_SECONDS: float = 5.0
SSH_CONNECT_TIMEOUT_SECONDS: int = 5
SENSOR_STOP_GRACE_SECONDS: float = 1.0
REMOTE_STOP_TIMEOUT_SECONDS: float = 6.0
REMOTE_TERMINATE_TIMEOUT_SECONDS: float = 3.0
SUPERVISION_POLL_INTERVAL_SECONDS: float = 0.2
SSH_TARGET_PATTERN: re.Pattern[str] = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._:-]*$"
)
REMOTE_SENSOR_LOOP: str = r'''
import datetime
import json
import subprocess
import sys
import threading
import time

interval_seconds: float = float(sys.argv[1])
sensor_timeout_seconds: float = float(sys.argv[2])
stop_event: threading.Event = threading.Event()


def watch_stdin() -> None:
    sys.stdin.read()
    stop_event.set()


threading.Thread(target=watch_stdin, daemon=True).start()
while not stop_event.is_set():
    started_at = time.monotonic()
    captured_at_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    try:
        result = subprocess.run(
            ["sensors"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=sensor_timeout_seconds
        )
        sample = {
            "captured_at_utc": captured_at_utc,
            "return_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": None,
        }
    except FileNotFoundError:
        sample = {
            "captured_at_utc": captured_at_utc, "return_code": None,
            "stdout": "", "stderr": "", "error": "sensors command not found",
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        sample = {
            "captured_at_utc": captured_at_utc, "return_code": None,
            "stdout": "", "stderr": "", "error": str(error),
        }
    print(json.dumps(sample, ensure_ascii=False), flush=True)
    if sample["error"] == "sensors command not found":
        break
    stop_event.wait(max(0.0, interval_seconds - (time.monotonic() - started_at)))
'''


def timestamp_utc() -> str:
    """Return an ISO 8601 timestamp with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def write_capture(
    output: TextIO,
    output_lock: threading.Lock,
    *,
    captured_at_utc: str,
    node_role: str,
    node_ip: str | None,
    stdout: str,
    stderr: str,
    return_code: int | None,
    error: str | None,
) -> None:
    """Append one readable, timestamped sensor-command result."""
    identity = f"node={node_role} ip={node_ip or 'unknown'}"
    with output_lock:
        output.write(f"=== {captured_at_utc} | {identity} | sensors ===\n")
        if stdout.strip():
            output.write(stdout.rstrip() + "\n")
        else:
            output.write("[sensors produced no output]\n")
        if stderr.strip():
            output.write("stderr:\n" + stderr.rstrip() + "\n")
        if return_code not in (None, 0):
            output.write(f"[sensors exited with status {return_code}]\n")
        if error:
            output.write(f"[sensor capture error: {error}]\n")
        output.write("\n")
        output.flush()


def capture_local_sensors(
    output: TextIO,
    output_lock: threading.Lock,
    stop_event: threading.Event,
    *,
    node_ip: str | None,
    interval_seconds: float,
) -> None:
    """Periodically capture the local host's sensors output."""
    while not stop_event.is_set():
        started_at = time.monotonic()
        captured_at_utc = timestamp_utc()
        try:
            result = subprocess.run(
                ["sensors"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False,
                timeout=SENSOR_COMMAND_TIMEOUT_SECONDS,
            )
            stdout = result.stdout
            stderr = result.stderr
            return_code: int | None = result.returncode
            error: str | None = None
        except FileNotFoundError:
            stdout, stderr, return_code = "", "", None
            error = "sensors command not found"
        except (OSError, subprocess.TimeoutExpired) as capture_error:
            stdout, stderr, return_code = "", "", None
            error = str(capture_error)
        write_capture(
            output,
            output_lock,
            captured_at_utc=captured_at_utc,
            node_role="head",
            node_ip=node_ip,
            stdout=stdout,
            stderr=stderr,
            return_code=return_code,
            error=error,
        )
        if error == "sensors command not found":
            return
        stop_event.wait(max(0.0, interval_seconds - (time.monotonic() - started_at)))


def remote_command(interval_seconds: float) -> str:
    """Build a quoted remote command for the worker-side sensor loop."""
    return (
        "python3 -u -c " + shlex.quote(REMOTE_SENSOR_LOOP) + " "
        + shlex.quote(str(interval_seconds)) + " "
        + shlex.quote(str(SENSOR_COMMAND_TIMEOUT_SECONDS))
    )


def capture_worker_sensors(
    process: subprocess.Popen[str],
    output: TextIO,
    output_lock: threading.Lock,
    *,
    node_ip: str | None,
) -> None:
    """Read timestamped worker sensor output from its persistent SSH stream."""
    if process.stdout is None:
        return
    for line in process.stdout:
        try:
            sample: object = json.loads(line)
        except json.JSONDecodeError:
            write_capture(
                output,
                output_lock,
                captured_at_utc=timestamp_utc(),
                node_role="worker",
                node_ip=node_ip,
                stdout="",
                stderr=line.rstrip(),
                return_code=None,
                error="invalid worker sensor record",
            )
            continue
        if not isinstance(sample, dict):
            continue
        write_capture(
            output,
            output_lock,
            captured_at_utc=str(sample.get("captured_at_utc") or timestamp_utc()),
            node_role="worker",
            node_ip=node_ip,
            stdout=str(sample.get("stdout") or ""),
            stderr=str(sample.get("stderr") or ""),
            return_code=(sample.get("return_code")
                         if isinstance(sample.get("return_code"), int) else None),
            error=(str(sample["error"]) if sample.get("error") else None),
        )
        if sample.get("error") == "sensors command not found":
            return


def parse_arguments() -> argparse.Namespace:
    """Parse sensor capture options."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--head-ip", default="")
    parser.add_argument("--worker-node-ip", default="")
    parser.add_argument("--worker-ssh-target", default="")
    parser.add_argument(
        "--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS
    )
    arguments = parser.parse_args()
    if (not math.isfinite(arguments.interval_seconds)
            or arguments.interval_seconds <= 0):
        parser.error("--interval-seconds must be a finite positive number")
    if (arguments.worker_ssh_target
            and SSH_TARGET_PATTERN.fullmatch(arguments.worker_ssh_target) is None):
        parser.error("--worker-ssh-target contains unsupported characters")
    return arguments


def main() -> int:
    """Capture local and optional worker sensors until the process stops."""
    arguments = parse_arguments()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event()
    output_lock = threading.Lock()

    def request_stop(_signal_number: int, _frame: FrameType | None) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    with arguments.output.open("x", encoding="utf-8", buffering=1) as output:
        output.write(
            f"# Command: sensors\n# Interval: {arguments.interval_seconds:g}s\n"
            "# Each block contains the command output captured on its host.\n\n"
        )
        local_thread = threading.Thread(
            target=capture_local_sensors,
            args=(output, output_lock, stop_event),
            kwargs={
                "node_ip": arguments.head_ip or None,
                "interval_seconds": arguments.interval_seconds,
            },
            daemon=True,
        )
        local_thread.start()

        worker_process: subprocess.Popen[str] | None = None
        worker_thread: threading.Thread | None = None
        if arguments.worker_ssh_target:
            ssh_command = [
                "ssh", "-T", "-o", "BatchMode=yes", "-o",
                "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no", "-o",
                f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}", "-o",
                "ConnectionAttempts=1", arguments.worker_ssh_target,
                remote_command(arguments.interval_seconds),
            ]
            try:
                worker_process = subprocess.Popen(
                    ssh_command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as error:
                write_capture(
                    output,
                    output_lock,
                    captured_at_utc=timestamp_utc(),
                    node_role="worker",
                    node_ip=arguments.worker_node_ip or None,
                    stdout="",
                    stderr="",
                    return_code=None,
                    error=f"could not start ssh sensor capture: {error}",
                )
            else:
                worker_thread = threading.Thread(
                    target=capture_worker_sensors,
                    args=(worker_process, output, output_lock),
                    kwargs={"node_ip": arguments.worker_node_ip or None},
                    daemon=True,
                )
                worker_thread.start()
        elif arguments.worker_node_ip:
            write_capture(
                output,
                output_lock,
                captured_at_utc=timestamp_utc(),
                node_role="worker",
                node_ip=arguments.worker_node_ip,
                stdout="",
                stderr="",
                return_code=None,
                error="worker SSH target was not configured",
            )

        try:
            while not stop_event.wait(SUPERVISION_POLL_INTERVAL_SECONDS):
                if not local_thread.is_alive() and (
                    worker_thread is None or not worker_thread.is_alive()
                ):
                    break
        finally:
            stop_event.set()
            if worker_process is not None:
                if worker_process.stdin is not None:
                    try:
                        worker_process.stdin.close()
                    except OSError:
                        pass
                try:
                    worker_process.wait(timeout=REMOTE_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    worker_process.terminate()
                    try:
                        worker_process.wait(
                            timeout=REMOTE_TERMINATE_TIMEOUT_SECONDS
                        )
                    except subprocess.TimeoutExpired:
                        worker_process.kill()
                        worker_process.wait(
                            timeout=REMOTE_TERMINATE_TIMEOUT_SECONDS
                        )
                if worker_process.stderr is not None:
                    stderr = worker_process.stderr.read().strip()
                    if stderr:
                        write_capture(
                            output,
                            output_lock,
                            captured_at_utc=timestamp_utc(),
                            node_role="worker",
                            node_ip=arguments.worker_node_ip or None,
                            stdout="",
                            stderr=stderr,
                            return_code=worker_process.returncode,
                            error="ssh sensor stream ended",
                        )
            local_thread.join(
                timeout=SENSOR_COMMAND_TIMEOUT_SECONDS + SENSOR_STOP_GRACE_SECONDS
            )
            if worker_thread is not None:
                worker_thread.join(timeout=REMOTE_TERMINATE_TIMEOUT_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
