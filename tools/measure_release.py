#!/usr/bin/env python3
"""Bounded, privacy-minimized release measurements for PorkyHUD.

This source-tree tool intentionally exposes only fixed PorkyHUD operations.  It is
not a general command runner, terminal recorder, or runtime dependency.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import platform
import pty
import re
import select
import selectors
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "porkyhud.py"
CORE = ROOT / "porkyhud_core.py"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
TERMINATE_GRACE_S = 0.5
REAP_DEADLINE_S = 2.0
DEFAULT_RUNS = 20
DEFAULT_STARTUP_TIMEOUT_S = 5.0
DEFAULT_SNAPSHOT_TIMEOUT_S = 15.0
DEFAULT_WARMUP_S = 30
DEFAULT_LIVE_SECONDS = 300
DEFAULT_SOAK_SECONDS = 1800
DEFAULT_ITERATIONS = 10_000
DEFAULT_WARMUP_ITERATIONS = 1_000
LIVE_LAUNCH_RECORD_PATH_ENV = "PORKYHUD_MEASURE_LAUNCH_RECORD"
LIVE_THREAD_RECORD_PATH_ENV = "PORKYHUD_MEASURE_THREAD_RECORD"
LIVE_WALL_ORIGIN_NS_ENV = "PORKYHUD_MEASURE_WALL_ORIGIN_NS"
LIVE_MONO_ORIGIN_S_ENV = "PORKYHUD_MEASURE_MONO_ORIGIN_S"
_THREAD_PREFIXES = ("SourcePoller", "AsyncPoller", "PorkyHUD-command")
_THREAD_EVENT_CATEGORIES = _THREAD_PREFIXES + ("Other",)
STARTUP_MARKERS = (
    "CHECKING 0/4 core sources",
    "Loading system readings.",
    "CPU CHECKING",
    "MEM CHECKING",
    "THERM CHECKING",
    "DISK CHECKING",
)
_CPU_EVIDENCE = re.compile(r"CPU\s+(?:[0-9]+%|[0-9]+\.[0-9]+%)")
_MEMORY_EVIDENCE = re.compile(r"MEM\s+(?:NORMAL|ELEVATED|CRITICAL)")
_THERMAL_EVIDENCE = re.compile(r"THERM\s+(?:CLEAR|WARN(?:\s+T[0-9]+/P[0-9]+)?)")
_ALLOWED_ENVIRONMENT_KEYS = frozenset({
    "architecture", "chip", "logical_cpus", "macos", "model", "python",
    "ram_bytes",
})
_PRIVATE_KEYS = frozenset({
    "argv", "command", "commands", "cwd", "home", "hostname", "path",
    "pid", "pids", "ppid", "process", "processes", "pty", "raw",
    "screen", "stderr", "stdin", "stdout", "stream", "streams", "user",
    "username",
})
_PRIVATE_KEY_PARTS = frozenset({
    "argv", "command", "cwd", "home", "host", "hostname", "path", "pid",
    "process", "pty", "raw", "screen", "stderr", "stdin", "stdout",
    "stream", "user", "username",
})
_FORBIDDEN_ARGUMENT_TEXT = (
    "--setup-sensors", "--install-sensor-access", "--remove-sensor-access",
    "--sensor-access-status",
)
_FORBIDDEN_EXECUTABLE_NAMES = frozenset({
    "bash", "csh", "dash", "doas", "fish", "ksh", "osascript", "pkexec",
    "sh", "sudo", "tcsh", "visudo", "zsh",
})
_SNAPSHOT_TOP_LEVEL = frozenset({
    "generated_at", "read", "system", "cpu", "memory", "battery",
    "sensors", "disk", "processes",
})
_SNAPSHOT_SECRET_PATTERN = re.compile(
    r"(?i)(?:--?|/)(?:api[-_]?key|access[-_]?token|auth(?:orization)?|"
    r"password|passwd|secret|token)(?:=|\s+)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)",
)


class MeasurementError(RuntimeError):
    """A bounded probe failed without exposing private child output."""


@dataclass(frozen=True)
class BoundedResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_ms: float


@dataclass(frozen=True)
class BatchAuthority:
    ac_state: str
    wall_s: float
    mono_s: float
    load: float


def _finite_number(value: Any, name: str = "value") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a finite number" % name)
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("%s must be a finite number" % name)
    return result


def _aligned_event_time(
    event_wall_ns: int, origin_wall_ns: int, origin_mono_s: float,
) -> float:
    """Project a child wall-clock event into the parent's monotonic domain."""

    if any(isinstance(value, bool) or not isinstance(value, int)
           for value in (event_wall_ns, origin_wall_ns)):
        raise ValueError("event wall clocks must be integer nanoseconds")
    origin = _finite_number(origin_mono_s, "origin_mono_s")
    return origin + (event_wall_ns - origin_wall_ns) / 1_000_000_000.0


@contextlib.contextmanager
def _repository_import_path() -> Iterable[None]:
    """Temporarily expose only this tool's validated repository root."""

    root = ROOT.resolve()
    required = (APP, CORE, ROOT / "tests" / "test_core.py",
                ROOT / "tests" / "test_porkyhud.py")
    for path in required:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise MeasurementError("required benchmark source is unavailable") from None
        try:
            contained = os.path.commonpath((str(root), str(resolved))) == str(root)
        except ValueError:
            contained = False
        if not contained or not resolved.is_file():
            raise MeasurementError("required benchmark source is outside the repository")
    root_text = str(root)
    inserted = not sys.path or sys.path[0] != root_text
    if inserted:
        sys.path.insert(0, root_text)
    try:
        yield
    finally:
        if inserted:
            try:
                sys.path.remove(root_text)
            except ValueError:
                pass


def _require_repository_module(module: Any, relative: str) -> Any:
    expected = (ROOT / relative).resolve()
    module_file = getattr(module, "__file__", None)
    if module_file is None or Path(module_file).resolve() != expected:
        raise MeasurementError("benchmark import did not resolve to repository source")
    return module


def nearest_rank(values: Sequence[float], quantile: float) -> float:
    """Return a nearest-rank quantile using ceil(q*n)-1 indexing."""

    if not values:
        raise ValueError("values must not be empty")
    q_value = _finite_number(quantile, "quantile")
    if q_value <= 0.0 or q_value > 1.0:
        raise ValueError("quantile must be greater than zero and at most one")
    ordered = sorted(_finite_number(value) for value in values)
    return ordered[int(math.ceil(q_value * len(ordered))) - 1]


def summarize(values: Sequence[float]) -> Dict[str, Any]:
    """Return exact finite summary statistics used by the release record."""

    if not values:
        raise ValueError("values must not be empty")
    checked = [_finite_number(value) for value in values]
    return {
        "count": len(checked),
        "maximum": max(checked),
        "mean": statistics.fmean(checked),
        "median": statistics.median(checked),
        "minimum": min(checked),
        "p95": nearest_rank(checked, 0.95),
    }


def _public_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("public measurement values must be finite")
        return value
    if isinstance(value, Mapping):
        return sanitize_public(value)
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    raise ValueError("unsupported public measurement value")


def sanitize_public(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively remove process, host-identity, and raw-artifact material."""

    if not isinstance(payload, Mapping):
        raise ValueError("public payload must be a mapping")
    sanitized: Dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise ValueError("public payload keys must be strings")
        lowered = key.casefold().replace("-", "_")
        key_parts = frozenset(part for part in lowered.split("_") if part)
        if ((lowered in _PRIVATE_KEYS or lowered.endswith("_pid")
             or key_parts.intersection(_PRIVATE_KEY_PARTS))
                and key not in _THREAD_PREFIXES):
            continue
        if lowered == "environment":
            if not isinstance(value, Mapping):
                raise ValueError("environment must be a mapping")
            sanitized[key] = {
                env_key: _public_value(env_value)
                for env_key, env_value in value.items()
                if isinstance(env_key, str) and env_key in _ALLOWED_ENVIRONMENT_KEYS
            }
            continue
        sanitized[key] = _public_value(value)
    return sanitized


def _validate_argv(argv: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError("argv must be a non-empty sequence")
    checked = []
    for value in argv:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("argv entries must be non-empty strings")
        if any(forbidden in value for forbidden in _FORBIDDEN_ARGUMENT_TEXT):
            raise ValueError("privileged PorkyHUD operations are forbidden")
        checked.append(value)
    executable_name = Path(checked[0]).name.casefold()
    if executable_name in _FORBIDDEN_EXECUTABLE_NAMES:
        raise ValueError("shell and privilege executables are forbidden")
    return tuple(checked)


def _validate_timeout(timeout_s: float) -> float:
    timeout = _finite_number(timeout_s, "timeout_s")
    if timeout <= 0.0 or timeout > 3600.0:
        raise ValueError("timeout_s must be greater than zero and at most 3600")
    return timeout


def _signal_process_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    except OSError as error:
        if error.errno != errno.ESRCH:
            raise


def _process_group_exists(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        raise
    return True


def _cleanup_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate the child session, prove group absence, and reap its leader."""

    try:
        group_exists = _process_group_exists(process)
    except OSError:
        raise MeasurementError("child process-group inspection failed") from None
    if group_exists:
        _signal_process_group(process, signal.SIGTERM)
    if process.poll() is None:
        try:
            process.wait(timeout=TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
    try:
        if _process_group_exists(process):
            _signal_process_group(process, signal.SIGKILL)
    except OSError:
        raise MeasurementError("child process-group inspection failed") from None
    deadline = time.monotonic() + REAP_DEADLINE_S
    while True:
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.05, max(0.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
            except InterruptedError:
                pass
        try:
            group_exists = _process_group_exists(process)
        except OSError:
            raise MeasurementError("child process-group inspection failed") from None
        if process.poll() is not None and not group_exists:
            return
        if time.monotonic() >= deadline:
            raise MeasurementError("child cleanup exceeded its deadline")
        if group_exists:
            _signal_process_group(process, signal.SIGKILL)
        time.sleep(0.01)


def _bounded_communicate(
    process: subprocess.Popen[bytes], timeout_s: float,
) -> Tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise MeasurementError("bounded operation did not expose private pipes")
    streams = (process.stdout, process.stderr)
    buffers = (bytearray(), bytearray())
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout_s
    try:
        for index, stream in enumerate(streams):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise MeasurementError("bounded operation timed out")
            events = selector.select(min(0.1, remaining)) if selector.get_map() else ()
            for key, _mask in events:
                index = key.data
                buffer = buffers[index]
                allowance = max(1, min(65536, MAX_CAPTURE_BYTES + 1 - len(buffer)))
                try:
                    chunk = os.read(key.fileobj.fileno(), allowance)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer.extend(chunk)
                if len(buffer) > MAX_CAPTURE_BYTES:
                    raise MeasurementError("bounded operation exceeded its capture limit")
        return bytes(buffers[0]), bytes(buffers[1])
    finally:
        selector.close()


def run_bounded(
    argv: Sequence[str], *, timeout_s: float, cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> BoundedResult:
    """Run one explicit argv without a shell in a separately killable session."""

    checked = _validate_argv(argv)
    timeout = _validate_timeout(timeout_s)
    working = None if cwd is None else str(Path(cwd).resolve())
    child_env = None
    if env is not None:
        child_env = {str(key): str(value) for key, value in env.items()}
    started = time.monotonic()
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        process = subprocess.Popen(
            checked,
            cwd=working,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        stdout, stderr = _bounded_communicate(process, timeout)
        _cleanup_process(process)
        return BoundedResult(
            process.returncode,
            stdout,
            stderr,
            (time.monotonic() - started) * 1000.0,
        )
    except BaseException as error:
        if process is not None:
            try:
                _cleanup_process(process)
            except BaseException:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise error
                raise MeasurementError("bounded operation cleanup could not complete") from None
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, MeasurementError):
            raise
        raise MeasurementError("bounded operation could not complete") from None
    finally:
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


@contextlib.contextmanager
def private_workspace() -> Iterable[Path]:
    """Create auto-removed artifacts with an owner-only creation mask."""

    previous = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="porkyhud-measure-") as temporary:
            yield Path(temporary)
    finally:
        os.umask(previous)


def _text(result: BoundedResult) -> str:
    try:
        return result.stdout.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        raise MeasurementError("environment probe returned undecodable output") from None


def _probe_text(argv: Sequence[str], timeout_s: float = 3.0) -> Optional[str]:
    try:
        result = run_bounded(argv, timeout_s=timeout_s, cwd=ROOT)
    except MeasurementError:
        return None
    if result.returncode != 0:
        return None
    value = _text(result)
    return value or None


def collect_environment() -> Dict[str, Any]:
    """Return allowlisted support evidence, never user or machine identity."""

    model = _probe_text(("/usr/sbin/sysctl", "-n", "hw.model"))
    chip = _probe_text(("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"))
    ram = _probe_text(("/usr/sbin/sysctl", "-n", "hw.memsize"))
    cpus = _probe_text(("/usr/sbin/sysctl", "-n", "hw.logicalcpu"))
    macos = _probe_text(("/usr/bin/sw_vers", "-productVersion"))
    result: Dict[str, Any] = {
        "architecture": platform.machine() or "unknown",
        "chip": chip or platform.processor() or "unknown",
        "logical_cpus": int(cpus) if cpus and cpus.isdigit() else (os.cpu_count() or 0),
        "macos": macos or platform.mac_ver()[0] or "unknown",
        "model": model or "unknown",
        "python": platform.python_version(),
        "ram_bytes": int(ram) if ram and ram.isdigit() else 0,
    }
    return result


def _capture_batch_authority() -> BatchAuthority:
    power_text = _probe_text(("/usr/bin/pmset", "-g", "batt")) or ""
    if "AC Power" in power_text:
        ac_state = "AC"
    elif "Battery Power" in power_text:
        ac_state = "BATTERY"
    else:
        ac_state = "UNKNOWN"
    try:
        load = float(os.getloadavg()[0])
    except (AttributeError, OSError):
        load = 0.0
    return BatchAuthority(
        ac_state, time.time(), time.monotonic(), _finite_number(load, "load"),
    )


def _finish_batch_guard(
    initial: BatchAuthority, final: BatchAuthority,
) -> Dict[str, Any]:
    if not isinstance(initial, BatchAuthority) or not isinstance(final, BatchAuthority):
        raise ValueError("batch authority endpoints are required")
    if initial.ac_state != final.ac_state:
        raise MeasurementError("batch power source changed")
    wall_elapsed = final.wall_s - initial.wall_s
    mono_elapsed = final.mono_s - initial.mono_s
    drift = wall_elapsed - mono_elapsed
    if wall_elapsed < 0.0 or mono_elapsed < 0.0 or abs(drift) > 2.0:
        raise MeasurementError("batch sleep or clock discontinuity detected")
    return {
        "ac_state": initial.ac_state,
        "final_load": _finite_number(final.load, "final load"),
        "initial_load": _finite_number(initial.load, "initial load"),
        "wall_mono_drift_s": drift,
    }


def _base_payload(mode: str, category: str) -> Dict[str, Any]:
    return {
        "category": category,
        "environment": collect_environment(),
        "mode": mode,
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "measured",
    }


class _VisibleScreen:
    """Small VT projection sufficient for PorkyHUD's bounded startup markers."""

    _CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")

    def __init__(self, rows: int = 24, columns: int = 80) -> None:
        self.rows = rows
        self.columns = columns
        self.cells = [[" "] * columns for _ in range(rows)]
        self.row = 0
        self.column = 0
        self._pending = ""

    def _clear(self, mode: int) -> None:
        if mode in (2, 3):
            self.cells = [[" "] * self.columns for _ in range(self.rows)]
            self.row = self.column = 0
        elif mode == 0:
            for row in range(self.row, self.rows):
                start = self.column if row == self.row else 0
                for column in range(start, self.columns):
                    self.cells[row][column] = " "

    def _erase_line(self, mode: int) -> None:
        if mode == 2:
            start, end = 0, self.columns
        elif mode == 1:
            start, end = 0, min(self.column + 1, self.columns)
        else:
            start, end = self.column, self.columns
        for column in range(start, end):
            self.cells[self.row][column] = " "

    def _csi(self, values: str, command: str) -> None:
        if values.startswith("?"):
            return
        parsed = [int(value) if value else 0 for value in values.split(";")]
        first = parsed[0] if parsed else 0
        amount = first or 1
        if command in ("H", "f"):
            row = (parsed[0] if parsed and parsed[0] else 1) - 1
            column = (parsed[1] if len(parsed) > 1 and parsed[1] else 1) - 1
            self.row = min(max(row, 0), self.rows - 1)
            self.column = min(max(column, 0), self.columns - 1)
        elif command == "A":
            self.row = max(0, self.row - amount)
        elif command == "B":
            self.row = min(self.rows - 1, self.row + amount)
        elif command == "C":
            self.column = min(self.columns - 1, self.column + amount)
        elif command == "D":
            self.column = max(0, self.column - amount)
        elif command == "G":
            self.column = min(self.columns - 1, max(0, amount - 1))
        elif command == "d":
            self.row = min(self.rows - 1, max(0, amount - 1))
        elif command == "J":
            self._clear(first)
        elif command == "K":
            self._erase_line(first)

    def feed(
        self, data: bytes, observer: Optional[Callable[[], None]] = None,
    ) -> None:
        text = self._pending + data.decode("utf-8", "replace")
        self._pending = ""
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\x1b":
                match = self._CSI.match(text, index)
                if match:
                    self._csi(match.group(1), match.group(2))
                    if observer is not None:
                        observer()
                    index = match.end()
                    continue
                if index + 1 < len(text) and text[index + 1] in "()#%*+":
                    if index + 2 >= len(text):
                        self._pending = text[index:]
                        return
                    index += 3
                    continue
                if index + 1 == len(text) or (text[index + 1] == "[" and not re.search(
                        r"[A-Za-z]", text[index + 2:])):
                    self._pending = text[index:]
                    return
                index += 2
                continue
            if char == "\r":
                self.column = 0
            elif char == "\n":
                self.row = min(self.rows - 1, self.row + 1)
            elif char == "\b":
                self.column = max(0, self.column - 1)
            elif char >= " ":
                if self.column < self.columns:
                    self.cells[self.row][self.column] = char
                    self.column += 1
            if observer is not None:
                observer()
            index += 1

    def text(self) -> str:
        return "\n".join("".join(row).rstrip() for row in self.cells)


def _set_pty_size(fd: int, rows: int, columns: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def _claim_pty_session() -> None:
    """Create the child session and make redirected stdin its controlling PTY."""

    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _quit_pty_process(
    process: subprocess.Popen[bytes], master_fd: int, timeout_s: float = 5.0,
) -> None:
    """Send the documented quit key while continuing to drain terminal output."""

    try:
        os.write(master_fd, b"q")
    except OSError:
        raise MeasurementError("PTY quit key could not be sent") from None
    deadline = time.monotonic() + _validate_timeout(timeout_s)
    while process.poll() is None and time.monotonic() < deadline:
        readable, _, _ = select.select((master_fd,), (), (), 0.05)
        if not readable:
            continue
        try:
            os.read(master_fd, 65536)
        except BlockingIOError:
            continue
        except OSError as error:
            if error.errno != errno.EIO:
                raise MeasurementError("PTY drain failed during quit") from None
            break
    try:
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise MeasurementError("PTY probe did not exit after quit") from None
    returncode = process.returncode
    try:
        residual_group = _process_group_exists(process)
    except OSError:
        raise MeasurementError("PTY process-group inspection failed") from None
    _cleanup_process(process)
    if residual_group:
        raise MeasurementError("PTY probe left a residual process group")
    if returncode != 0:
        raise MeasurementError("PTY probe exited unsuccessfully")


def _startup_once(timeout_s: float) -> Dict[str, Any]:
    master_fd, slave_fd = pty.openpty()
    _set_pty_size(slave_fd, 24, 80)
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    child_env = os.environ.copy()
    child_env.update({
        "NO_COLOR": "1", "TERM": "xterm-256color", "PYTHONUNBUFFERED": "1",
    })
    process: Optional[subprocess.Popen[bytes]] = None
    screen = _VisibleScreen()
    shell_ms: Optional[float] = None
    evidence_ms: Optional[float] = None
    started = time.monotonic()
    captured = 0
    try:
        process = subprocess.Popen(
            (sys.executable, str(APP)), cwd=str(ROOT), env=child_env,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True, shell=False, preexec_fn=_claim_pty_session,
        )
        os.close(slave_fd)
        slave_fd = -1
        deadline = started + timeout_s
        while time.monotonic() < deadline:
            readable, _, _ = select.select((master_fd,), (), (), 0.02)
            if readable:
                try:
                    chunk = os.read(master_fd, 65536)
                except BlockingIOError:
                    chunk = b""
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    if process.poll() is not None:
                        break
                    continue
                captured += len(chunk)
                if captured > MAX_CAPTURE_BYTES:
                    raise MeasurementError("startup PTY exceeded its capture limit")
                def observe() -> None:
                    nonlocal shell_ms, evidence_ms
                    visible = screen.text()
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    if (shell_ms is None
                            and all(marker in visible for marker in STARTUP_MARKERS)):
                        shell_ms = elapsed_ms
                    if (evidence_ms is None and _CPU_EVIDENCE.search(visible)
                            and _MEMORY_EVIDENCE.search(visible)
                            and _THERMAL_EVIDENCE.search(visible)):
                        evidence_ms = elapsed_ms

                screen.feed(chunk, observe)
                if evidence_ms is not None:
                    break
            elif process.poll() is not None:
                break
        if shell_ms is None or evidence_ms is None:
            raise MeasurementError("startup markers were not observed before the deadline")
        _quit_pty_process(process, master_fd, min(5.0, timeout_s))
        return {
            "cleanup": "clean",
            "evidence_ms": evidence_ms,
            "exit_status": "clean",
            "shell_ms": shell_ms,
        }
    except MeasurementError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise MeasurementError("startup probe could not complete") from None
    finally:
        if process is not None:
            _cleanup_process(process)
        for fd in (master_fd, slave_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def measure_startup(runs: int, timeout_s: float, gap_s: float) -> Dict[str, Any]:
    samples = []
    for index in range(runs):
        result = _startup_once(timeout_s)
        samples.append({"run": index + 1, **result})
        if index + 1 < runs and gap_s:
            time.sleep(gap_s)
    shell = [sample["shell_ms"] for sample in samples]
    evidence = [sample["evidence_ms"] for sample in samples]
    payload = _base_payload("startup", "host_dependent")
    payload.update({
        "runs": runs,
        "samples": samples,
        "shell_ms": summarize(shell),
        "evidence_ms": summarize(evidence),
    })
    return payload


def _validate_snapshot_payload(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_TOP_LEVEL:
        raise MeasurementError("snapshot schema did not match the expected legacy projection")
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
        processes = value["processes"]
    except (KeyError, TypeError, ValueError):
        raise MeasurementError("snapshot schema did not match the expected legacy projection") from None
    if not isinstance(processes, list):
        raise MeasurementError("snapshot process projection was malformed")
    for process in processes:
        if not isinstance(process, dict) or not isinstance(process.get("command"), str):
            raise MeasurementError("snapshot process projection was malformed")
        for match in _SNAPSHOT_SECRET_PATTERN.finditer(process["command"]):
            if match.group("value") != "<redacted>":
                raise MeasurementError("snapshot redaction invariant failed")


def measure_snapshot(runs: int, timeout_s: float) -> Dict[str, Any]:
    elapsed_samples: List[float] = []
    rss_samples: List[float] = []
    samples = []
    time_path = Path("/usr/bin/time")
    if not time_path.is_file():
        raise MeasurementError("snapshot RSS probe is unavailable")
    for index in range(runs):
        result = run_bounded(
            (str(time_path), "-l", sys.executable, str(APP), "--json"),
            timeout_s=timeout_s, cwd=ROOT,
        )
        if result.returncode != 0:
            raise MeasurementError("snapshot probe exited unsuccessfully")
        try:
            parsed = json.loads(result.stdout.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MeasurementError("snapshot probe did not return valid JSON") from None
        _validate_snapshot_payload(parsed)
        match = re.search(rb"([0-9]+)\s+maximum resident set size", result.stderr)
        if match is None:
            raise MeasurementError("snapshot RSS probe returned no recognized measurement")
        rss_bytes = float(int(match.group(1)))
        elapsed_samples.append(result.elapsed_ms)
        rss_samples.append(rss_bytes)
        samples.append({
            "cleanup": "clean", "elapsed_ms": result.elapsed_ms,
            "maximum_rss_bytes": rss_bytes, "run": index + 1,
        })
    payload = _base_payload("snapshot", "host_dependent")
    payload.update({
        "elapsed_ms": summarize(elapsed_samples),
        "maximum_rss_bytes": summarize(rss_samples),
        "runs": runs,
        "samples": samples,
    })
    return payload


def _policy_benchmark(warmups: int, iterations: int) -> List[float]:
    import importlib

    with _repository_import_path():
        core = _require_repository_module(
            importlib.import_module("porkyhud_core"), "porkyhud_core.py",
        )
        tests = _require_repository_module(
            importlib.import_module("tests.test_core"), "tests/test_core.py",
        )
    facts, now = tests.make_ready_policy_case()
    state = core.initial_policy_state()
    for _ in range(warmups):
        state, _assessment = core.reduce_health(state, facts, now)
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        state, _assessment = core.reduce_health(state, facts, now)
        samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return samples


def _frame_benchmark(warmups: int, iterations: int) -> List[float]:
    import importlib

    with _repository_import_path():
        app = _require_repository_module(
            importlib.import_module("porkyhud"), "porkyhud.py",
        )
        tests = _require_repository_module(
            importlib.import_module("tests.test_porkyhud"),
            "tests/test_porkyhud.py",
        )
    frame = tests.make_health_frame_for_test("normal")

    def project() -> None:
        app.decision_header_lines(frame, "balanced", 80, 20.0)
        app.evidence_ribbon_lines(frame, 80, 20.0)

    for _ in range(warmups):
        project()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        project()
        samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return samples


def measure_pure(warmups: int, iterations: int) -> Dict[str, Any]:
    policy = _policy_benchmark(warmups, iterations)
    frame = _frame_benchmark(warmups, iterations)
    payload = _base_payload("pure", "deterministic")
    payload.update({
        "frame_projection_ms": summarize(frame),
        "iterations": iterations,
        "policy_reduction_ms": summarize(policy),
        "warmup_iterations": warmups,
    })
    return payload


def _descendants(root_pid: int, rows: Sequence[Tuple[int, int]]) -> Tuple[int, ...]:
    children: Dict[int, List[int]] = {}
    for pid, parent in rows:
        children.setdefault(parent, []).append(pid)
    found: List[int] = []
    pending = list(children.get(root_pid, ()))
    while pending:
        child = pending.pop()
        if child in found:
            continue
        found.append(child)
        pending.extend(children.get(child, ()))
    return tuple(found)


def summarize_launch_events(
    events: Sequence[Tuple[float, int]], *, window_start_s: float,
    window_seconds: float, is_alive: Callable[[int], bool],
) -> Dict[str, Any]:
    """Aggregate identity-only launch events into half-open minute bins."""

    start = _finite_number(window_start_s, "window_start_s")
    duration = _finite_number(window_seconds, "window_seconds")
    if duration <= 0.0 or duration > 3600.0:
        raise ValueError("window_seconds must be greater than zero and at most 3600")
    if not callable(is_alive):
        raise ValueError("is_alive must be callable")
    count = int(math.ceil(duration / 60.0))
    bins = [0] * count
    included: List[int] = []
    end = start + duration
    for event in events:
        if type(event) is not tuple or len(event) != 2:
            raise ValueError("launch events must be timestamp and integer identity tuples")
        timestamp = _finite_number(event[0], "launch timestamp")
        identity = event[1]
        if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
            raise ValueError("launch identity must be a positive integer")
        if timestamp < start or timestamp >= end:
            continue
        index = min(count - 1, int((timestamp - start) // 60.0))
        bins[index] += 1
        included.append(identity)
    survivors = 0
    for identity in included:
        try:
            survivors += bool(is_alive(identity))
        except BaseException:
            raise MeasurementError("recorded-launch survivor check failed") from None
    return {
        "launch_bins": bins,
        "maximum_per_minute": max(bins, default=0),
        "recorded_launches": len(included),
        "survivor_count": survivors,
    }


def _soak_window_summary(
    samples: Sequence[Tuple[float, float, float, Mapping[str, int]]],
) -> Dict[str, Any]:
    os_threads = [_finite_number(sample[1], "os_threads") for sample in samples]
    python_threads = [
        _finite_number(sample[2], "python_threads") for sample in samples
    ]
    names = sorted({name for sample in samples for name in sample[3]})
    prefixes: Dict[str, int] = {}
    for name in names:
        counts = []
        for sample in samples:
            value = sample[3].get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("thread prefix counts must be nonnegative integers")
            counts.append(value)
        prefixes[name] = max(counts, default=0)
    return {
        "os_threads": {
            "maximum": max(os_threads), "p95": nearest_rank(os_threads, 0.95),
        },
        "prefixes": prefixes,
        "python_threads": {
            "maximum": max(python_threads),
            "p95": nearest_rank(python_threads, 0.95),
        },
    }


def compare_soak_windows(
    samples: Sequence[Tuple[float, float, float, Mapping[str, int]]], *,
    interval_s: int = 5, window_s: int = 300,
) -> Dict[str, Any]:
    """Compare first/final fixed windows of five-second thread-prefix samples."""

    if isinstance(interval_s, bool) or not isinstance(interval_s, int) or interval_s <= 0:
        raise ValueError("interval_s must be a positive integer")
    if isinstance(window_s, bool) or not isinstance(window_s, int) or window_s <= 0:
        raise ValueError("window_s must be a positive integer")
    if window_s % interval_s:
        raise ValueError("window_s must be divisible by interval_s")
    window_count = window_s // interval_s
    if len(samples) < 2 * window_count:
        raise ValueError("soak samples do not cover distinct first and final windows")
    checked = []
    previous: Optional[float] = None
    for sample in samples:
        if type(sample) is not tuple or len(sample) != 4 or not isinstance(sample[3], Mapping):
            raise ValueError("soak samples have an invalid shape")
        timestamp = _finite_number(sample[0], "sample timestamp")
        if previous is not None and timestamp - previous < float(interval_s) - 1e-9:
            raise ValueError("soak samples are not spaced at the declared interval")
        previous = timestamp
        checked.append(sample)
    first = _soak_window_summary(checked[:window_count])
    final = _soak_window_summary(checked[-window_count:])
    all_prefixes = set(first["prefixes"]) | set(final["prefixes"])
    stable = bool(
        final["os_threads"]["maximum"] <= first["os_threads"]["maximum"]
        and final["os_threads"]["p95"] <= first["os_threads"]["p95"]
        and final["python_threads"]["maximum"]
            <= first["python_threads"]["maximum"]
        and final["python_threads"]["p95"] <= first["python_threads"]["p95"]
        and all(
            final["prefixes"].get(name, 0) <= first["prefixes"].get(name, 0)
            for name in all_prefixes
        )
    )
    return {"first": first, "final": final, "stable": stable}


def _process_snapshot(root_pid: int) -> Dict[str, float]:
    result = run_bounded(
        ("/bin/ps", "-axo", "pid=,ppid=,%cpu=,rss="), timeout_s=2.0,
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise MeasurementError("resource sampler could not inspect process metrics")
    rows: List[Tuple[int, int, float, float]] = []
    try:
        for line in result.stdout.decode("ascii", "strict").splitlines():
            pid_text, parent_text, cpu_text, rss_text = line.split()
            rows.append((int(pid_text), int(parent_text), float(cpu_text), float(rss_text)))
    except (UnicodeDecodeError, ValueError):
        raise MeasurementError("resource sampler received malformed process metrics") from None
    identities = [(pid, parent) for pid, parent, _cpu, _rss in rows]
    included = {root_pid, *_descendants(root_pid, identities)}
    cpu = sum(value for pid, _parent, value, _rss in rows if pid in included)
    rss = sum(value for pid, _parent, _cpu, value in rows if pid in included) * 1024.0
    descendants = max(0, len(included) - 1)
    return {"cpu_percent": cpu, "descendants": float(descendants), "rss_bytes": rss}


def _thread_count(pid: int) -> int:
    result = run_bounded(("/bin/ps", "-M", "-p", str(pid)), timeout_s=2.0, cwd=ROOT)
    if result.returncode != 0:
        raise MeasurementError("thread sampler could not inspect the live process")
    return max(0, len(result.stdout.splitlines()) - 1)


def _write_live_wrapper(temporary: Path) -> Tuple[Path, Path, Path]:
    record_path = temporary / "launch-events"
    thread_path = temporary / "thread-events"
    wrapper = temporary / "live-wrapper.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os,pathlib,subprocess,sys,threading,time\n"
        "launch_path=pathlib.Path(os.environ[%r])\n"
        "thread_path=pathlib.Path(os.environ[%r])\n"
        "origin_wall_ns=int(os.environ[%r])\n"
        "origin_mono_s=float(os.environ[%r])\n"
        "original_popen=subprocess.Popen\n"
        "original_start=threading.Thread.start\n"
        "counter=0\n"
        "lock=threading.Lock()\n"
        "def append(path,line):\n"
        "    with lock:\n"
        "        with path.open('a',encoding='ascii') as stream:\n"
        "            stream.write(line+'\\n')\n"
        "def event_time():\n"
        "    return origin_mono_s+(time.time_ns()-origin_wall_ns)/1000000000.0\n"
        "class RecordingPopen(original_popen):\n"
        "    def __init__(self,*args,**kwargs):\n"
        "        super().__init__(*args,**kwargs)\n"
        "        append(launch_path,'%%.9f %%d'%%(event_time(),self.pid))\n"
        "def recording_start(thread,*args,**kwargs):\n"
        "    global counter\n"
        "    target=getattr(thread,'_target',None)\n"
        "    owner=getattr(target,'__self__',None)\n"
        "    prefix=owner.__class__.__name__ if owner is not None else ''\n"
        "    name=getattr(thread,'name','')\n"
        "    if str(name).startswith('PorkyHUD-command-'):\n"
        "        prefix='PorkyHUD-command'\n"
        "    if prefix not in %r:\n"
        "        prefix='Other'\n"
        "    with lock:\n"
        "        counter+=1\n"
        "        identity=counter\n"
        "    original_run=thread.run\n"
        "    def measured_run():\n"
        "        append(thread_path,'%%.9f start %%s %%d'%%(event_time(),prefix,identity))\n"
        "        try:\n"
        "            original_run()\n"
        "        finally:\n"
        "            append(thread_path,'%%.9f stop %%s %%d'%%(event_time(),prefix,identity))\n"
        "    thread.run=measured_run\n"
        "    return original_start(thread,*args,**kwargs)\n"
        "subprocess.Popen=RecordingPopen\n"
        "threading.Thread.start=recording_start\n"
        "root=pathlib.Path(sys.argv[1]).resolve(strict=True)\n"
        "expected=(root/'porkyhud.py').resolve(strict=True)\n"
        "sys.path.insert(0,str(root))\n"
        "del sys.argv[1:]\n"
        "import porkyhud\n"
        "if pathlib.Path(porkyhud.__file__).resolve()!=expected:\n"
        "    raise RuntimeError('application import is outside the repository')\n"
        "raise SystemExit(porkyhud.main())\n"
        % (
            LIVE_LAUNCH_RECORD_PATH_ENV, LIVE_THREAD_RECORD_PATH_ENV,
            LIVE_WALL_ORIGIN_NS_ENV, LIVE_MONO_ORIGIN_S_ENV, _THREAD_PREFIXES,
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper, record_path, thread_path


def _read_launch_events(path: Path) -> Tuple[Tuple[float, int], ...]:
    if not path.exists():
        return ()
    events = []
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            timestamp, identity = line.split()
            events.append((float(timestamp), int(identity)))
    except (OSError, UnicodeError, ValueError):
        raise MeasurementError("launch recorder artifact was malformed") from None
    return tuple(events)


def _read_thread_events(
    path: Path,
) -> Tuple[Tuple[float, str, str, int], ...]:
    if not path.exists():
        return ()
    events = []
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            timestamp, action, prefix, identity = line.split()
            if action not in ("start", "stop") or prefix not in _THREAD_EVENT_CATEGORIES:
                raise ValueError
            events.append((float(timestamp), action, prefix, int(identity)))
    except (OSError, UnicodeError, ValueError):
        raise MeasurementError("thread recorder artifact was malformed") from None
    return tuple(events)


def _thread_prefix_counts_at(
    events: Sequence[Tuple[float, str, str, int]], timestamp: float,
) -> Dict[str, int]:
    active: Dict[int, str] = {}
    for occurred, action, prefix, identity in _ordered_thread_events(events):
        if occurred > timestamp:
            break
        if action == "start":
            active[identity] = prefix
        else:
            active.pop(identity, None)
    return {
        prefix: sum(value == prefix for value in active.values())
        for prefix in _THREAD_EVENT_CATEGORIES
    }


def _python_thread_count_at(
    events: Sequence[Tuple[float, str, str, int]], timestamp: float,
) -> int:
    active = set()
    for occurred, action, _prefix, identity in _ordered_thread_events(events):
        if occurred > timestamp:
            break
        if action == "start":
            active.add(identity)
        else:
            active.discard(identity)
    return 1 + len(active)


def _ordered_thread_events(
    events: Sequence[Tuple[float, str, str, int]],
) -> List[Tuple[float, str, str, int]]:
    """Order concurrent records; an equal-time stop closes its prior start."""

    return sorted(
        events,
        key=lambda event: (
            event[0], 0 if event[1] == "start" else 1, event[3], event[2],
        ),
    )


def _process_identity_alive(identity: int) -> bool:
    try:
        os.kill(identity, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _launch_live_process(
    temporary: Path,
) -> Tuple[subprocess.Popen[bytes], int, Path, Path]:
    master_fd, slave_fd = pty.openpty()
    _set_pty_size(slave_fd, 24, 80)
    wrapper, record_path, thread_path = _write_live_wrapper(temporary)
    origin_wall_ns = time.time_ns()
    origin_mono_s = time.monotonic()
    child_env = os.environ.copy()
    child_env.update({
        "NO_COLOR": "1", "TERM": "xterm-256color",
        LIVE_LAUNCH_RECORD_PATH_ENV: str(record_path),
        LIVE_THREAD_RECORD_PATH_ENV: str(thread_path),
        LIVE_WALL_ORIGIN_NS_ENV: str(origin_wall_ns),
        LIVE_MONO_ORIGIN_S_ENV: repr(origin_mono_s),
    })
    process = subprocess.Popen(
        (sys.executable, str(wrapper), str(ROOT)), cwd=str(ROOT), env=child_env,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True, shell=False, preexec_fn=_claim_pty_session,
    )
    os.close(slave_fd)
    return process, master_fd, record_path, thread_path


def _drain_fd(fd: int, stop: threading.Event, failure: List[BaseException]) -> None:
    captured = 0
    try:
        while not stop.is_set():
            readable, _, _ = select.select((fd,), (), (), 0.1)
            if not readable:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    return
                raise
            if not chunk:
                return
            captured += len(chunk)
            if captured > MAX_CAPTURE_BYTES:
                raise MeasurementError("live PTY exceeded its capture limit")
    except BaseException as error:
        failure.append(error)


def _measure_live_window(
    warmup_s: int, seconds: int, soak: bool,
) -> Dict[str, Any]:
    process: Optional[subprocess.Popen[bytes]] = None
    master_fd = -1
    stop = threading.Event()
    drain_failures: List[BaseException] = []
    drainer: Optional[threading.Thread] = None
    with private_workspace() as temporary:
        record_path = temporary / "launch-events"
        thread_path = temporary / "thread-events"
        try:
            process, master_fd, record_path, thread_path = _launch_live_process(temporary)
            drainer = threading.Thread(
                target=_drain_fd, args=(master_fd, stop, drain_failures),
                name="porkyhud-measure-drain", daemon=True,
            )
            drainer.start()
            end_warmup = time.monotonic() + warmup_s
            while time.monotonic() < end_warmup:
                if process.poll() is not None:
                    raise MeasurementError("live probe exited during warmup")
                time.sleep(min(0.25, end_warmup - time.monotonic()))
            measurement_start = time.monotonic()
            cpu: List[float] = []
            rss: List[float] = []
            descendants: List[float] = []
            threads: List[float] = []
            soak_samples: List[Tuple[float, float, float, Mapping[str, int]]] = []
            sample_interval = 5 if soak else 1
            sample_count = seconds // sample_interval
            if sample_count <= 0 or (soak and seconds % sample_interval):
                raise MeasurementError("measurement duration does not cover exact sample intervals")
            sampled_times: List[float] = []
            for sample_index in range(1, sample_count + 1):
                next_sample = measurement_start + sample_index * sample_interval
                remaining = next_sample - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                if process.poll() is not None:
                    raise MeasurementError("live probe exited during measurement")
                sampled_at = time.monotonic()
                sample = _process_snapshot(process.pid)
                os_threads = float(_thread_count(process.pid))
                sampled_times.append(sampled_at)
                cpu.append(sample["cpu_percent"])
                rss.append(sample["rss_bytes"])
                descendants.append(sample["descendants"])
                threads.append(os_threads)
                if soak:
                    thread_events = _read_thread_events(thread_path)
                    prefixes = _thread_prefix_counts_at(thread_events, sampled_at)
                    python_threads = float(_python_thread_count_at(
                        thread_events, sampled_at,
                    ))
                    soak_samples.append((
                        float(sample_index * sample_interval),
                        os_threads, python_threads, prefixes,
                    ))
            _quit_pty_process(process, master_fd, 5.0)
            stop.set()
            if drainer is not None:
                drainer.join(timeout=1.0)
            if drainer is not None and drainer.is_alive():
                raise MeasurementError("live PTY drain did not stop")
            if drain_failures:
                raise MeasurementError("live PTY drain failed")
            all_launches = _read_launch_events(record_path)
            if any(_process_identity_alive(identity) for _timestamp, identity in all_launches):
                raise MeasurementError("live probe left a recorded child identity")
            launches = summarize_launch_events(
                all_launches,
                window_start_s=measurement_start,
                window_seconds=float(seconds),
                is_alive=_process_identity_alive,
            )
            if launches["survivor_count"]:
                raise MeasurementError("live probe left a recorded child identity")
            mode = "soak" if soak else "live"
            payload = _base_payload(mode, "host_dependent")
            payload.update({
                "cleanup": "clean",
                "cpu_percent": summarize(cpu),
                "descendants": summarize(descendants),
                "launches": launches,
                "rss_bytes": summarize(rss),
                "samples": len(cpu),
                "sample_gap_count": sum(
                    gap > sample_interval * 1.5
                    for gap in (
                        sampled_times[index] - sampled_times[index - 1]
                        for index in range(1, len(sampled_times))
                    )
                ),
                "thread_count": summarize(threads),
                "warmup_seconds": warmup_s,
            })
            if soak:
                # Tiny development probes still exercise the same comparison;
                # release evidence uses the fixed five-minute windows.
                comparison_window = 300 if seconds >= 600 else max(
                    5, (len(soak_samples) // 2) * 5,
                )
                payload["thread_stability"] = compare_soak_windows(
                    soak_samples, interval_s=5, window_s=comparison_window,
                )
            return payload
        finally:
            stop.set()
            if process is not None and process.poll() is None:
                _cleanup_process(process)
            if drainer is not None:
                drainer.join(timeout=1.0)
            if master_fd >= 0:
                try:
                    os.close(master_fd)
                except OSError:
                    pass


def _await_process_identities(
    path: Path, *, timeout_s: float,
) -> Tuple[int, int]:
    """Wait for a complete private direct/grandchild identity record."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            direct, grandchild = (
                int(value) for value in path.read_text(encoding="ascii").split()
            )
        except (OSError, UnicodeError, ValueError):
            direct, grandchild = 0, 0
        if direct > 0 and grandchild > 0 and direct != grandchild:
            return direct, grandchild
        time.sleep(0.01)
    raise MeasurementError("timeout probe did not establish process identities")


def _timeout_cleanup_probe() -> Dict[str, Any]:
    import importlib

    with private_workspace() as temporary:
        identity_path = temporary / "grandchild.identity"
        grandchild_source = "import time; time.sleep(30)"
        child_source = (
            "import os,pathlib,subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c',%r]);"
            "path=pathlib.Path(sys.argv[1]);part=path.with_suffix('.part');"
            "part.write_text('%%d %%d'%%(os.getpid(),p.pid),encoding='ascii');"
            "os.replace(str(part),str(path));"
            "sys.stdout.write('private-timeout-stdout');sys.stdout.flush();"
            "sys.stderr.write('private-timeout-stderr');sys.stderr.flush();"
            "time.sleep(30)" % grandchild_source
        )
        started = time.monotonic()
        with _repository_import_path():
            app = _require_repository_module(
                importlib.import_module("porkyhud"), "porkyhud.py",
            )
            result = app.run_structured_command(
                (sys.executable, "-c", child_source, str(identity_path)),
                category="release-timeout-probe",
                timeout_s=1.0,
                stdout_limit=1024,
                stderr_limit=1024,
            )
        if (
            result.outcome is not app.AttemptOutcome.TIMEOUT
            or result.failure is not app.FailureCode.COMMAND_TIMEOUT
        ):
            raise MeasurementError("application did not return the exact typed timeout")
        if result.stdout or result.stderr:
            raise MeasurementError("application exposed private timeout output")
        direct_identity, grandchild_identity = _await_process_identities(
            identity_path, timeout_s=1.0,
        )
        direct_reaped = not _process_identity_alive(direct_identity)
        grandchild_absent = not _process_identity_alive(grandchild_identity)
        absence_deadline = time.monotonic() + REAP_DEADLINE_S
        while (
            not direct_reaped or not grandchild_absent
        ) and time.monotonic() < absence_deadline:
            time.sleep(0.02)
            direct_reaped = not _process_identity_alive(direct_identity)
            grandchild_absent = not _process_identity_alive(grandchild_identity)
        if not direct_reaped or not grandchild_absent:
            raise MeasurementError("timeout probe left a recorded process identity")
    payload = _base_payload("timeout-cleanup", "deterministic")
    payload.update({
        "cleanup": "process_group_terminated",
        "direct_reaped": direct_reaped,
        "elapsed_ms": (time.monotonic() - started) * 1000.0,
        "failure": result.failure.value,
        "grandchild_absent": grandchild_absent,
        "outcome": result.outcome.value,
    })
    return payload


def _bounded_int(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if isinstance(parsed, bool) or parsed < minimum or parsed > maximum:
            raise argparse.ArgumentTypeError(
                "must be between %d and %d" % (minimum, maximum)
            )
        return parsed
    return parse


def _bounded_float(minimum: float, maximum: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a number") from None
        if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
            raise argparse.ArgumentTypeError(
                "must be between %s and %s" % (minimum, maximum)
            )
        return parsed
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run fixed, bounded PorkyHUD release measurements as JSON.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("environment", help="capture allowlisted support facts")

    startup = subparsers.add_parser("startup", help="measure progressive PTY startup")
    startup.add_argument("--runs", type=_bounded_int(1, 100), default=DEFAULT_RUNS)
    startup.add_argument(
        "--timeout-seconds", type=_bounded_float(0.25, 30.0),
        default=DEFAULT_STARTUP_TIMEOUT_S,
    )
    startup.add_argument("--gap-seconds", type=_bounded_float(0.0, 5.0), default=0.5)

    snapshot = subparsers.add_parser("snapshot", help="measure one-shot JSON snapshots")
    snapshot.add_argument("--runs", type=_bounded_int(1, 100), default=DEFAULT_RUNS)
    snapshot.add_argument(
        "--timeout-seconds", type=_bounded_float(1.0, 60.0),
        default=DEFAULT_SNAPSHOT_TIMEOUT_S,
    )

    pure = subparsers.add_parser("pure", help="measure deterministic pure projections")
    pure.add_argument(
        "--iterations", type=_bounded_int(1, 100_000), default=DEFAULT_ITERATIONS,
    )
    pure.add_argument(
        "--warmup-iterations", type=_bounded_int(0, 10_000),
        default=DEFAULT_WARMUP_ITERATIONS,
    )

    live = subparsers.add_parser("live", help="measure bounded steady live resources")
    live.add_argument(
        "--seconds", type=_bounded_int(1, 600), default=DEFAULT_LIVE_SECONDS,
    )
    live.add_argument(
        "--warmup-seconds", type=_bounded_int(0, 120), default=DEFAULT_WARMUP_S,
    )

    soak = subparsers.add_parser("soak", help="measure bounded live lifecycle stability")
    soak.add_argument(
        "--seconds", type=_bounded_int(10, 3600), default=DEFAULT_SOAK_SECONDS,
    )
    soak.add_argument(
        "--warmup-seconds", type=_bounded_int(0, 120), default=DEFAULT_WARMUP_S,
    )

    subparsers.add_parser(
        "timeout-cleanup", help="verify nonprivileged process-group timeout cleanup",
    )
    return parser


def _dispatch(namespace: argparse.Namespace) -> Dict[str, Any]:
    if namespace.mode == "environment":
        payload = _base_payload("environment", "host_dependent")
        payload["environment"] = collect_environment()
        return payload
    guarded = namespace.mode in ("startup", "snapshot", "live", "soak")
    initial_authority = _capture_batch_authority() if guarded else None
    if namespace.mode == "startup":
        payload = measure_startup(
            namespace.runs, namespace.timeout_seconds, namespace.gap_seconds,
        )
    elif namespace.mode == "snapshot":
        payload = measure_snapshot(namespace.runs, namespace.timeout_seconds)
    elif namespace.mode == "pure":
        payload = measure_pure(namespace.warmup_iterations, namespace.iterations)
    elif namespace.mode == "live":
        payload = _measure_live_window(namespace.warmup_seconds, namespace.seconds, False)
    elif namespace.mode == "soak":
        payload = _measure_live_window(namespace.warmup_seconds, namespace.seconds, True)
    elif namespace.mode == "timeout-cleanup":
        payload = _timeout_cleanup_probe()
    else:
        raise MeasurementError("unknown measurement mode")
    if initial_authority is not None:
        payload["batch_authority"] = _finish_batch_guard(
            initial_authority, _capture_batch_authority(),
        )
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    try:
        with private_workspace():
            payload = sanitize_public(_dispatch(namespace))
        print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0
    except Exception:
        error_payload = {
            "category": (
                "deterministic" if namespace.mode in ("pure", "timeout-cleanup")
                else "host_dependent"
            ),
            "mode": namespace.mode,
            "status": "failed",
        }
        print(json.dumps(error_payload, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
