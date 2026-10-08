"""Bounded local recordings, replay data, comparisons, and private exports."""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .model import ToolReport


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024
MAX_LOAD_SAMPLES = 20000
MAX_EVENTS = 256
MAX_METRICS = 128
MAX_PROCESSES = 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SEVERITIES = frozenset(("NORMAL", "WATCH", "CRITICAL", "UNKNOWN", "CHECKING"))
_READINESS = frozenset(("LIVE", "DEGRADED", "INCOMPLETE", "DELAYED", "CHECKING"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int, label: str, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > limit
            or (not empty and not value.strip())
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Invalid %s" % label)
    return value


def _number(value: Any, label: str, *, minimum: Optional[float] = None) -> float:
    if type(value) not in (int, float):
        raise ValueError("Invalid %s" % label)
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Invalid %s" % label) from None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError("Invalid %s" % label)
    return result


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid %s" % label)
    return value


def _timestamp(value: Any) -> str:
    text = _text(value, 64, "timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid timestamp") from None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Timestamp must use UTC")
    return parsed.isoformat()


def _start_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if type(value) in (int, float):
        return str(_number(value, "process start identifier", minimum=0))
    return _text(value, 128, "process start identifier")


def _event(value: Any, *, include_private: bool = True) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("Invalid session event")
    result = {
        "kind": _text(value.get("kind", "mark"), 32, "event kind"),
        "label": _text(value.get("label", ""), 160, "event label", empty=True),
    }
    if "elapsed_s" in value:
        result["elapsed_s"] = _number(value["elapsed_s"], "event time", minimum=0)
    if "timestamp" in value:
        result["timestamp"] = _timestamp(value["timestamp"])
    if not include_private and result["kind"] != "mark":
        labels = {"gap": "Recording gap", "health": "Health status changed",
                  "status": "Source status changed", "devices": "Devices changed",
                  "network": "Network changed", "connections": "Connections changed",
                  "processes": "Process list changed"}
        result["label"] = labels.get(result["kind"], "Inventory changed")
        if result["kind"] not in labels:
            result["kind"] = "change"
    return result


def validate_sample(sample: Any, *, include_private: bool = True) -> dict:
    """Copy only the v1 fields; reject nonfinite readings and unsafe shapes."""

    if not isinstance(sample, Mapping) or type(sample.get("schema_version")) is not int:
        raise ValueError("Expected a versioned sample")
    if sample["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported sample version")
    metrics = sample.get("metrics")
    if not isinstance(metrics, Mapping) or len(metrics) > MAX_METRICS:
        raise ValueError("Invalid sample metrics")
    normalized_metrics = {}
    for name, reading in metrics.items():
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise ValueError("Invalid metric name")
        if not isinstance(reading, Mapping):
            raise ValueError("Invalid metric reading")
        value = reading.get("value")
        if value is not None:
            _number(value, "metric value")
        normalized_metrics[name] = {
            "value": value,
            "unit": _text(reading.get("unit"), 32, "metric unit", empty=True),
            "status": _text(reading.get("status"), 32, "metric status"),
        }
        if "age_s" in reading:
            normalized_metrics[name]["age_s"] = _number(reading["age_s"], "metric age", minimum=0)
        if "revision" in reading:
            normalized_metrics[name]["revision"] = _integer(
                reading["revision"], "metric revision", 0, 2**63 - 1)
        if "max_gap_s" in reading:
            gap = _number(reading["max_gap_s"], "metric gap budget", minimum=0)
            if not 0 < gap <= 300:
                raise ValueError("Invalid metric gap budget")
            normalized_metrics[name]["max_gap_s"] = gap
    processes = sample.get("processes", [])
    if not isinstance(processes, (list, tuple)) or len(processes) > MAX_PROCESSES:
        raise ValueError("Invalid sample processes")
    normalized_processes = []
    if include_private:
        for process in processes:
            if not isinstance(process, Mapping):
                raise ValueError("Invalid process reading")
            cpu = process.get("cpu_pct")
            if cpu is not None:
                _number(cpu, "process CPU", minimum=0)
            rss = process.get("rss_bytes")
            if rss is not None:
                _integer(rss, "process memory", 0, 2**63 - 1)
            ppid = process.get("ppid")
            if ppid is not None:
                _integer(ppid, "parent process ID", 0, 2**31 - 1)
            normalized_processes.append({
                "pid": _integer(process.get("pid"), "process ID", 1, 2**31 - 1),
                "start_id": _start_id(process.get("start_id")),
                "ppid": ppid,
                "name": _text(process.get("name", "unknown"), 128, "process name"),
                "cpu_pct": cpu, "rss_bytes": rss,
                "state": _text(process.get("state", "unknown"), 32, "process state"),
            })
    events = sample.get("events", [])
    if not isinstance(events, (list, tuple)) or len(events) > MAX_EVENTS:
        raise ValueError("Invalid sample events")
    health = sample.get("health")
    if not isinstance(health, Mapping):
        raise ValueError("Invalid sample health")
    severity, readiness = health.get("severity"), health.get("readiness")
    if (not isinstance(severity, str) or not isinstance(readiness, str)
            or severity not in _SEVERITIES or readiness not in _READINESS):
        raise ValueError("Invalid sample health status")
    result = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": _timestamp(sample.get("timestamp")),
        "elapsed_s": _number(sample.get("elapsed_s"), "elapsed time", minimum=0),
        "metrics": normalized_metrics,
        "processes": normalized_processes,
        "events": [_event(event, include_private=include_private) for event in events],
        "health": {"severity": severity, "readiness": readiness},
    }
    for flag in ("processes_complete", "processes_truncated"):
        if flag in sample:
            if type(sample[flag]) is not bool:
                raise ValueError("Invalid process coverage state")
            result[flag] = sample[flag] if include_private else False
    if "process_source" in sample:
        source = sample["process_source"]
        if not isinstance(source, Mapping):
            raise ValueError("Invalid process source state")
        result["process_source"] = {
            "status": _text(source.get("status"), 32, "process source status"),
            "revision": _integer(source.get("revision"), "process source revision", 0, 2**63 - 1),
            "age_s": _number(source.get("age_s"), "process source age", minimum=0),
        }
    if "system" in sample:
        system = sample["system"]
        if not isinstance(system, Mapping):
            raise ValueError("Invalid sample system configuration")
        normalized_system = {}
        for name in ("model", "chip", "os", "build"):
            if name in system:
                normalized_system[name] = _text(system[name], 128, "system " + name)
        if "ram_bytes" in system:
            normalized_system["ram_bytes"] = _integer(
                system["ram_bytes"], "system RAM", 1, 2**63 - 1)
        result["system"] = normalized_system
    return result


def _encoded(record: Any) -> bytes:
    try:
        result = (json.dumps(record, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Report is not valid JSON") from None
    if len(result) > MAX_RECORD_BYTES:
        raise ValueError("Session record is too large")
    return result


class SessionRecorder:
    """Keep a bounded sample ring and optionally queue bounded JSONL writes."""

    def __init__(self, path=None, *, max_samples: int = 1800,
                 max_bytes: int = MAX_FILE_BYTES, queue_size: int = 128,
                 include_private: bool = False) -> None:
        self.max_samples = _integer(max_samples, "sample limit", 1, MAX_LOAD_SAMPLES)
        self.max_bytes = _integer(max_bytes, "recording byte limit", 1024, MAX_FILE_BYTES)
        self.queue_size = _integer(queue_size, "queue limit", 1, 1024)
        if type(include_private) is not bool:
            raise ValueError("Invalid private-data setting")
        self.include_private = include_private
        self.path = Path(path).expanduser() if path is not None else None
        self._samples = deque()
        self._sample_sizes = deque()
        self._events = deque(maxlen=MAX_EVENTS)
        self._memory_bytes = 0
        self._last_elapsed = None
        self._total_samples = 0
        self._retained_truncated = False
        self._stopped = False
        self._started = None
        self._last_append_clock = None
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=self.queue_size)
        self._queue_bytes = 0
        self._queue_byte_limit = min(self.max_bytes, self.queue_size * MAX_RECORD_BYTES)
        self._dropped_records = 0
        self._pending_drops = 0
        self._file_bytes = 0
        self._file_samples = 0
        self._disk_truncated = False
        self._writer_error = ""
        self._writer_finished = False
        self._stop_event = threading.Event()
        self._worker = None
        if self.path is not None:
            header = _encoded({"type": "header", "format": "porkyhud-session",
                               "schema_version": SCHEMA_VERSION, "timestamp": _utc_now(),
                               "include_private": include_private,
                               "limits": {"samples": MAX_LOAD_SAMPLES, "bytes": self.max_bytes}})
            self._queue.put_nowait(header)
            self._queue_bytes = len(header)
            self._worker = threading.Thread(target=self._write_loop,
                                            name="porkyhud-session-writer", daemon=True)
            self._worker.start()

    @property
    def samples(self) -> tuple:
        with self._lock:
            return tuple(copy.deepcopy(sample) for sample in self._samples)

    def _enqueue_locked(self, record: dict) -> bool:
        if self.path is None:
            return True
        if self._writer_error or self._disk_truncated:
            self._dropped_records += 1
            return False
        if self._pending_drops:
            record = dict(record, dropped_before=self._pending_drops)
        encoded = _encoded(record)
        if (self._queue.full()
                or self._queue_bytes + len(encoded) > self._queue_byte_limit):
            self._dropped_records += 1
            self._pending_drops += 1
            return False
        self._queue.put_nowait(encoded)
        self._queue_bytes += len(encoded)
        self._pending_drops = 0
        return True

    def append(self, sample: dict, *, observed_mono_s=None) -> bool:
        supplied_clock = (None if observed_mono_s is None else
                          _number(observed_mono_s, "sample observation clock"))
        normalized = validate_sample(sample)
        sample_size = len(_encoded(normalized))
        disk_sample = (validate_sample(normalized, include_private=self.include_private)
                       if self.path is not None else None)
        with self._lock:
            if self._stopped:
                return False
            elapsed = normalized["elapsed_s"]
            if self._last_elapsed is not None:
                if elapsed < self._last_elapsed:
                    raise ValueError("Sample time moved backwards")
                if elapsed == self._last_elapsed:
                    return False
            self._last_elapsed = elapsed
            observed_at = time.monotonic() if supplied_clock is None else supplied_clock
            if self._started is None:
                self._started = observed_at
            self._last_append_clock = observed_at
            self._total_samples += 1
            self._samples.append(normalized)
            self._sample_sizes.append(sample_size)
            self._memory_bytes += sample_size
            for event in normalized["events"]:
                if len(self._events) == MAX_EVENTS:
                    self._retained_truncated = True
                self._events.append(copy.deepcopy(event))
            while (len(self._samples) > self.max_samples
                   or self._memory_bytes > self.max_bytes):
                self._samples.popleft()
                self._memory_bytes -= self._sample_sizes.popleft()
                self._retained_truncated = True
            self._enqueue_locked({"type": "sample", "schema_version": SCHEMA_VERSION,
                                  "sample": disk_sample})
        return True

    def mark(self, label: str) -> dict:
        label = _text(label, 160, "bookmark label")
        with self._lock:
            if self._stopped:
                raise RuntimeError("Recording has stopped")
            marked_at = time.monotonic()
            if self._started is None:
                self._started = marked_at
            elapsed = (self._last_elapsed + max(0.0, marked_at - self._last_append_clock)
                       if self._last_elapsed is not None else
                       max(0.0, marked_at - self._started))
            event = {"kind": "mark", "label": label, "timestamp": _utc_now(),
                     "elapsed_s": elapsed}
            if len(self._events) == MAX_EVENTS:
                self._retained_truncated = True
            self._events.append(event)
            self._enqueue_locked({"type": "mark", "schema_version": SCHEMA_VERSION,
                                  "event": event})
            return copy.deepcopy(event)

    def _write_loop(self) -> None:
        try:
            # Exclusive creation prevents an existing file or symlink being replaced.
            descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                last_flush = time.monotonic()
                dirty = False
                while not self._stop_event.is_set() or not self._queue.empty():
                    try:
                        encoded = self._queue.get(timeout=0.05)
                    except queue.Empty:
                        if dirty and time.monotonic() - last_flush >= 1.0:
                            stream.flush()
                            dirty = False
                            last_flush = time.monotonic()
                        continue
                    with self._lock:
                        self._queue_bytes -= len(encoded)
                        # Leave room for the completion record, even at the size limit.
                        fits = self._file_bytes + len(encoded) <= self.max_bytes - 512
                        is_sample = encoded.startswith(b'{"type":"sample",')
                        if is_sample and self._file_samples >= MAX_LOAD_SAMPLES:
                            fits = False
                        if not fits:
                            self._disk_truncated = True
                            self._dropped_records += 1
                    if fits:
                        stream.write(encoded)
                        dirty = True
                        with self._lock:
                            self._file_bytes += len(encoded)
                            if is_sample:
                                self._file_samples += 1
                        if not is_sample or time.monotonic() - last_flush >= 1.0:
                            stream.flush()
                            dirty = False
                            last_flush = time.monotonic()
                    self._queue.task_done()
                with self._lock:
                    final = _encoded({"type": "stop", "schema_version": SCHEMA_VERSION,
                                      "complete": not self._disk_truncated and not self._dropped_records,
                                      "truncated": self._disk_truncated,
                                      "dropped_samples": self._dropped_records})
                stream.write(final)
                stream.flush()
                with self._lock:
                    self._file_bytes += len(final)
        except OSError:
            with self._lock:
                self._writer_error = "Recording file could not be written."
        except Exception:
            with self._lock:
                self._writer_error = "Recording writer failed."
        finally:
            with self._lock:
                self._writer_finished = True

    def stop(self, timeout_s: float = 1.5) -> dict:
        timeout = _number(timeout_s, "stop timeout", minimum=0)
        if timeout > 10:
            raise ValueError("Stop timeout is too large")
        with self._lock:
            self._stopped = True
            self._stop_event.set()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        with self._lock:
            alive = worker is not None and worker.is_alive()
            return {"stopped": True, "writer_finished": not alive,
                    "complete": not alive and not self._writer_error
                    and not self._disk_truncated and not self._dropped_records,
                    "truncated": self._retained_truncated or self._disk_truncated,
                    "dropped_samples": self._dropped_records,
                    "queued_records": self._queue.qsize(), "error": self._writer_error}

    def _session(self) -> dict:
        with self._lock:
            writer_finished = self._worker is None or self._writer_finished
            return {"schema_version": SCHEMA_VERSION,
                    "samples": copy.deepcopy(list(self._samples)),
                    "events": copy.deepcopy(list(self._events)),
                    "complete": self._stopped and not self._writer_error
                    and not self._dropped_records and not self._disk_truncated
                    and not self._retained_truncated and writer_finished,
                    "truncated": self._retained_truncated or self._disk_truncated,
                    "dropped_samples": self._dropped_records,
                    "writer_finished": writer_finished,
                    "details": [self._writer_error] if self._writer_error else []}

    def to_report(self) -> ToolReport:
        session = self._session()
        samples = session["samples"]
        span = samples[-1]["elapsed_s"] - samples[0]["elapsed_s"] if samples else 0.0
        details = list(session["details"])
        if session["dropped_samples"]:
            details.append("%d records were not written." % session["dropped_samples"])
        if session["truncated"]:
            details.append("The retained recording reached its limit.")
        if self._stopped and not session["writer_finished"]:
            details.append("The recording writer is still stopping.")
        for event in session["events"]:
            details.append("%.1f s · %s" % (event.get("elapsed_s", 0), event["label"]))
        stats = _metric_stats(samples)
        rows = []
        current = samples[-1]["metrics"] if samples else {}
        for name, metric in sorted(current.items()):
            observed = stats.get((name, metric["unit"]))
            rows.append((name, "—" if metric["value"] is None else "%.3g" % metric["value"],
                         "—" if observed is None else "%.3g" % observed["mean"],
                         "—" if observed is None else "%.3g" % observed["max"], metric["unit"],
                         "0" if observed is None else str(observed["count"]), metric["status"]))
        return ToolReport(
            tool="Session timeline", status="ERROR" if session["details"] else
            ("STOPPING" if self._stopped and not session["writer_finished"] else
             "STOPPED" if self._stopped else "RECORDING" if self.path is not None else "LIVE"),
            summary="%d retained samples over %.1f seconds" % (len(samples), span),
            columns=("Metric", "Current", "Mean", "Peak", "Unit", "Samples", "Status"),
            rows=tuple(rows),
            details=tuple(details), complete=session["complete"],
            truncated=session["truncated"], data=session,
        )


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              ValueError("Invalid JSON number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Invalid recording JSON") from None


def _session_data(value: Any, *, include_private: bool = True) -> dict:
    if (not isinstance(value, Mapping) or type(value.get("schema_version")) is not int
            or value["schema_version"] != SCHEMA_VERSION):
        raise ValueError("Unsupported session version")
    samples = value.get("samples")
    if not isinstance(samples, (list, tuple)) or len(samples) > MAX_LOAD_SAMPLES:
        raise ValueError("Invalid session samples")
    normalized = []
    previous = None
    for sample in samples:
        sample = validate_sample(sample, include_private=include_private)
        if previous is not None and sample["elapsed_s"] <= previous:
            raise ValueError("Session sample times must increase")
        previous = sample["elapsed_s"]
        normalized.append(sample)
    events = value.get("events", [])
    if not isinstance(events, (list, tuple)) or len(events) > MAX_EVENTS:
        raise ValueError("Invalid session events")
    complete, truncated = value.get("complete", False), value.get("truncated", False)
    if type(complete) is not bool or type(truncated) is not bool:
        raise ValueError("Invalid session completion state")
    dropped = _integer(value.get("dropped_samples", 0), "dropped count", 0, 2**63 - 1)
    return {"schema_version": SCHEMA_VERSION, "samples": normalized,
            "events": [_event(event, include_private=include_private) for event in events],
            "complete": complete and not truncated and not dropped,
            "truncated": truncated, "dropped_samples": dropped, "details": []}


def load_session(path) -> dict:
    """Read a capped v1 JSONL recording or JSON session/sample export."""

    selected = Path(path).expanduser()
    if not selected.is_file() or selected.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Recording is missing or exceeds the size limit")
    with selected.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Recording exceeds the size limit")
    # JSON exports have a format marker and may contain many samples on one line.
    try:
        whole = _decode(raw)
    except ValueError:
        whole = None
    if isinstance(whole, Mapping) and whole.get("format") == "porkyhud-export":
        if type(whole.get("schema_version")) is not int or whole["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported export version")
        if whole.get("kind") == "sample":
            sample = validate_sample(whole.get("sample"))
            return _session_data({"schema_version": SCHEMA_VERSION, "samples": [sample],
                                  "complete": True})
        if whole.get("kind") == "session":
            return _session_data(whole.get("session"))
        raise ValueError("Unsupported export kind")
    result = {"schema_version": SCHEMA_VERSION, "samples": [], "events": [],
              "complete": False, "truncated": False, "dropped_samples": 0, "details": []}
    header = False
    stopped = False
    previous = None
    skipped = 0
    for line_number, line in enumerate(raw.splitlines(keepends=True)):
        if len(line) > MAX_RECORD_BYTES:
            raise ValueError("Session record is too large")
        if stopped:
            if line.strip():
                raise ValueError("Data follows session completion")
            continue
        try:
            record = _decode(line)
        except ValueError:
            if not line.endswith(b"\n") and line_number == len(raw.splitlines()) - 1:
                result["truncated"] = True
                result["details"].append("An incomplete final record was omitted.")
                break
            raise
        if not isinstance(record, Mapping) or type(record.get("schema_version")) is not int:
            raise ValueError("Invalid session record")
        if record["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported session version")
        kind = record.get("type")
        if line_number == 0:
            if kind != "header" or record.get("format") != "porkyhud-session":
                raise ValueError("Missing session header")
            header = True
            continue
        if kind == "sample":
            if len(result["samples"]) == MAX_LOAD_SAMPLES:
                raise ValueError("Recording exceeds the sample limit")
            sample = validate_sample(record.get("sample"))
            if previous is not None and sample["elapsed_s"] <= previous:
                raise ValueError("Session sample times must increase")
            previous = sample["elapsed_s"]
            result["samples"].append(sample)
            for event in sample["events"]:
                if len(result["events"]) == MAX_EVENTS:
                    result["events"].pop(0)
                    result["truncated"] = True
                result["events"].append(event)
            if "dropped_before" in record:
                count = _integer(record["dropped_before"], "gap count", 1, 2**63 - 1)
                skipped += count
                result["details"].append("%d records were skipped before a sample." % count)
        elif kind == "mark":
            if len(result["events"]) == MAX_EVENTS:
                result["events"].pop(0)
                result["truncated"] = True
            result["events"].append(_event(record.get("event")))
            if "dropped_before" in record:
                skipped += _integer(record["dropped_before"], "gap count", 1, 2**63 - 1)
        elif kind == "stop":
            stopped = True
            if type(record.get("complete")) is not bool or type(record.get("truncated")) is not bool:
                raise ValueError("Invalid session completion state")
            result["dropped_samples"] = max(skipped, _integer(
                record.get("dropped_samples", 0), "dropped count", 0, 2**63 - 1))
            result["truncated"] = result["truncated"] or record["truncated"]
            result["complete"] = record["complete"] and not result["truncated"] and not result["dropped_samples"]
        else:
            raise ValueError("Unknown session record")
    if not header:
        raise ValueError("Missing session header")
    if not stopped:
        result["dropped_samples"] = skipped
        result["details"].append("Recording ended without a completion record.")
    if result["dropped_samples"]:
        result["details"].append("%d records were not written." % result["dropped_samples"])
    return result


def _atomic_json(path, value: dict) -> int:
    encoded = (json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("Export exceeds the size limit")
    selected = Path(path).expanduser()
    descriptor, temporary = tempfile.mkstemp(prefix=".porkyhud-", suffix=".json",
                                             dir=str(selected.parent))
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
        os.replace(temporary, str(selected))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return len(encoded)


def export_report(sample_or_session, path, include_private: bool = False) -> dict:
    """Atomically export a validated sample/session, omitting processes by default."""

    if type(include_private) is not bool:
        raise ValueError("Invalid private-data setting")
    if isinstance(sample_or_session, SessionRecorder):
        sample_or_session = sample_or_session._session()
    if isinstance(sample_or_session, ToolReport):
        sample_or_session = sample_or_session.data
    if not isinstance(sample_or_session, Mapping):
        raise ValueError("Expected a sample or session")
    payload = {"format": "porkyhud-export", "schema_version": SCHEMA_VERSION,
               "include_private": include_private}
    if "metrics" in sample_or_session:
        payload.update(kind="sample", sample=validate_sample(
            sample_or_session, include_private=include_private))
    else:
        payload.update(kind="session", session=_session_data(
            sample_or_session, include_private=include_private))
    size = _atomic_json(path, payload)
    return {"schema_version": SCHEMA_VERSION, "kind": payload["kind"],
            "bytes_written": size, "include_private": include_private}


def _metric_stats(samples: Iterable[dict]) -> dict:
    readings = {}
    revisions = {}
    last_revisions = {}
    for sample in samples:
        for name, metric in sample["metrics"].items():
            if metric["value"] is None or metric["status"] not in ("LIVE", "OK"):
                continue
            key = (name, metric["unit"])
            if "revision" in metric:
                revision = metric["revision"]
                if key in last_revisions and revision < last_revisions[key]:
                    revisions[key] = set()
                last_revisions[key] = revision
                if revision in revisions.setdefault(key, set()):
                    continue
                revisions[key].add(revision)
            readings.setdefault(key, []).append(float(metric["value"]))
    return {key: {"count": len(values), "mean": math.fsum(value / len(values) for value in values),
                  "min": min(values), "max": max(values)}
            for key, values in readings.items()}


def compare_sessions(path_a, path_b) -> ToolReport:
    first, second = load_session(path_a), load_session(path_b)
    stats_a, stats_b = _metric_stats(first["samples"]), _metric_stats(second["samples"])
    rows = []
    comparisons = []
    for name, unit in sorted(set(stats_a) & set(stats_b)):
        a, b = stats_a[(name, unit)], stats_b[(name, unit)]
        delta = b["mean"] - a["mean"]
        if not math.isfinite(delta):
            delta = None
        rows.append((name, unit, "%.3g" % a["mean"], "%.3g" % b["mean"],
                     "out of range" if delta is None else "%+.3g" % delta,
                     "%d / %d" % (a["count"], b["count"])))
        comparisons.append({"metric": name, "unit": unit, "first": a, "second": b,
                            "mean_change": delta})
    spans = [samples[-1]["elapsed_s"] - samples[0]["elapsed_s"] if samples else 0.0
             for samples in (first["samples"], second["samples"])]
    details = ["Observed sample means; unequal timing or workload can affect this comparison.",
               "Session spans: %.1f s / %.1f s." % tuple(spans)]
    configurations = []
    changed = []
    for index, session in enumerate((first, second)):
        configuration = {}
        changed_fields = set()
        for sample in session["samples"]:
            for name, value in sample.get("system", {}).items():
                if isinstance(value, str) and value.casefold() in ("unknown", "unavailable", "checking"):
                    continue
                if name in configuration and configuration[name] != value:
                    changed_fields.add(name)
                else:
                    configuration.setdefault(name, value)
        configurations.append(configuration)
        changed.append(sorted(changed_fields))
        if changed_fields:
            details.append("Configuration changed within session %d: %s." %
                           (index + 1, ", ".join(sorted(changed_fields))))
    labels = {"model": "Mac model", "chip": "Chip", "os": "macOS", "build": "Build",
              "ram_bytes": "RAM bytes"}
    differences = []
    for name in ("model", "chip", "os", "build", "ram_bytes"):
        if name in configurations[0] and name in configurations[1]:
            if configurations[0][name] != configurations[1][name]:
                differences.append(name)
                details.append("%s differs: %s / %s." %
                               (labels[name], configurations[0][name], configurations[1][name]))
    required = set(labels)
    if any(set(configuration) != required for configuration in configurations):
        details.append("Host configuration is incomplete; unknown fields cannot be compared.")
    if not first["complete"] or not second["complete"]:
        details.append("At least one recording is incomplete; missing intervals are excluded.")
    return ToolReport(tool="compare", status="LIVE" if rows else "EMPTY",
                      summary="%d comparable metrics" % len(rows),
                      columns=("Metric", "Unit", "First mean", "Second mean", "Change", "Samples"),
                      rows=tuple(rows), details=tuple(details),
                      complete=first["complete"] and second["complete"],
                      truncated=first["truncated"] or second["truncated"],
                      data={"schema_version": SCHEMA_VERSION, "comparisons": comparisons,
                            "spans_s": spans,
                            "configurations": {"first": configurations[0], "second": configurations[1],
                                               "differences": differences,
                                               "changed_fields": changed}})


def process_trends(samples, pid: int, start_id) -> ToolReport:
    """Show one process lifetime; a reused PID never joins the selected lifetime."""

    pid = _integer(pid, "process ID", 1, 2**31 - 1)
    identity = _start_id(start_id)
    if identity is None:
        return ToolReport(tool="process-trends", status="INCOMPLETE",
                          summary="A process start identifier is required for history.")
    if not isinstance(samples, (list, tuple, deque)) or len(samples) > MAX_LOAD_SAMPLES:
        raise ValueError("Invalid process history")
    rows, points = [], []
    last_elapsed = None
    seen = False
    missing = False
    unavailable = False
    gaps = 0
    source_revision = None
    source_time = None
    pending_gap = False
    reading_count = 0
    for raw in samples:
        sample = validate_sample(raw)
        elapsed = sample["elapsed_s"]
        if last_elapsed is not None and elapsed <= last_elapsed:
            raise ValueError("Process history times must increase")
        gap = last_elapsed is not None and elapsed - last_elapsed > 5.0
        last_elapsed = elapsed
        source = sample.get("process_source")
        measured_at = elapsed
        if source is not None:
            if source["status"] != "LIVE":
                unavailable = True
                pending_gap = True
                points.append({"elapsed_s": elapsed, "cpu_pct": None, "rss_bytes": None,
                               "status": source["status"], "source_revision": source["revision"]})
                rows.append(("%.1f s" % elapsed, "—", "—", "—", source["status"]))
                continue
            if source["revision"] == source_revision:
                continue
            source_revision = source["revision"]
            measured_at = max(0.0, elapsed - source["age_s"])
            gap = pending_gap or (source_time is not None and measured_at - source_time > 5.0)
            source_time = measured_at
            pending_gap = False
        matching = [process for process in sample["processes"]
                    if process["pid"] == pid and process["start_id"] == identity]
        if len(matching) > 1:
            raise ValueError("Duplicate process identity")
        if not matching:
            if seen:
                missing = True
                points.append({"elapsed_s": elapsed, "cpu_pct": None,
                               "rss_bytes": None, "status": "MISSING"})
                if source is not None:
                    rows.append(("%.1f s" % measured_at, "—", "—", "—", "NOT OBSERVED"))
                    pending_gap = True
            continue
        process = matching[0]
        seen = True
        reading_count += 1
        if gap:
            gaps += 1
        points.append({"elapsed_s": measured_at, "cpu_pct": process["cpu_pct"],
                       "rss_bytes": process["rss_bytes"], "status": "GAP" if gap else "LIVE"})
        rows.append(("%.1f s" % measured_at,
                     "—" if process["cpu_pct"] is None else "%.1f%%" % process["cpu_pct"],
                     "—" if process["rss_bytes"] is None else "%.1f MiB" % (process["rss_bytes"] / 1024**2),
                     process["state"], "gap" if gap else "live"))
    details = ["History follows one process start identifier."]
    if gaps:
        details.append("%d collection gaps; readings are not interpolated." % gaps)
    if missing:
        details.append("The selected process was not observed in one or more samples.")
    if unavailable:
        details.append("Process readings were unavailable in one or more samples.")
    return ToolReport(tool="process-trends", status=("PARTIAL" if unavailable or missing or gaps else "LIVE")
                      if reading_count else "INCOMPLETE" if unavailable else "EMPTY",
                      summary="%d readings for process %d" % (reading_count, pid),
                      columns=("Time", "CPU", "Memory", "State", "Status"),
                      rows=tuple(rows), details=tuple(details), complete=bool(reading_count)
                      and not gaps and not missing and not unavailable,
                      data={"schema_version": SCHEMA_VERSION, "pid": pid,
                            "start_id": identity, "points": points, "gaps": gaps})
