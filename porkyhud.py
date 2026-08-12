#!/usr/bin/env python3
"""
PorkyHUD: a dependency-free macOS terminal system monitor.

Double-click PorkyHUD.command to run it in Terminal.
Keys: q quit, m sort process list by CPU/MEM, arrows/page keys scroll.
"""

from __future__ import annotations

import argparse
import curses
import ctypes
import ctypes.util
import json
import locale
import math
import os
import platform
import re
import select
import selectors
import shutil
import shlex
import signal
import socket
import struct
import sys
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, Optional, Sequence, TypeVar

from porkyhud_core import (
    AttemptOutcome,
    ContributorState,
    CpuContributor,
    CpuUsage,
    DiskCapacity,
    EvidenceRole,
    FailureCode,
    HealthAssessment,
    HealthFacts,
    MemoryPressure,
    MemoryPressureLevel,
    OsThermalState,
    PolicyState,
    ProcessFact,
    SourceId,
    SourceState,
    SourceView,
    complete_failure,
    complete_success,
    initial_policy_state,
    is_decision_ready,
    new_source,
    reduce_health,
    reset_policy_state,
    select_cpu_contributor,
    source_view,
    start_attempt,
    validate_source_state,
)


REFRESH_SECONDS = 1.0
PROCESS_REFRESH_SECONDS = 2.5
SENSOR_REFRESH_SECONDS = 12.0
DISK_ACTIVITY_REFRESH_SECONDS = 4.0
MEMORY_DETAIL_REFRESH_SECONDS = 2.0
SWAP_REFRESH_SECONDS = 15.0
NETWORK_REFRESH_SECONDS = 1.0
BATTERY_REFRESH_SECONDS = 15.0
OPTIONAL_SENSOR_REFRESH_SECONDS = 15.0
STEADY_STATE_COMMAND_ALLOWANCES: Mapping[str, int] = MappingProxyType({
    "memory_pressure": 60,
    "memory_detail": 30,
    "swap": 4,
    "network": 60,
    "process_table": 24,
    "thermal": 12,
    "battery": 8,
    "disk_capacity": 2,
    "disk_activity": 12,
    "process_gpu": 12,
    "powermetrics": 4,
})
RETRY_LAUNCH_RESERVE = 12
VERSION = "0.1.2"
COPYRIGHT_TEXT = "Copyright (c) DMS"
HISTORY_SECONDS = 60
LAYOUTS = ["balanced", "compute", "thermals", "io", "processes", "compact", "cinema"]
DARK_BG_RGB = "#05070d"
DARK_FG_RGB = "#e6f7ff"
DARK_CURSOR_RGB = "#5df2ff"
POWER_METRICS_PATH = "/usr/bin/powermetrics"
VISUDO_PATH = "/usr/sbin/visudo"
SUDO_PATH = "/usr/bin/sudo"
INSTALL_PATH = "/usr/bin/install"
RM_PATH = "/bin/rm"
SUDOERS_PATH = "/etc/sudoers.d/porkyhud"
POWER_METRICS_SAMPLER_SETS = [
    "thermal,cpu_power,gpu_power,ane_power,battery,smc",
    "thermal,cpu_power,gpu_power,ane_power,battery",
    "all",
]
POWER_METRICS_SAMPLER_INDEX = 0
POWER_METRICS_SAMPLER_LOCK = threading.Lock()
POWER_METRICS_SAMPLE_COUNT = 1
POWER_METRICS_INTERVAL_MS = 1000
PRIVILEGED_COMMAND_TIMEOUT_S = 3
POWER_METRICS_RUNNER_TIMEOUT_S = 5.5
KERNEL_INDEX_SMC = 2
SMC_CMD_READ_BYTES = 5
SMC_CMD_READ_INDEX = 8
SMC_CMD_READ_KEYINFO = 9
SMC_TEMP_KEY_CACHE: list[str] | None = None
BOOT_EPOCH: int | None = None
MEMORY_SIZE_BYTES: float | None = None
MEMORY_SIZE_LOCK = threading.Lock()
SENSITIVE_ARGUMENT_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?:--?|/)(?:api[-_]?key|access[-_]?token|auth(?:orization)?|password|passwd|secret|token)(?:=|\s+))"
    r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)"
)
COMMAND_CATEGORY_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
COMMAND_READ_CHUNK_BYTES = 64 * 1024
COMMAND_TERMINATE_GRACE_SECONDS = 0.2
COMMAND_CLEANUP_SECONDS = 1.0
COMMAND_CLEANUP_RETRY_SECONDS = 0.01
T = TypeVar("T")
THEMES: list[dict[str, Any]] = [
    {
        "name": "Neon Grid",
        "colors": {
            1: 231,
            2: 51,
            3: 220,
            4: 196,
            5: 39,
            6: 46,
            7: 201,
            8: 45,
            9: 171,
            10: 214,
        },
    },
    {
        "name": "Catppuccin",
        "colors": {
            1: 189,
            2: 119,
            3: 229,
            4: 203,
            5: 111,
            6: 153,
            7: 183,
            8: 110,
            9: 147,
            10: 215,
        },
    },
    {
        "name": "Solar Circuit",
        "colors": {
            1: 255,
            2: 82,
            3: 226,
            4: 202,
            5: 33,
            6: 118,
            7: 208,
            8: 87,
            9: 165,
            10: 220,
        },
    },
    {
        "name": "Graphite",
        "colors": {
            1: 252,
            2: 250,
            3: 222,
            4: 203,
            5: 245,
            6: 110,
            7: 248,
            8: 152,
            9: 183,
            10: 216,
        },
    },
]


@dataclass
class HudConfig:
    theme_index: int = 0
    animation_mode: int = 1
    layout_index: int = 0
    show_help: bool = False
    message: str = ""
    message_until: float = 0.0

    @property
    def theme_name(self) -> str:
        return THEMES[self.theme_index % len(THEMES)]["name"]

    @property
    def layout_name(self) -> str:
        return LAYOUTS[self.layout_index % len(LAYOUTS)]


@dataclass
class ProcessRow:
    pid: int
    cpu: float
    mem: float
    gpu: float | None
    stat: str
    etime: str
    command: str


@dataclass(frozen=True)
class GpuCounterSnapshot:
    """A complete AGX counter enumeration, isolated from mutable parser state."""

    counters_ns: Mapping[int, int]
    identities: Mapping[int, str]
    complete: bool

    def __post_init__(self) -> None:
        counters = dict(self.counters_ns)
        identities = dict(self.identities)
        if not isinstance(self.complete, bool) or set(counters) != set(identities):
            raise ValueError("GPU counter evidence must have matching identities")
        for pid, counter in counters.items():
            if type(pid) is not int or pid <= 0 or type(counter) is not int or counter < 0:
                raise ValueError("GPU counter evidence must use positive PIDs and nonnegative counters")
            if not isinstance(identities[pid], str) or not identities[pid]:
                raise ValueError("GPU counter evidence requires non-empty identities")
        object.__setattr__(self, "counters_ns", MappingProxyType(counters))
        object.__setattr__(self, "identities", MappingProxyType(identities))


@dataclass(frozen=True)
class GpuProcessSample:
    """Measured GPU attribution; missing map entries deliberately remain unknown."""

    percentages: Mapping[int, float]
    identities: Mapping[int, str]
    complete: bool
    baseline_ready: bool

    def __post_init__(self) -> None:
        percentages = dict(self.percentages)
        identities = dict(self.identities)
        if not isinstance(self.complete, bool) or not isinstance(self.baseline_ready, bool):
            raise ValueError("GPU sample flags must be booleans")
        if not set(percentages).issubset(identities):
            raise ValueError("GPU percentages must have matching identities")
        for pid, identity in identities.items():
            if type(pid) is not int or pid <= 0 or not isinstance(identity, str) or not identity:
                raise ValueError("GPU samples require positive PIDs and non-empty identities")
        for pid, percentage in percentages.items():
            if type(pid) is not int or pid <= 0 or not isinstance(percentage, (int, float)) or isinstance(percentage, bool) or not math.isfinite(percentage) or percentage < 0:
                raise ValueError("GPU percentages must be finite and nonnegative")
        object.__setattr__(self, "percentages", MappingProxyType(percentages))
        object.__setattr__(self, "identities", MappingProxyType(identities))


@dataclass
class BatteryInfo:
    percent: int | None
    present: bool
    source: str
    state: str
    remaining: str
    external_connected: bool
    is_charging: bool
    fully_charged: bool
    cycle_count: int | None
    design_cycles: int | None
    health_percent: int | None
    temp_c: float | None
    virtual_temp_c: float | None
    voltage_v: float | None
    amperage_a: float | None
    charger_watts: int | None
    charger_name: str


@dataclass
class FanReading:
    name: str
    rpm: int
    minimum_rpm: int | None = None
    maximum_rpm: int | None = None
    target_rpm: int | None = None
    mode: str = ""


@dataclass
class TempReading:
    key: str
    name: str
    value_c: float


@dataclass
class SensorInfo:
    thermal_warning: str
    performance_warning: str
    cpu_power_w: float | None
    gpu_power_w: float | None
    cpu_temp_c: float | None
    gpu_temp_c: float | None
    fan_rpm: int | None
    privileged_locked: bool
    raw_hint: str
    ane_power_w: float | None = None
    dram_power_w: float | None = None
    gpu_sram_power_w: float | None = None
    package_power_w: float | None = None
    gpu_active_percent: float | None = None
    gpu_freq_mhz: int | None = None
    e_cluster_active: float | None = None
    p_cluster_active: float | None = None
    s_cluster_active: float | None = None
    e_cluster_freq_mhz: int | None = None
    p_cluster_freq_mhz: int | None = None
    s_cluster_freq_mhz: int | None = None
    dram_read_gbs: float | None = None
    dram_write_gbs: float | None = None
    fan_count: int = 0
    fans: list[FanReading] = field(default_factory=list)
    temp_sensors: list[TempReading] = field(default_factory=list)
    sensor_sample_age_s: float = 0.0


@dataclass
class DiskInfo:
    label: str
    mount: str
    total: int
    used: int
    free: int


@dataclass
class DiskActivity:
    bytes_per_sec: float = 0.0
    iops: float = 0.0
    sample_age: float = 0.0
    source: str = "iostat"


@dataclass
class RuntimeStats:
    memory: dict[str, float]
    battery: BatteryInfo
    disk: DiskInfo
    network_totals: tuple[int, int]
    load_average: tuple[float, float, float]
    collected_at: float


@dataclass
class MetricHistory:
    cpu: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY_SECONDS))
    ram: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY_SECONDS))
    net: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY_SECONDS))
    disk: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY_SECONDS))

    def add(self, cpu_pct: float, ram_pct: float, net_bps: float, disk_pct: float) -> None:
        self.cpu.append(clamp(cpu_pct))
        self.ram.append(clamp(ram_pct))
        self.net.append(max(0.0, net_bps))
        self.disk.append(clamp(disk_pct))


LIVE_SOURCE_SPECS: tuple[tuple[SourceId, float, float], ...] = (
    (SourceId.CPU_USAGE, 1.0, 2.5),
    (SourceId.MEMORY_PRESSURE, 1.0, 2.5),
    (SourceId.THERMAL_OS, 5.0, 12.0),
    (SourceId.DISK_CAPACITY, 30.0, 75.0),
    (SourceId.PROCESS_TABLE, 2.5, 7.5),
    (SourceId.PROCESS_GPU, 5.0, 15.0),
)


def steady_state_launch_budget() -> Mapping[str, int]:
    """Publish the fixed half-open minute allowance used by live scheduling tests."""

    scheduled = sum(STEADY_STATE_COMMAND_ALLOWANCES.values())
    return MappingProxyType({
        "scheduled": scheduled,
        "retry_reserve": RETRY_LAUNCH_RESERVE,
        "total": scheduled + RETRY_LAUNCH_RESERVE,
    })


class FrameOrigin(str, Enum):
    LIVE = "live"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class HealthFrame:
    """Immutable presentation projection of one chronologically coherent reduction."""

    assessment: HealthAssessment
    source_views: Mapping[SourceId, SourceView]
    cpu_percent: Optional[float]
    memory_pressure: Optional[MemoryPressure]
    thermal: Optional[OsThermalState]
    disk_capacity: Optional[DiskCapacity]
    contributor: CpuContributor
    origin: FrameOrigin
    captured_mono_s: float
    decisive_triggered_mono_s: Optional[float]

    def __post_init__(self) -> None:
        if not isinstance(self.assessment, HealthAssessment):
            raise ValueError("assessment must be a HealthAssessment")
        if not isinstance(self.origin, FrameOrigin):
            raise ValueError("origin must be a FrameOrigin")
        if (type(self.captured_mono_s) not in (int, float)
                or not math.isfinite(self.captured_mono_s)):
            raise ValueError("captured_mono_s must be finite")
        if self.decisive_triggered_mono_s is not None and (
                type(self.decisive_triggered_mono_s) not in (int, float)
                or not math.isfinite(self.decisive_triggered_mono_s)):
            raise ValueError("decisive_triggered_mono_s must be finite or None")
        ordered = dict(self.source_views)
        if tuple(ordered) != tuple(source_id for source_id, _cadence, _stale in LIVE_SOURCE_SPECS):
            raise ValueError("source_views must contain the live registry in order")
        if any(view.source_id is not source_id for source_id, view in ordered.items()):
            raise ValueError("source view has the wrong source id")
        object.__setattr__(self, "source_views", MappingProxyType(ordered))


class AsyncPoller:
    """Runs slow collectors off the render loop and keeps the last good sample."""

    def __init__(
        self,
        interval: float,
        collector: Callable[[], Any],
        initial: Any = None,
        initial_is_fresh: bool = False,
    ) -> None:
        self.interval = interval
        self.collector = collector
        self.value = initial
        self.error = ""
        initialized_at = time.monotonic() if initial_is_fresh else 0.0
        self.last_success = initialized_at
        self.last_start = initialized_at
        self.last_finish = initialized_at
        self.first_start = initialized_at
        self._running = False
        self._force_pending = False
        self._lock = threading.Lock()

    def tick(self, now: float, force: bool = False) -> Any:
        thread: threading.Thread | None = None
        with self._lock:
            due = force or self.last_start == 0.0 or now - self.last_finish >= self.interval
            if force and self._running:
                self._force_pending = True
            elif due and not self._running:
                thread = self._prepare_thread(now)
            value = self.value
        if thread is not None:
            thread.start()
        return value

    def _prepare_thread(self, now: float) -> threading.Thread:
        self._running = True
        self.last_start = now
        if self.first_start == 0.0:
            self.first_start = now
        return threading.Thread(target=self._run, daemon=True)

    def snapshot(self, now: float | None = None) -> tuple[Any, float, str]:
        sampled_at = time.monotonic() if now is None else now
        with self._lock:
            reference = self.last_success or self.first_start
            age = max(0.0, sampled_at - reference) if reference else 0.0
            return self.value, age, self.error

    def _run(self) -> None:
        try:
            value = self.collector()
            error = ""
        except Exception as exc:
            value = None
            error = str(exc)
        next_thread: threading.Thread | None = None
        with self._lock:
            if value is not None:
                self.value = value
                self.last_success = time.monotonic()
            self.error = error
            self.last_finish = time.monotonic()
            self._running = False
            if self._force_pending:
                self._force_pending = False
                next_thread = self._prepare_thread(self.last_finish)
        if next_thread is not None:
            next_thread.start()


class SourcePoller(Generic[T]):
    """Single-flight scheduler for collectors that return typed evidence."""

    _RETRY_DELAYS = (1.0, 2.0, 4.0, 10.0)
    _TERMINAL_OUTCOMES = (
        AttemptOutcome.UNSUPPORTED,
        AttemptOutcome.NOT_APPLICABLE,
    )

    def __init__(
        self,
        initial_state: SourceState[T],
        collector: Callable[[], AdapterResult[T]],
    ) -> None:
        if not isinstance(initial_state, SourceState):
            raise ValueError("initial_state must be a SourceState")
        if initial_state.active_revision is not None:
            raise ValueError("initial_state cannot have an active attempt")
        validation_now = 0.0
        if initial_state.last_attempt is not None:
            validation_now = max(
                validation_now, initial_state.last_attempt.completed_mono_s
            )
        if initial_state.last_valid is not None:
            validation_now = max(
                validation_now, initial_state.last_valid.completed_mono_s
            )
        validate_source_state(initial_state, validation_now)
        if not callable(collector):
            raise ValueError("collector must be callable")

        self._state = initial_state
        self._collector = collector
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._work_pending = False
        self._pending_revision: Optional[int] = None
        self._force_pending = False
        self._failure_count = 0
        self._next_due_mono_s: Optional[float] = None
        self._automatic_stopped = False
        self._closed = False
        self._initialize_schedule(initial_state)
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def _initialize_schedule(self, state: SourceState[T]) -> None:
        attempt = state.last_attempt
        if attempt is None:
            return
        if attempt.outcome is AttemptOutcome.SUCCESS:
            self._next_due_mono_s = (
                attempt.completed_mono_s + state.expected_cadence_s
            )
        elif attempt.outcome in self._TERMINAL_OUTCOMES:
            self._automatic_stopped = True
        else:
            self._failure_count = 1
            self._next_due_mono_s = attempt.completed_mono_s + 1.0

    @staticmethod
    def _validate_mono(value: float, label: str) -> float:
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
        ):
            raise ValueError("%s must be a finite int or float" % label)
        return float(value)

    def tick(
        self, now_mono_s: float, force: bool = False
    ) -> SourceState[T]:
        with self._condition:
            if self._closed:
                raise RuntimeError("source poller is closed")
            now_mono_s = self._validate_mono(now_mono_s, "now_mono_s")
            if type(force) is not bool:
                raise ValueError("force must be a bool")
            if self._state.active_revision is not None:
                if force:
                    self._failure_count = 0
                    self._automatic_stopped = False
                    self._next_due_mono_s = None
                    self._force_pending = True
                return self._state

            due = (
                not self._automatic_stopped
                and (
                    self._next_due_mono_s is None
                    or now_mono_s >= self._next_due_mono_s
                )
            )
            if force or due:
                state, revision = start_attempt(self._state, now_mono_s)
                self._state = state
                self._pending_revision = revision
                self._work_pending = True
                if force:
                    self._failure_count = 0
                    self._automatic_stopped = False
                    self._next_due_mono_s = None
                self._condition.notify_all()
            return self._state

    def snapshot(self) -> SourceState[T]:
        with self._condition:
            return self._state

    def close(self, timeout_s: float = 1.0) -> None:
        timeout_s = self._validate_mono(timeout_s, "timeout_s")
        if timeout_s < 0:
            raise ValueError("timeout_s must be nonnegative")
        with self._condition:
            self._closed = True
            self._force_pending = False
            if (
                self._work_pending
                and self._pending_revision is not None
                and self._state.active_revision == self._pending_revision
            ):
                completed_mono_s, completed_at_utc = self._fallback_completion(
                    self._state.active_started_mono_s or 0.0
                )
                self._state = complete_failure(
                    self._state,
                    self._pending_revision,
                    AttemptOutcome.CANCELLED,
                    FailureCode.USER_CANCELLED,
                    completed_mono_s,
                    completed_at_utc,
                )
                self._pending_revision = None
            self._work_pending = False
            self._condition.notify_all()
            worker = self._worker
        if worker is not threading.current_thread():
            worker.join(timeout_s)

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._work_pending:
                    if self._closed:
                        return
                    self._condition.wait()
                if self._closed:
                    self._work_pending = False
                    return
                revision = self._pending_revision
                self._work_pending = False

            try:
                result: Any = self._collector()
            except BaseException:
                result = None

            with self._condition:
                if revision is None or self._state.active_revision != revision:
                    if self._pending_revision == revision:
                        self._pending_revision = None
                    self._condition.notify_all()
                    if self._closed:
                        return
                    continue

                completion_mono_s = self._complete_locked(revision, result)
                self._pending_revision = None
                if self._force_pending and not self._closed:
                    self._force_pending = False
                    self._failure_count = 0
                    self._automatic_stopped = False
                    self._next_due_mono_s = None
                    state, next_revision = start_attempt(
                        self._state, completion_mono_s
                    )
                    self._state = state
                    self._pending_revision = next_revision
                    self._work_pending = True
                self._condition.notify_all()
                if self._closed and not self._work_pending:
                    return

    def _complete_locked(self, revision: int, result: Any) -> float:
        started_mono_s = self._state.active_started_mono_s
        if started_mono_s is None:
            return 0.0
        validated = self._validated_result(result, started_mono_s)
        if validated is None:
            completed_mono_s, completed_at_utc = self._fallback_completion(
                started_mono_s
            )
            self._state = complete_failure(
                self._state,
                revision,
                AttemptOutcome.UNKNOWN_ERROR,
                FailureCode.UNEXPECTED_EXCEPTION,
                completed_mono_s,
                completed_at_utc,
            )
            outcome = AttemptOutcome.UNKNOWN_ERROR
        elif validated.outcome is AttemptOutcome.SUCCESS:
            self._state = complete_success(
                self._state,
                revision,
                validated.value,
                validated.completed_mono_s,
                validated.completed_at_utc,
            )
            completed_mono_s = validated.completed_mono_s
            outcome = validated.outcome
        else:
            self._state = complete_failure(
                self._state,
                revision,
                validated.outcome,
                validated.failure,
                validated.completed_mono_s,
                validated.completed_at_utc,
            )
            completed_mono_s = validated.completed_mono_s
            outcome = validated.outcome

        if outcome is AttemptOutcome.SUCCESS:
            self._failure_count = 0
            self._automatic_stopped = False
            self._next_due_mono_s = (
                completed_mono_s + self._state.expected_cadence_s
            )
        elif outcome in self._TERMINAL_OUTCOMES:
            self._automatic_stopped = True
            self._next_due_mono_s = None
        else:
            self._failure_count += 1
            delay_index = min(
                self._failure_count - 1, len(self._RETRY_DELAYS) - 1
            )
            self._automatic_stopped = False
            self._next_due_mono_s = (
                completed_mono_s + self._RETRY_DELAYS[delay_index]
            )
        return completed_mono_s

    @staticmethod
    def _validated_result(
        result: Any, accepted_started_mono_s: float
    ) -> Optional[AdapterResult[Any]]:
        if not isinstance(result, AdapterResult):
            return None
        try:
            validated = AdapterResult(
                result.value,
                result.started_mono_s,
                result.completed_mono_s,
                result.completed_at_utc,
                result.outcome,
                result.failure,
            )
        except BaseException:
            return None
        if validated.started_mono_s < accepted_started_mono_s:
            return None
        return validated

    @staticmethod
    def _fallback_completion(started_mono_s: float) -> tuple[float, datetime]:
        try:
            sampled_mono_s = time.monotonic()
        except BaseException:
            sampled_mono_s = started_mono_s
        if (
            type(sampled_mono_s) not in (int, float)
            or not math.isfinite(sampled_mono_s)
        ):
            sampled_mono_s = started_mono_s
        completed_mono_s = max(float(sampled_mono_s), started_mono_s)
        try:
            completed_at_utc = _normalize_utc_timestamp(
                utc_now(), "source completion timestamp"
            )
        except BaseException:
            completed_at_utc = datetime.now(timezone.utc)
        return completed_mono_s, completed_at_utc


def run_command(args: list[str], timeout: float = 2.0) -> str:
    try:
        return subprocess.check_output(
            args,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).strip()
    except Exception:
        return ""


def _normalize_utc_timestamp(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("%s must be timezone-aware" % label)
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise ValueError("%s must have a valid UTC offset" % label) from exc
    if offset is None:
        raise ValueError("%s must be timezone-aware" % label)
    try:
        return value.astimezone(timezone.utc)
    except Exception as exc:
        raise ValueError("%s must have a valid UTC offset" % label) from exc


@dataclass(frozen=True)
class CommandResult:
    """A bounded, classified command execution result for effect adapters."""

    category: str
    stdout: str
    stderr: str
    returncode: Optional[int]
    started_mono_s: float
    completed_mono_s: float
    completed_at_utc: datetime
    outcome: AttemptOutcome
    failure: Optional[FailureCode]

    def __post_init__(self) -> None:
        if not isinstance(self.category, str) or not COMMAND_CATEGORY_PATTERN.fullmatch(self.category):
            raise ValueError("command category must be a stable safe identifier")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("command output must be text")
        if self.returncode is not None and (
            isinstance(self.returncode, bool) or not isinstance(self.returncode, int)
        ):
            raise TypeError("command return code must be an integer or None")
        if (
            not isinstance(self.started_mono_s, (int, float))
            or isinstance(self.started_mono_s, bool)
            or not isinstance(self.completed_mono_s, (int, float))
            or isinstance(self.completed_mono_s, bool)
            or not math.isfinite(self.started_mono_s)
            or not math.isfinite(self.completed_mono_s)
            or self.completed_mono_s < self.started_mono_s
        ):
            raise ValueError("command timestamps must be finite and ordered")
        object.__setattr__(
            self,
            "completed_at_utc",
            _normalize_utc_timestamp(self.completed_at_utc, "command completion timestamp"),
        )
        if not isinstance(self.outcome, AttemptOutcome):
            raise TypeError("command outcome must be an AttemptOutcome")
        if self.failure is not None and not isinstance(self.failure, FailureCode):
            raise TypeError("command failure must be a FailureCode or None")
        legal_failures = {
            AttemptOutcome.SUCCESS: set(),
            AttemptOutcome.MISSING_DEPENDENCY: {FailureCode.MISSING_EXECUTABLE},
            AttemptOutcome.PERMISSION_DENIED: {FailureCode.PERMISSION_DENIED},
            AttemptOutcome.TIMEOUT: {FailureCode.COMMAND_TIMEOUT, FailureCode.DEADLINE_EXCEEDED},
            AttemptOutcome.COMMAND_ERROR: {
                FailureCode.NONZERO_EXIT,
                FailureCode.DECODE_ERROR,
                FailureCode.OUTPUT_LIMIT,
            },
            AttemptOutcome.UNKNOWN_ERROR: {FailureCode.UNEXPECTED_EXCEPTION},
        }
        allowed = legal_failures.get(self.outcome)
        if allowed is None or (
            self.outcome is AttemptOutcome.SUCCESS and self.failure is not None
        ) or (
            self.outcome is not AttemptOutcome.SUCCESS and self.failure not in allowed
        ):
            raise ValueError("command outcome and failure are inconsistent")


@dataclass(frozen=True)
class AdapterResult(Generic[T]):
    """A parsed health observation with the outcome of its bounded attempt."""

    value: Optional[T]
    started_mono_s: float
    completed_mono_s: float
    completed_at_utc: datetime
    outcome: AttemptOutcome
    failure: Optional[FailureCode]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.started_mono_s, (int, float))
            or isinstance(self.started_mono_s, bool)
            or not isinstance(self.completed_mono_s, (int, float))
            or isinstance(self.completed_mono_s, bool)
            or not math.isfinite(self.started_mono_s)
            or not math.isfinite(self.completed_mono_s)
            or self.completed_mono_s < self.started_mono_s
        ):
            raise ValueError("adapter timestamps must be finite and ordered")
        object.__setattr__(
            self,
            "completed_at_utc",
            _normalize_utc_timestamp(self.completed_at_utc, "adapter completion timestamp"),
        )
        legal_failures = {
            AttemptOutcome.SUCCESS: set(),
            AttemptOutcome.NOT_APPLICABLE: {FailureCode.NOT_APPLICABLE},
            AttemptOutcome.UNSUPPORTED: {FailureCode.UNSUPPORTED_CAPABILITY},
            AttemptOutcome.PERMISSION_DENIED: {FailureCode.PERMISSION_DENIED},
            AttemptOutcome.MISSING_DEPENDENCY: {FailureCode.MISSING_EXECUTABLE},
            AttemptOutcome.TIMEOUT: {FailureCode.COMMAND_TIMEOUT, FailureCode.DEADLINE_EXCEEDED},
            AttemptOutcome.COMMAND_ERROR: {
                FailureCode.NONZERO_EXIT, FailureCode.DECODE_ERROR, FailureCode.OUTPUT_LIMIT,
            },
            AttemptOutcome.PARSE_ERROR: {FailureCode.EMPTY_OUTPUT, FailureCode.PARSER_REJECTED},
            AttemptOutcome.CANCELLED: {FailureCode.USER_CANCELLED},
            AttemptOutcome.UNKNOWN_ERROR: {FailureCode.UNEXPECTED_EXCEPTION},
        }
        if not isinstance(self.outcome, AttemptOutcome) or (
            self.failure is not None and not isinstance(self.failure, FailureCode)
        ):
            raise TypeError("adapter outcome and failure must be core enums")
        allowed = legal_failures[self.outcome]
        if (self.outcome is AttemptOutcome.SUCCESS and (self.failure is not None or self.value is None)) or (
            self.outcome is not AttemptOutcome.SUCCESS and (self.failure not in allowed or self.value is not None)
        ):
            raise ValueError("adapter outcome, failure, and value are inconsistent")


def utc_now() -> datetime:
    """Indirection keeps adapter completion-time tests deterministic."""

    return datetime.now(timezone.utc)


def _adapter_from_command(result: CommandResult, parser: Callable[[str], T]) -> AdapterResult[T]:
    if result.outcome is not AttemptOutcome.SUCCESS:
        return AdapterResult(
            None, result.started_mono_s, result.completed_mono_s, result.completed_at_utc,
            result.outcome, result.failure,
        )
    try:
        value = parser(result.stdout)
    except ValueError:
        failure = FailureCode.EMPTY_OUTPUT if not result.stdout.strip() else FailureCode.PARSER_REJECTED
        return AdapterResult(
            None, result.started_mono_s, time.monotonic(), utc_now(),
            AttemptOutcome.PARSE_ERROR, failure,
        )
    return AdapterResult(
        value, result.started_mono_s, time.monotonic(), utc_now(), AttemptOutcome.SUCCESS, None,
    )


def parse_memory_pressure_level(raw: str) -> MemoryPressure:
    """Parse only the public vm-pressure values Apple documents."""

    value = raw.strip()
    if value not in {"1", "2", "4"} or not re.fullmatch(r"[ \t\r]*[124][ \t\r]*\n?", raw):
        raise ValueError("unrecognized memory pressure level")
    return MemoryPressure(MemoryPressureLevel(int(value)))


def collect_memory_pressure() -> AdapterResult[MemoryPressure]:
    return _adapter_from_command(
        run_structured_command(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            category="memory-pressure", timeout_s=1.0, stdout_limit=8192, stderr_limit=8192,
        ),
        parse_memory_pressure_level,
    )


_THERMAL_CLEAR = "Note: No thermal warning level has been recorded"
_PERFORMANCE_CLEAR = "Note: No performance warning level has been recorded"
_THERMAL_NUMERIC = re.compile(r"^Thermal Warning Level = ([0-9]+)$")
_PERFORMANCE_NUMERIC = re.compile(r"^Performance Warning Level = ([0-9]+)$")


def parse_os_thermal_state(raw: str) -> OsThermalState:
    """Require one exact public thermal and performance indication each."""

    thermal: Optional[int] = None
    performance: Optional[int] = None
    for line in raw.splitlines():
        if line == _THERMAL_CLEAR:
            candidate = 0
            if thermal is not None:
                raise ValueError("duplicate thermal signal")
            thermal = candidate
            continue
        if line == _PERFORMANCE_CLEAR:
            candidate = 0
            if performance is not None:
                raise ValueError("duplicate performance signal")
            performance = candidate
            continue
        thermal_match = _THERMAL_NUMERIC.fullmatch(line)
        if thermal_match:
            if thermal is not None:
                raise ValueError("duplicate thermal signal")
            thermal = int(thermal_match.group(1))
            continue
        performance_match = _PERFORMANCE_NUMERIC.fullmatch(line)
        if performance_match:
            if performance is not None:
                raise ValueError("duplicate performance signal")
            performance = int(performance_match.group(1))
            continue
        lowered = line.lower()
        if "thermal warning" in lowered or "performance warning" in lowered:
            raise ValueError("unrecognized thermal signal")
    if thermal is None or performance is None:
        raise ValueError("incomplete thermal state")
    return OsThermalState(thermal, performance)


def collect_os_thermal_state() -> AdapterResult[OsThermalState]:
    return _adapter_from_command(
        run_structured_command(
            ["/usr/bin/pmset", "-g", "therm"], category="thermal-os", timeout_s=1.5,
            stdout_limit=8192, stderr_limit=8192,
        ),
        parse_os_thermal_state,
    )


def collect_disk_capacity() -> AdapterResult[DiskCapacity]:
    """Select the writable macOS data volume, then writable root, with provenance."""

    started_mono_s = time.monotonic()
    inspection_failed = False
    for volume_name, mount_point in (("Data", "/System/Volumes/Data"), ("Root", "/")):
        try:
            filesystem = os.statvfs(mount_point)
        except FileNotFoundError:
            continue
        except Exception:
            inspection_failed = True
            continue
        try:
            if filesystem.f_flag & os.ST_RDONLY:
                continue
            usage = shutil.disk_usage(mount_point)
            capacity = DiskCapacity(
                "fsid:%s" % filesystem.f_fsid,
                volume_name,
                mount_point,
                usage.total,
                usage.free,
            )
        except Exception:
            inspection_failed = True
            continue
        return AdapterResult(
            capacity,
            started_mono_s, time.monotonic(), utc_now(), AttemptOutcome.SUCCESS, None,
        )
    completed_mono_s = time.monotonic()
    if inspection_failed:
        return AdapterResult(
            None, started_mono_s, completed_mono_s, utc_now(), AttemptOutcome.UNKNOWN_ERROR,
            FailureCode.UNEXPECTED_EXCEPTION,
        )
    return AdapterResult(
        None, started_mono_s, completed_mono_s, utc_now(), AttemptOutcome.NOT_APPLICABLE,
        FailureCode.NOT_APPLICABLE,
    )


def _validate_structured_command(
    args: Sequence[str], category: str, timeout_s: float, stdout_limit: int, stderr_limit: int
) -> tuple[str, ...]:
    if isinstance(args, (str, bytes)):
        raise TypeError("command arguments must be a sequence of strings")
    try:
        normalized_args = tuple(args)
    except TypeError as exc:
        raise TypeError("command arguments must be a sequence of strings") from exc
    if not normalized_args:
        raise ValueError("command arguments must not be empty")
    if any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in normalized_args):
        raise ValueError("command arguments must be nonempty strings without NUL")
    if not isinstance(category, str) or not COMMAND_CATEGORY_PATTERN.fullmatch(category):
        raise ValueError("command category must be a stable safe identifier")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise ValueError("command timeout must be finite and positive")
    for limit in (stdout_limit, stderr_limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("command output limits must be positive integers")
    return normalized_args


def _command_result(
    category: str,
    stdout: str,
    stderr: str,
    returncode: Optional[int],
    started_mono_s: float,
    outcome: AttemptOutcome,
    failure: Optional[FailureCode],
) -> CommandResult:
    completed_mono_s = time.monotonic()
    return CommandResult(
        category=category,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        started_mono_s=started_mono_s,
        completed_mono_s=max(started_mono_s, completed_mono_s),
        completed_at_utc=datetime.now(timezone.utc),
        outcome=outcome,
        failure=failure,
    )


class _CommandGroupState(Enum):
    ABSENT = "absent"
    EXISTS = "exists"
    UNCONFIRMED = "unconfirmed"


class _CommandLifecycleState(Enum):
    RUNNING = "running"
    CANCELLED = "cancelled"
    NORMAL_COMMITTED = "normal_committed"


@dataclass
class _CommandLifecycleControl:
    state: _CommandLifecycleState = _CommandLifecycleState.RUNNING
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class _CommandWorkerReport:
    result: CommandResult
    interruption: Optional[BaseException] = None
    interruption_traceback: Any = None


@dataclass(frozen=True)
class _CommandWorkerRequest:
    args: tuple[str, ...]
    category: str
    timeout_s: float
    stdout_limit: int
    stderr_limit: int
    started_mono_s: float
    start_gate: threading.Event
    cancel_reader: socket.socket
    published: threading.Event
    control: _CommandLifecycleControl


def _command_start_thread(thread: threading.Thread) -> None:
    thread.start()


def _command_open_start_gate(gate: threading.Event) -> None:
    gate.set()


def _command_wait_publication(event: threading.Event, timeout_s: float) -> bool:
    return event.wait(timeout_s)


def _command_join_thread(thread: threading.Thread, timeout_s: float) -> None:
    thread.join(timeout_s)


def _command_thread_is_alive(thread: threading.Thread) -> bool:
    return thread.is_alive()


def _command_notify_cancel(cancel_writer: socket.socket) -> bool:
    try:
        cancel_writer.send(b"x")
    except (BlockingIOError, BrokenPipeError):
        return True
    except InterruptedError:
        return False
    except OSError:
        return True
    return True


def _command_wait_start_gate(start_gate: threading.Event) -> None:
    start_gate.wait()


def _command_request_cancellation(control: _CommandLifecycleControl) -> bool:
    with control.lock:
        if control.state is _CommandLifecycleState.RUNNING:
            control.state = _CommandLifecycleState.CANCELLED
            return True
        return control.state is _CommandLifecycleState.CANCELLED


def _command_try_normal_commit(control: _CommandLifecycleControl) -> bool:
    with control.lock:
        if control.state is _CommandLifecycleState.RUNNING:
            control.state = _CommandLifecycleState.NORMAL_COMMITTED
            return True
        return control.state is _CommandLifecycleState.NORMAL_COMMITTED


def _command_was_cancelled(control: _CommandLifecycleControl) -> bool:
    with control.lock:
        return control.state is _CommandLifecycleState.CANCELLED


def _command_normal_was_committed(control: _CommandLifecycleControl) -> bool:
    with control.lock:
        return control.state is _CommandLifecycleState.NORMAL_COMMITTED


def _command_worker_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
    return subprocess.Popen(*args, **kwargs)


def _command_worker_popen_owned(
    owner: "_CommandLifecycleWorker", args: tuple[str, ...], **kwargs: Any,
) -> subprocess.Popen[bytes]:
    process = _command_worker_popen(args, **kwargs)
    owner.process = process
    return process


def _command_worker_open_exit_watch(
    owner: "_CommandLifecycleWorker", pid: int,
) -> bool:
    watch = select.kqueue()
    owner.exit_watch = watch
    event = select.kevent(
        pid,
        filter=select.KQ_FILTER_PROC,
        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
        fflags=select.KQ_NOTE_EXIT,
    )
    try:
        watch.control([event], 0, 0)
    except ProcessLookupError:
        return True
    return False


def _command_worker_observe_exit(watch: Any, pid: int) -> bool:
    events = watch.control(None, 1, 0)
    return any(
        event.ident == pid and event.fflags & select.KQ_NOTE_EXIT
        for event in events
    )


def _command_worker_select(
    selector: selectors.BaseSelector, timeout_s: float,
) -> list[tuple[selectors.SelectorKey, int]]:
    return selector.select(timeout_s)


def _command_worker_read(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _command_worker_register(
    selector: selectors.BaseSelector, fileobj: Any, data: str,
) -> None:
    selector.register(fileobj, selectors.EVENT_READ, data)


def _command_worker_signal_group(pgid: int, sig: int) -> _CommandGroupState:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return _CommandGroupState.ABSENT
    return _CommandGroupState.EXISTS


def _command_worker_probe_group(pgid: int) -> _CommandGroupState:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return _CommandGroupState.ABSENT
    except PermissionError:
        return _CommandGroupState.UNCONFIRMED
    return _CommandGroupState.EXISTS


def _command_worker_grace_wait(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _command_worker_retry_wait(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(COMMAND_CLEANUP_RETRY_SECONDS, remaining))


def _command_worker_reap_leader(
    process: subprocess.Popen[bytes], deadline: float,
) -> bool:
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return process.returncode is not None


def _command_worker_unregister(
    selector: selectors.BaseSelector, fileobj: Any,
) -> bool:
    try:
        selector.unregister(fileobj)
    except KeyError:
        pass
    return True


def _command_worker_close_stream(stream: Any) -> bool:
    stream.close()
    return bool(getattr(stream, "closed", True))


def _command_worker_close_socket(sock: socket.socket) -> bool:
    sock.close()
    return sock.fileno() == -1


def _command_worker_close_selector(selector: selectors.BaseSelector) -> bool:
    selector.close()
    return True


def _command_worker_close_exit_watch(watch: Any) -> bool:
    watch.close()
    return bool(getattr(watch, "closed", True))


class _CommandLifecycleWorker:
    """Sole owner of one command process group and all of its I/O resources."""

    def __init__(self, request: _CommandWorkerRequest) -> None:
        self.request = request
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.selector: Optional[selectors.BaseSelector] = None
        self.exit_watch: Any = None
        self.leader_exit_observed = False
        self.streams: list[Any] = []
        self.prepared: Optional[
            tuple[str, str, AttemptOutcome, Optional[FailureCode]]
        ] = None
        self.abnormal = False
        self.group_absent = False
        self.leader_reaped = False
        self.owned_resources_closed = False
        self.cleanup_fault = False
        self.interruption_info: Optional[tuple[BaseException, Any]] = None

    def _capture_failure(self, exc: BaseException) -> None:
        self.cleanup_fault = True
        if not isinstance(exc, Exception) and self.interruption_info is None:
            self.interruption_info = (exc, sys.exc_info()[2])

    def _prepare_unknown(self) -> None:
        self.prepared = (
            "", "", AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
        )

    def _prepare_abnormal(
        self, outcome: AttemptOutcome, failure: FailureCode,
    ) -> None:
        self.abnormal = True
        self.prepared = ("", "", outcome, failure)

    def _spawn(self) -> bool:
        environment = dict(os.environ)
        environment["LC_ALL"] = "C"
        try:
            _command_worker_popen_owned(
                self,
                self.request.args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                env=environment,
            )
        except FileNotFoundError:
            if self.process is None:
                self.prepared = (
                    "", "", AttemptOutcome.MISSING_DEPENDENCY,
                    FailureCode.MISSING_EXECUTABLE,
                )
            else:
                self.abnormal = True
                self._prepare_unknown()
            return False
        except PermissionError:
            if self.process is None:
                self.prepared = (
                    "", "", AttemptOutcome.PERMISSION_DENIED,
                    FailureCode.PERMISSION_DENIED,
                )
            else:
                self.abnormal = True
                self._prepare_unknown()
            return False
        except OSError:
            if self.process is not None:
                self.abnormal = True
            self._prepare_unknown()
            return False
        assert self.process is not None
        self.leader_exit_observed = _command_worker_open_exit_watch(
            self, self.process.pid,
        )
        return True

    def _normal_wait(self, deadline: float) -> bool:
        assert self.process is not None
        while True:
            if _command_was_cancelled(self.request.control):
                self._prepare_abnormal(
                    AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
                )
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._prepare_abnormal(
                    AttemptOutcome.TIMEOUT, FailureCode.COMMAND_TIMEOUT,
                )
                return False
            if not self.leader_exit_observed:
                events = _command_worker_select(self.selector, min(remaining, 0.05))
                for key, _mask in events:
                    if key.data == "cancel":
                        _command_request_cancellation(self.request.control)
                    elif key.data == "leader-exit":
                        self.leader_exit_observed = _command_worker_observe_exit(
                            self.exit_watch, self.process.pid,
                        )
                if not self.leader_exit_observed:
                    continue
            if not _command_try_normal_commit(self.request.control):
                self._prepare_abnormal(
                    AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
                )
                return False
            self.leader_reaped = _command_worker_reap_leader(
                self.process, deadline,
            )
            return self.leader_reaped

    def _collect_output(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None and self.process.stderr is not None
        assert self.selector is not None
        self.streams.extend((self.process.stdout, self.process.stderr))
        _command_worker_register(self.selector, self.request.cancel_reader, "cancel")
        _command_worker_register(self.selector, self.process.stdout, "stdout")
        _command_worker_register(self.selector, self.process.stderr, "stderr")
        assert self.exit_watch is not None
        _command_worker_register(self.selector, self.exit_watch, "leader-exit")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        limits = {
            "stdout": self.request.stdout_limit,
            "stderr": self.request.stderr_limit,
        }
        open_streams = {"stdout", "stderr"}
        deadline = self.request.started_mono_s + self.request.timeout_s

        while open_streams:
            if _command_was_cancelled(self.request.control):
                self._prepare_abnormal(
                    AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._prepare_abnormal(
                    AttemptOutcome.TIMEOUT, FailureCode.COMMAND_TIMEOUT,
                )
                return
            events = _command_worker_select(self.selector, remaining)
            if not events:
                continue
            for key, _mask in events:
                name = key.data
                if name == "cancel":
                    try:
                        self.request.cancel_reader.recv(4096)
                    except BlockingIOError:
                        pass
                    _command_request_cancellation(self.request.control)
                    self._prepare_abnormal(
                        AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
                    )
                    return
                if name == "leader-exit":
                    self.leader_exit_observed = _command_worker_observe_exit(
                        self.exit_watch, self.process.pid,
                    )
                    continue
                if name not in open_streams:
                    continue
                remaining_capacity = limits[name] - len(buffers[name])
                data = _command_worker_read(
                    key.fileobj.fileno(),
                    min(COMMAND_READ_CHUNK_BYTES, remaining_capacity + 1),
                )
                if not data:
                    if not _command_worker_unregister(self.selector, key.fileobj):
                        raise RuntimeError("command stream unregister was not confirmed")
                    open_streams.remove(name)
                    continue
                if len(data) > remaining_capacity:
                    self._prepare_abnormal(
                        AttemptOutcome.COMMAND_ERROR, FailureCode.OUTPUT_LIMIT,
                    )
                    return
                buffers[name].extend(data)

        try:
            stdout = bytes(buffers["stdout"]).decode("utf-8", "strict")
            stderr = bytes(buffers["stderr"]).decode("utf-8", "strict")
        except UnicodeDecodeError:
            self._prepare_abnormal(
                AttemptOutcome.COMMAND_ERROR, FailureCode.DECODE_ERROR,
            )
            return

        if not self._normal_wait(deadline):
            return
        if self.process.returncode == 0:
            self.prepared = (stdout, stderr, AttemptOutcome.SUCCESS, None)
        else:
            self.prepared = (
                stdout, stderr, AttemptOutcome.COMMAND_ERROR, FailureCode.NONZERO_EXIT,
            )

    def _retry_group_operation(
        self, operation: Callable[[int], _CommandGroupState], phase_deadline: float,
    ) -> _CommandGroupState:
        assert self.process is not None
        attempts = 0
        while attempts < 2 or time.monotonic() < phase_deadline:
            attempts += 1
            try:
                return operation(self.process.pid)
            except BaseException as exc:
                self._capture_failure(exc)
                try:
                    _command_worker_retry_wait(phase_deadline)
                except BaseException as wait_exc:
                    self._capture_failure(wait_exc)
        return _CommandGroupState.UNCONFIRMED

    def _retry_reap(self, deadline: float) -> None:
        assert self.process is not None
        attempts = 0
        while not self.leader_reaped and (
            attempts < 2 or time.monotonic() < deadline
        ):
            attempts += 1
            try:
                self.leader_reaped = _command_worker_reap_leader(
                    self.process, deadline,
                )
            except BaseException as exc:
                self._capture_failure(exc)
            if not self.leader_reaped:
                try:
                    _command_worker_retry_wait(deadline)
                except BaseException as wait_exc:
                    self._capture_failure(wait_exc)

    def _terminate_group(self, cleanup_deadline: float) -> None:
        assert self.process is not None
        term_deadline = min(cleanup_deadline, time.monotonic() + 0.05)
        term_state = self._retry_group_operation(
            lambda pgid: _command_worker_signal_group(pgid, signal.SIGTERM),
            term_deadline,
        )
        if term_state is _CommandGroupState.ABSENT:
            self.group_absent = True

        if not self.group_absent:
            grace_deadline = min(
                cleanup_deadline,
                time.monotonic() + COMMAND_TERMINATE_GRACE_SECONDS,
            )
            while time.monotonic() < grace_deadline:
                try:
                    _command_worker_grace_wait(grace_deadline)
                except BaseException as exc:
                    self._capture_failure(exc)

        kill_deadline = max(time.monotonic(), cleanup_deadline - 0.08)
        first_kill = True
        while not self.group_absent and (
            first_kill or time.monotonic() < kill_deadline
        ):
            first_kill = False
            state = self._retry_group_operation(
                lambda pgid: _command_worker_signal_group(pgid, signal.SIGKILL),
                kill_deadline,
            )
            if state is _CommandGroupState.ABSENT:
                self.group_absent = True
                break
            state = self._retry_group_operation(
                _command_worker_probe_group, kill_deadline,
            )
            if state is _CommandGroupState.ABSENT:
                self.group_absent = True
                break
            try:
                _command_worker_retry_wait(kill_deadline)
            except BaseException as exc:
                self._capture_failure(exc)

        self._retry_reap(cleanup_deadline)

    def _retry_cleanup(self, operation: Callable[[], bool], deadline: float) -> bool:
        attempts = 0
        while attempts < 2 or time.monotonic() < deadline:
            attempts += 1
            try:
                if operation():
                    return True
                self.cleanup_fault = True
            except BaseException as exc:
                self._capture_failure(exc)
            try:
                _command_worker_retry_wait(deadline)
            except BaseException as exc:
                self._capture_failure(exc)
        return False

    def _close_owned_resources(self, deadline: float) -> bool:
        operations: list[Callable[[], bool]] = []
        if self.selector is not None:
            for stream in self.streams:
                operations.append(
                    lambda stream=stream: _command_worker_unregister(
                        self.selector, stream,
                    )
                )
            if self.exit_watch is not None:
                operations.append(
                    lambda: _command_worker_unregister(
                        self.selector, self.exit_watch,
                    )
                )
            operations.append(
                lambda: _command_worker_unregister(
                    self.selector, self.request.cancel_reader,
                )
            )
        for stream in self.streams:
            operations.append(
                lambda stream=stream: _command_worker_close_stream(stream)
            )
        operations.append(
            lambda: _command_worker_close_socket(self.request.cancel_reader)
        )
        if self.exit_watch is not None:
            operations.append(
                lambda: _command_worker_close_exit_watch(self.exit_watch)
            )
        if self.selector is not None:
            operations.append(lambda: _command_worker_close_selector(self.selector))
        results = [self._retry_cleanup(operation, deadline) for operation in operations]
        return all(results)

    def execute(self) -> _CommandWorkerReport:
        cleanup_deadline: Optional[float] = None
        try:
            _command_wait_start_gate(self.request.start_gate)
            if _command_was_cancelled(self.request.control):
                self._prepare_unknown()
            else:
                self.selector = selectors.DefaultSelector()
                if self._spawn():
                    self._collect_output()
        except BaseException as exc:
            self._capture_failure(exc)
            if (
                self.process is not None
                and not self.leader_reaped
                and not _command_normal_was_committed(self.request.control)
            ):
                self.abnormal = True
            self._prepare_unknown()
        finally:
            cleanup_deadline = time.monotonic() + COMMAND_CLEANUP_SECONDS
            if self.process is not None:
                for stream in (self.process.stdout, self.process.stderr):
                    if stream is not None and stream not in self.streams:
                        self.streams.append(stream)
            if self.process is not None and not self.leader_reaped:
                if _command_normal_was_committed(self.request.control):
                    self._retry_reap(cleanup_deadline)
                elif self.abnormal:
                    termination_attempts = 0
                    while termination_attempts < 2:
                        termination_attempts += 1
                        try:
                            self._terminate_group(cleanup_deadline)
                            break
                        except BaseException as exc:
                            self._capture_failure(exc)
            try:
                self.owned_resources_closed = self._close_owned_resources(
                    cleanup_deadline,
                )
            except BaseException as exc:
                self._capture_failure(exc)
                self.owned_resources_closed = False

        if self.prepared is None:
            self._prepare_unknown()
        verified = self.owned_resources_closed
        if self.process is not None:
            verified = verified and self.leader_reaped
            if self.abnormal:
                verified = verified and self.group_absent
        if self.cleanup_fault or not verified:
            self._prepare_unknown()
        assert self.prepared is not None
        stdout, stderr, outcome, failure = self.prepared
        returncode = self.process.returncode if self.process is not None else None
        result = _command_result(
            self.request.category,
            stdout,
            stderr,
            returncode,
            self.request.started_mono_s,
            outcome,
            failure,
        )
        interruption: Optional[BaseException] = None
        interruption_traceback: Any = None
        if self.interruption_info is not None:
            interruption, interruption_traceback = self.interruption_info
        return _CommandWorkerReport(result, interruption, interruption_traceback)


def _private_unknown_command_result(category: str, started_mono_s: float) -> CommandResult:
    return _command_result(
        category,
        "",
        "",
        None,
        started_mono_s,
        AttemptOutcome.UNKNOWN_ERROR,
        FailureCode.UNEXPECTED_EXCEPTION,
    )


def _command_publish_report(
    request: _CommandWorkerRequest,
    reports: list[_CommandWorkerReport],
    report: _CommandWorkerReport,
) -> None:
    reports[:] = [report]
    request.published.set()


def _command_worker_entry(
    request: _CommandWorkerRequest, reports: list[_CommandWorkerReport],
) -> None:
    try:
        report = _CommandLifecycleWorker(request).execute()
    except BaseException as exc:
        interruption = None if isinstance(exc, Exception) else exc
        traceback = None if interruption is None else sys.exc_info()[2]
        try:
            request.cancel_reader.close()
        except BaseException:
            pass
        report = _CommandWorkerReport(
            _private_unknown_command_result(
                request.category, request.started_mono_s,
            ),
            interruption,
            traceback,
        )
    publication_interruption: Optional[tuple[BaseException, Any]] = None
    if report.interruption is not None:
        publication_interruption = (
            report.interruption, report.interruption_traceback,
        )
    while True:
        try:
            _command_publish_report(request, reports, report)
            return
        except BaseException as exc:
            if not isinstance(exc, Exception) and publication_interruption is None:
                publication_interruption = (exc, sys.exc_info()[2])
            report = _CommandWorkerReport(
                _private_unknown_command_result(
                    request.category, request.started_mono_s,
                ),
                report.interruption,
                report.interruption_traceback,
            )
            if publication_interruption is not None:
                report = _CommandWorkerReport(
                    report.result,
                    publication_interruption[0],
                    publication_interruption[1],
                )

def _close_command_socket_in_caller(sock: socket.socket) -> bool:
    sock.close()
    return sock.fileno() == -1


def run_structured_command(
    args: Sequence[str],
    category: str,
    timeout_s: float,
    stdout_limit: int,
    stderr_limit: int,
) -> CommandResult:
    """Run a no-shell command with bounded output and private failure classification."""

    normalized_args = _validate_structured_command(
        args, category, timeout_s, stdout_limit, stderr_limit
    )
    started_mono_s = time.monotonic()
    interruption_info: Optional[tuple[BaseException, Any]] = None
    caller_fault = False

    def capture(exc: BaseException) -> None:
        nonlocal interruption_info, caller_fault
        caller_fault = True
        if not isinstance(exc, Exception) and interruption_info is None:
            interruption_info = (exc, sys.exc_info()[2])

    def close_caller_socket(sock: socket.socket) -> bool:
        nonlocal caller_fault
        deadline = time.monotonic() + 0.2
        attempts = 0
        while attempts < 2 or time.monotonic() < deadline:
            attempts += 1
            try:
                if _close_command_socket_in_caller(sock):
                    return True
                caller_fault = True
            except BaseException as exc:
                capture(exc)
            try:
                time.sleep(0)
            except BaseException as exc:
                capture(exc)
        return False

    cancel_reader: Optional[socket.socket] = None
    cancel_writer: Optional[socket.socket] = None
    start_gate: Optional[threading.Event] = None
    published: Optional[threading.Event] = None
    control: Optional[_CommandLifecycleControl] = None
    reports: list[_CommandWorkerReport] = []
    thread: Optional[threading.Thread] = None
    request: Optional[_CommandWorkerRequest] = None
    start_returned = False
    start_attempted = False
    setup_ready = False
    lifecycle_complete = False
    report: Optional[_CommandWorkerReport] = None

    def request_cancel() -> None:
        if control is None:
            return
        while True:
            try:
                _command_request_cancellation(control)
                return
            except BaseException as exc:
                capture(exc)

    def open_gate() -> None:
        if start_gate is None:
            return
        while True:
            try:
                _command_open_start_gate(start_gate)
                return
            except BaseException as exc:
                capture(exc)
                request_cancel()

    notified = False

    def notify_cancellation() -> None:
        nonlocal notified
        request_cancel()
        if cancel_writer is None:
            return
        while not notified:
            try:
                notified = _command_notify_cancel(cancel_writer)
            except BaseException as exc:
                capture(exc)

    try:
        try:
            cancel_reader, cancel_writer = socket.socketpair()
            assert cancel_reader is not None and cancel_writer is not None
            cancel_reader.setblocking(False)
            cancel_writer.setblocking(False)
            start_gate = threading.Event()
            published = threading.Event()
            control = _CommandLifecycleControl()
            request = _CommandWorkerRequest(
                normalized_args,
                category,
                float(timeout_s),
                stdout_limit,
                stderr_limit,
                started_mono_s,
                start_gate,
                cancel_reader,
                published,
                control,
            )
            thread = threading.Thread(
                target=_command_worker_entry,
                args=(request, reports),
                name="PorkyHUD-command-%x" % id(published),
                daemon=False,
            )
        except BaseException as exc:
            capture(exc)
        setup_ready = request is not None and thread is not None

        while setup_ready and not lifecycle_complete:
            try:
                if not start_attempted:
                    start_attempted = True
                    assert thread is not None
                    _command_start_thread(thread)
                    start_returned = True
                if caller_fault:
                    notify_cancellation()
                open_gate()
                if not start_returned:
                    lifecycle_complete = True
                    continue
                publication_ready = False
                while not publication_ready:
                    if caller_fault:
                        notify_cancellation()
                    assert published is not None
                    publication_ready = _command_wait_publication(published, 0.05)
                assert thread is not None
                while _command_thread_is_alive(thread):
                    _command_join_thread(thread, 0.05)
                if reports:
                    report = reports[0]
                lifecycle_complete = True
            except BaseException as exc:
                capture(exc)
                try:
                    notify_cancellation()
                except BaseException as later_exc:
                    capture(later_exc)
                try:
                    open_gate()
                except BaseException as later_exc:
                    capture(later_exc)
    finally:
        while True:
            try:
                if setup_ready and not lifecycle_complete:
                    request_cancel()
                    open_gate()
                if not start_returned and cancel_reader is not None:
                    close_caller_socket(cancel_reader)
                if cancel_writer is not None:
                    close_caller_socket(cancel_writer)
                break
            except BaseException as exc:
                capture(exc)

    if interruption_info is not None:
        raise interruption_info[0].with_traceback(interruption_info[1])
    if not setup_ready or not start_returned or report is None:
        return _private_unknown_command_result(category, started_mono_s)
    if report.interruption is not None:
        raise report.interruption.with_traceback(report.interruption_traceback)
    if caller_fault:
        return _private_unknown_command_result(category, started_mono_s)
    return report.result


def sysctl_value(name: str) -> str:
    return run_command(["sysctl", "-n", name], timeout=1.0)


def sysctl_int(name: str) -> int | None:
    raw = sysctl_value(name)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def unicode_ok() -> bool:
    return "UTF" in locale.getpreferredencoding(False).upper()


def color_id(value: int) -> int:
    if value <= 7:
        return value
    if curses.COLORS > value:
        return value
    fallback = {
        196: curses.COLOR_RED,
        203: curses.COLOR_RED,
        202: curses.COLOR_RED,
        220: curses.COLOR_YELLOW,
        226: curses.COLOR_YELLOW,
        214: curses.COLOR_YELLOW,
        215: curses.COLOR_YELLOW,
        46: curses.COLOR_GREEN,
        82: curses.COLOR_GREEN,
        118: curses.COLOR_GREEN,
        119: curses.COLOR_GREEN,
        33: curses.COLOR_BLUE,
        39: curses.COLOR_BLUE,
        45: curses.COLOR_CYAN,
        51: curses.COLOR_CYAN,
        87: curses.COLOR_CYAN,
        110: curses.COLOR_CYAN,
        147: curses.COLOR_MAGENTA,
        165: curses.COLOR_MAGENTA,
        171: curses.COLOR_MAGENTA,
        183: curses.COLOR_MAGENTA,
        201: curses.COLOR_MAGENTA,
    }
    return fallback.get(value, curses.COLOR_WHITE)


def color_pair(pair_id: int) -> int:
    if os.environ.get("NO_COLOR") is not None:
        return 0
    try:
        return curses.color_pair(pair_id)
    except (curses.error, ValueError):
        return 0


def enforce_dark_terminal() -> bool:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR") is not None:
        return False
    sequence = (
        f"\033]10;{DARK_FG_RGB}\007"
        f"\033]11;{DARK_BG_RGB}\007"
        f"\033]12;{DARK_CURSOR_RGB}\007"
        "\033[?5l"
    )
    try:
        sys.stdout.write(sequence)
        sys.stdout.flush()
    except OSError:
        return False
    return True


def restore_terminal_colors() -> None:
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write("\033[0m\033]110\007\033]111\007\033]112\007")
        sys.stdout.flush()
    except OSError:
        pass


def terminal_ui_problem() -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return "interactive mode needs a TTY"
    if os.environ.get("TERM", "").lower() in ("", "dumb", "unknown"):
        return "this terminal does not provide cursor-addressing support"
    return ""


def apply_dark_screen(screen: curses.window) -> None:
    if os.environ.get("NO_COLOR") is not None:
        return
    try:
        screen.bkgd(" ", color_pair(1))
    except (curses.error, ValueError):
        pass


class SMCKeyDataVers(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_char),
        ("minor", ctypes.c_char),
        ("build", ctypes.c_char),
        ("reserved", ctypes.c_char),
        ("release", ctypes.c_ushort),
    ]


class SMCKeyDataPLimit(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_ushort),
        ("length", ctypes.c_ushort),
        ("cpuPLimit", ctypes.c_uint32),
        ("gpuPLimit", ctypes.c_uint32),
        ("memPLimit", ctypes.c_uint32),
    ]


class SMCKeyDataKeyInfo(ctypes.Structure):
    _fields_ = [
        ("dataSize", ctypes.c_uint32),
        ("dataType", ctypes.c_uint32),
        ("dataAttributes", ctypes.c_ubyte),
    ]


class SMCKeyData(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32),
        ("vers", SMCKeyDataVers),
        ("pLimitData", SMCKeyDataPLimit),
        ("keyInfo", SMCKeyDataKeyInfo),
        ("result", ctypes.c_ubyte),
        ("status", ctypes.c_ubyte),
        ("data8", ctypes.c_ubyte),
        ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_ubyte * 32),
    ]


def smc_key_to_int(key: str) -> int:
    raw = key.encode("ascii", errors="ignore")[:4].ljust(4, b" ")
    return raw[0] << 24 | raw[1] << 16 | raw[2] << 8 | raw[3]


def smc_int_to_key(value: int) -> str:
    raw = bytes(
        [
            (value >> 24) & 0xFF,
            (value >> 16) & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ]
    )
    return raw.decode("ascii", errors="ignore")


def smc_type_to_string(value: int) -> str:
    return smc_int_to_key(value)


def decode_smc_value(data_type: str, data: bytes) -> float | int | None:
    kind = data_type.strip()
    try:
        if data_type == "flt " and len(data) >= 4:
            return float(struct.unpack("<f", data[:4])[0])
        if kind == "fpe2" and len(data) >= 2:
            return int.from_bytes(data[:2], "big", signed=False) / 4.0
        if kind == "sp78" and len(data) >= 2:
            return int.from_bytes(data[:2], "big", signed=True) / 256.0
        if kind == "ui8" and data:
            return int(data[0])
        if kind == "ui16" and len(data) >= 2:
            return int.from_bytes(data[:2], "big", signed=False)
        if kind == "ui32" and len(data) >= 4:
            return int.from_bytes(data[:4], "big", signed=False)
        if kind == "si8" and data:
            return int.from_bytes(data[:1], "big", signed=True)
        if kind == "si16" and len(data) >= 2:
            return int.from_bytes(data[:2], "big", signed=True)
    except (ValueError, struct.error):
        return None
    return None


def sensor_group_name(key: str, has_s_cores: bool) -> str:
    if len(key) < 2:
        return "Other"
    if not key.startswith("T"):
        if key.startswith("He"):
            return "CPU E-Core"
        if key.startswith("Hp"):
            return "CPU P-Core"
        if key.startswith("Hs"):
            return "CPU S-Core"
        if key.startswith("Hg"):
            return "GPU"
        if key.startswith("Nv"):
            return "NVMe"
        return "Other"
    if len(key) >= 3:
        if key[1] == "P" and key[2] in "DMS":
            return "SoC Package"
        if key[1] == "R" and key[2] == "D":
            return "GPU"
        if key[1] == "C" and key[2] in "MD":
            return "CPU Die"
    if key[1] == "s":
        return "CPU S-Core" if has_s_cores else "SSD"
    group_map = {
        "p": "CPU P-Core",
        "e": "CPU E-Core",
        "f": "CPU P-Core",
        "g": "GPU",
        "C": "CPU Core",
        "c": "CPU Core",
        "m": "Memory",
        "M": "Memory",
        "S": "SSD",
        "H": "NAND",
        "N": "NAND",
        "a": "Ambient",
        "A": "Ambient",
        "F": "Ambient",
        "B": "Board",
        "b": "Board",
        "V": "VRM",
        "P": "SoC Package",
        "R": "GPU",
        "T": "Thunderbolt",
        "I": "Thunderbolt",
        "w": "Wireless",
        "W": "Wireless",
        "D": "Display",
        "d": "Display",
        "L": "Display",
    }
    return group_map.get(key[1], "Other")


class AppleSMCReader:
    def __init__(self) -> None:
        self.conn = ctypes.c_uint32(0)
        self.available = False
        try:
            iokit_path = ctypes.util.find_library("IOKit")
            system_path = ctypes.util.find_library("System")
            if not iokit_path or not system_path:
                return
            self.iokit = ctypes.CDLL(iokit_path)
            self.system = ctypes.CDLL(system_path)
            self._configure()
            self.available = self._open()
        except Exception:
            self.available = False

    def _configure(self) -> None:
        self.iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        self.iokit.IOServiceMatching.restype = ctypes.c_void_p
        self.iokit.IOServiceGetMatchingServices.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
        self.iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
        self.iokit.IOIteratorNext.restype = ctypes.c_uint32
        self.iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        self.iokit.IOObjectRelease.restype = ctypes.c_int
        self.iokit.IOServiceOpen.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.iokit.IOServiceOpen.restype = ctypes.c_int
        self.iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
        self.iokit.IOServiceClose.restype = ctypes.c_int
        self.iokit.IOConnectCallStructMethod.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.iokit.IOConnectCallStructMethod.restype = ctypes.c_int

    def _task_self(self) -> int:
        try:
            self.system.mach_task_self.restype = ctypes.c_uint32
            return int(self.system.mach_task_self())
        except Exception:
            return int(ctypes.c_uint32.in_dll(self.system, "mach_task_self_").value)

    def _open(self) -> bool:
        iterator = ctypes.c_uint32(0)
        matching = self.iokit.IOServiceMatching(b"AppleSMC")
        if not matching:
            return False
        result = self.iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator))
        if result != 0 or not iterator.value:
            return False
        device = self.iokit.IOIteratorNext(iterator.value)
        self.iokit.IOObjectRelease(iterator.value)
        if not device:
            return False
        try:
            result = self.iokit.IOServiceOpen(device, self._task_self(), 0, ctypes.byref(self.conn))
        finally:
            self.iokit.IOObjectRelease(device)
        return result == 0 and bool(self.conn.value)

    def close(self) -> None:
        if self.available and self.conn.value:
            try:
                self.iokit.IOServiceClose(self.conn.value)
            except Exception:
                pass
        self.available = False
        self.conn = ctypes.c_uint32(0)

    def __enter__(self) -> "AppleSMCReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _call(self, input_data: SMCKeyData) -> tuple[int, SMCKeyData]:
        output = SMCKeyData()
        output_size = ctypes.c_size_t(ctypes.sizeof(SMCKeyData))
        result = self.iokit.IOConnectCallStructMethod(
            self.conn.value,
            KERNEL_INDEX_SMC,
            ctypes.byref(input_data),
            ctypes.sizeof(SMCKeyData),
            ctypes.byref(output),
            ctypes.byref(output_size),
        )
        return int(result), output

    def read_key(self, key: str) -> tuple[float | int, str] | None:
        if not self.available:
            return None
        input_data = SMCKeyData()
        input_data.key = smc_key_to_int(key)
        input_data.data8 = SMC_CMD_READ_KEYINFO
        result, output = self._call(input_data)
        if result != 0 or output.keyInfo.dataSize <= 0:
            return None
        size = min(int(output.keyInfo.dataSize), 32)
        data_type = smc_type_to_string(int(output.keyInfo.dataType))
        input_data.keyInfo.dataSize = output.keyInfo.dataSize
        input_data.data8 = SMC_CMD_READ_BYTES
        result, output = self._call(input_data)
        if result != 0:
            return None
        decoded = decode_smc_value(data_type, bytes(output.bytes[:size]))
        return (decoded, data_type) if decoded is not None else None

    def read_number(self, key: str) -> float | None:
        value = self.read_key(key)
        if not value:
            return None
        decoded, _data_type = value
        if isinstance(decoded, (int, float)):
            return float(decoded)
        return None

    def key_count(self) -> int:
        value = self.read_number("#KEY")
        if value is None:
            return 0
        return max(0, int(value))

    def key_at(self, index: int) -> str:
        input_data = SMCKeyData()
        input_data.data8 = SMC_CMD_READ_INDEX
        input_data.data32 = index
        result, output = self._call(input_data)
        if result != 0:
            return ""
        return smc_int_to_key(int(output.key)).strip()

    def temp_keys(self) -> list[str]:
        global SMC_TEMP_KEY_CACHE
        if SMC_TEMP_KEY_CACHE is not None:
            return SMC_TEMP_KEY_CACHE
        keys: list[str] = []
        for index in range(self.key_count()):
            key = self.key_at(index)
            if len(key) == 4 and key.startswith("T"):
                keys.append(key)
        SMC_TEMP_KEY_CACHE = keys
        return keys

    def fan_readings(self) -> list[FanReading]:
        fan_count = self.read_number("FNum")
        if fan_count is None or fan_count <= 0:
            return []
        fans: list[FanReading] = []
        for index in range(min(8, int(fan_count))):
            rpm = self.read_number(f"F{index}Ac")
            if rpm is None or rpm <= 0:
                continue
            minimum = self.read_number(f"F{index}Mn")
            maximum = self.read_number(f"F{index}Mx")
            target = self.read_number(f"F{index}Tg")
            mode_value = self.read_number(f"F{index}Md")
            if mode_value is None:
                mode = ""
            else:
                mode = "manual" if mode_value >= 0.5 else "auto"
            fans.append(
                FanReading(
                    name=f"Fan {index}",
                    rpm=int(round(rpm)),
                    minimum_rpm=int(round(minimum)) if minimum is not None and minimum > 0 else None,
                    maximum_rpm=int(round(maximum)) if maximum is not None and maximum > 0 else None,
                    target_rpm=int(round(target)) if target is not None and target > 0 else None,
                    mode=mode,
                )
            )
        return fans

    def temperature_readings(self) -> list[TempReading]:
        readings: list[TempReading] = []
        seen: set[str] = set()
        has_s_cores = bool(perflevel_counts().get("S"))
        for key in self.temp_keys():
            value = self.read_number(key)
            if value is None or value != value or value < 5 or value > 130:
                continue
            group = sensor_group_name(key, has_s_cores)
            dedupe_key = f"{key}:{value:.1f}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            readings.append(TempReading(key=key, name=f"{group} {key}", value_c=float(value)))
        readings.sort(key=lambda item: item.value_c, reverse=True)
        return readings[:48]


def read_smc_sensors() -> tuple[list[FanReading], list[TempReading]]:
    try:
        with AppleSMCReader() as smc:
            if not smc.available:
                return [], []
            return smc.fan_readings(), smc.temperature_readings()
    except Exception:
        return [], []


def powermetrics_args(samplers: str) -> list[str]:
    return [
        POWER_METRICS_PATH,
        "--samplers",
        samplers,
        "--show-extra-power-info",
        "-n",
        str(POWER_METRICS_SAMPLE_COUNT),
        "-i",
        str(POWER_METRICS_INTERVAL_MS),
    ]


def advanced_sensor_access_available() -> bool:
    if not os.path.exists(POWER_METRICS_PATH):
        return False
    command = powermetrics_args(POWER_METRICS_SAMPLER_SETS[0])
    return subprocess.run(
        [SUDO_PATH, "-n", "-l", *command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def sudoers_contents() -> str:
    command_lines = []
    for samplers in POWER_METRICS_SAMPLER_SETS:
        escaped_samplers = samplers.replace(",", r"\,")
        command_lines.append(" ".join(powermetrics_args(escaped_samplers)))
    return (
        "# PorkyHUD advanced sensor access.\n"
        "# Allows admin users to run PorkyHUD's bounded powermetrics samples without a password.\n"
        f"Cmnd_Alias PORKYHUD_POWERMETRICS = {', '.join(command_lines)}\n"
        f"%admin ALL=(root) TIMEOUT={PRIVILEGED_COMMAND_TIMEOUT_S}s NOPASSWD: PORKYHUD_POWERMETRICS\n"
    )


def validate_sudoers_file(path: str) -> bool:
    return subprocess.run([VISUDO_PATH, "-c", "-s", "-f", path]).returncode == 0


def install_sensor_access() -> int:
    if platform.system() != "Darwin":
        print("PorkyHUD sensor access setup is only available on macOS.")
        return 1
    if not os.path.exists(POWER_METRICS_PATH):
        print(f"Cannot find {POWER_METRICS_PATH}. Advanced sensor setup was not installed.")
        return 1
    if not os.path.exists(VISUDO_PATH):
        print(f"Cannot find {VISUDO_PATH}. Advanced sensor setup was not installed.")
        return 1

    contents = sudoers_contents()
    fd, tmp_path = tempfile.mkstemp(prefix="porkyhud-sudoers-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(contents)
        os.chmod(tmp_path, 0o440)
        if not validate_sudoers_file(tmp_path):
            print("Generated sudoers rule did not validate. Nothing was installed.")
            return 1

        print("PorkyHUD advanced sensor setup")
        print(f"Installing {SUDOERS_PATH}")
        print("This grants admin users passwordless access only to PorkyHUD's bounded powermetrics samples.")
        print("macOS will ask for your administrator password once to install the rule.")
        print()
        result = subprocess.run(
            [
                SUDO_PATH,
                INSTALL_PATH,
                "-o",
                "root",
                "-g",
                "wheel",
                "-m",
                "0440",
                tmp_path,
                SUDOERS_PATH,
            ]
        )
        if result.returncode != 0:
            print("Sensor access setup was not installed.")
            return result.returncode

        verify = subprocess.run([SUDO_PATH, VISUDO_PATH, "-cf", SUDOERS_PATH])
        if verify.returncode != 0:
            subprocess.run([SUDO_PATH, RM_PATH, "-f", SUDOERS_PATH])
            print("Installed rule failed validation and was removed.")
            return verify.returncode

        if advanced_sensor_access_available():
            print("Advanced sensor access is ready. You can now run `porkyhud` without a sensor password prompt.")
        else:
            print("Setup installed, but passwordless powermetrics access was not available yet.")
            print("Try opening a new terminal and run `porkyhud --sensor-access-status`.")
        return 0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def remove_sensor_access() -> int:
    print(f"Removing {SUDOERS_PATH}")
    result = subprocess.run([SUDO_PATH, RM_PATH, "-f", SUDOERS_PATH])
    if result.returncode == 0:
        print("PorkyHUD advanced sensor access removed.")
    else:
        print("Could not remove PorkyHUD advanced sensor access.")
    return result.returncode


def print_sensor_access_status() -> int:
    print("PorkyHUD advanced sensor access")
    print(f"powermetrics: {POWER_METRICS_PATH if os.path.exists(POWER_METRICS_PATH) else 'not found'}")
    print(f"sudoers file: {SUDOERS_PATH if os.path.exists(SUDOERS_PATH) else 'not installed'}")
    if advanced_sensor_access_available():
        print("status: ready")
        print("Normal `porkyhud` launches can sample advanced sensors without a password prompt.")
    else:
        print("status: not ready")
        print("Run `porkyhud --setup-sensors` to enable one-time advanced sensor setup.")
    return 0


def unlock_privileged_sensors(screen: curses.window) -> bool:
    curses.def_prog_mode()
    curses.endwin()
    print()
    print("PorkyHUD advanced sensor unlock")
    print("macOS requires an administrator password for powermetrics sensor data.")
    print("This may unlock CPU/GPU power data when your Mac exposes it.")
    print()
    result = subprocess.run([SUDO_PATH, "-v"])
    print()
    if result.returncode == 0:
        print("Advanced sensor session unlocked. Returning to PorkyHUD...")
    else:
        print("Sensor unlock skipped or failed. Returning to PorkyHUD...")
    time.sleep(1.2)
    curses.reset_prog_mode()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.nodelay(True)
    screen.refresh()
    return result.returncode == 0


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def human_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            return f"{size:3.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024.0
    return f"{size:.1f}PB"


def format_uptime(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def visible_command(command: str, max_width: int) -> str:
    command = sanitize_command(command)
    app_match = re.search(r"/([^/]+)\.app(?:/Contents/MacOS/\S+)?", command)
    if app_match:
        label = app_match.group(1)
        app_executable = re.match(r".*?\.app/Contents/MacOS/\S+", command)
        remainder = command[app_executable.end() :].strip() if app_executable else ""
        command = f"{label} {remainder}".strip()
    else:
        executable_path, separator, remainder = command.partition(" ")
        label = os.path.basename(executable_path) or executable_path
        command = f"{label}{separator}{remainder}".strip()
    if len(command) <= max_width:
        return command
    if max_width <= 3:
        return command[: max(0, max_width)]
    return command[: max_width - 3].rstrip() + "..."


def sanitize_command(command: str) -> str:
    command = command.replace(os.path.expanduser("~"), "~")
    command = re.sub(r"\s+", " ", command).strip()
    return SENSITIVE_ARGUMENT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        command,
    )


def format_temp(value: float | None) -> str:
    if value is None:
        return "locked"
    return f"{value:4.1f}C"


def format_watts(value: float | None) -> str:
    if value is None:
        return "locked"
    if value < 1:
        return f"{value * 1000:4.0f}mW"
    return f"{value:4.1f}W"


def fan_text(sensor: "SensorInfo") -> str:
    if sensor.fan_rpm is not None:
        return f"{sensor.fan_rpm} rpm"
    return "admin locked" if sensor.privileged_locked else "no RPM exposed"


def default_sensor_info() -> SensorInfo:
    return SensorInfo(
        thermal_warning="checking",
        performance_warning="checking",
        cpu_power_w=None,
        gpu_power_w=None,
        cpu_temp_c=None,
        gpu_temp_c=None,
        fan_rpm=None,
        privileged_locked=False,
        raw_hint="checking sensors",
    )


def thermal_level_from_sysctl() -> tuple[str, bool] | None:
    value = sysctl_int("machdep.xcpm.cpu_thermal_level")
    if value is None:
        return None
    levels = {
        0: ("nominal", False),
        1: ("fair", True),
        2: ("serious", True),
        3: ("critical", True),
    }
    return levels.get(value, ("unknown", False))


def format_optional_int(value: int | None, suffix: str = "") -> str:
    if value is None:
        return "locked"
    return f"{value}{suffix}"


def parse_ioreg_int(raw: str, key: str) -> int | None:
    matches = re.findall(rf'"{re.escape(key)}"\s*=\s*(-?\d+)', raw)
    if not matches:
        return None
    try:
        value = int(matches[-1])
    except ValueError:
        return None
    if value >= 2**63:
        value -= 2**64
    return value


def parse_ioreg_bool(raw: str, key: str) -> bool:
    matches = re.findall(rf'"{re.escape(key)}"\s*=\s*(Yes|No)', raw)
    return bool(matches and matches[-1] == "Yes")


def parse_ioreg_string(raw: str, key: str) -> str:
    matches = re.findall(rf'"{re.escape(key)}"\s*=\s*"([^"]*)"', raw)
    return matches[-1] if matches else ""


def apple_battery_temp(raw_value: int | None) -> float | None:
    if raw_value is None:
        return None
    if raw_value > 1000:
        return raw_value / 10.0 - 273.15
    return raw_value / 10.0


def apple_virtual_battery_temp(raw_value: int | None) -> float | None:
    if raw_value is None:
        return None
    if raw_value > 1000:
        return raw_value / 100.0
    return raw_value / 10.0


def parse_power_value(raw: str, label: str) -> float | None:
    pattern = rf"{re.escape(label)}\s*:\s*([\d.]+)\s*(mW|W)"
    match = re.search(pattern, raw, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value / 1000.0 if unit == "mw" else value


def parse_any_power_value(raw: str, labels: list[str]) -> float | None:
    for label in labels:
        value = parse_power_value(raw, label)
        if value is not None:
            return value
    return None


def parse_percent_value(raw: str, labels: list[str]) -> float | None:
    for label in labels:
        pattern = rf"{re.escape(label)}[^\n:]*:\s*([\d.]+)\s*%"
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            try:
                return clamp(float(match.group(1)))
            except ValueError:
                return None
    return None


def parse_frequency_mhz(raw: str, labels: list[str]) -> int | None:
    for label in labels:
        pattern = rf"{re.escape(label)}[^\n:]*:\s*([\d.]+)\s*(MHz|GHz)"
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                return None
            if match.group(2).lower() == "ghz":
                value *= 1000.0
            return int(round(value))
    return None


@dataclass(frozen=True)
class _PowermetricsAttempt:
    """Private classified optional-sensor result with no diagnostic projection."""

    text: str
    permission_denied: bool


def _powermetrics_permission_denied(result: CommandResult) -> bool:
    """Accept only the exact sudo -n password prompt as lock proof."""

    return (
        result.outcome is AttemptOutcome.COMMAND_ERROR
        and result.failure is FailureCode.NONZERO_EXIT
        and result.stderr.strip() == "sudo: a password is required"
    )


def _powermetrics_capability_rejection(result: CommandResult) -> bool:
    """Recognize only bounded fixture-shaped sampler capability rejections."""

    return (
        result.outcome is AttemptOutcome.COMMAND_ERROR
        and result.failure is FailureCode.NONZERO_EXIT
        and result.stderr.startswith("powermetrics: ")
    )


def _collect_powermetrics_attempt() -> _PowermetricsAttempt:
    """Run exactly one selected powermetrics child and advance only next attempt."""

    global POWER_METRICS_SAMPLER_INDEX
    with POWER_METRICS_SAMPLER_LOCK:
        index = min(POWER_METRICS_SAMPLER_INDEX, len(POWER_METRICS_SAMPLER_SETS) - 1)
        samplers = POWER_METRICS_SAMPLER_SETS[index]
    result = run_structured_command(
        [SUDO_PATH, "-n", *powermetrics_args(samplers)],
        category="sensor-powermetrics",
        timeout_s=POWER_METRICS_RUNNER_TIMEOUT_S,
        stdout_limit=1024 * 1024,
        stderr_limit=64 * 1024,
    )
    if result.outcome is AttemptOutcome.SUCCESS and result.stdout:
        with POWER_METRICS_SAMPLER_LOCK:
            POWER_METRICS_SAMPLER_INDEX = index
        return _PowermetricsAttempt(result.stdout, False)
    if ((result.outcome is AttemptOutcome.SUCCESS and not result.stdout)
            or _powermetrics_capability_rejection(result)):
        with POWER_METRICS_SAMPLER_LOCK:
            if index < len(POWER_METRICS_SAMPLER_SETS) - 1:
                POWER_METRICS_SAMPLER_INDEX = index + 1
    return _PowermetricsAttempt("", _powermetrics_permission_denied(result))


def powermetrics_sample() -> str:
    """Legacy text-only optional sensor projection."""

    return _collect_powermetrics_attempt().text


def parse_fan_readings(raw: str) -> list[FanReading]:
    fans: list[FanReading] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        if "fan" not in line.lower() or "rpm" not in line.lower():
            continue
        rpm_match = re.search(r"(\d{3,6})\s*rpm", line, re.IGNORECASE)
        if not rpm_match:
            continue
        rpm = int(rpm_match.group(1))
        prefix = line[: rpm_match.start()].strip(" :-")
        id_match = re.search(r"fan\s*(\d+)", line, re.IGNORECASE)
        name = prefix or (f"Fan {id_match.group(1)}" if id_match else f"Fan {len(fans)}")
        name = re.sub(r"\s+", " ", name)
        key = f"{name}:{rpm}"
        if key in seen:
            continue
        seen.add(key)

        def rpm_field(label: str) -> int | None:
            match = re.search(rf"{label}[^\d]*(\d{{3,6}})\s*rpm", line, re.IGNORECASE)
            return int(match.group(1)) if match else None

        mode = ""
        mode_match = re.search(r"\b(auto|automatic|forced|manual)\b", line, re.IGNORECASE)
        if mode_match:
            mode = mode_match.group(1).lower()
        fans.append(
            FanReading(
                name=name[:42],
                rpm=rpm,
                minimum_rpm=rpm_field("min"),
                maximum_rpm=rpm_field("max"),
                target_rpm=rpm_field("target"),
                mode=mode,
            )
        )
    return fans


def parse_temperature_readings(raw: str) -> list[TempReading]:
    readings: list[TempReading] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        if not re.search(r"(temperature|temp|\bT[A-Za-z0-9]{2,4}\b)", normalized, re.IGNORECASE):
            continue
        match = re.search(
            r"(?:(\b[A-Za-z][A-Za-z0-9]{2,4}\b)\s+)?(.{0,64}?)(?:temperature|temp)?\s*:?\s*(-?\d+(?:\.\d+)?)\s*(?:C|°C|celsius)\b",
            normalized,
            re.IGNORECASE,
        )
        if not match:
            continue
        key = match.group(1) or ""
        name = (match.group(2) or key or "temperature").strip(" :-")
        value = float(match.group(3))
        if value < -20 or value > 140:
            continue
        label = name or key or "temperature"
        dedupe_key = f"{key}:{label}:{value:.1f}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        readings.append(TempReading(key=key, name=re.sub(r"\s+", " ", label)[:54], value_c=value))
    readings.sort(key=lambda item: item.value_c, reverse=True)
    return readings[:24]


def temp_from_readings(readings: list[TempReading], needles: tuple[str, ...]) -> float | None:
    candidates = []
    for reading in readings:
        haystack = f"{reading.key} {reading.name}".lower()
        if any(needle in haystack for needle in needles):
            candidates.append(reading.value_c)
    return max(candidates) if candidates else None


def average_temp_from_smc_keys(readings: list[TempReading], second_chars: set[str]) -> float | None:
    values = [
        reading.value_c
        for reading in readings
        if len(reading.key) >= 2 and reading.key[0] == "T" and reading.key[1] in second_chars
    ]
    return sum(values) / len(values) if values else None


def merge_temperature_readings(*groups: list[TempReading]) -> list[TempReading]:
    merged: list[TempReading] = []
    seen: set[str] = set()
    for readings in groups:
        for reading in readings:
            key = reading.key or reading.name.lower()
            dedupe_key = re.sub(r"\s+", " ", key.strip().lower())
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append(reading)
    merged.sort(key=lambda item: item.value_c, reverse=True)
    return merged[:48]


def parse_temp_value(raw: str, pattern: str) -> float | None:
    match = re.search(pattern, raw, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def core_topology_counts(raw: str) -> dict[str, int]:
    values = [int(value) for value in re.findall(r"\d+", raw)]
    if not values:
        return {}
    if raw.startswith("proc") and len(values) >= 4:
        total, efficiency, _, performance = values[:4]
        return {
            "total": total,
            "efficiency": efficiency,
            "performance": performance,
        }
    if len(values) >= 3 and values[0] == values[1] + values[2]:
        return {
            "total": values[0],
            "performance": values[1],
            "efficiency": values[2],
        }
    if len(values) == 1:
        return {"total": values[0]}
    return {}


def format_core_topology(raw: str) -> str:
    topology = core_topology_counts(raw)
    if not topology:
        return ""
    total = topology.get("total")
    performance = topology.get("performance", 0)
    efficiency = topology.get("efficiency", 0)
    if total and performance and efficiency:
        return f"{total} cores ({performance}P/{efficiency}E)"
    if total:
        return f"{total} cores"
    return raw


def perflevel_clusters(topology: dict[str, int]) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    nlevels = sysctl_int("hw.nperflevels") or 4
    for index in range(nlevels):
        logical = sysctl_int(f"hw.perflevel{index}.logicalcpu") or 0
        if logical <= 0:
            continue
        raw_name = sysctl_value(f"hw.perflevel{index}.name") or f"Cluster {index}"
        normalized = raw_name.strip().lower()
        performance_count = topology.get("performance", 0)
        efficiency_count = topology.get("efficiency", 0)
        if "super" in normalized:
            code = "S"
            label = "Super"
        elif "performance" in normalized or (performance_count and logical == performance_count and logical != efficiency_count):
            code = "P"
            label = "Performance"
        elif "efficiency" in normalized or (efficiency_count and logical == efficiency_count and logical != performance_count):
            code = "E"
            label = "Efficiency"
        else:
            code = raw_name[:1].upper() if raw_name else "C"
            label = raw_name.title()
        clusters.append(
            {
                "code": code,
                "label": label,
                "logical": logical,
                "source": raw_name,
            }
        )
    return clusters


def perflevel_counts() -> dict[str, int]:
    counts = {"E": 0, "P": 0, "S": 0}
    nlevels = sysctl_int("hw.nperflevels") or 0
    if nlevels:
        for index in range(nlevels):
            logical = sysctl_int(f"hw.perflevel{index}.logicalcpu") or 0
            name = sysctl_value(f"hw.perflevel{index}.name").lower()
            if logical <= 0:
                continue
            if name.startswith("super"):
                counts["S"] += logical
            elif name.startswith("efficiency"):
                counts["E"] += logical
            else:
                counts["P"] += logical
    else:
        counts["P"] = sysctl_int("hw.perflevel0.logicalcpu") or 0
        counts["E"] = sysctl_int("hw.perflevel1.logicalcpu") or 0
    return counts


def detect_core_topology() -> list[tuple[int, str]]:
    raw = run_command(["ioreg", "-l", "-p", "IODeviceTree"], timeout=3.0)
    entries: list[tuple[int, str]] = []
    if not raw:
        return entries
    lines = raw.splitlines()
    seen: set[int] = set()
    last_cluster_type: str | None = None
    for index, line in enumerate(lines):
        cluster_match = re.search(r'"cluster-type"\s*=\s*<"([A-Za-z])">', line)
        if cluster_match:
            last_cluster_type = cluster_match.group(1).upper()
        name_match = re.search(r'"name"\s*=\s*<"cpu(\d+)">', line)
        if not name_match:
            continue
        cpu_id = int(name_match.group(1))
        if cpu_id in seen:
            continue
        core_type = last_cluster_type
        if core_type is None:
            for lookahead in lines[index + 1 : index + 8]:
                cluster_match = re.search(r'"cluster-type"\s*=\s*<"([A-Za-z])">', lookahead)
                if cluster_match:
                    core_type = cluster_match.group(1).upper()
                    break
        if core_type is None:
            continue
        entries.append((cpu_id, core_type))
        seen.add(cpu_id)
        last_cluster_type = None
    entries.sort(key=lambda item: item[0])
    return entries


def build_core_labels() -> tuple[list[str], list[int], dict[str, int]]:
    topology = detect_core_topology()
    counts = {"E": 0, "P": 0, "S": 0}
    if not topology:
        return [], [], counts

    perf_counts = perflevel_counts()
    grouped: dict[str, list[tuple[int, int]]] = {"E": [], "P": [], "S": [], "M": []}
    for mach_index, (_cpu_id, raw_type) in enumerate(topology):
        grouped.setdefault(raw_type, []).append((mach_index, len(grouped.get(raw_type, []))))

    # M5-era device trees report "M" for Performance-tier cores and "P" for Super-tier cores.
    if grouped.get("M"):
        s_cores = grouped.get("P", [])
        p_cores = grouped.get("M", [])
        if perf_counts.get("S") and len(s_cores) != perf_counts["S"]:
            s_cores = grouped.get("S", []) + grouped.get("P", [])
        grouped["P"] = p_cores
        grouped["S"] = s_cores

    labels: list[str] = []
    index_map: list[int] = []
    for code in ("E", "P", "S"):
        for display_index, (mach_index, _raw_index) in enumerate(grouped.get(code, [])):
            labels.append(f"{code}{display_index}")
            index_map.append(mach_index)
        counts[code] = len(grouped.get(code, []))
    return labels, index_map, counts


def gpu_core_count_fast() -> int | None:
    raw = run_command(["ioreg", "-r", "-c", "AGXAccelerator", "-d", "1"], timeout=2.0)
    match = re.search(r'"gpu-core-count"\s*=\s*(\d+)', raw)
    if not match:
        config_match = re.search(r'"num_cores"\s*=\s*(\d+)', raw)
        match = config_match
    return int(match.group(1)) if match else None


def max_gpu_frequency_mhz() -> int | None:
    raw = run_command(["ioreg", "-r", "-c", "AppleARMIODevice", "-d", "1", "-k", "voltage-states9"], timeout=3.0)
    match = re.search(r'"voltage-states9"\s*=\s*<([0-9a-fA-F]+)>', raw)
    if not match:
        return None
    blob = bytes.fromhex(match.group(1))
    if len(blob) < 8:
        return None
    max_mhz = 0
    for offset in range(0, len(blob) - 7, 8):
        freq_hz = int.from_bytes(blob[offset : offset + 4], "little", signed=False)
        if freq_hz:
            max_mhz = max(max_mhz, freq_hz // 1_000_000)
    return max_mhz or None


class MacCpuSampler:
    PROCESSOR_CPU_LOAD_INFO = 2
    CPU_STATE_USER = 0
    CPU_STATE_SYSTEM = 1
    CPU_STATE_IDLE = 2
    CPU_STATE_NICE = 3
    CPU_STATE_MAX = 4

    def __init__(self) -> None:
        self.available = False
        self.previous: list[tuple[int, int, int, int]] | None = None
        self.last_read_completed_mono_s: Optional[float] = None
        self.last_per_core: tuple[float, ...] = ()
        try:
            self.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            self.lib.mach_host_self.restype = ctypes.c_uint
            self.lib.mach_task_self.restype = ctypes.c_uint
            self.lib.host_processor_info.argtypes = [
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                ctypes.POINTER(ctypes.c_uint),
            ]
            self.lib.host_processor_info.restype = ctypes.c_int
            self.lib.vm_deallocate.argtypes = [
                ctypes.c_uint,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ]
            self.host = self.lib.mach_host_self()
        except Exception:
            return
        self.available = True
        try:
            initial = self._read_raw()
        except Exception:
            return
        if initial:
            self.previous = initial
            self.last_read_completed_mono_s = time.monotonic()

    def _read_raw(self) -> list[tuple[int, int, int, int]]:
        cpu_count = ctypes.c_uint(0)
        info_count = ctypes.c_uint(0)
        info = ctypes.POINTER(ctypes.c_int)()
        result = self.lib.host_processor_info(
            self.host,
            self.PROCESSOR_CPU_LOAD_INFO,
            ctypes.byref(cpu_count),
            ctypes.byref(info),
            ctypes.byref(info_count),
        )
        if result != 0 or not info:
            return []

        rows: list[tuple[int, int, int, int]] = []
        try:
            for index in range(cpu_count.value):
                offset = index * self.CPU_STATE_MAX
                rows.append(
                    (
                        int(info[offset + self.CPU_STATE_USER]),
                        int(info[offset + self.CPU_STATE_SYSTEM]),
                        int(info[offset + self.CPU_STATE_IDLE]),
                        int(info[offset + self.CPU_STATE_NICE]),
                    )
                )
        finally:
            address = ctypes.cast(info, ctypes.c_void_p).value
            if address:
                self.lib.vm_deallocate(
                    self.lib.mach_task_self(),
                    address,
                    int(info_count.value) * ctypes.sizeof(ctypes.c_int),
                )
        return rows

    def _failure(self, started_mono_s: float, outcome: AttemptOutcome, failure: FailureCode) -> AdapterResult[CpuUsage]:
        self.last_per_core = ()
        return AdapterResult(None, started_mono_s, time.monotonic(), utc_now(), outcome, failure)

    def sample_usage(self) -> AdapterResult[CpuUsage]:
        """Read Mach counters and aggregate their deltas without per-core weighting."""

        started_mono_s = time.monotonic()
        if not self.available:
            self.previous = None
            self.last_read_completed_mono_s = None
            return self._failure(
                started_mono_s, AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
            )
        try:
            current = self._read_raw()
        except Exception:
            current = []
        completed_mono_s = time.monotonic()
        if not current:
            self.previous = None
            self.last_read_completed_mono_s = None
            self.last_per_core = ()
            return AdapterResult(
                None, started_mono_s, completed_mono_s, utc_now(), AttemptOutcome.UNKNOWN_ERROR,
                FailureCode.UNEXPECTED_EXCEPTION,
            )
        before = self.previous
        previous_completed = self.last_read_completed_mono_s
        if not before or previous_completed is None or len(current) != len(before):
            self.previous = current
            self.last_read_completed_mono_s = completed_mono_s
            self.last_per_core = ()
            return AdapterResult(
                None, started_mono_s, completed_mono_s, utc_now(), AttemptOutcome.PARSE_ERROR,
                FailureCode.PARSER_REJECTED,
            )

        busy_total = 0
        total_total = 0
        per_core: list[float] = []
        for prior, latest in zip(before, current):
            if any(latest[index] < prior[index] for index in range(self.CPU_STATE_MAX)):
                self.previous = current
                self.last_read_completed_mono_s = completed_mono_s
                self.last_per_core = ()
                return AdapterResult(
                    None, started_mono_s, completed_mono_s, utc_now(), AttemptOutcome.PARSE_ERROR,
                    FailureCode.PARSER_REJECTED,
                )
            deltas = [latest[index] - prior[index] for index in range(self.CPU_STATE_MAX)]
            busy = deltas[self.CPU_STATE_USER] + deltas[self.CPU_STATE_SYSTEM] + deltas[self.CPU_STATE_NICE]
            total = busy + deltas[self.CPU_STATE_IDLE]
            if total <= 0:
                self.previous = current
                self.last_read_completed_mono_s = completed_mono_s
                self.last_per_core = ()
                return AdapterResult(
                    None, started_mono_s, completed_mono_s, utc_now(), AttemptOutcome.PARSE_ERROR,
                    FailureCode.PARSER_REJECTED,
                )
            busy_total += busy
            total_total += total
            per_core.append(busy / total * 100.0)
        if total_total <= 0 or completed_mono_s <= previous_completed:
            self.previous = current
            self.last_read_completed_mono_s = completed_mono_s
            self.last_per_core = ()
            return AdapterResult(
                None, started_mono_s, completed_mono_s, utc_now(), AttemptOutcome.PARSE_ERROR,
                FailureCode.PARSER_REJECTED,
            )
        self.previous = current
        self.last_read_completed_mono_s = completed_mono_s
        self.last_per_core = tuple(per_core)
        return AdapterResult(
            CpuUsage(busy_total / total_total * 100.0, previous_completed, completed_mono_s),
            started_mono_s, completed_mono_s, utc_now(), AttemptOutcome.SUCCESS, None,
        )

    def sample(self) -> list[float]:
        """Legacy display projection; Tasks 10/11 own migration of its callers."""

        result = self.sample_usage()
        return list(self.last_per_core) if result.outcome is AttemptOutcome.SUCCESS else []


def collect_static_info() -> dict[str, Any]:
    core_labels, cpu_index_map, core_counts = build_core_labels()
    info: dict[str, Any] = {
        "host": socket.gethostname().split(".")[0],
        "os": f"macOS {run_command(['sw_vers', '-productVersion'])}",
        "build": run_command(["sw_vers", "-buildVersion"]),
        "kernel": platform.release(),
        "model": sysctl_value("hw.model"),
        "chip": sysctl_value("machdep.cpu.brand_string"),
        "physical_cpu": sysctl_value("hw.physicalcpu"),
        "logical_cpu": sysctl_value("hw.logicalcpu"),
        "memory": "",
        "cpu_clusters": [],
        "core_labels": core_labels,
        "cpu_index_map": cpu_index_map,
        "core_counts": core_counts,
        "gpus": [],
        "gpu_core_count": gpu_core_count_fast(),
        "max_gpu_freq_mhz": max_gpu_frequency_mhz(),
    }

    raw = run_command(
        ["system_profiler", "SPHardwareDataType", "SPDisplaysDataType", "-json"],
        timeout=12.0,
    )
    if raw:
        try:
            prof = json.loads(raw)
            hardware = (prof.get("SPHardwareDataType") or [{}])[0]
            info["model"] = hardware.get("machine_name") or info["model"]
            info["model_id"] = hardware.get("machine_model") or ""
            info["chip"] = (
                hardware.get("chip_type")
                or hardware.get("cpu_type")
                or info["chip"]
                or "Unknown CPU"
            )
            processor_topology = str(hardware.get("number_processors") or "")
            info["cpu_cores"] = (
                hardware.get("number_cores")
                or format_core_topology(processor_topology)
            )
            info["cpu_clusters"] = perflevel_clusters(core_topology_counts(processor_topology))
            info["memory"] = hardware.get("physical_memory") or ""

            gpus: list[dict[str, str]] = []
            for gpu in prof.get("SPDisplaysDataType") or []:
                name = (
                    gpu.get("sppci_model")
                    or gpu.get("_name")
                    or gpu.get("spdisplays_device-id")
                    or "GPU"
                )
                cores = (
                    gpu.get("spdisplays_cores")
                    or gpu.get("spdisplays_core_count")
                    or gpu.get("sppci_cores")
                    or ""
                )
                if not cores and info.get("gpu_core_count"):
                    cores = str(info["gpu_core_count"])
                metal = gpu.get("spdisplays_metalfamily") or gpu.get("spdisplays_metal") or ""
                metal = metal or gpu.get("spdisplays_mtlgpufamilysupport") or ""
                displays = gpu.get("spdisplays_ndrvs") or []
                display_names = []
                for display in displays if isinstance(displays, list) else []:
                    display_names.append(display.get("_name") or display.get("spdisplays_display_type") or "Display")
                gpus.append(
                    {
                        "name": str(name),
                        "cores": str(cores),
                        "metal": str(metal),
                        "displays": ", ".join(display_names[:3]),
                        "max_freq_mhz": str(info.get("max_gpu_freq_mhz") or ""),
                    }
                )
            info["gpus"] = gpus
        except Exception:
            pass

    if not info["chip"]:
        info["chip"] = "Apple Silicon" if platform.machine() == "arm64" else platform.processor()
    if not info["cpu_clusters"]:
        info["cpu_clusters"] = perflevel_clusters({})
    core_counts = info.get("core_counts") or {}
    detected_total = sum(int(core_counts.get(code, 0) or 0) for code in ("E", "P", "S"))
    if detected_total:
        parts = [f"{core_counts[code]}{code}" for code in ("P", "E", "S") if core_counts.get(code)]
        info["cpu_cores"] = f"{detected_total} cores ({'/'.join(parts)})"
    if not info["memory"]:
        total = sysctl_value("hw.memsize")
        info["memory"] = human_bytes(float(total or 0))
    if info.get("gpu_core_count") and info.get("max_gpu_freq_mhz"):
        info["gpu_fp32_tflops"] = float(info["gpu_core_count"]) * float(info["max_gpu_freq_mhz"]) * 0.000256
    return info


def boot_seconds() -> int:
    global BOOT_EPOCH
    if BOOT_EPOCH is not None:
        return max(0, int(time.time()) - BOOT_EPOCH)
    raw = run_command(["sysctl", "-n", "kern.boottime"])
    match = re.search(r"sec = (\d+)", raw)
    if not match:
        return 0
    BOOT_EPOCH = int(match.group(1))
    return max(0, int(time.time()) - BOOT_EPOCH)


def _physical_memory_bytes() -> float:
    """Read physical memory once because it cannot change within a HUD session."""

    global MEMORY_SIZE_BYTES
    with MEMORY_SIZE_LOCK:
        if MEMORY_SIZE_BYTES is not None:
            return MEMORY_SIZE_BYTES
        total_raw = sysctl_value("hw.memsize")
        try:
            total = float(total_raw or 0)
        except (TypeError, ValueError):
            total = 0.0
        if total <= 0:
            try:
                total = float(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
            except (OSError, ValueError):
                total = 0.0
        MEMORY_SIZE_BYTES = total
        return total


def memory_detail() -> dict[str, float]:
    """Return vm_stat-derived detail using the cached physical byte size."""

    total = _physical_memory_bytes()
    raw = run_command(["vm_stat"], timeout=1.5)
    page_match = re.search(r"page size of (\d+) bytes", raw)
    page_size = int(page_match.group(1)) if page_match else 4096

    pages: dict[str, int] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value_match = re.search(r"([\d.]+)", value.replace(".", ""))
        if value_match:
            pages[key.lower()] = int(value_match.group(1))

    free_pages = pages.get("pages free", 0) + pages.get("pages speculative", 0)
    free = free_pages * page_size
    cached_pages = max(pages.get("file-backed pages", 0), pages.get("pages purgeable", 0))
    cached = cached_pages * page_size
    app = pages.get("anonymous pages", 0) * page_size
    compressed = pages.get("pages occupied by compressor", 0) * page_size
    wired = pages.get("pages wired down", 0) * page_size
    active = pages.get("pages active", 0) * page_size
    inactive = pages.get("pages inactive", 0) * page_size
    used = min(total, max(0.0, app + wired + compressed))
    available = min(total, max(0.0, free + cached))

    return {
        "total": total,
        "used": used,
        "available": available,
        "free": free,
        "cached": cached,
        "app": app,
        "active": active,
        "inactive": inactive,
        "wired": wired,
        "compressed": compressed,
    }


def swap_stats() -> tuple[float, float]:
    """Return swap used/total bytes from one bounded legacy probe."""

    swap_raw = run_command(["sysctl", "vm.swapusage"])
    swap_total = swap_used = 0.0
    swap_match = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", swap_raw)
    if swap_match:
        swap_total = float(swap_match.group(1)) * 1024 * 1024
        swap_used = float(swap_match.group(2)) * 1024 * 1024

    return swap_used, swap_total


def merge_memory_stats(
    detail: Mapping[str, float], swap: tuple[float, float],
) -> dict[str, float]:
    """Compose separate descriptive probes without mutating their retained values."""

    result = dict(detail)
    result["swap_used"] = swap[0]
    result["swap_total"] = swap[1]
    return result


def memory_stats() -> dict[str, float]:
    """Snapshot compatibility composition for callers that need one mapping."""

    return merge_memory_stats(memory_detail(), swap_stats())


def battery_info() -> BatteryInfo:
    pmset_raw = run_command(["pmset", "-g", "batt"])
    ioreg_raw = run_command(["ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"], timeout=1.5)

    source_match = re.search(r"Now drawing from '([^']+)'", pmset_raw)
    battery_match = re.search(r"(\d+)%;\s*([^;]+);\s*([^;]+);?", pmset_raw)
    source = source_match.group(1) if source_match else "Power"
    present = battery_match is not None or bool(ioreg_raw.strip())
    percent: int | None = None
    state = "unknown"
    remaining = "unknown"
    if battery_match:
        percent = int(battery_match.group(1))
        state = battery_match.group(2).strip()
        remaining = re.sub(r"\s*present:\s*(true|false)\b", "", battery_match.group(3).strip(), flags=re.IGNORECASE)
    elif pmset_raw:
        remaining = pmset_raw.splitlines()[-1].strip()[:32]

    voltage_mv = parse_ioreg_int(ioreg_raw, "Voltage")
    amperage_ma = parse_ioreg_int(ioreg_raw, "InstantAmperage")
    health = parse_ioreg_int(ioreg_raw, "MaxCapacity")
    charger_watts = parse_ioreg_int(ioreg_raw, "Watts")
    charger_name = parse_ioreg_string(ioreg_raw, "Name")

    return BatteryInfo(
        percent=percent,
        present=present,
        source=source,
        state=state,
        remaining=remaining,
        external_connected=(
            parse_ioreg_bool(ioreg_raw, "ExternalConnected")
            if present
            else any(label in source.lower() for label in ("ac", "adapter", "external"))
        ),
        is_charging=parse_ioreg_bool(ioreg_raw, "IsCharging"),
        fully_charged=parse_ioreg_bool(ioreg_raw, "FullyCharged"),
        cycle_count=parse_ioreg_int(ioreg_raw, "CycleCount"),
        design_cycles=parse_ioreg_int(ioreg_raw, "DesignCycleCount9C"),
        health_percent=health,
        temp_c=apple_battery_temp(parse_ioreg_int(ioreg_raw, "Temperature")),
        virtual_temp_c=apple_virtual_battery_temp(parse_ioreg_int(ioreg_raw, "VirtualTemperature")),
        voltage_v=(voltage_mv / 1000.0 if voltage_mv is not None else None),
        amperage_a=(amperage_ma / 1000.0 if amperage_ma is not None else None),
        charger_watts=charger_watts,
        charger_name=charger_name,
    )


def battery_summary(battery: BatteryInfo) -> str:
    if not battery.present:
        return f"{battery.source}: no internal battery"
    percent = "--" if battery.percent is None else f"{battery.percent}%"
    if battery.is_charging:
        state = "charging"
    elif battery.external_connected:
        state = battery.state or "AC"
    else:
        state = "battery"
    return f"{battery.source}: {percent} {state} {battery.remaining}".strip()


def sensor_info(os_thermal: Optional[OsThermalState] = None) -> SensorInfo:
    """Collect optional sensor detail without replacing independent OS warning evidence."""

    def warning_state(level: Optional[int]) -> str:
        if level is None:
            return "unavailable"
        return "nominal" if level == 0 else "active"

    thermal_warning = warning_state(
        os_thermal.thermal_warning_level if os_thermal is not None else None
    )
    performance_warning = warning_state(
        os_thermal.performance_warning_level if os_thermal is not None else None
    )

    smc_fans, smc_temp_sensors = read_smc_sensors()
    smc_online = bool(smc_fans or smc_temp_sensors)
    power_attempt = _collect_powermetrics_attempt()
    power_raw = power_attempt.text
    locked = power_attempt.permission_denied
    fans = smc_fans or parse_fan_readings(power_raw)
    temp_sensors = merge_temperature_readings(smc_temp_sensors, parse_temperature_readings(power_raw))
    cpu_temp = parse_temp_value(power_raw, r"(?:CPU|processor)[^\n:]*temperature[^\d-]*(-?[\d.]+)\s*(?:C|°C)")
    gpu_temp = parse_temp_value(power_raw, r"GPU[^\n:]*temperature[^\d-]*(-?[\d.]+)\s*(?:C|°C)")
    cpu_temp = cpu_temp if cpu_temp is not None else average_temp_from_smc_keys(temp_sensors, {"p", "e", "f", "s"})
    gpu_temp = gpu_temp if gpu_temp is not None else average_temp_from_smc_keys(temp_sensors, {"g", "R"})
    cpu_temp = cpu_temp if cpu_temp is not None else temp_from_readings(temp_sensors, ("cpu", "processor", "p-core", "e-core", "soc"))
    gpu_temp = gpu_temp if gpu_temp is not None else temp_from_readings(temp_sensors, ("gpu", "graphics", "agx"))
    fan_rpm = max((fan.rpm for fan in fans), default=None)
    fan_count = len(fans)

    cpu_power = parse_any_power_value(power_raw, ["CPU Power", "Processor Power"])
    gpu_power = parse_any_power_value(power_raw, ["GPU Power"])
    ane_power = parse_any_power_value(power_raw, ["ANE Power", "Neural Engine Power"])
    dram_power = parse_any_power_value(power_raw, ["DRAM Power", "Memory Power"])
    gpu_sram_power = parse_any_power_value(power_raw, ["GPU SRAM Power", "GPUSRAM Power"])
    package_power = parse_any_power_value(power_raw, ["Package Power", "Combined Power", "SoC Power", "System Power"])
    if package_power is None:
        parts = [cpu_power, gpu_power, ane_power, dram_power, gpu_sram_power]
        known = [value for value in parts if value is not None]
        package_power = sum(known) if known else None
    if smc_online and locked:
        raw_hint = "smc + sudo powermetrics"
    elif smc_online:
        raw_hint = "smc + powermetrics"
    elif locked:
        raw_hint = "sudo powermetrics"
    else:
        raw_hint = "powermetrics"

    return SensorInfo(
        thermal_warning=thermal_warning,
        performance_warning=performance_warning,
        cpu_power_w=cpu_power,
        gpu_power_w=gpu_power,
        cpu_temp_c=cpu_temp,
        gpu_temp_c=gpu_temp,
        fan_rpm=fan_rpm,
        privileged_locked=locked,
        raw_hint=raw_hint,
        ane_power_w=ane_power,
        dram_power_w=dram_power,
        gpu_sram_power_w=gpu_sram_power,
        package_power_w=package_power,
        gpu_active_percent=parse_percent_value(power_raw, ["GPU active residency", "GPU Active", "GPU HW active residency"]),
        gpu_freq_mhz=parse_frequency_mhz(power_raw, ["GPU HW active frequency", "GPU active frequency", "GPU Frequency"]),
        e_cluster_active=parse_percent_value(power_raw, ["E-Cluster active residency", "E cluster active residency", "Efficiency cluster active residency"]),
        p_cluster_active=parse_percent_value(power_raw, ["P-Cluster active residency", "P cluster active residency", "Performance cluster active residency"]),
        s_cluster_active=parse_percent_value(power_raw, ["S-Cluster active residency", "S cluster active residency", "Super cluster active residency"]),
        e_cluster_freq_mhz=parse_frequency_mhz(power_raw, ["E-Cluster HW active frequency", "E cluster active frequency", "Efficiency cluster active frequency"]),
        p_cluster_freq_mhz=parse_frequency_mhz(power_raw, ["P-Cluster HW active frequency", "P cluster active frequency", "Performance cluster active frequency"]),
        s_cluster_freq_mhz=parse_frequency_mhz(power_raw, ["S-Cluster HW active frequency", "S cluster active frequency", "Super cluster active frequency"]),
        dram_read_gbs=parse_temp_value(power_raw, r"DRAM\s+read[^\n:]*:\s*([\d.]+)\s*GB/s"),
        dram_write_gbs=parse_temp_value(power_raw, r"DRAM\s+write[^\n:]*:\s*([\d.]+)\s*GB/s"),
        fan_count=fan_count,
        fans=fans,
        temp_sensors=temp_sensors,
    )


def disk_info() -> DiskInfo:
    capacity = collect_disk_capacity()
    if capacity.value is not None:
        value = capacity.value
        return DiskInfo(
            label=value.volume_name.lower(), mount=value.mount_point, total=value.total_bytes,
            used=value.total_bytes - value.free_bytes, free=value.free_bytes,
        )
    # Legacy callers cannot represent an unavailable capacity result. Preserve their
    # return type without treating that fallback as decision-driving provenance.
    usage = shutil.disk_usage("/")
    return DiskInfo(label="root", mount="/", total=usage.total, used=usage.used, free=usage.free)


def disk_activity() -> DiskActivity:
    raw = run_command(["iostat", "-Id", "-c", "2", "-w", "1"], timeout=2.5)
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) < 4:
        return DiskActivity()
    numeric_lines = [
        line
        for line in lines
        if re.match(r"^\s*[-+]?\d+(?:\.\d+)?\s+", line)
    ]
    if not numeric_lines:
        return DiskActivity()
    parts = numeric_lines[-1].split()
    values: list[float] = []
    for part in parts:
        try:
            values.append(float(part))
        except ValueError:
            pass
    total_mb = 0.0
    total_xfers = 0.0
    for offset in range(0, len(values) - 2, 3):
        total_xfers += max(0.0, values[offset + 1])
        total_mb += max(0.0, values[offset + 2])
    return DiskActivity(bytes_per_sec=total_mb * 1024 * 1024, iops=total_xfers)


def network_bytes() -> tuple[int, int]:
    raw = run_command(["netstat", "-ibn"], timeout=1.5)
    iface_totals: dict[str, tuple[int, int]] = {}
    header: list[str] | None = None
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "Name":
            header = parts
            continue
        if not header or len(parts) < len(header):
            continue
        try:
            ibytes_idx = header.index("Ibytes")
            obytes_idx = header.index("Obytes")
        except ValueError:
            continue
        name = parts[0]
        if name == "lo0":
            continue
        try:
            ibytes = int(parts[ibytes_idx])
            obytes = int(parts[obytes_idx])
        except Exception:
            continue
        previous = iface_totals.get(name, (0, 0))
        iface_totals[name] = (max(previous[0], ibytes), max(previous[1], obytes))
    return (
        sum(value[0] for value in iface_totals.values()),
        sum(value[1] for value in iface_totals.values()),
    )


def collect_runtime_stats() -> RuntimeStats:
    memory = memory_stats()
    battery = battery_info()
    disk = disk_info()
    network_totals = network_bytes()
    return RuntimeStats(
        memory=memory,
        battery=battery,
        disk=disk,
        network_totals=network_totals,
        load_average=os.getloadavg(),
        collected_at=time.monotonic(),
    )


def _process_evidence_error(command: CommandResult, raw: str) -> AdapterResult[Any]:
    """Convert a parser rejection without exposing command output or diagnostics."""

    return AdapterResult(
        None,
        command.started_mono_s,
        command.completed_mono_s,
        command.completed_at_utc,
        AttemptOutcome.PARSE_ERROR,
        FailureCode.EMPTY_OUTPUT if not raw.strip() else FailureCode.PARSER_REJECTED,
    )


def _normalized_process_text(text: str) -> str:
    normalized: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if category == "Cc":
            normalized.append(" ")
        elif category != "Cf":
            normalized.append(character)
    return " ".join("".join(normalized).split())


def process_identity(command: str) -> str:
    """Return a compact, comparison-safe process identity without arguments."""

    normalized = _normalized_process_text(command)
    if not normalized:
        return "a process"
    anchored_app = re.match(
        r'^(?:["\'](/.*?\.app)(?:/[^"\']*)?["\']|'
        r'(/(?:Applications|System/Applications)/.*?\.app)(?:/[^\s"\']*)?)'
        r'(?:\s|$)',
        normalized,
    )
    if anchored_app:
        app_path = anchored_app.group(1) or anchored_app.group(2)
        return os.path.basename(app_path)[:-4] or "a process"
    try:
        executable = shlex.split(normalized, posix=True)[0]
    except (ValueError, IndexError):
        executable = normalized.split(" ", 1)[0]
    app_match = re.search(r"/([^/]+)\.app(?:/|$)", executable)
    if app_match:
        return app_match.group(1) or "a process"
    return os.path.basename(executable) or executable or "a process"


def _agx_creator_identity(raw: str) -> str:
    """Normalize the full creator label; it is not a shell command or path."""

    return _normalized_process_text(raw)


def _ioreg_brace_delta(raw: str) -> int:
    """Count structural object braces while ignoring quoted property values."""

    delta = 0
    quoted = False
    escaped = False
    for character in raw:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == "{":
            delta += 1
        elif character == "}":
            delta -= 1
    if quoted or escaped:
        raise ValueError("unterminated AGX string")
    return delta


def _parse_agx_usage(raw: str) -> int:
    """Parse an empty usage list or a sequence of flat ioreg dictionaries."""

    if raw == "()":
        return 0
    if not raw.startswith("(") or not raw.endswith(")"):
        raise ValueError("malformed AGX AppUsage container")
    inner = raw[1:-1].strip()
    objects = re.fullmatch(r"\{.*\}(?:\s*,\s*\{.*\})*", inner)
    if objects is None:
        raise ValueError("AGX AppUsage must contain dictionaries")
    bodies = re.split(r"\}\s*,\s*\{", inner[1:-1])
    field = re.compile(
        r'\s*"((?:[^"\\]|\\.)*)"\s*=\s*'
        r'(-?\d+|"(?:[^"\\]|\\.)*")'
    )
    total = 0
    for body in bodies:
        position = 0
        counters: list[int] = []
        keys: set[str] = set()
        while position < len(body):
            match = field.match(body, position)
            if match is None:
                raise ValueError("malformed AGX AppUsage dictionary")
            key, value = match.groups()
            if key in keys:
                raise ValueError("duplicate AGX AppUsage property")
            keys.add(key)
            if key == "accumulatedGPUTime":
                if not re.fullmatch(r"-?\d+", value):
                    raise ValueError("malformed AGX counter")
                counter = int(value)
                if counter < 0:
                    raise ValueError("negative AGX counter")
                counters.append(counter)
            position = match.end()
            while position < len(body) and body[position].isspace():
                position += 1
            if position == len(body):
                break
            if body[position] != ",":
                raise ValueError("malformed AGX AppUsage dictionary")
            position += 1
            if not body[position:].strip():
                raise ValueError("malformed AGX AppUsage dictionary")
        if body.strip() and len(counters) != 1:
            raise ValueError("AGX AppUsage dictionary requires one counter")
        total += sum(counters)
    return total


def parse_elapsed_seconds(raw: str) -> Optional[int]:
    """Parse ps elapsed time only when it carries a complete unambiguous duration."""

    match = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d{2}):(\d{2})", raw)
    if match:
        days, hours, minutes, seconds = match.groups()
        if int(minutes) < 60 and int(seconds) < 60:
            return ((int(days or 0) * 24 + int(hours)) * 60 + int(minutes)) * 60 + int(seconds)
        return None
    match = re.fullmatch(r"(\d+):(\d{2})", raw)
    if match and int(match.group(2)) < 60:
        return int(match.group(1)) * 60 + int(match.group(2))
    return None


def parse_process_rows(raw: str) -> tuple[ProcessRow, ...]:
    """Parse a ps table atomically: malformed rows invalidate the full sample."""

    rows: list[ProcessRow] = []
    seen_pids: set[int] = set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split(None, 5)
        if len(parts) != 6:
            raise ValueError("malformed process row")
        try:
            pid = int(parts[0])
            cpu = float(parts[1])
            memory = float(parts[2])
        except ValueError as exc:
            raise ValueError("invalid process numeric field") from exc
        if pid <= 0 or pid in seen_pids or not math.isfinite(cpu) or not math.isfinite(memory) or cpu < 0 or memory < 0:
            raise ValueError("invalid process table value")
        seen_pids.add(pid)
        rows.append(ProcessRow(pid, cpu, memory, None, parts[3], parts[4], parts[5]))
    if not rows:
        raise ValueError("empty process table")
    return tuple(rows)


def display_cell_width(text: str) -> int:
    """Measure terminal cells conservatively without treating combining marks as columns."""

    width = 0
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf"} or unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def truncate_display_cells(text: str, limit: int) -> str:
    """Truncate before a cell boundary while retaining marks attached to included glyphs."""

    if limit <= 0:
        return ""
    output: list[str] = []
    width = 0
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf"}:
            continue
        char_width = 0 if unicodedata.combining(character) else (2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1)
        if char_width and width + char_width > limit:
            break
        output.append(character)
        width += char_width
    return "".join(output)


def process_facts(rows: Sequence[ProcessRow]) -> tuple[ProcessFact, ...]:
    """Expose only minimized, display-safe process facts to the pure core."""

    return tuple(
        ProcessFact(
            row.pid,
            truncate_display_cells(process_identity(row.command), 32),
            row.cpu,
            parse_elapsed_seconds(row.etime),
        )
        for row in rows
    )


def parse_agx_counters(raw: str) -> GpuCounterSnapshot:
    """Parse AGX user clients atomically; a valid prefix never becomes evidence."""

    if not raw.strip():
        raise ValueError("empty AGX output")
    counters: dict[int, int] = {}
    identities: dict[int, str] = {}
    current: Optional[dict[str, Any]] = None
    outer_depth = 0
    expecting_outer = False
    saw_client = False
    for line in raw.splitlines():
        stripped = line.strip()
        is_header = bool(
            re.match(r"^\+-o\s+AGXDeviceUserClient\s+<class\s+AGXDeviceUserClient(?:,|>)", stripped)
        )
        if current is None:
            if not stripped:
                continue
            if any(
                property_name in line
                for property_name in (
                    '"IOUserClientCreator"',
                    '"AppUsage"',
                    '"accumulatedGPUTime"',
                )
            ):
                raise ValueError("orphan AGX property")
            if expecting_outer:
                if stripped != "{":
                    raise ValueError("missing AGX outer object")
                current = {"pid": None, "identity": None, "app_usage": None}
                outer_depth = 1
                expecting_outer = False
            elif is_header:
                saw_client = True
                expecting_outer = True
                continue
            else:
                raise ValueError("unexpected content outside AGX client")
            continue
        if is_header:
            raise ValueError("nested AGX object")
        top_level = outer_depth == 1
        creator_property = top_level and "IOUserClientCreator" in line
        usage_property = top_level and '"AppUsage"' in line
        property_line = bool(
            re.fullmatch(r'"(?:[^"\\]|\\.)+"\s*=\s*\S.*', stripped)
        )
        if stripped and stripped != "}" and not property_line:
            raise ValueError("malformed AGX property line")
        if creator_property:
            match = re.search(r'"IOUserClientCreator"\s*=\s*"pid\s+(\d+)\s*,\s*(.*?)"\s*$', line.strip())
            if match is None or current["pid"] is not None:
                raise ValueError("malformed AGX creator")
            pid = int(match.group(1))
            identity = _agx_creator_identity(match.group(2))
            if pid <= 0 or not identity:
                raise ValueError("invalid AGX creator")
            current["pid"] = pid
            current["identity"] = identity
        if usage_property:
            if current["app_usage"] is not None:
                raise ValueError("duplicate AGX AppUsage")
            match = re.fullmatch(r'"AppUsage"\s*=\s*(\(.*\))', stripped)
            if match is None:
                raise ValueError("malformed AGX AppUsage")
            usage = match.group(1)
            current["app_usage"] = _parse_agx_usage(usage)
        elif "accumulatedGPUTime" in line:
            raise ValueError("orphan AGX counter")
        outer_depth += _ioreg_brace_delta(line)
        if outer_depth < 0:
            raise ValueError("unbalanced AGX object")
        if outer_depth == 0:
            if stripped != "}":
                raise ValueError("AGX outer object requires an exact close")
            pid = current["pid"]
            identity = current["identity"]
            counter = current["app_usage"]
            if pid is None or identity is None or counter is None:
                raise ValueError("incomplete AGX object")
            previous_identity = identities.get(pid)
            if previous_identity is not None and previous_identity != identity:
                raise ValueError("conflicting AGX identities")
            identities[pid] = identity
            counters[pid] = counters.get(pid, 0) + counter
            current = None
    if current is not None or expecting_outer:
        raise ValueError("unterminated AGX object")
    if not saw_client:
        raise ValueError("output did not enumerate AGX clients")
    return GpuCounterSnapshot(counters, identities, complete=True)


class GPUProcessSampler:
    """Derive per-process GPU percentages only from consecutive complete samples."""

    def __init__(self) -> None:
        self.previous: Optional[GpuCounterSnapshot] = None
        self.previous_time: Optional[float] = None

    def reset(self) -> None:
        self.previous = None
        self.previous_time = None

    def update(
        self,
        snapshot: GpuCounterSnapshot,
        completed_mono_s: float,
        system_gpu_percent: Optional[float] = None,
    ) -> GpuProcessSample:
        if not isinstance(completed_mono_s, (int, float)) or isinstance(completed_mono_s, bool) or not math.isfinite(completed_mono_s) or not snapshot.complete:
            self.reset()
            return GpuProcessSample({}, snapshot.identities, snapshot.complete, False)
        if self.previous is None or self.previous_time is None:
            self.previous = snapshot
            self.previous_time = float(completed_mono_s)
            return GpuProcessSample({}, snapshot.identities, True, False)
        elapsed = float(completed_mono_s) - self.previous_time
        if not math.isfinite(elapsed) or elapsed <= 0:
            self.reset()
            return GpuProcessSample({}, snapshot.identities, True, False)
        for pid, counter in snapshot.counters_ns.items():
            previous_counter = self.previous.counters_ns.get(pid)
            if previous_counter is not None and self.previous.identities.get(pid) == snapshot.identities[pid] and counter < previous_counter:
                self.reset()
                return GpuProcessSample({}, snapshot.identities, True, False)
        raw_percentages: dict[int, float] = {}
        for pid, counter in snapshot.counters_ns.items():
            previous_counter = self.previous.counters_ns.get(pid)
            if previous_counter is None or self.previous.identities.get(pid) != snapshot.identities[pid]:
                continue
            raw_percentages[pid] = (counter - previous_counter) / elapsed / 10_000_000.0
        total_percent = sum(raw_percentages.values())
        scale = 1.0
        if total_percent > 0 and isinstance(system_gpu_percent, (int, float)) and not isinstance(system_gpu_percent, bool) and math.isfinite(system_gpu_percent) and system_gpu_percent > 0:
            scale = float(system_gpu_percent) / total_percent
        self.previous = snapshot
        self.previous_time = float(completed_mono_s)
        return GpuProcessSample(
            {pid: percent * scale for pid, percent in raw_percentages.items()},
            snapshot.identities,
            True,
            True,
        )


def collect_process_rows() -> AdapterResult[tuple[ProcessRow, ...]]:
    command = run_structured_command(
        ["/bin/ps", "-axo", "pid=,pcpu=,pmem=,stat=,etime=,command="],
        category="process-table", timeout_s=2.0,
        stdout_limit=8 * 1024 * 1024, stderr_limit=64 * 1024,
    )
    if command.outcome is not AttemptOutcome.SUCCESS:
        return AdapterResult(None, command.started_mono_s, command.completed_mono_s,
                             command.completed_at_utc, command.outcome, command.failure)
    try:
        rows = parse_process_rows(command.stdout)
    except ValueError:
        return _process_evidence_error(command, command.stdout)
    return AdapterResult(rows, command.started_mono_s, command.completed_mono_s,
                         command.completed_at_utc, AttemptOutcome.SUCCESS, None)


def collect_gpu_process_sample(
    sampler: GPUProcessSampler,
    system_gpu_percent: Optional[float],
) -> AdapterResult[GpuProcessSample]:
    command = run_structured_command(
        ["/usr/sbin/ioreg", "-r", "-c", "AGXDeviceUserClient", "-l"],
        category="process-gpu", timeout_s=2.0,
        stdout_limit=8 * 1024 * 1024, stderr_limit=64 * 1024,
    )
    if command.outcome is not AttemptOutcome.SUCCESS:
        sampler.reset()
        return AdapterResult(None, command.started_mono_s, command.completed_mono_s,
                             command.completed_at_utc, command.outcome, command.failure)
    try:
        snapshot = parse_agx_counters(command.stdout)
    except ValueError:
        sampler.reset()
        return _process_evidence_error(command, command.stdout)
    sample = sampler.update(snapshot, command.completed_mono_s, system_gpu_percent)
    return AdapterResult(sample, command.started_mono_s, command.completed_mono_s,
                         command.completed_at_utc, AttemptOutcome.SUCCESS, None)


def _registry_states() -> Mapping[SourceId, SourceState[Any]]:
    """Create the canonical live registry without starting any workers."""

    return MappingProxyType({
        source_id: new_source(
            source_id, cadence_s, stale_after_s,
        )
        for source_id, cadence_s, stale_after_s in LIVE_SOURCE_SPECS
    })


def _sanitized_process_state(
    state: SourceState[Any], now_mono_s: float,
) -> SourceState[tuple[ProcessFact, ...]]:
    """Copy retained process rows across the effect/policy privacy boundary."""

    validate_source_state(state, now_mono_s, SourceId.PROCESS_TABLE)
    if state.last_valid is None:
        return state
    rows = state.last_valid.value
    if type(rows) is not tuple or any(not isinstance(row, ProcessRow) for row in rows):
        raise ValueError("process table sample must contain ProcessRow values")
    facts = tuple(
        ProcessFact(
            row.pid,
            visible_command(row.command, 160) or "a process",
            row.cpu,
            parse_elapsed_seconds(row.etime),
        )
        for row in rows
    )
    sample = replace(state.last_valid, value=facts)
    return replace(state, last_valid=sample)


def _validated_live_states(
    states: Mapping[SourceId, SourceState[Any]], now_mono_s: float,
) -> Mapping[SourceId, SourceState[Any]]:
    """Reject malformed, missing, extra, or misplaced evidence before reduction."""

    if not isinstance(states, Mapping):
        raise ValueError("states must be a mapping")
    expected = tuple(source_id for source_id, _cadence, _stale in LIVE_SOURCE_SPECS)
    if tuple(states) != expected:
        raise ValueError("states must use the exact live registry order")
    copied: dict[SourceId, SourceState[Any]] = {}
    for source_id in expected:
        state = states[source_id]
        validate_source_state(state, now_mono_s, source_id)
        copied[source_id] = state
    copied[SourceId.PROCESS_TABLE] = _sanitized_process_state(
        copied[SourceId.PROCESS_TABLE], now_mono_s,
    )
    return MappingProxyType(copied)


def _ready_value(
    state: SourceState[Any], expected_type: type, now_mono_s: float,
) -> Optional[Any]:
    if not is_decision_ready(state, now_mono_s) or state.last_valid is None:
        return None
    value = state.last_valid.value
    if not isinstance(value, expected_type):
        raise ValueError("ready source value has the wrong type")
    return value


def _latch_for_rule(policy: PolicyState, rule: Any) -> Any:
    name = {
        "memory.watch": "memory_latch",
        "memory.critical": "memory_latch",
        "thermal.watch": "thermal_latch",
        "storage.watch": "storage_latch",
        "cpu.watch": "cpu_latch",
    }.get(getattr(rule, "value", ""))
    if name is None:
        raise ValueError("unknown active rule")
    return getattr(policy, name)


def _decisive_trigger_time(
    policy: PolicyState, assessment: HealthAssessment,
) -> Optional[float]:
    if not assessment.active_rules:
        return None
    latch = _latch_for_rule(policy, assessment.active_rules[0])
    trigger = latch.trigger
    if trigger is None:
        return None
    for ref in assessment.evidence:
        if (ref.role is EvidenceRole.TRIGGER
                and ref.source_id is trigger.source_id
                and ref.revision == trigger.revision):
            return trigger.triggered_mono_s
    return None


def build_health_frame(
    policy_state: PolicyState,
    states: Mapping[SourceId, SourceState[Any]],
    now_mono_s: float,
) -> tuple[PolicyState, HealthFrame]:
    """Reduce one validated live registry into a privacy-safe frame."""

    if type(now_mono_s) not in (int, float) or not math.isfinite(now_mono_s):
        raise ValueError("now_mono_s must be finite")
    checked = _validated_live_states(states, float(now_mono_s))
    facts = HealthFacts(
        checked[SourceId.CPU_USAGE],
        checked[SourceId.MEMORY_PRESSURE],
        checked[SourceId.THERMAL_OS],
        checked[SourceId.DISK_CAPACITY],
        checked[SourceId.PROCESS_TABLE],
    )
    next_policy, assessment = reduce_health(policy_state, facts, float(now_mono_s))
    views = MappingProxyType({
        source_id: source_view(checked[source_id], float(now_mono_s))
        for source_id, _cadence, _stale in LIVE_SOURCE_SPECS
    })
    contributor = select_cpu_contributor(
        next_policy.process_history, checked[SourceId.PROCESS_TABLE], float(now_mono_s),
    )
    frame = HealthFrame(
        assessment=assessment,
        source_views=views,
        cpu_percent=_ready_value(checked[SourceId.CPU_USAGE], CpuUsage, float(now_mono_s)).percent
        if _ready_value(checked[SourceId.CPU_USAGE], CpuUsage, float(now_mono_s)) is not None else None,
        memory_pressure=_ready_value(checked[SourceId.MEMORY_PRESSURE], MemoryPressure, float(now_mono_s)),
        thermal=_ready_value(checked[SourceId.THERMAL_OS], OsThermalState, float(now_mono_s)),
        disk_capacity=_ready_value(checked[SourceId.DISK_CAPACITY], DiskCapacity, float(now_mono_s)),
        contributor=contributor,
        origin=FrameOrigin.LIVE,
        captured_mono_s=float(now_mono_s),
        decisive_triggered_mono_s=_decisive_trigger_time(next_policy, assessment),
    )
    return next_policy, frame


def initial_health_frame(now_mono_s: float) -> HealthFrame:
    """Return an effect-free initial CHECKING projection for progressive startup."""

    _policy, frame = build_health_frame(initial_policy_state(), _registry_states(), now_mono_s)
    return frame


class LiveHealthCoordinator:
    """Own typed polling, coherent health reduction, and live collector baselines."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._generation = 0
        self._policy = initial_policy_state()
        self._frame_states = _registry_states()
        self._frame = initial_health_frame(0.0)
        self._per_core: tuple[float, ...] = ()
        self._per_core_revision = 0
        self._system_gpu_percent: Optional[float] = None
        self._previous_time: Optional[tuple[float, datetime]] = None
        self._cpu_sampler: MacCpuSampler
        self._gpu_sampler: GPUProcessSampler
        self._pollers: Mapping[SourceId, SourcePoller[Any]]
        self._create_generation()

    def _create_generation(self) -> None:
        self._generation += 1
        generation = self._generation
        cpu_sampler = MacCpuSampler()
        gpu_sampler = GPUProcessSampler()
        pollers: dict[SourceId, SourcePoller[Any]] = {}

        def collect_cpu() -> AdapterResult[CpuUsage]:
            result = cpu_sampler.sample_usage()
            return result

        def collect_gpu() -> AdapterResult[GpuProcessSample]:
            with self._lock:
                calibration = self._system_gpu_percent
                current = self._generation == generation and not self._closed
            if not current:
                return AdapterResult(
                    None, time.monotonic(), time.monotonic(), utc_now(),
                    AttemptOutcome.CANCELLED, FailureCode.USER_CANCELLED,
                )
            return collect_gpu_process_sample(gpu_sampler, calibration)

        collectors: Mapping[SourceId, Callable[[], AdapterResult[Any]]] = {
            SourceId.CPU_USAGE: collect_cpu,
            SourceId.MEMORY_PRESSURE: collect_memory_pressure,
            SourceId.THERMAL_OS: collect_os_thermal_state,
            SourceId.DISK_CAPACITY: collect_disk_capacity,
            SourceId.PROCESS_TABLE: collect_process_rows,
            SourceId.PROCESS_GPU: collect_gpu,
        }
        deadline = time.monotonic() + 1.0
        try:
            for source_id, cadence_s, stale_after_s in LIVE_SOURCE_SPECS:
                pollers[source_id] = SourcePoller(
                    new_source(
                        source_id, cadence_s, stale_after_s,
                    ),
                    collectors[source_id],
                )
        except BaseException:
            self._close_poller_set(pollers, deadline)
            raise
        self._cpu_sampler = cpu_sampler
        self._gpu_sampler = gpu_sampler
        self._pollers = MappingProxyType(pollers)

    @staticmethod
    def _close_poller_set(
        pollers: Mapping[SourceId, SourcePoller[Any]], deadline: float,
    ) -> Optional[BaseException]:
        first_error: Optional[BaseException] = None
        for source_id, _cadence, _stale in LIVE_SOURCE_SPECS:
            poller = pollers.get(source_id)
            if poller is None:
                continue
            try:
                poller.close(max(0.0, deadline - time.monotonic()))
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        return first_error

    @staticmethod
    def _is_future_state(state: SourceState[Any], now_mono_s: float) -> bool:
        times = []
        if state.active_started_mono_s is not None:
            times.append(state.active_started_mono_s)
        if state.last_attempt is not None:
            times.append(state.last_attempt.completed_mono_s)
        if state.last_valid is not None:
            times.append(state.last_valid.completed_mono_s)
        return any(value > now_mono_s for value in times)

    def tick(self, now_mono_s: float, force: bool = False) -> HealthFrame:
        with self._lock:
            if self._closed:
                raise RuntimeError("live coordinator is closed")
        if type(now_mono_s) not in (int, float) or not math.isfinite(now_mono_s):
            raise ValueError("now_mono_s must be finite")
        if type(force) is not bool:
            raise ValueError("force must be a bool")
        with self._lock:
            snapshots: dict[SourceId, SourceState[Any]] = {}
            for source_id, _cadence, _stale in LIVE_SOURCE_SPECS:
                self._pollers[source_id].tick(float(now_mono_s), force)
            for source_id, _cadence, _stale in LIVE_SOURCE_SPECS:
                snapshots[source_id] = self._pollers[source_id].snapshot()
            frame_states = dict(self._frame_states)
            for source_id, state in snapshots.items():
                if not self._is_future_state(state, float(now_mono_s)):
                    frame_states[source_id] = state
            self._policy, self._frame = build_health_frame(
                self._policy, MappingProxyType(frame_states), float(now_mono_s),
            )
            self._frame_states = MappingProxyType(frame_states)
            cpu = frame_states[SourceId.CPU_USAGE]
            if (cpu.last_attempt is not None
                    and cpu.last_attempt.outcome is AttemptOutcome.SUCCESS
                    and cpu.last_valid is not None
                    and cpu.last_valid.revision != self._per_core_revision):
                self._per_core = tuple(self._cpu_sampler.last_per_core)
                self._per_core_revision = cpu.last_valid.revision
            return self._frame

    def source_state(self, source_id: SourceId) -> SourceState[Any]:
        if not isinstance(source_id, SourceId):
            raise ValueError("source_id must be a SourceId")
        with self._lock:
            return self._pollers[source_id].snapshot()

    def process_rows(self, now_mono_s: float) -> tuple[ProcessRow, ...]:
        if type(now_mono_s) not in (int, float) or not math.isfinite(now_mono_s):
            raise ValueError("now_mono_s must be finite")
        with self._lock:
            process = self._pollers[SourceId.PROCESS_TABLE].snapshot()
            if process.last_valid is None or type(process.last_valid.value) is not tuple:
                return ()
            rows = process.last_valid.value
            if any(not isinstance(row, ProcessRow) for row in rows):
                raise ValueError("process table sample must contain ProcessRow values")
            return merge_gpu_process_rows(
                rows, self._pollers[SourceId.PROCESS_GPU].snapshot(), float(now_mono_s),
            )

    def per_core(self) -> tuple[float, ...]:
        with self._lock:
            return self._per_core

    def set_system_gpu_percent(self, percent: Optional[float]) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("live coordinator is closed")
        if percent is not None and (
                type(percent) not in (int, float)
                or not math.isfinite(percent)
                or percent < 0 or percent > 100):
            raise ValueError("system GPU percent must be a finite 0..100 value or None")
        with self._lock:
            self._system_gpu_percent = None if percent is None else float(percent)

    def reset_policy(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("live coordinator is closed")
            self._policy = reset_policy_state()

    def observe_time(self, now_mono_s: float, now_utc: datetime) -> bool:
        with self._lock:
            if self._closed:
                raise RuntimeError("live coordinator is closed")
        if type(now_mono_s) not in (int, float) or not math.isfinite(now_mono_s):
            raise ValueError("now_mono_s must be finite")
        normalized = _normalize_utc_timestamp(now_utc, "now_utc")
        with self._lock:
            previous = self._previous_time
            self._previous_time = (float(now_mono_s), normalized)
            if previous is None:
                return False
            mono_delta = float(now_mono_s) - previous[0]
            wall_delta = (normalized - previous[1]).total_seconds()
            reset_sources = mono_delta < 0 or mono_delta > 30.0
            reset_policy_only = abs(wall_delta - mono_delta) > 5.0
            if not reset_sources and not reset_policy_only:
                return False
            self._policy = reset_policy_state()
            if reset_sources:
                old_pollers = self._pollers
                self._per_core = ()
                self._per_core_revision = 0
                self._frame_states = _registry_states()
                self._create_generation()
                error = self._close_poller_set(old_pollers, time.monotonic() + 1.0)
                if error is not None:
                    raise error
            return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pollers = self._pollers
        error = self._close_poller_set(pollers, time.monotonic() + 1.0)
        if error is not None:
            raise error


def merge_gpu_process_rows(
    rows: Sequence[ProcessRow],
    gpu_state: SourceState[GpuProcessSample],
    now_mono_s: float,
) -> tuple[ProcessRow, ...]:
    unknown = tuple(replace(row, gpu=None) for row in rows)
    try:
        validate_source_state(gpu_state, now_mono_s, SourceId.PROCESS_GPU)
    except (TypeError, ValueError):
        return unknown
    if (
        gpu_state.last_attempt is None
        or gpu_state.last_attempt.outcome is not AttemptOutcome.SUCCESS
        or gpu_state.last_valid is None
        or gpu_state.last_valid.revision != gpu_state.last_attempt.revision
    ):
        return unknown
    age = float(now_mono_s) - gpu_state.last_valid.completed_mono_s
    if age < 0 or age > 15.0:
        return unknown
    sample = gpu_state.last_valid.value
    if not isinstance(sample, GpuProcessSample):
        return unknown
    if not sample.complete or not sample.baseline_ready:
        return unknown
    merged: list[ProcessRow] = []
    for row in rows:
        identity = sample.identities.get(row.pid)
        if identity is None:
            merged.append(replace(row, gpu=0.0))
        elif process_identity(row.command) != identity:
            merged.append(replace(row, gpu=None))
        else:
            merged.append(replace(row, gpu=sample.percentages.get(row.pid)))
    return tuple(merged)


def sort_process_rows(rows: Sequence[ProcessRow], sort_mode: str) -> list[ProcessRow]:
    if sort_mode == "GPU":
        return sorted(rows, key=lambda row: (row.gpu is None, -(row.gpu or 0.0)))
    attribute = "mem" if sort_mode == "MEM" else "cpu"
    return sorted(rows, key=lambda row: getattr(row, attribute), reverse=True)


def process_rows(sort_mode: str) -> list[ProcessRow]:
    return sort_process_rows(collect_process_rows().value or (), sort_mode)


def color_for_percent(percent: float) -> int:
    if percent >= 85:
        return color_pair(4) | curses.A_BOLD
    if percent >= 60:
        return color_pair(3) | curses.A_BOLD
    return color_pair(2)


def safe_add(screen: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width - 1:
        return
    try:
        screen.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


def draw_box(screen: curses.window, y: int, x: int, h: int, w: int, title: str, attr: int) -> None:
    if h < 3 or w < 8:
        return
    if unicode_ok():
        tl, tr, bl, br, hz, vt = "╭", "╮", "╰", "╯", "─", "│"
    else:
        tl, tr, bl, br, hz, vt = "+", "+", "+", "+", "-", "|"
    top = tl + hz * (w - 2) + tr
    bottom = bl + hz * (w - 2) + br
    safe_add(screen, y, x, top, attr)
    for row in range(1, h - 1):
        safe_add(screen, y + row, x, vt, attr)
        safe_add(screen, y + row, x + w - 1, vt, attr)
    safe_add(screen, y + h - 1, x, bottom, attr)
    label = f" {title} "
    safe_add(screen, y, x + 2, label[: max(0, w - 4)], attr | curses.A_BOLD)


def fill_rect(screen: curses.window, y: int, x: int, h: int, w: int, attr: int = 0) -> None:
    if h <= 0 or w <= 0:
        return
    blank = " " * w
    for row in range(h):
        safe_add(screen, y + row, x, blank, attr)


def pulse_bar(percent: float, width: int, phase: int, label: str = "") -> str:
    width = max(4, width)
    filled = int(round(width * clamp(percent) / 100.0))
    fill_char, empty_char, pulse_char = ("█", "░", "◆") if unicode_ok() else ("#", ".", "@")
    body = [fill_char] * filled + [empty_char] * (width - filled)
    if filled:
        body[phase % filled] = pulse_char
    return f"{label}[{''.join(body)}] {percent:5.1f}%"


def spark_chars() -> list[str]:
    encoding = locale.getpreferredencoding(False).upper()
    if "UTF" not in encoding:
        return list(" .:-=+*#")
    return [chr(code) for code in range(0x2581, 0x2589)]


def sparkline(values: deque[float] | list[float], width: int, max_value: float | None = 100.0) -> str:
    width = max(1, width)
    data = list(values)[-width:]
    if not data:
        return " " * width
    chars = spark_chars()
    if max_value is None:
        scale = max(data) or 1.0
    else:
        scale = max(max_value, 1.0)
    rendered = []
    for value in data:
        index = int(round(clamp(value / scale * 100.0) / 100.0 * (len(chars) - 1)))
        rendered.append(chars[max(0, min(index, len(chars) - 1))])
    return (" " * max(0, width - len(rendered))) + "".join(rendered)


def cpu_total_percent(per_core: list[float]) -> float:
    return sum(per_core) / len(per_core) if per_core else 0.0


def ram_percent(mem: dict[str, float]) -> float:
    total = mem.get("total", 0.0)
    if total <= 0:
        return 0.0
    return mem.get("used", 0.0) / total * 100.0


def disk_percent(disk: DiskInfo) -> float:
    return disk.used / disk.total * 100.0 if disk.total else 0.0


def comet(width: int, phase: int, density: float = 0.65) -> str:
    width = max(4, width)
    empty = "·" if unicode_ok() else "."
    trail = [empty] * width
    head = phase % width
    chars = ["◆", "●", "•", "·", "-", empty] if unicode_ok() else ["@", "#", "*", "+", "-", "."]
    for offset, char in enumerate(chars):
        idx = (head - offset) % width
        trail[idx] = char
    fill = int(width * clamp(density * 100.0) / 100.0)
    for idx in range(fill, width):
        if trail[idx] == empty:
            trail[idx] = " "
    return "".join(trail)


def flow_text(phase: int, charging: bool, external: bool) -> str:
    frames = [">  ", ">> ", ">>>", " >>", "  >"] if charging else ["<  ", "<< ", "<<<", " <<", "  <"]
    if external and not charging:
        frames = ["== ", "===", " ==", "==="]
    return frames[phase % len(frames)]


def temp_attr(value: float | None) -> int:
    if value is None:
        return color_pair(3)
    if value >= 85:
        return color_pair(4) | curses.A_BOLD
    if value >= 65:
        return color_pair(3) | curses.A_BOLD
    return color_pair(2)


def fan_percent(fan: FanReading) -> float:
    if fan.maximum_rpm and fan.maximum_rpm > 0:
        return clamp(fan.rpm / fan.maximum_rpm * 100.0)
    if fan.target_rpm and fan.target_rpm > 0:
        return clamp(fan.rpm / fan.target_rpm * 100.0)
    return clamp(fan.rpm / 7000.0 * 100.0)


def fan_attr(fan: FanReading) -> int:
    pct = fan_percent(fan)
    if pct >= 85:
        return color_pair(4) | curses.A_BOLD
    if pct >= 55:
        return color_pair(3) | curses.A_BOLD
    return color_pair(2)


def temperature_group_rows(readings: list[TempReading]) -> list[tuple[str, float, float, int]]:
    grouped: dict[str, list[float]] = {}
    for reading in readings:
        label = reading.name
        if reading.key and label.endswith(reading.key):
            label = label[: -len(reading.key)].strip() or reading.key
        label = re.sub(r"\s+", " ", label.strip()) or "Sensor"
        grouped.setdefault(label, []).append(reading.value_c)
    rows = [
        (label, sum(values) / len(values), max(values), len(values))
        for label, values in grouped.items()
        if values
    ]
    rows.sort(key=lambda item: item[2], reverse=True)
    return rows


def simple_process_name(command: str) -> str:
    app_match = re.search(r"/([^/]+)\.app(?:/|$)", command)
    if app_match:
        return app_match.group(1)
    first = command.strip().split(None, 1)[0] if command.strip() else "a process"
    name = os.path.basename(first) or first
    return visible_command(name, 22)


def system_read(
    per_core: list[float],
    mem: dict[str, float],
    disk: DiskInfo,
    activity: DiskActivity,
    battery: BatteryInfo,
    sensor: SensorInfo,
    rows: list[ProcessRow],
    net_down: float = 0.0,
    net_up: float = 0.0,
) -> tuple[str, str]:
    cpu_pct = cpu_total_percent(per_core)
    mem_pct = ram_percent(mem)
    disk_pct = disk_percent(disk)
    swap_total = mem.get("swap_total", 0.0)
    swap_pct = mem.get("swap_used", 0.0) / swap_total * 100.0 if swap_total else 0.0
    hottest = max(
        [value for value in (sensor.cpu_temp_c, sensor.gpu_temp_c, battery.temp_c) if value is not None],
        default=None,
    )
    max_fan_pct = max((fan_percent(fan) for fan in sensor.fans), default=0.0)
    top_cpu = rows[0] if rows else None
    measured_gpu_rows = [row for row in rows if row.gpu is not None]
    top_gpu = max(measured_gpu_rows, key=lambda row: row.gpu, default=None)
    net_bps = net_down + net_up

    settled_states = ("nominal", "pending", "checking", "unavailable")
    if sensor.performance_warning not in settled_states:
        return ("read: macOS is holding performance back; heat is the reason.", "hot")
    if sensor.thermal_warning not in settled_states:
        return ("read: Thermal pressure is active; fans need time to catch up.", "hot")
    if hottest is not None and hottest >= 92 and cpu_pct >= 55:
        return ("read: Hot CPU work is driving the machine right now.", "hot")
    if hottest is not None and hottest >= 88 and max_fan_pct >= 80:
        return ("read: Very warm, with fans already working hard.", "hot")
    if mem_pct >= 90 or swap_pct >= 25:
        return ("read: Memory is tight; closing heavy apps would help.", "hot")
    if top_cpu and cpu_pct >= 72 and top_cpu.cpu >= 80:
        return (f"read: CPU is busy; {simple_process_name(top_cpu.command)} is the main pull.", "watch")
    if cpu_pct >= 72:
        return ("read: CPU is busy; something is working hard.", "watch")
    if top_gpu and (sensor.gpu_active_percent or top_gpu.gpu or 0.0) >= 45:
        name = simple_process_name(top_gpu.command) if (top_gpu.gpu or 0.0) >= 8 else "graphics work"
        return (f"read: GPU is busy; {name} is driving it.", "watch")
    if max_fan_pct >= 75:
        return ("read: Fans are high; this is a sustained workload.", "watch")
    if sensor.package_power_w is not None and sensor.package_power_w >= 70:
        return ("read: Power draw is high, but thermals still look controlled.", "watch")
    if activity.bytes_per_sec >= 900_000_000 or activity.iops >= 15_000:
        return ("read: Disk is moving a lot of data right now.", "watch")
    if mem_pct >= 82:
        return ("read: Memory use is high, but there is still room.", "watch")
    if disk_pct >= 88:
        return ("read: Storage is getting full; keep an eye on free space.", "watch")
    if battery.present and not battery.external_connected and (cpu_pct >= 45 or net_bps >= 25_000_000):
        return ("read: Running hard on battery; runtime will drop.", "watch")
    if hottest is not None and hottest >= 78 and cpu_pct < 30:
        return ("read: Warm for a light load; charging or displays may be adding heat.", "watch")
    if cpu_pct <= 25 and mem_pct <= 75 and (hottest is None or hottest <= 70):
        return ("read: Quiet. Nothing unusual stands out.", "good")
    return ("read: Normal workload; no obvious pressure right now.", "good")


def read_attr(level: str) -> int:
    if level == "hot":
        return color_pair(4) | curses.A_BOLD
    if level == "watch":
        return color_pair(3) | curses.A_BOLD
    return color_pair(7)


def display_core_usage(per_core: list[float], static: dict[str, Any]) -> tuple[list[float], list[str]]:
    index_map = static.get("cpu_index_map") or []
    labels = static.get("core_labels") or []
    if index_map and labels and len(index_map) == len(labels):
        ordered: list[float] = []
        ordered_labels: list[str] = []
        for label, mach_index in zip(labels, index_map):
            if isinstance(mach_index, int) and 0 <= mach_index < len(per_core):
                ordered.append(per_core[mach_index])
                ordered_labels.append(str(label))
        if ordered:
            return ordered, ordered_labels
    return per_core, [f"W{index}" for index in range(len(per_core))]


def cpu_groups(static: dict[str, Any], core_count: int) -> list[dict[str, Any]]:
    labels = static.get("core_labels") or []
    index_map = static.get("cpu_index_map") or []
    if labels and index_map and len(labels) == core_count:
        names = {
            "E": "Efficiency",
            "P": "Performance",
            "S": "Super",
            "M": "Medium",
            "W": "Workers",
        }
        detected_groups: list[dict[str, Any]] = []
        start = 0
        while start < core_count:
            code = str(labels[start])[:1] or "W"
            end = start + 1
            while end < core_count and str(labels[end]).startswith(code):
                end += 1
            detected_groups.append({"code": code, "label": names.get(code, "Cluster"), "start": start, "count": end - start})
            start = end
        return detected_groups

    clusters = static.get("cpu_clusters") or []
    fallback_groups: list[dict[str, Any]] = []
    start = 0
    for cluster in clusters:
        try:
            count = int(cluster.get("logical", 0))
        except (TypeError, ValueError):
            count = 0
        count = min(count, max(0, core_count - start))
        if count <= 0:
            continue
        fallback_groups.append(
            {
                "code": str(cluster.get("code") or "C")[:1],
                "label": str(cluster.get("label") or "Cluster"),
                "start": start,
                "count": count,
            }
        )
        start += count
    if start < core_count:
        fallback_groups.append({"code": "W", "label": "Workers", "start": start, "count": core_count - start})
    if not fallback_groups and core_count:
        fallback_groups.append({"code": "W", "label": "Workers", "start": 0, "count": core_count})
    return fallback_groups


def cpu_group_columns(panel_width: int) -> int:
    inner_width = max(1, panel_width - 4)
    return max(1, inner_width // 13)


def proportional_heights(total: int, specs: list[tuple[int, int]]) -> list[int]:
    if not specs:
        return []
    minimums = [minimum for minimum, _weight in specs]
    weights = [weight for _minimum, weight in specs]
    min_sum = sum(minimums)
    if total <= min_sum:
        heights = minimums[:]
        overflow = min_sum - total
        for index in sorted(range(len(heights)), key=lambda item: heights[item], reverse=True):
            reducible = max(0, heights[index] - 4)
            take = min(reducible, overflow)
            heights[index] -= take
            overflow -= take
            if overflow <= 0:
                break
        if overflow > 0:
            heights[-1] = max(3, heights[-1] - overflow)
        return heights

    extra = total - min_sum
    weight_sum = sum(weights) or len(specs)
    heights = minimums[:]
    assigned = 0
    for index, weight in enumerate(weights[:-1]):
        add = extra * (weight or 1) // weight_sum
        heights[index] += add
        assigned += add
    heights[-1] += extra - assigned
    return heights


def compact_top_heights(total: int) -> tuple[int, int]:
    """Split a short stacked region without drawing either panel past it."""
    system_height = max(3, min(7, total // 2))
    power_height = total - system_height
    if power_height < 3:
        power_height = 3
        system_height = max(1, total - power_height)
    return system_height, power_height


def wide_column_widths(width: int, layout: str) -> tuple[int, int, int]:
    if layout in ("io", "thermals"):
        left_width = 46
    elif layout == "compute":
        left_width = 34
    elif layout == "compact":
        left_width = 36
    else:
        left_width = 38

    available = max(1, width - left_width - 4)
    if layout == "processes":
        right_target = max(56, width * 43 // 100)
    elif layout == "compute":
        right_target = 36
    elif layout == "cinema":
        right_target = 40
    elif layout in ("io", "thermals"):
        right_target = max(34, min(48, available - 44))
    else:
        right_target = 34 if width < 132 else 48

    center_cap = 96 if layout in ("compute", "cinema") else 72
    center_width = min(center_cap, max(34, available - right_target))
    center_width = min(center_width, max(1, available - 24))
    right_width = max(1, available - center_width)
    return left_width, center_width, right_width


def process_body_height(panel_height: int) -> int:
    return max(0, panel_height - 5)


def render_interval(animation_mode: int) -> float:
    return (1.00, 0.40, 0.08)[animation_mode % 3]


def cpu_lane_text(label: str, index: int, percent: float, width: int, phase: int) -> str:
    if width >= 20:
        return pulse_bar(percent, max(4, width - 13), phase + index, f"{label} ")[: max(0, width)]
    if width >= 11:
        percentage = f"{int(round(clamp(percent))):3d}"
        prefix = f"{label} ["
        suffix = f"] {percentage}"
        bar_width = max(1, width - len(prefix) - len(suffix))
        filled = int(round(bar_width * clamp(percent) / 100.0))
        fill_char, empty_char, pulse_char = ("█", "░", "◆") if unicode_ok() else ("#", ".", "@")
        body = [fill_char] * filled + [empty_char] * (bar_width - filled)
        if filled:
            body[(phase + index) % filled] = pulse_char
        return f"{prefix}{''.join(body)}{suffix}"[: max(0, width)]
    return f"{label[-3:]} {clamp(percent):3.0f}%"[: max(0, width)]


def pig_mascot_lines(phase: int) -> list[str]:
    blink = (phase // 10) % 18 == 0
    grin_frames = ["\\____/", "\\_u__/", "\\____/", "\\_v__/"]
    eyes = "--  --" if blink else "oo  oo" if (phase // 18) % 5 else "^^  ^^"
    ear_tip = "~" if (phase // 9) % 2 else "^"
    return [
        f"   /{ear_tip}\\   _.._   /{ear_tip}\\  ",
        "  /  `.'    `.'  \\ ",
        f" |    {eyes}    |",
        " |    .-oo-.    |",
        f" |    {grin_frames[(phase // 7) % len(grin_frames)]:^6}    |",
        "  '._  ----  _.'  ",
    ]


def draw_header(screen: curses.window, width: int, phase: int, static: dict[str, Any], config: HudConfig) -> int:
    screen.erase()
    now_text = time.strftime("%Y-%m-%d %H:%M:%S")
    height, _screen_width = screen.getmaxyx()
    if width >= 118 and height >= 34 and config.layout_name != "compact":
        title_lines = [
            " ____            _          _   _ _   _ ____  ",
            "|  _ \\ ___  _ __| | ___   _| | | | | | |  _ \\ ",
            "| |_) / _ \\| '__| |/ / | | | |_| | | | | | | |",
            "|  __/ (_) | |  |   <| |_| |  _  | |_| | |_| |",
            "|_|   \\___/|_|  |_|\\_\\\\__, |_| |_|\\___/|____/ ",
            "                       |___/                  ",
        ]
        mascot_lines = pig_mascot_lines(phase)
        for idx, line in enumerate(title_lines):
            attr = color_pair(2) | curses.A_BOLD if idx in (0, 2) else color_pair(1)
            safe_add(screen, idx, 2, line, attr)
        pig_x = min(max(58, len(title_lines[0]) + 8), max(2, width - 54))
        for idx, line in enumerate(mascot_lines):
            attr = color_pair(3) | curses.A_BOLD if idx in (2, 3) else color_pair(7)
            safe_add(screen, idx, pig_x, line, attr)
        safe_add(screen, 1, max(58, width - len(now_text) - 3), now_text, color_pair(6) | curses.A_BOLD)
        rig = f"{static.get('chip', 'Mac')} / {static.get('model', '')}".strip()
        safe_add(screen, 3, max(58, width - len(rig) - 3), rig[: max(0, width - 62)], color_pair(1))
        meta = f"{COPYRIGHT_TEXT}  |  {config.theme_name} / {config.layout_name}"
        safe_add(screen, 5, max(58, width - len(meta) - 3), meta, color_pair(7) | curses.A_BOLD)
        divider = list("-" * (width - 2))
        head = (phase * 3) % max(1, width - 2)
        for offset, char in enumerate("<*>"):
            idx = head + offset
            if 0 <= idx < len(divider):
                divider[idx] = char
        safe_add(screen, 6, 1, "".join(divider), color_pair(5))
        return 7

    title = " P O R K Y H U D "
    safe_add(screen, 0, 1, title, color_pair(2) | curses.A_BOLD)
    safe_add(screen, 0, max(1, width - len(now_text) - 2), now_text, color_pair(6) | curses.A_BOLD)
    meta = f"{COPYRIGHT_TEXT} | {config.theme_name} / {config.layout_name}"
    safe_add(screen, 1, max(1, width - len(meta) - 2), meta[: max(0, width - 3)], color_pair(7))
    divider = list("-" * (width - 2))
    divider[(phase * 2) % max(1, width - 2)] = "*"
    safe_add(screen, 2, 1, "".join(divider), color_pair(5))
    return 3


def draw_cpu_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    per_core: list[float],
    static: dict[str, Any],
    history: MetricHistory,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "CPU WORKER CORES", color_pair(5))
    if h < 5:
        return
    display_core, labels = display_core_usage(per_core, static)
    total = cpu_total_percent(display_core)
    logical = static.get("logical_cpu") or str(len(display_core) or "?")
    physical = static.get("physical_cpu") or "?"
    core_line = static.get("cpu_cores") or f"{physical} physical / {logical} logical"
    safe_add(screen, y + 1, x + 2, f"{static.get('chip', 'CPU')}"[: w - 4], color_pair(1) | curses.A_BOLD)
    safe_add(screen, y + 2, x + 2, f"cores: {core_line}"[: w - 4], color_pair(1))
    safe_add(screen, y + 3, x + 2, pulse_bar(total, max(8, w - 18), phase, "total "), color_for_percent(total))
    safe_add(screen, y + 4, x + 2, f"60s  {sparkline(history.cpu, max(8, w - 10), 100.0)}"[: w - 4], color_pair(6))

    lanes_start = y + 6
    lanes_available = max(0, h - 8)
    if not display_core or lanes_available <= 0:
        safe_add(screen, y + 6, x + 2, "per-core stream unavailable", color_pair(3))
        return

    groups = cpu_groups(static, len(display_core))
    columns = cpu_group_columns(w)
    col_width = (w - 4) // columns
    row_y = lanes_start
    lane_limit = y + h - 2
    hidden = 0
    for group in groups:
        if row_y >= lane_limit:
            hidden += group["count"]
            continue
        label = f"{group['code']} cluster {group['count']}c"
        safe_add(screen, row_y, x + 2, label[: w - 4], color_pair(7) | curses.A_BOLD)
        row_y += 1
        for offset in range(group["count"]):
            core_index = group["start"] + offset
            column = offset % columns
            group_row = offset // columns
            cy = row_y + group_row
            if cy >= lane_limit:
                hidden += 1
                continue
            cx = x + 2 + column * col_width
            pct = display_core[core_index]
            core_label = labels[core_index] if core_index < len(labels) else f"{group['code']}{offset:02d}"
            safe_add(screen, cy, cx, cpu_lane_text(core_label, core_index, pct, max(1, col_width - 1), phase), color_for_percent(pct))
        row_y += (group["count"] + columns - 1) // columns
    if hidden:
        safe_add(screen, y + h - 2, x + 2, f"+{hidden} workers hidden", color_pair(1))


def draw_system_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    static: dict[str, Any],
    battery: BatteryInfo,
    uptime_seconds: int,
    load_average: tuple[float, float, float],
) -> None:
    draw_box(screen, y, x, h, w, "SYSTEM", color_pair(5))
    lines = [
        f"node: {static.get('host', 'localhost')}",
        f"rig:  {static.get('model', 'Mac')} {static.get('model_id', '')}".strip(),
        f"os:   {static.get('os', 'macOS')} build {static.get('build', '')}".strip(),
        f"kern: Darwin {static.get('kernel', '')}",
        f"up:   {format_uptime(uptime_seconds)}",
        f"load: {load_average[0]:.2f} {load_average[1]:.2f} {load_average[2]:.2f}",
        battery_summary(battery),
    ]
    for index, line in enumerate(lines[: h - 2], start=1):
        attr = color_pair(1)
        if index == 1:
            attr |= curses.A_BOLD
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def draw_memory_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    mem: dict[str, float],
    history: MetricHistory,
    phase: int = 0,
) -> None:
    draw_box(screen, y, x, h, w, "MEMORY", color_pair(5))
    total = mem.get("total", 0.0) or 1.0
    used = mem.get("used", 0.0)
    available = mem.get("available", 0.0)
    cached = mem.get("cached", 0.0)
    app = mem.get("app", 0.0)
    swap_total = mem.get("swap_total", 0.0)
    swap_used = mem.get("swap_used", 0.0)
    used_pct = used / total * 100.0
    swap_pct = (swap_used / swap_total * 100.0) if swap_total else 0.0
    lines = [
        (pulse_bar(used_pct, max(8, w - 18), phase, "ram  "), color_for_percent(used_pct)),
        (f"60s  {sparkline(history.ram, max(8, w - 10), 100.0)}", color_pair(6)),
        (f"used {human_bytes(used)} / {human_bytes(total)}  avail {human_bytes(available)}", color_pair(1)),
        (f"app {human_bytes(app)}  wired {human_bytes(mem.get('wired', 0.0))}", color_pair(1)),
        (f"cache {human_bytes(cached)}  comp {human_bytes(mem.get('compressed', 0.0))}", color_pair(1)),
        (pulse_bar(swap_pct, max(8, w - 18), phase + 4, "swap "), color_for_percent(swap_pct)),
    ]
    if h > 8:
        lines.append((f"swap {human_bytes(swap_used)} / {human_bytes(swap_total)}", color_pair(1)))
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def draw_battery_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryInfo,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "POWER CELL", color_pair(5))
    if not battery.present:
        desktop_lines = [
            ("desktop power", color_pair(2) | curses.A_BOLD),
            (f"source: {battery.source}", color_pair(1)),
            ("internal battery: not present", color_pair(1)),
        ]
        for index, (line, line_attr) in enumerate(desktop_lines[: h - 2], start=1):
            safe_add(screen, y + index, x + 2, line[: w - 4], line_attr)
        return
    pct = float(battery.percent if battery.percent is not None else 0)
    attr = color_pair(2) | curses.A_BOLD
    if battery.percent is not None and battery.percent <= 30:
        attr = color_pair(3) | curses.A_BOLD
    if battery.percent is not None and battery.percent <= 20 and not battery.external_connected:
        attr = color_pair(4) | curses.A_BOLD

    state = "charging" if battery.is_charging else battery.state
    if battery.external_connected and not battery.is_charging:
        state = "AC hold" if "not charging" in state.lower() else state
    charger_label = battery.charger_name or format_optional_int(battery.charger_watts, "W")
    if battery.charger_watts is not None and battery.charger_name:
        watts_prefix = f"{battery.charger_watts}W"
        if battery.charger_name.lower().startswith(watts_prefix.lower()):
            charger_label = battery.charger_name
        else:
            charger_label = f"{watts_prefix} {battery.charger_name}"

    lines: list[tuple[str, int]] = [
        (pulse_bar(pct, max(8, w - 18), phase, "cell "), attr),
        (f"state {flow_text(phase, battery.is_charging, battery.external_connected)} {state}", color_pair(6) | curses.A_BOLD),
        (f"source: {battery.source}", color_pair(1)),
        (f"charger: {charger_label}".strip(), color_pair(1)),
        (
            f"cycles: {format_optional_int(battery.cycle_count)} / {format_optional_int(battery.design_cycles)}",
            color_pair(1),
        ),
        (
            f"health: {format_optional_int(battery.health_percent, '%')}  temp: {format_temp(battery.temp_c)}",
            temp_attr(battery.temp_c),
        ),
    ]
    if battery.voltage_v is not None and battery.amperage_a is not None:
        lines.append((f"pack: {battery.voltage_v:4.2f}V  {battery.amperage_a:5.2f}A", color_pair(1)))
    for index, (line, line_attr) in enumerate(lines[: h - 2], start=1):
        safe_add(screen, y + index, x + 2, line[: w - 4], line_attr)


def draw_thermal_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryInfo,
    sensor: SensorInfo,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "THERMAL", color_pair(5))
    def status_line(label: str, value: str) -> tuple[str, int]:
        if value in ("checking", "pending"):
            return (f"{label}: checking...", color_pair(1))
        if value == "nominal":
            return (f"{label}: {value}", color_pair(2) | curses.A_BOLD)
        return (f"{label}: {value}", color_pair(3) | curses.A_BOLD)

    lines: list[tuple[str, int]] = [
        status_line("pressure", sensor.thermal_warning),
        status_line("performance", sensor.performance_warning),
    ]
    if battery.present:
        lines.extend(
            [
                (f"battery skin: {format_temp(battery.temp_c)}", temp_attr(battery.temp_c)),
                (f"virtual pack: {format_temp(battery.virtual_temp_c)}", temp_attr(battery.virtual_temp_c)),
            ]
        )
    else:
        lines.append(("battery: not installed", color_pair(1)))
    exposed_count = 0
    if sensor.cpu_temp_c is not None:
        lines.append((f"processor temp: {format_temp(sensor.cpu_temp_c)}", temp_attr(sensor.cpu_temp_c)))
        exposed_count += 1
    if sensor.gpu_temp_c is not None:
        lines.append((f"graphics temp:  {format_temp(sensor.gpu_temp_c)}", temp_attr(sensor.gpu_temp_c)))
        exposed_count += 1
    if sensor.fan_rpm is not None:
        fan_prefix = f"{sensor.fan_count} fans" if sensor.fan_count > 1 else "fan"
        lines.append((f"{fan_prefix}: {fan_text(sensor)}", color_pair(2)))
        exposed_count += 1
    if sensor.privileged_locked:
        if sensor.thermal_warning in ("checking", "pending"):
            lines.append(("advanced: checking sensor access", color_pair(1)))
        else:
            locked_text = "power sampler: press u" if exposed_count else "advanced: press u to unlock"
            lines.append((locked_text, color_pair(3) | curses.A_BOLD))
    else:
        if sensor.cpu_power_w is not None:
            lines.append((f"cpu power: {format_watts(sensor.cpu_power_w)}", color_pair(1)))
            exposed_count += 1
        if sensor.gpu_power_w is not None:
            lines.append((f"gpu power: {format_watts(sensor.gpu_power_w)}", color_pair(1)))
            exposed_count += 1
        if sensor.ane_power_w is not None:
            lines.append((f"ane power: {format_watts(sensor.ane_power_w)}", color_pair(9)))
            exposed_count += 1
        if sensor.dram_power_w is not None:
            bw = ""
            if sensor.dram_read_gbs is not None or sensor.dram_write_gbs is not None:
                bw = f"  bw {(sensor.dram_read_gbs or 0) + (sensor.dram_write_gbs or 0):.1f}GB/s"
            lines.append((f"dram power: {format_watts(sensor.dram_power_w)}{bw}", color_pair(10)))
            exposed_count += 1
        if sensor.package_power_w is not None:
            lines.append((f"package: {format_watts(sensor.package_power_w)}", color_pair(6) | curses.A_BOLD))
            exposed_count += 1
        if exposed_count == 0:
            lines.append(("advanced: no extra sensors exposed", color_pair(3)))
    cluster_bits = []
    for code, active, freq in (
        ("E", sensor.e_cluster_active, sensor.e_cluster_freq_mhz),
        ("P", sensor.p_cluster_active, sensor.p_cluster_freq_mhz),
        ("S", sensor.s_cluster_active, sensor.s_cluster_freq_mhz),
    ):
        if active is not None or freq is not None:
            active_text = "--" if active is None else f"{active:.0f}%"
            freq_text = "--" if freq is None else f"{freq}MHz"
            cluster_bits.append(f"{code} {active_text}/{freq_text}")
    if cluster_bits:
        lines.append(("clusters: " + "  ".join(cluster_bits), color_pair(2)))
    if h > 10:
        lines.append((f"sensor bus: {comet(max(8, w - 18), phase, 0.85)}", color_pair(2)))
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def draw_sensors_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryInfo,
    sensor: SensorInfo,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "FANS + TEMP SENSORS", color_pair(5))
    if h < 5:
        return

    line_y = y + 1
    source = sensor.raw_hint.replace("smc", "SMC")
    if sensor.privileged_locked and sensor.raw_hint.startswith("smc"):
        source = "SMC online / power locked"
    source_attr = color_pair(6) | curses.A_BOLD
    if sensor.sensor_sample_age_s > SENSOR_REFRESH_SECONDS * 2:
        source = f"{source} · stale {sensor.sensor_sample_age_s:.0f}s"
        source_attr = color_pair(3) | curses.A_BOLD
    safe_add(screen, line_y, x + 2, source[: w - 4], source_attr)
    line_y += 1

    if sensor.fans:
        safe_add(screen, line_y, x + 2, f"fan array: {sensor.fan_count} active", color_pair(2) | curses.A_BOLD)
        line_y += 1
        fan_rows = min(len(sensor.fans), max(1, (h - 6) // 3 + 1))
        for index, fan in enumerate(sensor.fans[:fan_rows]):
            if line_y >= y + h - 1:
                break
            pct = fan_percent(fan)
            bar_width = max(6, min(18, w - 28))
            mode = f" {fan.mode}" if fan.mode else ""
            max_text = f"/{fan.maximum_rpm}" if fan.maximum_rpm else ""
            line = f"{fan.name:<7.7} {pulse_bar(pct, bar_width, phase + index, '')} {fan.rpm}{max_text}rpm{mode}"
            safe_add(screen, line_y, x + 2, line[: w - 4], fan_attr(fan))
            line_y += 1
            if line_y < y + h - 1 and (fan.minimum_rpm or fan.target_rpm or fan.maximum_rpm):
                bounds = f"min {format_optional_int(fan.minimum_rpm)}  target {format_optional_int(fan.target_rpm)}  max {format_optional_int(fan.maximum_rpm)}"
                safe_add(screen, line_y, x + 4, bounds[: w - 6], color_pair(7))
                line_y += 1
    else:
        if sensor.thermal_warning in ("checking", "pending"):
            text = "fans: checking sensor bus..."
            attr = color_pair(1)
        else:
            text = "fans: press u or SMC unavailable" if sensor.privileged_locked else "fans: none exposed"
            attr = color_pair(3)
        safe_add(screen, line_y, x + 2, text[: w - 4], attr)
        line_y += 1

    quick_temps = [
        ("CPU", sensor.cpu_temp_c),
        ("GPU", sensor.gpu_temp_c),
        ("BAT", battery.temp_c),
    ]
    for label, value in quick_temps:
        if line_y >= y + h - 1 or value is None:
            continue
        width_for_bar = max(6, w - 20)
        safe_add(screen, line_y, x + 2, pulse_bar(clamp(value), width_for_bar, phase, f"{label:<3} ")[: w - 4], temp_attr(value))
        line_y += 1

    rows = temperature_group_rows(sensor.temp_sensors)
    if rows and line_y < y + h - 1:
        safe_add(screen, line_y, x + 2, "hottest groups", color_pair(6) | curses.A_BOLD)
        line_y += 1
    for label, average, hottest, count in rows:
        if line_y >= y + h - 1:
            break
        line = f"{label:<13.13} avg {average:4.1f}C  max {hottest:4.1f}C  n={count}"
        safe_add(screen, line_y, x + 2, line[: w - 4], temp_attr(hottest))
        line_y += 1

    if line_y < y + h - 1:
        if sensor.temp_sensors:
            line = f"sensor sweep {comet(max(8, w - 16), phase + 5, 0.75)}"
        else:
            line = "temperature sensors: no detail exposed"
        safe_add(screen, line_y, x + 2, line[: w - 4], color_pair(2))


def draw_gpu_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    static: dict[str, Any],
    sensor: SensorInfo,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "GPU", color_pair(5))
    gpus = static.get("gpus") or []
    if not gpus:
        safe_add(screen, y + 1, x + 2, "GPU scan unavailable", color_pair(3))
        safe_add(screen, y + 2, x + 2, "try: system_profiler SPDisplaysDataType", color_pair(1))
        return

    line_y = y + 1
    for gpu_index, gpu in enumerate(gpus):
        if line_y >= y + h - 1:
            break
        name = gpu.get("name", "GPU")
        cores = gpu.get("cores", "") or str(static.get("gpu_core_count") or "")
        core_text = f" [{cores} GPU cores]" if cores else ""
        safe_add(screen, line_y, x + 2, f"{name}{core_text}"[: w - 4], color_pair(2) | curses.A_BOLD)
        line_y += 1
        if line_y < y + h - 1:
            if sensor.gpu_active_percent is not None:
                activity_text = pulse_bar(
                    sensor.gpu_active_percent,
                    max(8, w - 18),
                    phase,
                    "active ",
                )
                activity_attr = color_for_percent(sensor.gpu_active_percent)
            elif sensor.privileged_locked:
                activity_text = (
                    "press u for GPU activity"
                    if w < 40
                    else "activity: press u for live telemetry"
                )
                activity_attr = color_pair(3)
            else:
                activity_text = (
                    "GPU activity unavailable"
                    if w < 40
                    else "activity: not exposed by this Mac"
                )
                activity_attr = color_pair(7)
            safe_add(screen, line_y, x + 2, activity_text[: w - 4], activity_attr)
            line_y += 1
        if line_y < y + h - 1 and (sensor.gpu_temp_c is not None or sensor.gpu_power_w is not None or sensor.gpu_freq_mhz is not None):
            stats = []
            if sensor.gpu_temp_c is not None:
                stats.append(f"temp: {format_temp(sensor.gpu_temp_c)}")
            if sensor.gpu_power_w is not None:
                stats.append(f"power: {format_watts(sensor.gpu_power_w)}")
            if sensor.gpu_sram_power_w is not None:
                stats.append(f"sram: {format_watts(sensor.gpu_sram_power_w)}")
            if sensor.gpu_freq_mhz is not None:
                stats.append(f"freq: {sensor.gpu_freq_mhz}MHz")
            safe_add(screen, line_y, x + 2, "  ".join(stats)[: w - 4], temp_attr(sensor.gpu_temp_c))
            line_y += 1
        peak = gpu.get("max_freq_mhz") or str(static.get("max_gpu_freq_mhz") or "")
        fp32 = static.get("gpu_fp32_tflops")
        if line_y < y + h - 1 and (peak or fp32):
            peak_text = f"peak {peak}MHz" if peak else "peak freq unknown"
            fp32_text = f"  {fp32:.1f} TFLOPS est" if isinstance(fp32, float) else ""
            safe_add(screen, line_y, x + 2, f"{peak_text}{fp32_text}"[: w - 4], color_pair(8))
            line_y += 1
        metal = gpu.get("metal", "")
        if metal and line_y < y + h - 1:
            safe_add(screen, line_y, x + 2, f"metal: {metal}"[: w - 4], color_pair(1))
            line_y += 1
        displays = gpu.get("displays", "")
        if displays and line_y < y + h - 1:
            safe_add(screen, line_y, x + 2, f"driving: {displays}"[: w - 4], color_pair(1))
            line_y += 1
        if gpu_index < len(gpus) - 1 and line_y < y + h - 1:
            safe_add(screen, line_y, x + 2, "-" * min(w - 4, 28), color_pair(5))
            line_y += 1


def draw_io_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    disk: DiskInfo,
    activity: DiskActivity,
    net_down: float,
    net_up: float,
    history: MetricHistory,
    phase: int = 0,
) -> None:
    draw_box(screen, y, x, h, w, "I/O", color_pair(5))
    disk_pct = disk_percent(disk)
    net_peak = max(history.net) if history.net else net_down + net_up
    net_peak_text = f"pk {human_bytes(net_peak)}/s"
    net_spark_width = max(6, w - len("net 60s  ") - len(net_peak_text) - 6)
    activity_age = ""
    if activity.sample_age > DISK_ACTIVITY_REFRESH_SECONDS * 2:
        activity_age = f" · {activity.sample_age:.0f}s old"
    lines = [
        (pulse_bar(disk_pct, max(8, w - 18), phase, "disk "), color_for_percent(disk_pct)),
        (f"{disk.label} {human_bytes(disk.used)} / {human_bytes(disk.total)}  free {human_bytes(disk.free)}", color_pair(1)),
        (f"disk live {human_bytes(activity.bytes_per_sec)}/s  {activity.iops:.0f} iops{activity_age}", color_pair(10) | curses.A_BOLD),
        (f"net d {human_bytes(net_down)}/s  u {human_bytes(net_up)}/s", color_pair(2) | curses.A_BOLD),
        (f"net 60s  {sparkline(history.net, net_spark_width, None)} {net_peak_text}", color_pair(6)),
        (f"disk 60s {sparkline(history.disk, max(8, w - 18), 100.0)}", color_pair(2)),
    ]
    if h > 8:
        lines.append((f"rx {comet(max(8, w - 9), phase, min(1.0, net_down / 1_000_000 + 0.35))}", color_pair(6)))
        lines.append((f"tx {comet(max(8, w - 9), phase + 7, min(1.0, net_up / 1_000_000 + 0.35))}", color_pair(2)))
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def draw_process_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    rows: list[ProcessRow],
    scroll: int,
    sort_mode: str,
) -> None:
    draw_box(screen, y, x, h, w, f"PROCESS GRID sort={sort_mode}", color_pair(5))
    if h < 5:
        return
    compact = w < 58
    header = " PID     CPU%  MEM%  GPU ST   COMMAND" if compact else " PID      CPU%  MEM%  GPU% ST     ELAPSED    COMMAND"
    safe_add(screen, y + 1, x + 1, header[: w - 2], color_pair(6) | curses.A_BOLD)
    safe_add(screen, y + 2, x + 1, "-" * (w - 2), color_pair(5))

    body_height = process_body_height(h)
    scroll = max(0, min(scroll, max(0, len(rows) - body_height)))
    if not rows and body_height > 0:
        safe_add(screen, y + 3, x + 2, "collecting processes..."[: w - 4], color_pair(1))
    for index, row in enumerate(rows[scroll : scroll + body_height]):
        line_y = y + 3 + index
        if compact:
            command_width = max(10, w - 34)
            line = (
                f"{row.pid:>6} {row.cpu:6.1f} {row.mem:5.1f} {(f'{row.gpu:4.0f}' if row.gpu is not None else '--'):>4} "
                f"{row.stat:<4.4} {visible_command(row.command, command_width)}"
            )
        else:
            command_width = max(8, w - 49)
            line = (
                f"{row.pid:>6}  {row.cpu:6.1f} {row.mem:5.1f} {(f'{row.gpu:5.1f}' if row.gpu is not None else '--'):>5} "
                f"{row.stat:<5.5} {row.etime:>9.9}  {visible_command(row.command, command_width)}"
            )
        if sort_mode == "MEM":
            metric, critical, warning = row.mem, 10.0, 3.0
        elif sort_mode == "GPU":
            metric, critical, warning = row.gpu or 0.0, 80.0, 35.0
        else:
            metric, critical, warning = row.cpu, 80.0, 35.0
        attr = color_pair(1)
        if metric >= critical:
            attr = color_pair(4) | curses.A_BOLD
        elif metric >= warning:
            attr = color_pair(3) | curses.A_BOLD
        safe_add(screen, line_y, x + 1, line[: w - 2], attr)

    first = min(len(rows), scroll + 1) if rows else 0
    last = min(len(rows), scroll + body_height)
    keys = "↑↓/Pg" if unicode_ok() else "j/k/Pg"
    if not rows:
        footer = f"collecting · m {sort_mode}" if unicode_ok() else f"collecting | m {sort_mode}"
    elif w < 58:
        footer = f"{len(rows)} · {first}-{last} · {keys} · m {sort_mode}"
    else:
        footer = f"{len(rows)} processes · {first}-{last} · {keys} scroll · m sort"
    safe_add(screen, y + h - 2, x + 2, footer[: w - 4], color_pair(6) | curses.A_BOLD)


def set_message(config: HudConfig, text: str, ttl: float = 3.0) -> None:
    config.message = text
    config.message_until = time.monotonic() + ttl


def shortcut_status(width: int) -> str:
    if width < 100:
        return "h help  t theme  l view  a motion  u sensor  m sort  r refresh  q quit"
    return "h help  t theme  l layout  a motion  u sensors  m sort CPU/MEM/GPU  r refresh  q quit"


def draw_help_overlay(screen: curses.window, config: HudConfig, sensor: SensorInfo) -> None:
    height, width = screen.getmaxyx()
    w = min(72, max(48, width - 8))
    h = 16
    y = max(2, (height - h) // 2)
    x = max(2, (width - w) // 2)
    fill_rect(screen, y, x, h, w, color_pair(1))
    draw_box(screen, y, x, h, w, "SHORTCUTS", color_pair(6) | curses.A_BOLD)
    if sensor.thermal_warning in ("checking", "pending"):
        unlock_state = "checking"
    elif not sensor.privileged_locked:
        unlock_state = "online"
    elif sensor.raw_hint.startswith("smc"):
        unlock_state = "SMC online / power locked"
    else:
        unlock_state = "locked"
    lines = [
        "q             quit",
        "Esc / h / ?   close this panel",
        "t             cycle theme",
        "l             cycle terminal layout",
        "a             animation: off / calm / vivid",
        "m             sort process grid by CPU, memory, or GPU",
        "r             refresh live telemetry",
        "u             unlock advanced macOS sensors with sudo",
        "arrows/j/k    scroll processes",
        "PgUp/PgDn     faster process scrolling",
        "",
        f"theme: {config.theme_name}    layout: {config.layout_name}",
        f"sensors: {unlock_state}",
        COPYRIGHT_TEXT,
    ]
    for index, line in enumerate(lines[: h - 2], start=1):
        attr = color_pair(1)
        if line.startswith(("theme:", "sensors:")) or line == COPYRIGHT_TEXT:
            attr = color_pair(7) | curses.A_BOLD
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def init_colors(theme_index: int = 0) -> None:
    try:
        if not curses.has_colors():
            return
        curses.start_color()
    except (curses.error, ValueError):
        return

    background = curses.COLOR_BLACK
    try:
        curses.use_default_colors()
    except (curses.error, ValueError):
        pass

    if os.environ.get("NO_COLOR") is not None:
        return

    theme = THEMES[theme_index % len(THEMES)]["colors"]
    for pair_id, foreground in theme.items():
        try:
            curses.init_pair(pair_id, color_id(foreground), background)
        except (curses.error, ValueError):
            pass


def draw_small_screen(screen: curses.window, height: int, width: int) -> None:
    screen.erase()
    safe_add(screen, 1, 2, "PorkyHUD needs a larger terminal.", color_pair(3) | curses.A_BOLD)
    safe_add(screen, 3, 2, f"Current: {width}x{height}. Resize to at least 76x22.", color_pair(1))
    safe_add(screen, 5, 2, "Press q to quit.", color_pair(1))
    screen.refresh()


def draw_initial_shell(screen: curses.window, config: HudConfig) -> int:
    """Draw the effect-free progressive startup shell before any collection begins."""

    del config
    height, width = screen.getmaxyx()
    screen.erase()
    safe_add(screen, 0, 1, "PorkyHUD · CHECKING", color_pair(1) | curses.A_BOLD)
    if height > 2:
        safe_add(screen, 2, 1, "Establishing independent system evidence…"[: max(0, width - 2)], color_pair(6))
    return 3


def hud(screen: curses.window) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error, ValueError):
        pass
    screen.nodelay(True)
    screen.keypad(True)
    config = HudConfig()
    init_colors(config.theme_index)
    apply_dark_screen(screen)
    draw_initial_shell(screen, config)
    screen.refresh()

    coordinator = LiveHealthCoordinator()
    static: dict[str, Any] = {}

    def collect_live_static() -> dict[str, Any]:
        result = collect_static_info()
        result["boot_seconds"] = boot_seconds()
        return result

    static_poller = AsyncPoller(3600.0, collect_live_static, static)
    uptime_at_start = 0
    uptime_clock_start = time.monotonic()
    per_core: list[float] = []
    mem: dict[str, float] = {}
    battery = BatteryInfo(None, False, "Power", "checking", "checking", False,
                          False, False, None, None, None, None, None, None, None,
                          None, None)
    disk = DiskInfo("checking", "/", 1, 0, 1)
    net_down = 0.0
    net_up = 0.0
    rows: list[ProcessRow] = []
    sort_mode = "CPU"
    scroll = 0
    sensor = default_sensor_info()
    latest_thermal: list[Optional[OsThermalState]] = [None]
    activity = DiskActivity()
    memory_poller = AsyncPoller(MEMORY_DETAIL_REFRESH_SECONDS, memory_detail, mem)
    swap_poller = AsyncPoller(SWAP_REFRESH_SECONDS, swap_stats, (0.0, 0.0))
    network_poller = AsyncPoller(NETWORK_REFRESH_SECONDS, network_bytes, (0, 0))
    battery_poller = AsyncPoller(BATTERY_REFRESH_SECONDS, battery_info, battery)
    sensor_poller = AsyncPoller(
        OPTIONAL_SENSOR_REFRESH_SECONDS,
        lambda: sensor_info(latest_thermal[0]),
        sensor,
    )
    disk_activity_poller = AsyncPoller(DISK_ACTIVITY_REFRESH_SECONDS, disk_activity, activity)
    history = MetricHistory()
    history.add(cpu_total_percent(per_core), ram_percent(mem), 0.0, disk_percent(disk))
    force_live = True
    force_static = True
    force_memory = True
    force_swap = True
    force_network = True
    force_battery = True
    force_sensor = True
    force_disk_activity = True
    prior_network = (0, 0)
    prior_network_at = time.monotonic()
    last_render = 0.0

    def quit_hud() -> None:
        coordinator.close()

    while True:
        now = time.monotonic()
        key = screen.getch()
        if key in (ord("q"), ord("Q")):
            quit_hud()
            return
        if config.show_help:
            if key in (27, ord("h"), ord("H"), ord("?")):
                config.show_help = False
                set_message(config, "shortcut panel closed")
        elif key == 27:
            quit_hud()
            return
        elif key in (ord("h"), ord("H"), ord("?")):
            config.show_help = True
            set_message(config, "shortcut panel opened")
        elif key in (ord("t"), ord("T")):
            config.theme_index = (config.theme_index + 1) % len(THEMES)
            init_colors(config.theme_index)
            apply_dark_screen(screen)
            set_message(config, f"theme switched to {config.theme_name}")
        elif key in (ord("l"), ord("L")):
            config.layout_index = (config.layout_index + 1) % len(LAYOUTS)
            scroll = 0
            current_height, current_width = screen.getmaxyx()
            if current_width < 118 or current_height < 34:
                set_message(config, f"{config.layout_name} saved · wide layouts need 118x34")
            else:
                set_message(config, f"layout switched to {config.layout_name}")
        elif key in (ord("a"), ord("A")):
            config.animation_mode = (config.animation_mode + 1) % 3
            labels = ["off", "calm", "vivid"]
            set_message(config, f"animation {labels[config.animation_mode]}")
        elif key in (ord("u"), ord("U")):
            if unlock_privileged_sensors(screen):
                force_sensor = True
                set_message(config, "sensor access unlocked · refreshing")
            else:
                set_message(config, "advanced sensor unlock skipped")
        elif key in (ord("m"), ord("M")):
            sort_modes = ["CPU", "MEM", "GPU"]
            sort_mode = sort_modes[(sort_modes.index(sort_mode) + 1) % len(sort_modes)]
            rows = sort_process_rows(rows, sort_mode)
            scroll = 0
            set_message(config, f"process sort: {sort_mode}")
        elif key in (curses.KEY_DOWN, ord("j")):
            scroll += 1
        elif key in (curses.KEY_UP, ord("k")):
            scroll -= 1
        elif key == curses.KEY_NPAGE:
            scroll += 10
        elif key == curses.KEY_PPAGE:
            scroll -= 10
        elif key == curses.KEY_HOME:
            scroll = 0
        elif key == curses.KEY_END:
            scroll = 10**9
        elif key in (ord("r"), ord("R")):
            force_live = True
            force_memory = True
            force_swap = True
            force_network = True
            force_battery = True
            force_sensor = True
            force_disk_activity = True
            set_message(config, "refreshing live telemetry")

        coordinator.observe_time(now, utc_now())
        frame = coordinator.tick(now, force_live)
        latest_thermal[0] = frame.thermal
        per_core = list(coordinator.per_core())
        if frame.disk_capacity is not None:
            capacity = frame.disk_capacity
            disk = DiskInfo(capacity.volume_name.lower(), capacity.mount_point,
                            capacity.total_bytes, capacity.total_bytes - capacity.free_bytes,
                            capacity.free_bytes)
        rows = sort_process_rows(coordinator.process_rows(now), sort_mode)

        static_poller.tick(now, force_static)
        sampled_static, _static_age, _static_error = static_poller.snapshot(now)
        if sampled_static:
            static = sampled_static
            uptime_at_start = int(static.get("boot_seconds", uptime_at_start) or 0)
        memory_poller.tick(now, force_memory)
        sampled_memory, _memory_age, _memory_error = memory_poller.snapshot(now)
        if sampled_memory:
            mem = sampled_memory
        swap_poller.tick(now, force_swap)
        sampled_swap, _swap_age, _swap_error = swap_poller.snapshot(now)
        if sampled_swap is not None:
            mem = merge_memory_stats(mem, sampled_swap)
        network_poller.tick(now, force_network)
        sampled_network, _network_age, _network_error = network_poller.snapshot(now)
        if sampled_network is not None and sampled_network != prior_network:
            elapsed = max(0.1, now - prior_network_at)
            net_down = max(0.0, (sampled_network[0] - prior_network[0]) / elapsed)
            net_up = max(0.0, (sampled_network[1] - prior_network[1]) / elapsed)
            prior_network = sampled_network
            prior_network_at = now
        battery_poller.tick(now, force_battery)
        sampled_battery, _battery_age, _battery_error = battery_poller.snapshot(now)
        if sampled_battery is not None:
            battery = sampled_battery

        sensor_poller.tick(now, force_sensor)
        sampled_sensor, sensor_age, _sensor_error = sensor_poller.snapshot(now)
        if sampled_sensor is not None:
            sensor = replace(sampled_sensor, sensor_sample_age_s=sensor_age)

        disk_activity_poller.tick(now, force_disk_activity)
        sampled_activity, activity_age, _activity_error = disk_activity_poller.snapshot(now)
        if sampled_activity is not None:
            activity = replace(sampled_activity, sample_age=activity_age)

        history.add(cpu_total_percent(per_core), ram_percent(mem), net_down + net_up, disk_percent(disk))
        force_live = False
        force_static = False
        force_memory = False
        force_swap = False
        force_network = False
        force_battery = False
        force_sensor = False
        force_disk_activity = False

        height, width = screen.getmaxyx()
        if key == -1 and now - last_render < render_interval(config.animation_mode):
            time.sleep(0.03)
            continue
        last_render = now
        if height < 22 or width < 76:
            draw_small_screen(screen, height, width)
            time.sleep(0.03)
            continue

        if config.animation_mode == 0:
            phase = 0
        elif config.animation_mode == 2:
            phase = int(now * 16)
        else:
            phase = int(now * 3)
        header_h = draw_header(screen, width, phase, static, config)
        footer_lines = 2 if height >= 24 else 1
        body_height = height - header_h - footer_lines
        uptime_seconds = uptime_at_start + int(max(0.0, now - uptime_clock_start))
        try:
            load_average = os.getloadavg()
        except OSError:
            load_average = (0.0, 0.0, 0.0)

        if width >= 118 and height >= 34:
            layout = config.layout_name
            left_x = 1
            left_width, center_width, right_width = wide_column_widths(width, layout)
            center_x = left_x + left_width + 1
            right_x = center_x + center_width + 1
            body_y = header_h

            if layout == "thermals":
                system_h, power_h, thermal_h, sensors_h = proportional_heights(body_height, [(6, 0), (6, 0), (7, 1), (9, 4)])
                draw_system_panel(screen, body_y, left_x, system_h, left_width, static, battery, uptime_seconds, load_average)
                draw_battery_panel(screen, body_y + system_h, left_x, power_h, left_width, battery, phase)
                draw_thermal_panel(screen, body_y + system_h + power_h, left_x, thermal_h, left_width, battery, sensor, phase)
                draw_sensors_panel(screen, body_y + system_h + power_h + thermal_h, left_x, sensors_h, left_width, battery, sensor, phase)
            elif layout == "io":
                system_h, power_h, sensors_h, io_h = proportional_heights(body_height, [(6, 0), (6, 0), (7, 1), (8, 4)])
                draw_system_panel(screen, body_y, left_x, system_h, left_width, static, battery, uptime_seconds, load_average)
                draw_battery_panel(screen, body_y + system_h, left_x, power_h, left_width, battery, phase)
                draw_sensors_panel(screen, body_y + system_h + power_h, left_x, sensors_h, left_width, battery, sensor, phase)
                draw_io_panel(screen, body_y + system_h + power_h + sensors_h, left_x, io_h, left_width, disk, activity, net_down, net_up, history, phase)
            else:
                compact_left = layout == "compact"
                system_h, power_h, thermal_h, io_h = proportional_heights(
                    body_height,
                    [(5 if compact_left else 7, 0), (5 if compact_left else 7, 0), (6 if compact_left else 8, 1), (7, 3)],
                )
                draw_system_panel(screen, body_y, left_x, system_h, left_width, static, battery, uptime_seconds, load_average)
                draw_battery_panel(screen, body_y + system_h, left_x, power_h, left_width, battery, phase)
                draw_thermal_panel(screen, body_y + system_h + power_h, left_x, thermal_h, left_width, battery, sensor, phase)
                draw_io_panel(screen, body_y + system_h + power_h + thermal_h, left_x, io_h, left_width, disk, activity, net_down, net_up, history, phase)

            if layout == "compute":
                cpu_h, gpu_h, mem_h = proportional_heights(body_height, [(18, 5), (10, 2), (7, 1)])
            elif layout == "cinema":
                cpu_h, gpu_h, mem_h = proportional_heights(body_height, [(16, 4), (10, 3), (7, 1)])
            elif layout == "compact":
                cpu_h, gpu_h, mem_h = proportional_heights(body_height, [(11, 3), (6, 1), (6, 1)])
            else:
                cpu_h, gpu_h, mem_h = proportional_heights(body_height, [(14, 4), (8, 2), (7, 2)])
            draw_cpu_panel(screen, body_y, center_x, cpu_h, center_width, per_core, static, history, phase)
            draw_gpu_panel(screen, body_y + cpu_h, center_x, gpu_h, center_width, static, sensor, phase)
            draw_memory_panel(screen, body_y + cpu_h + gpu_h, center_x, mem_h, center_width, mem, history, phase)

            proc_h = body_height
            process_scroll_max = max(0, len(rows) - process_body_height(proc_h))
            scroll = max(0, min(scroll, process_scroll_max))
            draw_process_panel(screen, body_y, right_x, proc_h, right_width, rows, scroll, sort_mode)
        else:
            process_region_height = max(8, body_height // 2)
            top_height = body_height - process_region_height
            left_width = max(34, min(56, width * 42 // 100))
            if width - left_width - 3 < 38:
                left_width = max(32, width - 41)
            right_width = width - left_width - 3
            left_x = 1
            right_x = left_x + left_width + 1

            sys_h, power_h = compact_top_heights(top_height)
            cpu_h = top_height
            lower_h = height - (header_h + top_height) - footer_lines

            draw_system_panel(screen, header_h, left_x, sys_h, left_width, static, battery, uptime_seconds, load_average)
            draw_battery_panel(screen, header_h + sys_h, left_x, power_h, left_width, battery, phase)
            draw_cpu_panel(screen, header_h, right_x, cpu_h, right_width, per_core, static, history, phase)
            if lower_h >= 16:
                thermal_h, sensors_h, gpu_h = proportional_heights(lower_h, [(5, 1), (6, 2), (5, 1)])
                draw_thermal_panel(screen, header_h + top_height, left_x, thermal_h, left_width, battery, sensor, phase)
                draw_sensors_panel(screen, header_h + top_height + thermal_h, left_x, sensors_h, left_width, battery, sensor, phase)
                draw_gpu_panel(screen, header_h + top_height + thermal_h + sensors_h, left_x, gpu_h, left_width, static, sensor, phase)
            else:
                thermal_h, gpu_h = proportional_heights(lower_h, [(5, 1), (4, 1)])
                draw_thermal_panel(screen, header_h + top_height, left_x, thermal_h, left_width, battery, sensor, phase)
                draw_gpu_panel(screen, header_h + top_height + thermal_h, left_x, gpu_h, left_width, static, sensor, phase)

            proc_y = header_h + top_height
            proc_h = height - proc_y - footer_lines
            process_scroll_max = max(0, len(rows) - process_body_height(proc_h))
            scroll = max(0, min(scroll, process_scroll_max))
            draw_process_panel(screen, proc_y, right_x, proc_h, right_width, rows, scroll, sort_mode)

        if config.show_help:
            draw_help_overlay(screen, config, sensor)

        if footer_lines == 2:
            safe_add(screen, height - 2, 1, frame.assessment.reason[: width - 2], color_pair(1))
        status = shortcut_status(width)
        if config.message and now < config.message_until:
            suffix = status if width >= 100 else "h help  q quit"
            status = f"{config.message} | {suffix}"
        safe_add(screen, height - 1, 1, status[: width - 2], color_pair(6))
        screen.refresh()
        time.sleep(0.03)


def collect_snapshot() -> dict[str, Any]:
    cpu_sampler = MacCpuSampler()
    time.sleep(0.25)
    per_core = cpu_sampler.sample()
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="porkyhud-snapshot") as pool:
        static_future = pool.submit(collect_static_info)
        memory_future = pool.submit(memory_stats)
        battery_future = pool.submit(battery_info)
        sensor_future = pool.submit(sensor_info)
        disk_future = pool.submit(disk_info)
        activity_future = pool.submit(disk_activity)
        static = static_future.result()
        mem = memory_future.result()
        battery = battery_future.result()
        sensor = sensor_future.result()
        disk = disk_future.result()
        activity = activity_future.result()
    rows = process_rows("CPU")[:12]
    read_text, read_level = system_read(per_core, mem, disk, activity, battery, sensor, rows)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "read": {
            "message": read_text,
            "level": read_level,
        },
        "system": static,
        "cpu": {
            "average_percent": cpu_total_percent(per_core),
            "cores": [
                {"label": label, "percent": percent}
                for percent, label in zip(*display_core_usage(per_core, static))
            ],
        },
        "memory": {
            "used_percent": ram_percent(mem),
            "used_bytes": mem.get("used", 0.0),
            "total_bytes": mem.get("total", 0.0),
            "swap_used_bytes": mem.get("swap_used", 0.0),
            "swap_total_bytes": mem.get("swap_total", 0.0),
        },
        "battery": asdict(battery),
        "sensors": asdict(sensor),
        "disk": {
            "capacity": asdict(disk),
            "activity": asdict(activity),
        },
        "processes": [
            {**asdict(row), "command": sanitize_command(row.command)}
            for row in rows
        ],
    }


def print_text_snapshot(snapshot: dict[str, Any]) -> None:
    system = snapshot["system"]
    cpu = snapshot["cpu"]
    memory = snapshot["memory"]
    sensors = snapshot["sensors"]
    disk = snapshot["disk"]
    print("PorkyHUD snapshot")
    print(snapshot.get("read", {}).get("message", "read: snapshot collected"))
    print(f"Mac: {system.get('chip', 'Mac')} / {system.get('model', '')}")
    print(f"CPU: {cpu['average_percent']:.1f}% across {len(cpu['cores'])} sampled workers")
    print(f"RAM: {human_bytes(memory['used_bytes'])} / {human_bytes(memory['total_bytes'])} ({memory['used_percent']:.1f}%)")
    print(f"Thermal: {sensors.get('thermal_warning')}  Package: {format_watts(sensors.get('package_power_w'))}")
    fan_rows = sensors.get("fans") or []
    if fan_rows:
        print("Fans: " + ", ".join(f"{fan['name']} {fan['rpm']}rpm" for fan in fan_rows[:4]))
    temp_rows = sensors.get("temp_sensors") or []
    if temp_rows:
        hottest = ", ".join(f"{temp['name']} {temp['value_c']:.1f}C" for temp in temp_rows[:4])
        print(f"Hottest sensors: {hottest}")
    print(f"Disk: {human_bytes(disk['activity']['bytes_per_sec'])}/s, {disk['activity']['iops']:.0f} iops")
    print("Top processes:")
    for row in snapshot["processes"][:8]:
        gpu = row.get("gpu")
        gpu_text = f"{gpu:4.1f}%" if gpu is not None else "  --"
        print(f"  {row['pid']:>6} CPU {row['cpu']:5.1f}% MEM {row['mem']:4.1f}% GPU {gpu_text}  {visible_command(row['command'], 72)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PorkyHUD terminal system monitor for macOS.")
    parser.add_argument("--version", action="version", version=f"PorkyHUD {VERSION}")
    parser.add_argument("--json", action="store_true", help="print one machine-readable snapshot and exit")
    parser.add_argument("--snapshot", action="store_true", help="print one text snapshot and exit")
    parser.add_argument(
        "--setup-sensors",
        "--install-sensor-access",
        dest="setup_sensors",
        action="store_true",
        help="install a narrow sudoers rule for passwordless advanced sensor sampling",
    )
    parser.add_argument(
        "--remove-sensor-access",
        action="store_true",
        help="remove PorkyHUD's advanced sensor sudoers rule",
    )
    parser.add_argument(
        "--sensor-access-status",
        action="store_true",
        help="show whether passwordless advanced sensor sampling is ready",
    )
    args = parser.parse_args(argv)
    if args.setup_sensors:
        return install_sensor_access()
    if args.remove_sensor_access:
        return remove_sensor_access()
    if args.sensor_access_status:
        return print_sensor_access_status()
    if args.json or args.snapshot:
        snapshot = collect_snapshot()
        if args.json:
            print(json.dumps(snapshot, indent=2, sort_keys=True))
        else:
            print_text_snapshot(snapshot)
        return 0

    terminal_problem = terminal_ui_problem()
    if terminal_problem:
        print(f"PorkyHUD cannot start its live dashboard: {terminal_problem}.", file=sys.stderr)
        print("Use `porkyhud --snapshot` or run it from a full terminal.", file=sys.stderr)
        return 2

    class TerminationSignal(BaseException):
        def __init__(self, signum: int) -> None:
            super().__init__(signum)
            self.signum = signum

    def terminate(signum: int, _frame: Any) -> None:
        raise TerminationSignal(signum)

    previous_signal_handlers: dict[int, Any] = {}
    manage_terminal_colors = False
    try:
        for signal_name in ("SIGHUP", "SIGTERM"):
            signum = getattr(signal, signal_name, None)
            if signum is None:
                continue
            try:
                previous_signal_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, terminate)
            except (OSError, RuntimeError, ValueError):
                previous_signal_handlers.pop(signum, None)

        manage_terminal_colors = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        enforce_dark_terminal()
        curses.wrapper(hud)
        return 0
    except KeyboardInterrupt:
        return 0
    except TerminationSignal as exc:
        return 128 + exc.signum
    except Exception as exc:
        print(f"PorkyHUD crashed: {exc}")
        return 1
    finally:
        if manage_terminal_colors:
            restore_terminal_colors()
        for signum, previous_handler in previous_signal_handlers.items():
            try:
                signal.signal(signum, previous_handler)
            except (OSError, RuntimeError, ValueError):
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
