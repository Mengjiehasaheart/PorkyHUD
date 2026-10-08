#!/usr/bin/env python3
"""PorkyHUD: a dependency-free macOS system monitor and tools.

Press h for current shortcuts.
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
from typing import Any, Callable, Generic, Mapping, Optional, Sequence, Tuple, TypeVar

from porkyhud_insights import (
    InsightStatus, VmRateState, VmRateView, summarize_cpu_samples, update_vm_rates,
    vm_counters_from_mapping,
)
from porkyhud_visual import render_mascot, wordmark_lines
from porkyhud_inventory import (
    InventoryResult, NetworkInventory, InventoryStatus, ProbeResult,
    InventoryObservation, observe_inventory,
    collect_devices, collect_network, collect_connections,
    CONNECTION_FILTERS, SEARCH_QUERY_LIMIT, filter_inventory, summarize_inventory,
)

from porkyhud_core import (
    AttemptOutcome,
    ContributorState,
    CpuContributor,
    CpuUsage,
    DiskCapacity,
    EvidenceRole,
    EvidenceState,
    FailureCode,
    HealthAssessment,
    HealthFacts,
    HealthSeverity,
    MemoryPressure,
    MemoryPressureLevel,
    OsThermalState,
    PolicyState,
    ProcessFact,
    RuleId,
    SourceId,
    SourceState,
    SourceView,
    SourceViewStatus,
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
VERSION = "0.2.1"
COPYRIGHT_TEXT = "Copyright (c) DMS"
HISTORY_SECONDS = 60
LAYOUTS = ["balanced", "compute", "thermals", "io", "processes", "insights", "devices", "network", "connections", "system", "cinema"]
OPERATIONAL_MODES = tuple(mode for mode in LAYOUTS if mode != "cinema")
DIRECT_MODE_KEYS = {
    "1": "balanced", "2": "compute", "3": "thermals",
    "4": "io", "5": "processes", "6": "cinema", "7": "insights", "8": "devices", "9": "network", "0": "connections",
    "i": "system",
}
SEARCHABLE_MODES = frozenset(("balanced", "compute", "processes", "cinema",
                             "devices", "network", "connections"))
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
PAIR_PRIMARY = 1
PAIR_INFO = 2
PAIR_NOMINAL = 3
PAIR_WATCH = 4
PAIR_CRITICAL = 5
PAIR_STRUCTURE = 6
PAIR_SECONDARY = 7
PAIR_BRAND = 8
PAIR_PIG_LIT = 9
PAIR_PIG_BODY = 10
PAIR_PIG_SHADOW = 11
PAIR_PIG_DETAIL = 12
# Decorative colors are deliberately independent from health roles.
MASCOT_COLORS = {PAIR_PIG_LIT: 224, PAIR_PIG_BODY: 211,
                  PAIR_PIG_SHADOW: 175, PAIR_PIG_DETAIL: 255}
VISUAL_STYLES = ("sculpted", "outline", "ascii")
# Compatibility aliases for the existing public module surface. New rendering
# code uses the semantic PAIR_* names exclusively.
PRIMARY = PAIR_PRIMARY
INFO = PAIR_INFO
NOMINAL = PAIR_NOMINAL
WATCH = PAIR_WATCH
CRITICAL = PAIR_CRITICAL
STRUCTURE = PAIR_STRUCTURE
SECONDARY = PAIR_SECONDARY
BRAND = PAIR_BRAND
THEMES: list[dict[str, Any]] = [
    {
        "name": "Neon Grid",
        "colors": {
            1: 231,
            2: 51,
            3: 46,
            4: 220,
            5: 196,
            6: 39,
            7: 45,
            8: 201,
        },
    },
    {
        "name": "Catppuccin",
        "colors": {
            1: 189,
            2: 110,
            3: 119,
            4: 229,
            5: 203,
            6: 111,
            7: 153,
            8: 183,
        },
    },
    {
        "name": "Solar Circuit",
        "colors": {
            1: 255,
            2: 87,
            3: 82,
            4: 226,
            5: 202,
            6: 33,
            7: 87,
            8: 165,
        },
    },
    {
        "name": "Graphite",
        "colors": {
            1: 252,
            2: 110,
            3: 152,
            4: 222,
            5: 203,
            6: 245,
            7: 250,
            8: 183,
        },
    },
]


@dataclass
class HudConfig:
    theme_index: int = 3
    animation_mode: int = 0
    layout_index: int = 0
    show_help: bool = False
    visual_style: str = "sculpted"
    intro_enabled: bool = True
    show_menu: bool = False
    menu_index: int = 0
    search_editing: bool = False
    searches: dict[str, str] = field(default_factory=dict)
    connection_filter: str = "all"
    message: str = ""
    message_until: float = 0.0

    @property
    def theme_name(self) -> str:
        return THEMES[self.theme_index % len(THEMES)]["name"]

    @property
    def layout_name(self) -> str:
        return LAYOUTS[self.layout_index % len(LAYOUTS)]

    @property
    def search_query(self) -> str:
        return self.searches.get(self.layout_name, "")


def _validate_viewport(height: int, width: int) -> tuple[int, int]:
    if (type(height) is not int or type(width) is not int
            or height < 0 or width < 0):
        raise ValueError("viewport dimensions must be nonnegative integers")
    return height, width


def supported_modes(height: int, width: int) -> Tuple[str, ...]:
    """Return modes selectable in one viewport, preserving direct-key order."""

    height, width = _validate_viewport(height, width)
    return tuple(LAYOUTS) if height >= 34 and width >= 118 else OPERATIONAL_MODES


def _mode_copy_name(mode: str) -> str:
    try:
        return {
            "balanced": "Overview", "compute": "Compute",
            "thermals": "Thermals", "io": "I/O",
            "processes": "Processes", "insights": "Insights", "devices": "Devices", "network": "Network",
            "connections": "Connections", "cinema": "Cinema",
            "system": "This Mac",
        }[mode]
    except (KeyError, TypeError):
        raise ValueError("unknown display mode")


def select_mode(config: HudConfig, mode: str, height: int, width: int) -> str:
    """Select a supported mode without mutating state on rejection."""

    height, width = _validate_viewport(height, width)
    if not isinstance(config, HudConfig):
        raise ValueError("config must be a HudConfig")
    if mode not in LAYOUTS:
        raise ValueError("unknown display mode")
    if mode == "cinema" and mode not in supported_modes(height, width):
        return "Cinema requires 118×34 · staying in %s" % _mode_copy_name(
            config.layout_name,
        )
    config.layout_index = LAYOUTS.index(mode)
    config.search_editing = False
    return "%s selected" % _mode_copy_name(mode)


def cycle_mode(config: HudConfig, height: int, width: int, direction: int = 1) -> str:
    """Select the next mode supported by the current viewport."""

    modes = supported_modes(height, width)
    if not isinstance(config, HudConfig):
        raise ValueError("config must be a HudConfig")
    current = config.layout_name
    if direction not in (-1, 1):
        raise ValueError("mode direction must be -1 or 1")
    next_index = 0 if current not in modes else (modes.index(current) + direction) % len(modes)
    return select_mode(config, modes[next_index], height, width)


def normalize_search_query(query: str) -> str:
    """Bound a literal query and prevent terminal controls in its display."""

    if not isinstance(query, str) or len(query) > SEARCH_QUERY_LIMIT:
        raise ValueError("search must be text of at most %d characters" % SEARCH_QUERY_LIMIT)
    return "".join(character for character in " ".join(query.split()) if character.isprintable())


def edit_search(config: HudConfig, key: int | str) -> bool:
    """Consume editing keys before dashboard shortcuts, including q and digits."""

    if not config.search_editing or key == -1:
        return False
    character = key if isinstance(key, str) and len(key) == 1 else None
    code = ord(character) if character is not None else key
    query = config.search_query
    if code == 27:
        config.searches.pop(config.layout_name, None)
        config.search_editing = False
    elif code in (10, 13, curses.KEY_ENTER):
        config.search_editing = False
    elif code in (8, 127, curses.KEY_BACKSPACE):
        config.searches[config.layout_name] = query[:-1]
    elif code == 21:
        config.searches.pop(config.layout_name, None)
    elif len(query) < SEARCH_QUERY_LIMIT:
        if character is not None and character.isprintable():
            config.searches[config.layout_name] = query + character
        elif type(code) is int and 32 <= code <= 126:
            config.searches[config.layout_name] = query + chr(code)
    return True


def read_hud_key(screen: curses.window, search_editing: bool) -> int | str:
    """Use wide-character input for query editing, retaining integer shortcuts."""

    if search_editing and hasattr(screen, "get_wch"):
        try:
            return screen.get_wch()
        except curses.error:
            return -1
    return screen.getch()


def _process_search_text(row: ProcessRow) -> str:
    """Build the same privacy-safe fields used by direct and cached searches."""

    return ("%s %s %s %s" % (row.pid, process_identity(row.command), row.stat,
                              visible_command(row.command, 4096))).casefold()


def _matching_process_rows(
    rows: Sequence[ProcessRow], terms: Sequence[str],
    text_for_row: Callable[[ProcessRow], str],
) -> tuple[ProcessRow, ...]:
    """Keep literal AND matching and current source-row selection in one place."""

    if not terms:
        return tuple(rows)
    matched = []
    for row in rows:
        text = text_for_row(row)
        if all(term in text for term in terms):
            matched.append(row)
    return tuple(matched)


def filter_process_rows(rows: Sequence[ProcessRow], query: str) -> tuple[ProcessRow, ...]:
    """Search process identity, sanitized arguments, PID and state literally."""

    terms = normalize_search_query(query).casefold().split()
    return _matching_process_rows(rows, terms, _process_search_text)


class _ProcessSearchCache:
    """Reuse sanitized search fields for only the current table, never its rows."""

    def __init__(self) -> None:
        self._texts: dict[tuple[Any, ...], str] = {}

    @staticmethod
    def _key(row: ProcessRow) -> tuple[Any, ...]:
        # Keys are private source content, not output. Exact values and types
        # prevent mutable fields or bool/int equality from hiding a change.
        return (type(row.pid), row.pid, type(row.stat), row.stat,
                type(row.command), row.command)

    def filter(self, rows: Sequence[ProcessRow], query: str) -> tuple[ProcessRow, ...]:
        terms = normalize_search_query(query).casefold().split()
        previous = self._texts
        current: dict[tuple[Any, ...], str] = {}

        def text_for_row(row: ProcessRow) -> str:
            key = self._key(row)
            text = current.get(key, previous.get(key))
            if text is None:
                text = _process_search_text(row)
            current[key] = text
            return text

        matched = _matching_process_rows(rows, terms, text_for_row)
        if not terms:
            # An empty query needs no parsing, but still releases removed rows.
            for row in rows:
                key = self._key(row)
                if key in previous:
                    current[key] = previous[key]
        self._texts = current
        return matched


def normalize_mode_for_viewport(
    config: HudConfig, height: int, width: int,
) -> str:
    """Return unsupported Cinema to Overview exactly once after a resize."""

    height, width = _validate_viewport(height, width)
    if not isinstance(config, HudConfig):
        raise ValueError("config must be a HudConfig")
    if config.layout_name == "cinema" and "cinema" not in supported_modes(height, width):
        config.layout_index = LAYOUTS.index("balanced")
        return "Cinema requires 118×34 · returned to Overview"
    return ""


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

    def clear(self) -> None:
        self.cpu.clear()
        self.ram.clear()
        self.net.clear()
        self.disk.clear()


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
        if not isinstance(self.contributor, CpuContributor):
            raise ValueError("contributor must be a CpuContributor")
        for value, value_type, name in (
            (self.memory_pressure, MemoryPressure, "memory_pressure"),
            (self.thermal, OsThermalState, "thermal"),
            (self.disk_capacity, DiskCapacity, "disk_capacity"),
        ):
            if value is not None and not isinstance(value, value_type):
                raise ValueError("%s has the wrong type" % name)
        if self.cpu_percent is not None and (
                type(self.cpu_percent) not in (int, float)
                or not math.isfinite(self.cpu_percent) or self.cpu_percent < 0 or self.cpu_percent > 100):
            raise ValueError("cpu_percent must be finite in 0..100")
        if (type(self.captured_mono_s) not in (int, float)
                or not math.isfinite(self.captured_mono_s)):
            raise ValueError("captured_mono_s must be finite")
        if self.decisive_triggered_mono_s is not None and (
                type(self.decisive_triggered_mono_s) not in (int, float)
                or not math.isfinite(self.decisive_triggered_mono_s)):
            raise ValueError("decisive_triggered_mono_s must be finite or None")
        if self.decisive_triggered_mono_s is not None and (
                self.decisive_triggered_mono_s < 0
                or self.decisive_triggered_mono_s > self.captured_mono_s):
            raise ValueError("decisive trigger must be nonnegative and no later than capture")
        ordered = dict(self.source_views)
        if tuple(ordered) != tuple(source_id for source_id, _cadence, _stale in LIVE_SOURCE_SPECS):
            raise ValueError("source_views must contain the live registry in order")
        if any(not isinstance(view, SourceView) for view in ordered.values()):
            raise ValueError("source_views must contain SourceView values")
        if any(view.source_id is not source_id for source_id, view in ordered.items()):
            raise ValueError("source view has the wrong source id")

        def supports_projection(source_id: SourceId) -> bool:
            view = ordered[source_id]
            return (
                view.status is SourceViewStatus.LIVE
                and view.decision_ready
                and type(view.last_valid_revision) is int
                and view.last_valid_revision > 0
            )

        for value, source_id, name in (
            (self.cpu_percent, SourceId.CPU_USAGE, "cpu_percent"),
            (self.memory_pressure, SourceId.MEMORY_PRESSURE, "memory_pressure"),
            (self.thermal, SourceId.THERMAL_OS, "thermal"),
            (self.disk_capacity, SourceId.DISK_CAPACITY, "disk_capacity"),
        ):
            if value is not None and not supports_projection(source_id):
                raise ValueError("%s requires matching live evidence" % name)
        if self.contributor.state is ContributorState.LEADING and (
            not supports_projection(SourceId.PROCESS_TABLE)
            or self.contributor.source_revision
            != ordered[SourceId.PROCESS_TABLE].last_valid_revision
        ):
            raise ValueError("leading contributor requires matching process evidence")

        first_rule_source = None
        if self.assessment.active_rules:
            first_rule = self.assessment.active_rules[0]
            if first_rule in (RuleId.MEMORY_WATCH, RuleId.MEMORY_CRITICAL):
                first_rule_source = SourceId.MEMORY_PRESSURE
            elif first_rule is RuleId.THERMAL_WATCH:
                first_rule_source = SourceId.THERMAL_OS
            elif first_rule is RuleId.STORAGE_WATCH:
                first_rule_source = SourceId.DISK_CAPACITY
            elif first_rule is RuleId.CPU_WATCH:
                first_rule_source = SourceId.CPU_USAGE
        has_matching_trigger = any(
            ref.role is EvidenceRole.TRIGGER
            and ref.source_id is first_rule_source
            for ref in self.assessment.evidence
        )
        if has_matching_trigger != (self.decisive_triggered_mono_s is not None):
            raise ValueError("decisive trigger time must match active trigger evidence")
        object.__setattr__(self, "source_views", MappingProxyType(ordered))


@dataclass(frozen=True)
class DescriptiveSample:
    """One atomic value/timing projection; retained values keep their old age."""

    value: Any
    age_s: float
    error: str
    revision: int
    completed_mono_s: float


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
        self._revision = 0
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def tick(self, now: float, force: bool = False) -> Any:
        thread: threading.Thread | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("descriptive poller is closed")
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
        self._thread = threading.Thread(target=self._run, daemon=True)
        return self._thread

    def sample_snapshot(self, now: float | None = None) -> DescriptiveSample:
        sampled_at = time.monotonic() if now is None else now
        with self._lock:
            reference = self.last_success or self.first_start
            age = max(0.0, sampled_at - reference) if reference else 0.0
            return DescriptiveSample(self.value, age, self.error,
                                     self._revision, self.last_finish)

    def snapshot(self, now: float | None = None) -> tuple[Any, float, str]:
        sample = self.sample_snapshot(now)
        return sample.value, sample.age_s, sample.error

    def close(self, timeout_s: float = 1.0) -> None:
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("timeout_s must be finite and nonnegative")
        with self._lock:
            self._closed = True
            self._force_pending = False
            worker = self._thread
        if (worker is not None and worker is not threading.current_thread()
                and worker.ident is not None):
            worker.join(timeout_s)

    def _run(self) -> None:
        with self._lock:
            if self._closed:
                self._running = False
                return
        try:
            value = self.collector()
            error = ""
        except Exception as exc:
            value = None
            error = str(exc)
        next_thread: threading.Thread | None = None
        with self._lock:
            if value is not None and not self._closed:
                self.value = value
                self.last_success = time.monotonic()
            if not self._closed:
                self.error = error
                self.last_finish = time.monotonic()
                self._revision += 1
            self._running = False
            if self._force_pending and not self._closed:
                self._force_pending = False
                next_thread = self._prepare_thread(self.last_finish)
        if next_thread is not None:
            next_thread.start()


def _start_static_discovery(
    collector: Callable[[], Any], now_mono_s: float,
) -> AsyncPoller:
    """Start one background static-discovery attempt for this HUD session."""

    poller = AsyncPoller(float("inf"), collector)
    poller.tick(now_mono_s, force=True)
    return poller


_NO_ATTEMPT_CONTEXT = object()


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
        collector: Callable[..., AdapterResult[T]],
        attempt_context_factory: Optional[Callable[[int], Any]] = None,
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
        if (
            attempt_context_factory is not None
            and not callable(attempt_context_factory)
        ):
            raise ValueError("attempt_context_factory must be callable or None")

        self._state = initial_state
        self._collector = collector
        self._attempt_context_factory = attempt_context_factory
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._work_pending = False
        self._pending_revision: Optional[int] = None
        self._pending_context: Any = None
        self._pending_has_context = False
        self._accepting_token: Optional[object] = None
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
        reservation: Optional[tuple[object, SourceState[T], int]] = None
        with self._condition:
            if self._closed:
                raise RuntimeError("source poller is closed")
            now_mono_s = self._validate_mono(now_mono_s, "now_mono_s")
            if type(force) is not bool:
                raise ValueError("force must be a bool")
            if (
                self._state.active_revision is not None
                or self._accepting_token is not None
            ):
                if force:
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
                reservation = self._reserve_attempt_locked(now_mono_s)
            else:
                return self._state

        token, state, revision = reservation
        context: Any = None
        has_context = self._attempt_context_factory is not None
        try:
            if has_context:
                context = self._attempt_context_factory(revision)
        except BaseException:
            with self._condition:
                self._rollback_reservation_locked(token)
            raise

        with self._condition:
            if self._closed or self._accepting_token is not token:
                self._rollback_reservation_locked(token)
                raise RuntimeError("source poller closed during attempt acceptance")
            self._commit_reservation_locked(
                token, state, revision, context, has_context,
            )
            if force:
                self._failure_count = 0
                self._automatic_stopped = False
                self._next_due_mono_s = None
            self._condition.notify_all()
            return self._state

    def _reserve_attempt_locked(
        self, started_mono_s: float,
    ) -> tuple[object, SourceState[T], int]:
        """Reserve one revision without exposing it as accepted work yet."""

        if self._accepting_token is not None:
            raise RuntimeError("source poller already has an accepting attempt")
        state, revision = start_attempt(self._state, started_mono_s)
        token = object()
        self._accepting_token = token
        return token, state, revision

    def _commit_reservation_locked(
        self,
        token: object,
        state: SourceState[T],
        revision: int,
        context: Any,
        has_context: bool,
    ) -> None:
        if self._accepting_token is not token:
            raise RuntimeError("attempt reservation is no longer current")
        self._accepting_token = None
        self._state = state
        self._pending_revision = revision
        self._pending_context = context
        self._pending_has_context = has_context
        self._work_pending = True

    def _rollback_reservation_locked(self, token: object) -> None:
        if self._accepting_token is token:
            self._accepting_token = None
            self._force_pending = False
            self._condition.notify_all()

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
            self._accepting_token = None
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
                self._pending_context = None
                self._pending_has_context = False
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
                context = self._pending_context
                has_context = self._pending_has_context
                self._pending_context = None
                self._pending_has_context = False
                self._work_pending = False

            try:
                if has_context:
                    result = self._collector(context)
                else:
                    result = self._collector()
            except BaseException:
                result = None

            followup: Optional[tuple[object, SourceState[T], int]] = None
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
                    followup = self._reserve_attempt_locked(completion_mono_s)
                self._condition.notify_all()
                if self._closed and not self._work_pending:
                    return

            if followup is None:
                continue
            token, state, next_revision = followup
            next_context: Any = None
            next_has_context = self._attempt_context_factory is not None
            try:
                if next_has_context:
                    next_context = self._attempt_context_factory(next_revision)
            except BaseException:
                with self._condition:
                    self._rollback_reservation_locked(token)
                    if self._closed:
                        return
                continue
            with self._condition:
                if self._closed or self._accepting_token is not token:
                    self._rollback_reservation_locked(token)
                    if self._closed:
                        return
                    continue
                self._commit_reservation_locked(
                    token, state, next_revision, next_context,
                    next_has_context,
                )
                self._failure_count = 0
                self._automatic_stopped = False
                self._next_due_mono_s = None
                self._condition.notify_all()

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
            AttemptOutcome.CANCELLED: {FailureCode.USER_CANCELLED},
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
    except PermissionError:
        return _CommandGroupState.UNCONFIRMED
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
                    AttemptOutcome.CANCELLED, FailureCode.USER_CANCELLED,
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
                    AttemptOutcome.CANCELLED, FailureCode.USER_CANCELLED,
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
                    AttemptOutcome.CANCELLED, FailureCode.USER_CANCELLED,
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
                        AttemptOutcome.CANCELLED, FailureCode.USER_CANCELLED,
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
        if self.leader_reaped and not self.group_absent:
            state = self._retry_group_operation(
                _command_worker_probe_group, cleanup_deadline,
            )
            if state is _CommandGroupState.ABSENT:
                self.group_absent = True

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
                self._prepare_abnormal(AttemptOutcome.CANCELLED, FailureCode.USER_CANCELLED)
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
    *, cancel_event: Optional[threading.Event] = None,
) -> CommandResult:
    """Run a no-shell command with bounded output and private failure classification."""

    if cancel_event is not None and not isinstance(cancel_event, threading.Event):
        raise ValueError("cancel_event must be a threading.Event")
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
                if caller_fault or (cancel_event is not None and cancel_event.is_set()):
                    notify_cancellation()
                open_gate()
                if not start_returned:
                    lifecycle_complete = True
                    continue
                publication_ready = False
                while not publication_ready:
                    if caller_fault or (cancel_event is not None and cancel_event.is_set()):
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
        screen.bkgd(" ", color_pair(PAIR_PRIMARY))
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


def _powermetrics_capability_rejection(
    result: CommandResult, attempted_samplers: str,
) -> bool:
    """Recognize only bounded fixture-shaped sampler capability rejections."""

    exact_rejections = {
        "powermetrics: unrecognized sampler: %s" % sampler
        for sampler in attempted_samplers.split(",")
        if sampler
    }
    return (
        result.outcome is AttemptOutcome.COMMAND_ERROR
        and result.failure is FailureCode.NONZERO_EXIT
        and result.stderr.strip() in exact_rejections
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
    if result.outcome is AttemptOutcome.SUCCESS and result.stdout.strip():
        with POWER_METRICS_SAMPLER_LOCK:
            POWER_METRICS_SAMPLER_INDEX = index
        return _PowermetricsAttempt(result.stdout, False)
    if ((result.outcome is AttemptOutcome.SUCCESS and not result.stdout.strip())
            or _powermetrics_capability_rejection(result, samplers)):
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


def parse_vm_counters(raw: str) -> dict[str, int]:
    """Project only complete, recognized cumulative page counters from vm_stat.

    Older responses without all four optional counters expose no activity
    projection.  Present malformed or duplicate counters invalidate the
    attempt so retained activity cannot be mistaken for a fresh zero.
    """

    names = {
        "Compressions": "vm_compressions",
        "Decompressions": "vm_decompressions",
        "Swapins": "vm_swapins",
        "Swapouts": "vm_swapouts",
    }
    counters: dict[str, int] = {}
    page_sizes: list[int] = []
    for line in raw.splitlines():
        stripped = line.strip()
        page_match = re.fullmatch(
            r"Mach Virtual Memory Statistics: \(page size of ([0-9]+) bytes\)",
            stripped,
        )
        if page_match is not None:
            page_sizes.append(int(page_match.group(1)))
        name, separator, _value = stripped.partition(":")
        if name not in names or not separator:
            continue
        key = names[name]
        value_match = re.fullmatch(r"[A-Za-z]+:\s*([0-9]+)\.", stripped)
        if key in counters or value_match is None:
            raise ValueError("malformed or duplicate vm_stat activity counter")
        counters[key] = int(value_match.group(1))
        if counters[key] >= 2**64:
            raise ValueError("vm_stat activity counter exceeds its native range")
    if not counters:
        return {}
    if len(page_sizes) != 1:
        raise ValueError("vm_stat activity requires one recognized page size")
    page_size = page_sizes[0]
    if page_size <= 0 or page_size > 2**32 or page_size & (page_size - 1):
        raise ValueError("vm_stat page size must be a positive power of two")
    if len(counters) != len(names):
        return {}
    return {"vm_page_size": page_size, **counters}


def _memory_detail_attempt() -> tuple[dict[str, float], bool]:
    """Return legacy detail plus whether vm_stat supplied recognized evidence."""

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

    detail = {
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
    detail.update(parse_vm_counters(raw))
    required_used_fields = {
        "anonymous pages", "pages wired down", "pages occupied by compressor",
    }
    proven = (
        total > 0
        and page_match is not None
        and required_used_fields.issubset(pages)
    )
    return detail, proven


def memory_detail() -> dict[str, float]:
    """Return vm_stat-derived detail using the cached physical byte size."""

    detail, proven = _memory_detail_attempt()
    if not proven:
        raise ValueError("memory detail response unrecognized")
    return detail


def _swap_stats_attempt() -> tuple[tuple[float, float], bool]:
    """Return legacy swap values plus whether the response matched its grammar."""

    swap_raw = run_command(["sysctl", "vm.swapusage"])
    swap_total = swap_used = 0.0
    swap_match = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", swap_raw)
    if swap_match:
        swap_total = float(swap_match.group(1)) * 1024 * 1024
        swap_used = float(swap_match.group(2)) * 1024 * 1024

    return (swap_used, swap_total), swap_match is not None


def swap_stats() -> tuple[float, float]:
    """Return swap used/total bytes from one bounded legacy probe."""

    values, proven = _swap_stats_attempt()
    if not proven:
        raise ValueError("swap response unrecognized")
    return values


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


def _snapshot_memory_stats() -> dict[str, float]:
    """Collect only memory fields backed by recognized one-shot probe evidence."""

    detail, detail_proven = _memory_detail_attempt()
    swap, swap_proven = _swap_stats_attempt()
    result = dict(detail) if detail_proven else {}
    if swap_proven:
        result["swap_used"], result["swap_total"] = swap
    return result


def _battery_info_from_raw(pmset_raw: str, ioreg_raw: str) -> BatteryInfo:
    """Parse the legacy battery projection from already collected command text."""

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


def battery_info() -> BatteryInfo:
    pmset_raw = run_command(["pmset", "-g", "batt"])
    ioreg_raw = run_command(
        ["ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"], timeout=1.5,
    )
    return _battery_info_from_raw(pmset_raw, ioreg_raw)


def _snapshot_battery_info() -> Optional[BatteryInfo]:
    """Return battery data only when one legacy response has recognized evidence."""

    pmset_raw = run_command(["pmset", "-g", "batt"])
    ioreg_raw = run_command(
        ["ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"], timeout=1.5,
    )
    battery_row = re.search(r"(\d+)%;\s*([^;]+);\s*([^;]+);?", pmset_raw)
    recognized_ioreg = re.search(
        r'"(?:ExternalConnected|IsCharging|FullyCharged|CurrentCapacity|'
        r'MaxCapacity|Voltage|InstantAmperage|CycleCount)"\s*=',
        ioreg_raw,
    )
    recognized_batteryless = re.fullmatch(
        r"\s*Now drawing from 'AC Power'\s*", pmset_raw,
    )
    if battery_row is None and recognized_ioreg is None and recognized_batteryless is None:
        return None
    return _battery_info_from_raw(pmset_raw, ioreg_raw)


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
        return DiskActivity(source="unavailable")
    numeric_lines = [
        line
        for line in lines
        if re.match(r"^\s*[-+]?\d+(?:\.\d+)?\s+", line)
    ]
    if not numeric_lines:
        return DiskActivity(source="unavailable")
    parts = numeric_lines[-1].split()
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return DiskActivity(source="unavailable")
    if (len(values) < 3 or len(values) % 3 != 0
            or any(not math.isfinite(value) for value in values)):
        return DiskActivity(source="unavailable")
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
    if header is None or "Ibytes" not in header or "Obytes" not in header:
        raise ValueError("network counter response unrecognized")
    return (
        sum(value[0] for value in iface_totals.values()),
        sum(value[1] for value in iface_totals.values()),
    )


def _network_rate_update(
    baseline: Optional[tuple[tuple[int, int], float]],
    totals: tuple[int, int],
    sampled_mono_s: float,
) -> tuple[tuple[tuple[int, int], float], Optional[tuple[float, float]]]:
    """Derive rates from distinct completed samples, including measured idle."""

    if (len(totals) != 2 or any(type(value) is not int or value < 0 for value in totals)
            or isinstance(sampled_mono_s, bool)
            or not math.isfinite(sampled_mono_s) or sampled_mono_s < 0):
        raise ValueError("network counters and sample clock must be valid")
    sampled = (totals[0], totals[1])
    now = float(sampled_mono_s)
    current = (sampled, now)
    if baseline is None:
        return current, None
    previous, previous_mono_s = baseline
    elapsed = now - previous_mono_s
    if elapsed == 0 and sampled == previous:
        return baseline, None
    if (elapsed <= 0.0 or elapsed > 5.0
            or sampled[0] < previous[0] or sampled[1] < previous[1]):
        return current, None
    return current, ((sampled[0] - previous[0]) / elapsed,
                     (sampled[1] - previous[1]) / elapsed)


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


class _ProcessFactCache:
    """Retain one minimized projection, without trusting mutable process rows."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self._sample: Any = None
        self._row_key: tuple[Any, ...] = ()
        self._facts: tuple[ProcessFact, ...] = ()

    def facts(self, sample: Any) -> tuple[ProcessFact, ...]:
        # ProcessRow is mutable. Include field types so bool/int equality cannot
        # hide a corrupted PID or CPU value behind a previously valid sample.
        row_key = tuple(
            (type(row.pid), row.pid, type(row.cpu), row.cpu,
             type(row.etime), row.etime, type(row.command), row.command)
            for row in sample.value
        )
        if sample is self._sample and row_key == self._row_key:
            return self._facts
        facts = process_facts(sample.value)
        self._sample, self._row_key, self._facts = sample, row_key, facts
        return facts


def _sanitized_process_state(
    state: SourceState[Any], now_mono_s: float,
    process_cache: Optional[_ProcessFactCache] = None,
) -> SourceState[tuple[ProcessFact, ...]]:
    """Copy retained process rows across the effect/policy privacy boundary."""

    validate_source_state(state, now_mono_s, SourceId.PROCESS_TABLE)
    if state.last_valid is None:
        return state
    rows = state.last_valid.value
    if type(rows) is not tuple or any(not isinstance(row, ProcessRow) for row in rows):
        raise ValueError("process table sample must contain ProcessRow values")
    facts = (process_facts(rows) if process_cache is None
             else process_cache.facts(state.last_valid))
    sample = replace(state.last_valid, value=facts)
    return replace(state, last_valid=sample)


def _validated_live_states(
    states: Mapping[SourceId, SourceState[Any]], now_mono_s: float,
    process_cache: Optional[_ProcessFactCache] = None,
) -> Mapping[SourceId, SourceState[Any]]:
    """Reject malformed, missing, extra, or misplaced evidence before reduction."""

    if not isinstance(states, Mapping):
        raise ValueError("states must be a mapping")
    expected = tuple(source_id for source_id, _cadence, _stale in LIVE_SOURCE_SPECS)
    if tuple(states) != expected:
        raise ValueError("states must use the exact live registry order")
    copied: dict[SourceId, SourceState[Any]] = {}
    expected_specs = {
        source_id: (cadence_s, stale_after_s)
        for source_id, cadence_s, stale_after_s in LIVE_SOURCE_SPECS
    }
    for source_id in expected:
        state = states[source_id]
        validate_source_state(state, now_mono_s, source_id)
        if (
            state.expected_cadence_s,
            state.stale_after_s,
        ) != expected_specs[source_id]:
            raise ValueError("source cadence must match the live registry")
        copied[source_id] = state
    copied[SourceId.PROCESS_TABLE] = _sanitized_process_state(
        copied[SourceId.PROCESS_TABLE], now_mono_s, process_cache,
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
    *, process_cache: Optional[_ProcessFactCache] = None,
) -> tuple[PolicyState, HealthFrame]:
    """Reduce one validated live registry into a privacy-safe frame."""

    if type(now_mono_s) not in (int, float) or not math.isfinite(now_mono_s):
        raise ValueError("now_mono_s must be finite")
    checked = _validated_live_states(states, float(now_mono_s), process_cache)
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
    cpu_usage = _ready_value(checked[SourceId.CPU_USAGE], CpuUsage, float(now_mono_s))
    frame = HealthFrame(
        assessment=assessment,
        source_views=views,
        cpu_percent=cpu_usage.percent if cpu_usage is not None else None,
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
        self._cpu_per_core_result: Optional[
            tuple[int, int, tuple[float, ...]]
        ] = None
        self._system_gpu_percent: Optional[float] = None
        self._previous_time: Optional[tuple[float, datetime]] = None
        self._process_facts_cache = _ProcessFactCache()
        self._cpu_sampler: MacCpuSampler
        self._gpu_sampler: GPUProcessSampler
        self._pollers: Mapping[SourceId, SourcePoller[Any]]
        self._create_generation()

    def _commit_cpu_per_core(
        self, generation: int, revision: int, per_core: tuple[float, ...],
    ) -> None:
        """Commit per-core evidence only to its accepting live generation."""

        with self._lock:
            if self._closed or generation != self._generation:
                return
            self._cpu_per_core_result = (
                generation, revision, tuple(per_core),
            )

    def _consume_cpu_per_core_revision(self, revision: int) -> None:
        """Advance display evidence to one exact CPU source revision."""

        with self._lock:
            generation = self._generation
            result = self._cpu_per_core_result
            if result is None:
                return
            result_generation, result_revision, per_core = result
            if result_generation != generation or result_revision < revision:
                self._cpu_per_core_result = None
                return
            if result_revision != revision:
                return
            self._per_core = per_core
            self._per_core_revision = revision
            self._cpu_per_core_result = None

    def _create_generation(self) -> None:
        generation = self._generation + 1
        cpu_sampler = MacCpuSampler()
        gpu_sampler = GPUProcessSampler()
        pollers: dict[SourceId, SourcePoller[Any]] = {}

        def cpu_context(revision: int) -> tuple[int, int]:
            with self._lock:
                if self._closed or self._generation != generation:
                    raise RuntimeError("CPU generation is detached")
                return generation, revision

        def collect_cpu(context: tuple[int, int]) -> AdapterResult[CpuUsage]:
            accepted_generation, revision = context
            result = cpu_sampler.sample_usage()
            per_core = tuple(cpu_sampler.last_per_core)
            if result.outcome is AttemptOutcome.SUCCESS:
                self._commit_cpu_per_core(
                    accepted_generation, revision, per_core,
                )
            return result

        def gpu_context(
            revision: int,
        ) -> tuple[int, int, Optional[float]]:
            with self._lock:
                if self._closed or self._generation != generation:
                    raise RuntimeError("GPU generation is detached")
                return generation, revision, self._system_gpu_percent

        def collect_gpu(
            context: tuple[int, int, Optional[float]],
        ) -> AdapterResult[GpuProcessSample]:
            accepted_generation, _revision, calibration = context
            with self._lock:
                current = (
                    self._generation == accepted_generation
                    and not self._closed
                )
            if not current:
                started_mono_s = time.monotonic()
                return AdapterResult(
                    None, started_mono_s, time.monotonic(), utc_now(),
                    AttemptOutcome.CANCELLED, FailureCode.USER_CANCELLED,
                )
            return collect_gpu_process_sample(gpu_sampler, calibration)

        collectors: Mapping[SourceId, Callable[..., AdapterResult[Any]]] = {
            SourceId.CPU_USAGE: collect_cpu,
            SourceId.MEMORY_PRESSURE: collect_memory_pressure,
            SourceId.THERMAL_OS: collect_os_thermal_state,
            SourceId.DISK_CAPACITY: collect_disk_capacity,
            SourceId.PROCESS_TABLE: collect_process_rows,
            SourceId.PROCESS_GPU: collect_gpu,
        }
        context_factories: Mapping[SourceId, Callable[[int], Any]] = {
            SourceId.CPU_USAGE: cpu_context,
            SourceId.PROCESS_GPU: gpu_context,
        }
        deadline = time.monotonic() + 1.0
        try:
            for source_id, cadence_s, stale_after_s in LIVE_SOURCE_SPECS:
                pollers[source_id] = SourcePoller(
                    new_source(
                        source_id, cadence_s, stale_after_s,
                    ),
                    collectors[source_id],
                    context_factories.get(source_id),
                )
        except BaseException:
            self._close_poller_set(pollers, deadline)
            raise
        self._generation = generation
        self._cpu_sampler = cpu_sampler
        self._gpu_sampler = gpu_sampler
        self._pollers = MappingProxyType(pollers)
        self._process_facts_cache.clear()

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
            if self._closed:
                raise RuntimeError("live coordinator is closed")
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
                process_cache=getattr(self, "_process_facts_cache", None),
            )
            self._frame_states = MappingProxyType(frame_states)
            cpu = frame_states[SourceId.CPU_USAGE]
            if (cpu.last_valid is not None
                    and cpu.last_valid.revision != self._per_core_revision):
                self._consume_cpu_per_core_revision(cpu.last_valid.revision)
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

    def source_generation(self) -> int:
        """Return the live source-domain token used to reset chart evidence."""

        with self._lock:
            return self._generation

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
            if self._closed:
                raise RuntimeError("live coordinator is closed")
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
        old_pollers: Optional[Mapping[SourceId, SourcePoller[Any]]] = None
        with self._lock:
            if self._closed:
                raise RuntimeError("live coordinator is closed")
            previous = self._previous_time
            if previous is None:
                self._previous_time = (float(now_mono_s), normalized)
                return False
            mono_delta = float(now_mono_s) - previous[0]
            wall_delta = (normalized - previous[1]).total_seconds()
            reset_sources = mono_delta < 0 or mono_delta > 30.0
            reset_policy_only = abs(wall_delta - mono_delta) > 5.0
            if not reset_sources and not reset_policy_only:
                self._previous_time = (float(now_mono_s), normalized)
                return False
            if reset_sources:
                old_pollers = self._pollers
                self._create_generation()
                self._policy = reset_policy_state()
                self._per_core = ()
                self._per_core_revision = 0
                self._cpu_per_core_result = None
                self._frame_states = _registry_states()
            else:
                self._policy = reset_policy_state()
            self._previous_time = (float(now_mono_s), normalized)
        if old_pollers is not None:
            error = self._close_poller_set(
                old_pollers, time.monotonic() + 1.0,
            )
            if error is not None:
                raise error
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pollers = self._pollers
            process_cache = getattr(self, "_process_facts_cache", None)
            if process_cache is not None:
                process_cache.clear()
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
    if percent >= 60:
        return color_pair(PAIR_INFO) | curses.A_BOLD
    return color_pair(PAIR_INFO)


def safe_add(screen: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width - 1:
        return
    safe_text = truncate_display_cells(str(text), max(0, width - x - 1))
    try:
        screen.addnstr(y, x, safe_text, len(safe_text), attr)
    except curses.error:
        pass


def _rect_add(
    screen: curses.window, y: int, x: int, text: str, width: int,
    attr: int = 0,
) -> None:
    """Project and cell-truncate one write to its owning rectangle."""

    if width <= 0:
        return
    projected = semantic_text(str(text), unicode_ok())
    safe_add(screen, y, x, truncate_display_cells(projected, width), attr)


def draw_box(screen: curses.window, y: int, x: int, h: int, w: int, title: str, attr: int) -> None:
    if h < 3 or w < 8:
        return
    if unicode_ok():
        tl, tr, bl, br, hz, vt = "╭", "╮", "╰", "╯", "─", "│"
    else:
        tl, tr, bl, br, hz, vt = "+", "+", "+", "+", "-", "|"
    top = tl + hz * (w - 2) + tr
    bottom = bl + hz * (w - 2) + br
    _rect_add(screen, y, x, top, w, attr)
    for row in range(1, h - 1):
        _rect_add(screen, y + row, x, vt, 1, attr)
        _rect_add(screen, y + row, x + w - 1, vt, 1, attr)
    _rect_add(screen, y + h - 1, x, bottom, w, attr)
    label = f" {title} "
    _rect_add(screen, y, x + 2, label, w - 4, attr | curses.A_BOLD)


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


def fitted_percent_bar(percent: float, budget: int, phase: int, label: str = "") -> str:
    """Reserve the numeric reading and its unit before allocating bar cells."""

    number = "%5.1f%%" % percent
    bar_width = budget - display_cell_width(label) - display_cell_width(number) - 3
    if bar_width >= 4:
        return pulse_bar(percent, bar_width, phase, label)
    prefix = truncate_display_cells(label, max(0, budget - display_cell_width(number)))
    return prefix + number


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


def _record_history_frame(
    history: MetricHistory,
    frame: HealthFrame,
    last_cpu_revision: Optional[int],
    per_core: list[float],
    memory: dict[str, float],
    network_bps: float,
    disk: DiskInfo,
) -> Optional[int]:
    """Append once for each newly accepted 1 Hz CPU evidence revision."""

    cpu_view = frame.source_views[SourceId.CPU_USAGE]
    revision = cpu_view.last_valid_revision
    if (
        last_cpu_revision is not None
        and revision is None
        and cpu_view.status is SourceViewStatus.CHECKING
    ):
        history.clear()
        return None
    if (
        cpu_view.status is not SourceViewStatus.LIVE
        or not cpu_view.decision_ready
        or type(revision) is not int
        or revision <= 0
        or revision == last_cpu_revision
    ):
        return last_cpu_revision
    history.add(
        cpu_total_percent(per_core),
        ram_percent(memory),
        network_bps,
        disk_percent(disk),
    )
    return revision


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
        return color_pair(PAIR_SECONDARY)
    if value >= 65:
        return color_pair(PAIR_INFO) | curses.A_BOLD
    return color_pair(PAIR_INFO)


def fan_percent(fan: FanReading) -> float:
    if fan.maximum_rpm and fan.maximum_rpm > 0:
        return clamp(fan.rpm / fan.maximum_rpm * 100.0)
    if fan.target_rpm and fan.target_rpm > 0:
        return clamp(fan.rpm / fan.target_rpm * 100.0)
    return clamp(fan.rpm / 7000.0 * 100.0)


def fan_attr(fan: FanReading) -> int:
    pct = fan_percent(fan)
    if pct >= 55:
        return color_pair(PAIR_INFO) | curses.A_BOLD
    return color_pair(PAIR_INFO)


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


READ_LEVELS = {
    HealthSeverity.NORMAL: "good",
    HealthSeverity.WATCH: "watch",
    HealthSeverity.CRITICAL: "hot",
    HealthSeverity.UNKNOWN: "unknown",
}


def legacy_read_projection(assessment: HealthAssessment) -> dict[str, str]:
    """Project the pure health assessment into the established snapshot shape."""

    return {
        "message": "read: " + plain_reading_text(assessment.reason),
        "level": READ_LEVELS[assessment.severity],
    }


def source_state_from_result(
    state: SourceState[T], result: AdapterResult[T],
) -> SourceState[T]:
    """Complete one inactive source attempt using the adapter's exact timestamps."""

    if not isinstance(state, SourceState) or not isinstance(result, AdapterResult):
        raise TypeError("state and result must be source and adapter results")
    if state.active_revision is not None:
        raise ValueError("snapshot source state must be inactive")
    validate_source_state(state, result.started_mono_s, state.source_id)
    active, revision = start_attempt(state, result.started_mono_s)
    if result.outcome is AttemptOutcome.SUCCESS:
        if result.value is None:
            raise ValueError("successful adapter result requires a value")
        return complete_success(
            active, revision, result.value, result.completed_mono_s,
            result.completed_at_utc,
        )
    return complete_failure(
        active, revision, result.outcome, result.failure,
        result.completed_mono_s, result.completed_at_utc,
    )


def display_mode_name(mode: str) -> str:
    """Return the stable decision-header label for one backing mode ID."""

    names = {
        "balanced": "1 OVERVIEW",
        "compact": "1 OVERVIEW",
        "compute": "2 COMPUTE",
        "thermals": "3 THERMALS",
        "io": "4 I/O",
        "processes": "5 PROCESSES",
        "cinema": "6 CINEMA",
        "insights": "7 INSIGHTS",
        "devices": "8 DEVICES",
        "network": "9 NETWORK",
        "connections": "0 CONNECTIONS",
        "system": "i THIS MAC",
    }
    try:
        return names[mode]
    except (KeyError, TypeError):
        raise ValueError("unknown display mode")


def semantic_text(text: str, unicode_enabled: bool) -> str:
    """Project semantic copy to Unicode or a deterministic ASCII fallback."""

    if not isinstance(text, str) or type(unicode_enabled) is not bool:
        raise ValueError("semantic text requires a string and Boolean capability")
    if unicode_enabled:
        return text
    for before, after in (
        (" · ", " | "), ("≥", ">="), ("≤", "<="), ("×", "x"),
        ("–", "-"), ("—", "-"),
    ):
        text = text.replace(before, after)
    text = "".join(
        character for character in unicodedata.normalize("NFKD", text)
        if (unicodedata.category(character) not in {"Cc", "Cf"}
            and not unicodedata.combining(character))
    )
    return text.encode("ascii", "replace").decode("ascii")


def _finite_nonnegative(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("%s must be finite and nonnegative" % name)
    return float(value)


def _projection_width(width: int) -> int:
    if type(width) is not int or width < 76:
        raise ValueError("decision presentation requires at least 76 columns")
    return width - 1


def _fits_semantic_projections(text: str, budget: int) -> bool:
    """Require a complete candidate to fit in both Unicode and ASCII modes."""

    return all(
        display_cell_width(semantic_text(text, unicode_enabled)) <= budget
        for unicode_enabled in (True, False)
    )


def _frame_elapsed(frame: HealthFrame, now_mono_s: float) -> float:
    if not isinstance(frame, HealthFrame):
        raise ValueError("frame must be a HealthFrame")
    now = _finite_nonnegative(now_mono_s, "now_mono_s")
    elapsed = now - frame.captured_mono_s
    if elapsed < 0:
        raise ValueError("now_mono_s cannot precede frame capture")
    return elapsed


def format_evidence_age(age_s: float) -> str:
    """Floor an evidence age to the stable, once-per-second display tiers."""

    seconds = int(math.floor(_finite_nonnegative(age_s, "age_s")))
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        minutes, remaining = divmod(seconds, 60)
        return "%dm %02ds" % (minutes, remaining)
    if seconds < 86400:
        hours, remaining = divmod(seconds, 3600)
        return "%dh %02dm" % (hours, remaining // 60)
    days, remaining = divmod(seconds, 86400)
    return "%dd %02dh" % (days, remaining // 3600)


def plain_reading_text(text: str) -> str:
    """Translate fixed policy copy while preserving user and system names."""

    return {
        "Fresh core evidence shows no active CPU, memory, thermal, or storage pressure.":
            "No active CPU, memory, thermal, or storage pressure.",
        "No recommendation is available from incomplete evidence.":
            "System readings are incomplete.",
    }.get(text, text)


def source_view_text(view: SourceView, elapsed_s: float = 0.0) -> str:
    """Render a complete source status without exposing a retained value as live."""

    if not isinstance(view, SourceView) or not isinstance(view.status, SourceViewStatus):
        raise ValueError("view must be a SourceView")
    elapsed = _finite_nonnegative(elapsed_s, "elapsed_s")
    age = None
    if view.age_s is not None:
        age = _finite_nonnegative(view.age_s, "view.age_s") + elapsed
    if view.status is SourceViewStatus.LOCKED:
        if view.source_id is not SourceId.SENSOR_POWERMETRICS:
            raise ValueError("LOCKED is reserved for powermetrics")
        text = "LOCKED · u: temporary unlock"
    elif view.status is SourceViewStatus.STALE:
        text = "STALE" if age is None else "STALE · %s" % format_evidence_age(age)
    elif view.status is SourceViewStatus.ERROR:
        text = (
            "ERROR · no valid sample" if age is None
            else "ERROR · last good %s ago" % format_evidence_age(age)
        )
    else:
        text = view.status.value
    if view.refreshing and view.status is not SourceViewStatus.LIVE:
        text += " · checking"
    return text


_CORE_SOURCE_LABELS: Mapping[SourceId, str] = MappingProxyType({
    SourceId.CPU_USAGE: "CPU",
    SourceId.MEMORY_PRESSURE: "MEM",
    SourceId.THERMAL_OS: "THERM",
    SourceId.DISK_CAPACITY: "DISK",
})


def _trigger_suffix(frame: HealthFrame, now_mono_s: float) -> str:
    if frame.decisive_triggered_mono_s is None:
        return ""
    age = _finite_nonnegative(
        now_mono_s - frame.decisive_triggered_mono_s, "trigger age",
    )
    return " · triggered %s ago." % format_evidence_age(age)


def compact_reason(frame: HealthFrame, width: int, now_mono_s: float) -> str:
    """Return a complete decision reason that fits without semantic ellipsis."""

    budget = _projection_width(width)
    _frame_elapsed(frame, now_mono_s)
    assessment = frame.assessment
    reason = assessment.reason
    if assessment.severity is HealthSeverity.NORMAL:
        reason = "No active CPU, memory, thermal, or storage pressure."
    recovering = frame.decisive_triggered_mono_s is not None
    recovery_copy = {
        "macOS memory pressure is now normal; recovery confirmation is in progress.":
            "Memory pressure normal · confirming recovery",
        "macOS memory pressure has eased from critical to elevated; recovery confirmation is in progress.":
            "Memory eased to elevated · confirming recovery",
        "macOS warnings are now clear; thermal recovery confirmation is in progress.":
            "macOS warnings clear · confirming thermal recovery",
        "CPU use has eased; recovery confirmation is in progress.":
            "CPU use eased · confirming recovery",
        "Storage headroom has improved; recovery confirmation is in progress.":
            "Storage headroom improved · confirming recovery",
    }
    trigger_suffix = _trigger_suffix(frame, now_mono_s)
    preserve_snapshot_reason = bool(
        frame.origin is FrameOrigin.SNAPSHOT
        and reason == "CPU trend requires live monitoring."
    )
    if width < 90 and not preserve_snapshot_reason:
        if recovering:
            reason = recovery_copy.get(reason, reason.rstrip("."))
        elif assessment.severity is HealthSeverity.NORMAL:
            reason = "No active CPU, memory, thermal, or storage pressure."
        elif assessment.active_rules:
            rule = assessment.active_rules[0]
            if rule is RuleId.MEMORY_WATCH:
                reason = "macOS memory pressure is elevated."
            elif rule is RuleId.MEMORY_CRITICAL:
                reason = "macOS memory pressure is critical."
            elif rule is RuleId.THERMAL_WATCH:
                reason = "macOS thermal/performance warning is active."
            elif rule is RuleId.CPU_WATCH:
                reason = "CPU averaged ≥90% for 15s."
            elif rule is RuleId.STORAGE_WATCH:
                disk = frame.disk_capacity
                if disk is None:
                    reason = "Storage headroom is low."
                else:
                    reason = "Storage low · %s free on %s." % (
                        human_bytes(disk.free_bytes).strip(), disk.volume_name,
                    )
        elif assessment.blockers:
            source_id = assessment.blockers[0]
            label = _CORE_SOURCE_LABELS[source_id]
            state = source_view_text(
                frame.source_views[source_id],
                now_mono_s - frame.captured_mono_s,
            )
            reason = "Health incomplete · %s %s." % (label, state)
    elif recovering and not _fits_semantic_projections(
            reason + trigger_suffix, budget):
        reason = recovery_copy.get(reason, reason.rstrip("."))
    elif assessment.contributor_name and RuleId.CPU_WATCH in assessment.active_rules:
        addition = " · %s is the leading recent CPU process." % assessment.contributor_name
        if _fits_semantic_projections(reason + addition + trigger_suffix, budget):
            reason += addition
    if assessment.evidence_state is EvidenceState.DEGRADED and width >= 90:
        addition = " Other readings are incomplete."
        if _fits_semantic_projections(reason + addition + trigger_suffix, budget):
            reason += addition
    reason = plain_reading_text(reason) + trigger_suffix
    if not _fits_semantic_projections(reason, budget):
        if assessment.active_rules and assessment.active_rules[0] is RuleId.STORAGE_WATCH:
            reason = "Storage headroom is low." + _trigger_suffix(frame, now_mono_s)
        if not _fits_semantic_projections(reason, budget):
            raise ValueError("complete decision reason does not fit the supported width")
    return reason


def _is_bootstrap_checking(frame: HealthFrame) -> bool:
    return bool(
        frame.origin is FrameOrigin.LIVE
        and frame.assessment.severity is HealthSeverity.UNKNOWN
        and frame.assessment.evidence_state is EvidenceState.INCOMPLETE
        and frame.assessment.blockers
        and all(
            source_id in _CORE_SOURCE_LABELS
            and frame.source_views[source_id].status is SourceViewStatus.CHECKING
            for source_id in frame.assessment.blockers
        )
    )


def _is_cpu_baseline(frame: HealthFrame) -> bool:
    return bool(
        frame.origin is FrameOrigin.LIVE
        and frame.assessment.severity is HealthSeverity.UNKNOWN
        and frame.assessment.evidence_state is EvidenceState.INCOMPLETE
        and frame.assessment.blockers == (SourceId.CPU_USAGE,)
        and frame.assessment.cpu_coverage_s < 15.0
        and all(
            frame.source_views[source_id].status is SourceViewStatus.LIVE
            and frame.source_views[source_id].decision_ready
            for source_id in _CORE_SOURCE_LABELS
        )
    )


def decision_header_lines(
    frame: HealthFrame, mode: str, width: int, now_mono_s: float,
) -> Tuple[str, str]:
    """Construct the two decision rows without clocks, I/O, curses, or mutation."""

    budget = _projection_width(width)
    elapsed = _frame_elapsed(frame, now_mono_s)
    prefix = "PorkyHUD · %s · " % display_mode_name(mode)
    if frame.assessment.severity in (HealthSeverity.WATCH, HealthSeverity.CRITICAL):
        state = "%s · %s" % (
            frame.assessment.severity.value.upper(),
            frame.assessment.evidence_state.value.upper(),
        )
    elif frame.assessment.severity is HealthSeverity.NORMAL:
        state = "NORMAL · %s" % frame.assessment.evidence_state.value.upper()
    elif _is_cpu_baseline(frame):
        coverage = min(15, max(0, int(math.floor(frame.assessment.cpu_coverage_s))))
        state = "CHECKING CPU baseline %d/15s" % coverage
    elif _is_bootstrap_checking(frame):
        ready = sum(
            frame.source_views[source_id].status is SourceViewStatus.LIVE
            and frame.source_views[source_id].decision_ready
            for source_id in _CORE_SOURCE_LABELS
        )
        state = "CHECKING %d/4 core sources" % ready
    else:
        state = "UNKNOWN · %s" % frame.assessment.evidence_state.value.upper()
    if (frame.assessment.evidence_age_s is not None
            and frame.assessment.severity is not HealthSeverity.UNKNOWN):
        state += " · updated %s" % format_evidence_age(
            frame.assessment.evidence_age_s + elapsed,
        )
    first = prefix + state
    if _is_cpu_baseline(frame):
        second = "Building the 15-second CPU baseline."
    elif _is_bootstrap_checking(frame):
        second = "Loading system readings."
    else:
        second = compact_reason(frame, width, now_mono_s)
    if (not _fits_semantic_projections(first, budget)
            or not _fits_semantic_projections(second, budget)):
        raise ValueError("complete decision header does not fit the supported width")
    return first, second


def _live_ribbon_value(frame: HealthFrame, source_id: SourceId) -> str:
    if source_id is SourceId.CPU_USAGE:
        return "LIVE" if frame.cpu_percent is None else "%d%%" % round(frame.cpu_percent)
    if source_id is SourceId.MEMORY_PRESSURE:
        if frame.memory_pressure is None:
            return "LIVE"
        return {
            MemoryPressureLevel.NORMAL: "NORMAL",
            MemoryPressureLevel.WARN: "ELEVATED",
            MemoryPressureLevel.CRITICAL: "CRITICAL",
        }[frame.memory_pressure.level]
    if source_id is SourceId.THERMAL_OS:
        if frame.thermal is None:
            return "LIVE"
        thermal = frame.thermal
        if thermal.thermal_warning_level or thermal.performance_warning_level:
            return "WARN T%d/P%d" % (
                thermal.thermal_warning_level, thermal.performance_warning_level,
            )
        return "CLEAR"
    if frame.disk_capacity is None:
        return "LIVE"
    percent = 100.0 * frame.disk_capacity.free_bytes / frame.disk_capacity.total_bytes
    return "%d%% FREE" % round(percent)


def _ribbon_role(
    frame: HealthFrame, source_id: SourceId, view: SourceView,
) -> int:
    if view.status is not SourceViewStatus.LIVE:
        if view.status in (
            SourceViewStatus.NOT_APPLICABLE,
            SourceViewStatus.NOT_EXPOSED,
            SourceViewStatus.UNSUPPORTED,
        ):
            return PAIR_SECONDARY
        return PAIR_PRIMARY
    if source_id is SourceId.MEMORY_PRESSURE and frame.memory_pressure is not None:
        if frame.memory_pressure.level is MemoryPressureLevel.CRITICAL:
            return PAIR_CRITICAL
        if frame.memory_pressure.level is MemoryPressureLevel.WARN:
            return PAIR_WATCH
    if (source_id is SourceId.THERMAL_OS and frame.thermal is not None
            and (frame.thermal.thermal_warning_level
                 or frame.thermal.performance_warning_level)):
        return PAIR_WATCH
    if (source_id is SourceId.CPU_USAGE
            and RuleId.CPU_WATCH in frame.assessment.active_rules):
        return PAIR_WATCH
    if (source_id is SourceId.DISK_CAPACITY
            and RuleId.STORAGE_WATCH in frame.assessment.active_rules):
        return PAIR_WATCH
    return PAIR_NOMINAL


def _evidence_ribbon_projection(
    frame: HealthFrame, width: int, now_mono_s: float,
) -> Tuple[Tuple[Tuple[str, int], ...], str]:
    budget = _projection_width(width)
    elapsed = _frame_elapsed(frame, now_mono_s)
    detailed = []
    compact = []
    for source_id, label in _CORE_SOURCE_LABELS.items():
        view = frame.source_views[source_id]
        role = _ribbon_role(frame, source_id, view)
        if view.status is SourceViewStatus.LIVE:
            value = _live_ribbon_value(frame, source_id)
            detailed.append(("%s %s" % (label, value), role))
            compact.append((label, value, False, role))
        else:
            detailed.append(("%s %s" % (label, source_view_text(view, elapsed)), role))
            compact.append((label, view.status.value, True, role))
    line = " · ".join(text for text, _role in detailed)
    if _fits_semantic_projections(line, budget):
        return tuple(detailed), " · "
    groups = []
    index = 0
    while index < len(compact):
        label, value, groupable, role = compact[index]
        labels = [label]
        following = index + 1
        while (groupable and following < len(compact)
               and compact[following][1:] == (value, True, role)):
            labels.append(compact[following][0])
            following += 1
        groups.append(("%s %s" % ("/".join(labels), value), role))
        index = following
    line = " / ".join(text for text, _role in groups)
    if _fits_semantic_projections(line, budget):
        return tuple(groups), " / "
    line = "/".join(text for text, _role in groups)
    if _fits_semantic_projections(line, budget):
        return tuple(groups), "/"
    references = []
    first_label_by_status = {}
    for label, value, groupable, role in compact:
        if groupable and value in first_label_by_status:
            text = "%s=%s" % (label, first_label_by_status[value])
        else:
            text = "%s %s" % (label, value)
            if groupable:
                first_label_by_status[value] = label
        references.append((text, role))
    line = "/".join(text for text, _role in references)
    if _fits_semantic_projections(line, budget):
        return tuple(references), "/"
    raise ValueError("complete evidence ribbon does not fit the supported width")


def evidence_ribbon_lines(
    frame: HealthFrame, width: int, now_mono_s: float,
) -> Tuple[str, ...]:
    """Construct the fixed-order core ribbon, compacting status detail as needed."""

    segments, separator = _evidence_ribbon_projection(frame, width, now_mono_s)
    return (separator.join(text for text, _role in segments),)


def read_attr(level: str) -> int:
    """Map a semantic conclusion to the same color role in every theme."""

    if level in ("normal", "good", "nominal"):
        pair = PAIR_NOMINAL
    elif level == "watch":
        pair = PAIR_WATCH
    elif level in ("critical", "hot"):
        pair = PAIR_CRITICAL
    elif level in ("unknown", "checking", "incomplete", "delayed"):
        pair = PAIR_PRIMARY
    else:
        raise ValueError("unknown semantic level")
    return color_pair(pair) | curses.A_BOLD


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
        return fitted_percent_bar(percent, width, phase + index, f"{label} ")
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
    """Compatibility projection of the shared, pure mascot renderer."""

    return list(render_mascot(phase, unicode_enabled=unicode_ok()).plain_rows)


def draw_signature(
    screen: curses.window, y: int, width: int, static: Mapping[str, Any],
    config: HudConfig, phase: int,
) -> int:
    """Seven rows of branding; all health information remains above this band."""

    draw_mascot_art(screen, y, 2, phase, config)
    x = 33
    budget = width - x - 2
    mark = wordmark_lines("compact" if config.visual_style == "sculpted" else "outline",
                          unicode_enabled=unicode_ok() and config.visual_style != "ascii")
    for index, line in enumerate(mark[:3]):
        _rect_add(screen, y + index, x, line, budget,
                  color_pair(PAIR_PRIMARY) | curses.A_BOLD)
    _rect_add(screen, y + 3, x, "YOUR MAC, IN FOCUS", budget, color_pair(PAIR_INFO))
    chip = str(static.get("chip") or "Discovering your Mac")
    memory = str(static.get("memory") or "")
    _rect_add(screen, y + 4, x, chip + (" · " + memory if memory else ""), budget,
              color_pair(PAIR_PRIMARY))
    motion = ("off", "calm", "vivid")[config.animation_mode % 3]
    _rect_add(screen, y + 5, x, "%s · %s · motion %s" %
              (config.theme_name, config.visual_style, motion), budget,
              color_pair(PAIR_SECONDARY))
    _rect_add(screen, y + 6, x, "─" * budget if unicode_ok() else "-" * budget,
              budget, color_pair(PAIR_STRUCTURE))
    return y + 7


def draw_header(screen: curses.window, width: int, phase: int,
                static: dict[str, Any], config: HudConfig) -> int:
    """Compatibility branding adapter; frame lifecycle stays with the caller."""

    height, _ = screen.getmaxyx()
    if width >= 118 and height >= 34:
        return draw_signature(screen, 0, width, static, config, phase)
    _rect_add(screen, 0, 1, " P O R K Y H U D ", width - 2,
              color_pair(PAIR_PRIMARY) | curses.A_BOLD)
    _rect_add(screen, 1, 1, "%s · %s / %s" %
              (COPYRIGHT_TEXT, config.theme_name, config.layout_name), width - 2,
              color_pair(PAIR_SECONDARY))
    _rect_add(screen, 2, 1, "-" * (width - 2), width - 2, color_pair(PAIR_STRUCTURE))
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
    draw_box(screen, y, x, h, w, "CPU WORKER CORES", color_pair(PAIR_STRUCTURE))
    if h < 5:
        return
    display_core, labels = display_core_usage(per_core, static)
    total = cpu_total_percent(display_core)
    logical = static.get("logical_cpu") or str(len(display_core) or "?")
    physical = static.get("physical_cpu") or "?"
    core_line = static.get("cpu_cores") or f"{physical} physical / {logical} logical"
    _rect_add(screen, y + 1, x + 2, f"{static.get('chip', 'CPU')}", w - 4,
              color_pair(PAIR_PRIMARY) | curses.A_BOLD)
    _rect_add(screen, y + 2, x + 2, f"cores: {core_line}", w - 4,
              color_pair(PAIR_PRIMARY))
    _rect_add(screen, y + 3, x + 2,
              fitted_percent_bar(total, w - 4, phase, "total "), w - 4,
              color_for_percent(total))
    _rect_add(screen, y + 4, x + 2,
              f"60s  {sparkline(history.cpu, max(8, w - 10), 100.0)}",
              w - 4, color_pair(PAIR_STRUCTURE))

    lanes_start = y + 6
    lanes_available = max(0, h - 8)
    if not display_core or lanes_available <= 0:
        _rect_add(screen, y + 6, x + 2, "per-core stream unavailable", w - 4,
                  color_pair(PAIR_SECONDARY))
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
        _rect_add(screen, row_y, x + 2, label, w - 4,
                  color_pair(PAIR_SECONDARY) | curses.A_BOLD)
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
            _rect_add(
                screen, cy, cx,
                cpu_lane_text(
                    core_label, core_index, pct, max(1, col_width - 1), phase,
                ),
                max(1, col_width - 1), color_for_percent(pct),
            )
        row_y += (group["count"] + columns - 1) // columns
    if hidden:
        _rect_add(screen, y + h - 2, x + 2, f"+{hidden} workers hidden",
                  w - 4, color_pair(PAIR_PRIMARY))


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
    draw_box(screen, y, x, h, w, "SYSTEM", color_pair(PAIR_STRUCTURE))
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
        attr = color_pair(PAIR_PRIMARY)
        if index == 1:
            attr |= curses.A_BOLD
        _rect_add(screen, y + index, x + 2, line, w - 4, attr)


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
    draw_box(screen, y, x, h, w, "MEMORY", color_pair(PAIR_STRUCTURE))
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
        (f"60s  {sparkline(history.ram, max(8, w - 10), 100.0)}", color_pair(PAIR_STRUCTURE)),
        (f"used {human_bytes(used)} / {human_bytes(total)}  avail {human_bytes(available)}", color_pair(PAIR_PRIMARY)),
        (f"app {human_bytes(app)}  wired {human_bytes(mem.get('wired', 0.0))}", color_pair(PAIR_PRIMARY)),
        (f"cache {human_bytes(cached)}  comp {human_bytes(mem.get('compressed', 0.0))}", color_pair(PAIR_PRIMARY)),
        (pulse_bar(swap_pct, max(8, w - 18), phase + 4, "swap "), color_for_percent(swap_pct)),
    ]
    if h > 8:
        lines.append((f"swap {human_bytes(swap_used)} / {human_bytes(swap_total)}", color_pair(PAIR_PRIMARY)))
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        _rect_add(screen, y + index, x + 2, line, w - 4, attr)


def battery_display_notice(battery: BatteryInfo) -> Optional[str]:
    """Distinguish pending/unknown power data from a confirmed batteryless Mac."""

    if battery.state in ("checking", "pending"):
        return "Power CHECKING"
    if not battery.present and battery.source == "Power":
        return "Power UNAVAILABLE"
    return None


def draw_battery_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryInfo,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "POWER CELL", color_pair(PAIR_STRUCTURE))
    notice = battery_display_notice(battery)
    if notice:
        _rect_add(screen, y + 1, x + 2, notice, w - 4, color_pair(PAIR_SECONDARY))
        return
    if not battery.present:
        desktop_lines = [
            ("desktop power", color_pair(PAIR_INFO) | curses.A_BOLD),
            (f"source: {battery.source}", color_pair(PAIR_PRIMARY)),
            ("internal battery: not present", color_pair(PAIR_PRIMARY)),
        ]
        for index, (line, line_attr) in enumerate(desktop_lines[: h - 2], start=1):
            _rect_add(screen, y + index, x + 2, line, w - 4, line_attr)
        return
    pct = float(battery.percent) if battery.percent is not None else None
    attr = color_pair(PAIR_INFO) | curses.A_BOLD
    if battery.percent is not None and battery.percent <= 30:
        attr = color_pair(PAIR_INFO) | curses.A_BOLD

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
        (fitted_percent_bar(pct, w - 4, phase, "cell ") if pct is not None else "Charge NOT EXPOSED", attr),
        (f"state {flow_text(phase, battery.is_charging, battery.external_connected)} {state}", color_pair(PAIR_STRUCTURE) | curses.A_BOLD),
        (f"source: {battery.source}", color_pair(PAIR_PRIMARY)),
        (f"charger: {charger_label}".strip(), color_pair(PAIR_PRIMARY)),
        (
            f"cycles: {format_optional_int(battery.cycle_count)} / {format_optional_int(battery.design_cycles)}",
            color_pair(PAIR_PRIMARY),
        ),
        (
            f"health: {format_optional_int(battery.health_percent, '%')}  temp: {format_temp(battery.temp_c)}",
            temp_attr(battery.temp_c),
        ),
    ]
    if battery.voltage_v is not None and battery.amperage_a is not None:
        lines.append((f"pack: {battery.voltage_v:4.2f}V  {battery.amperage_a:5.2f}A", color_pair(PAIR_PRIMARY)))
    for index, (line, line_attr) in enumerate(lines[: h - 2], start=1):
        _rect_add(screen, y + index, x + 2, line, w - 4, line_attr)


def draw_thermal_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryInfo,
    sensor: SensorInfo,
    phase: int,
    os_thermal: Optional[OsThermalState] = None,
) -> None:
    draw_box(screen, y, x, h, w, "THERMAL", color_pair(PAIR_STRUCTURE))

    def os_status_line(label: str, value: Optional[int]) -> tuple[str, int]:
        if value is None:
            return (f"OS {label}: UNAVAILABLE", color_pair(PAIR_SECONDARY))
        if value == 0:
            return (f"OS {label}: CLEAR", read_attr("nominal"))
        return (f"OS {label}: WARN {value}", read_attr("watch"))

    def status_line(label: str, value: str) -> tuple[str, int]:
        if value in ("checking", "pending"):
            return (f"optional {label}: checking...", color_pair(PAIR_PRIMARY))
        if value == "nominal":
            return (f"optional {label}: {value}",
                    color_pair(PAIR_INFO) | curses.A_BOLD)
        return (f"optional {label}: {value}",
                color_pair(PAIR_PRIMARY) | curses.A_BOLD)

    lines: list[tuple[str, int]] = [
        os_status_line(
            "thermal",
            None if os_thermal is None else os_thermal.thermal_warning_level,
        ),
        os_status_line(
            "performance",
            None if os_thermal is None else os_thermal.performance_warning_level,
        ),
        status_line("pressure", sensor.thermal_warning),
        status_line("performance", sensor.performance_warning),
    ]
    notice = battery_display_notice(battery)
    if notice:
        lines.append((notice, color_pair(PAIR_SECONDARY)))
    elif battery.present:
        lines.extend(
            [
                (f"battery skin: {format_temp(battery.temp_c)}", temp_attr(battery.temp_c)),
                (f"virtual pack: {format_temp(battery.virtual_temp_c)}", temp_attr(battery.virtual_temp_c)),
            ]
        )
    else:
        lines.append(("battery: not installed", color_pair(PAIR_PRIMARY)))
    exposed_count = 0
    if sensor.cpu_temp_c is not None:
        lines.append((f"processor temp: {format_temp(sensor.cpu_temp_c)}", temp_attr(sensor.cpu_temp_c)))
        exposed_count += 1
    if sensor.gpu_temp_c is not None:
        lines.append((f"graphics temp:  {format_temp(sensor.gpu_temp_c)}", temp_attr(sensor.gpu_temp_c)))
        exposed_count += 1
    if sensor.fan_rpm is not None:
        fan_prefix = f"{sensor.fan_count} fans" if sensor.fan_count > 1 else "fan"
        lines.append((f"{fan_prefix}: {fan_text(sensor)}", color_pair(PAIR_INFO)))
        exposed_count += 1
    if sensor.privileged_locked:
        if sensor.thermal_warning in ("checking", "pending"):
            lines.append(("advanced: checking sensor access", color_pair(PAIR_PRIMARY)))
        else:
            locked_text = "power sampler: press u" if exposed_count else "advanced: press u to unlock"
            lines.append((locked_text, color_pair(PAIR_PRIMARY) | curses.A_BOLD))
    else:
        if sensor.cpu_power_w is not None:
            lines.append((f"cpu power: {format_watts(sensor.cpu_power_w)}", color_pair(PAIR_PRIMARY)))
            exposed_count += 1
        if sensor.gpu_power_w is not None:
            lines.append((f"gpu power: {format_watts(sensor.gpu_power_w)}", color_pair(PAIR_PRIMARY)))
            exposed_count += 1
        if sensor.ane_power_w is not None:
            lines.append((f"ane power: {format_watts(sensor.ane_power_w)}",
                          color_pair(PAIR_INFO)))
            exposed_count += 1
        if sensor.dram_power_w is not None:
            bw = ""
            if sensor.dram_read_gbs is not None or sensor.dram_write_gbs is not None:
                bw = f"  bw {(sensor.dram_read_gbs or 0) + (sensor.dram_write_gbs or 0):.1f}GB/s"
            lines.append((f"dram power: {format_watts(sensor.dram_power_w)}{bw}",
                          color_pair(PAIR_INFO)))
            exposed_count += 1
        if sensor.package_power_w is not None:
            lines.append((f"package: {format_watts(sensor.package_power_w)}", color_pair(PAIR_STRUCTURE) | curses.A_BOLD))
            exposed_count += 1
        if exposed_count == 0:
            lines.append(("advanced: no extra sensors exposed",
                          color_pair(PAIR_SECONDARY)))
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
        lines.append(("clusters: " + "  ".join(cluster_bits), color_pair(PAIR_INFO)))
    if h > 10:
        lines.append((f"sensor bus: {comet(max(8, w - 18), phase, 0.85)}", color_pair(PAIR_INFO)))
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        _rect_add(screen, y + index, x + 2, line, w - 4, attr)


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
    draw_box(screen, y, x, h, w, "FANS + TEMP SENSORS", color_pair(PAIR_STRUCTURE))
    if h < 5:
        return

    line_y = y + 1
    source = sensor.raw_hint.replace("smc", "SMC")
    if sensor.privileged_locked and sensor.raw_hint.startswith("smc"):
        source = "SMC online / power locked"
    source_attr = color_pair(PAIR_STRUCTURE) | curses.A_BOLD
    if sensor.sensor_sample_age_s > SENSOR_REFRESH_SECONDS * 2:
        source = f"{source} · stale {sensor.sensor_sample_age_s:.0f}s"
        source_attr = color_pair(PAIR_PRIMARY) | curses.A_BOLD
    _rect_add(screen, line_y, x + 2, source, w - 4, source_attr)
    line_y += 1

    if sensor.fans:
        _rect_add(screen, line_y, x + 2,
                  f"fan array: {sensor.fan_count} active", w - 4,
                  color_pair(PAIR_INFO) | curses.A_BOLD)
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
            _rect_add(screen, line_y, x + 2, line, w - 4, fan_attr(fan))
            line_y += 1
            if line_y < y + h - 1 and (fan.minimum_rpm or fan.target_rpm or fan.maximum_rpm):
                bounds = f"min {format_optional_int(fan.minimum_rpm)}  target {format_optional_int(fan.target_rpm)}  max {format_optional_int(fan.maximum_rpm)}"
                _rect_add(screen, line_y, x + 4, bounds, w - 6,
                          color_pair(PAIR_SECONDARY))
                line_y += 1
    else:
        if sensor.thermal_warning in ("checking", "pending"):
            text = "fans: checking sensor bus..."
            attr = color_pair(PAIR_PRIMARY)
        else:
            text = "fans: press u or SMC unavailable" if sensor.privileged_locked else "fans: none exposed"
            attr = color_pair(
                PAIR_PRIMARY if sensor.privileged_locked else PAIR_SECONDARY
            )
        _rect_add(screen, line_y, x + 2, text, w - 4, attr)
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
        _rect_add(
            screen, line_y, x + 2,
            pulse_bar(clamp(value), width_for_bar, phase, f"{label:<3} "),
            w - 4, temp_attr(value),
        )
        line_y += 1

    rows = temperature_group_rows(sensor.temp_sensors)
    if rows and line_y < y + h - 1:
        _rect_add(screen, line_y, x + 2, "hottest groups", w - 4,
                  color_pair(PAIR_STRUCTURE) | curses.A_BOLD)
        line_y += 1
    for label, average, hottest, count in rows:
        if line_y >= y + h - 1:
            break
        line = f"{label:<13.13} avg {average:4.1f}C  max {hottest:4.1f}C  n={count}"
        _rect_add(screen, line_y, x + 2, line, w - 4, temp_attr(hottest))
        line_y += 1

    if line_y < y + h - 1:
        if sensor.temp_sensors:
            line = f"sensor sweep {comet(max(8, w - 16), phase + 5, 0.75)}"
        else:
            line = "temperature sensors: no detail exposed"
        _rect_add(screen, line_y, x + 2, line, w - 4,
                  color_pair(PAIR_INFO))


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
    draw_box(screen, y, x, h, w, "GPU", color_pair(PAIR_STRUCTURE))
    gpus = static.get("gpus") or []
    if not gpus:
        _rect_add(screen, y + 1, x + 2, "GPU scan unavailable", w - 4,
                  color_pair(PAIR_SECONDARY))
        _rect_add(screen, y + 2, x + 2,
                  "try: system_profiler SPDisplaysDataType", w - 4,
                  color_pair(PAIR_PRIMARY))
        return

    line_y = y + 1
    for gpu_index, gpu in enumerate(gpus):
        if line_y >= y + h - 1:
            break
        name = gpu.get("name", "GPU")
        cores = gpu.get("cores", "") or str(static.get("gpu_core_count") or "")
        core_text = f" [{cores} GPU cores]" if cores else ""
        _rect_add(screen, line_y, x + 2, f"{name}{core_text}", w - 4,
                  color_pair(PAIR_INFO) | curses.A_BOLD)
        line_y += 1
        if line_y < y + h - 1:
            if sensor.gpu_active_percent is not None:
                activity_text = fitted_percent_bar(
                    sensor.gpu_active_percent,
                    w - 4,
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
                activity_attr = color_pair(PAIR_PRIMARY)
            else:
                activity_text = (
                    "GPU activity unavailable"
                    if w < 40
                    else "activity: not exposed by this Mac"
                )
                activity_attr = color_pair(PAIR_SECONDARY)
            _rect_add(screen, line_y, x + 2, activity_text, w - 4,
                      activity_attr)
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
            _rect_add(screen, line_y, x + 2, "  ".join(stats), w - 4,
                      temp_attr(sensor.gpu_temp_c))
            line_y += 1
        peak = gpu.get("max_freq_mhz") or str(static.get("max_gpu_freq_mhz") or "")
        fp32 = static.get("gpu_fp32_tflops")
        if line_y < y + h - 1 and (peak or fp32):
            peak_text = f"peak {peak}MHz" if peak else "peak freq unknown"
            fp32_text = f"  {fp32:.1f} TFLOPS est" if isinstance(fp32, float) else ""
            _rect_add(screen, line_y, x + 2, f"{peak_text}{fp32_text}",
                      w - 4, color_pair(PAIR_BRAND))
            line_y += 1
        metal = gpu.get("metal", "")
        if metal and line_y < y + h - 1:
            _rect_add(screen, line_y, x + 2, f"metal: {metal}", w - 4,
                      color_pair(PAIR_PRIMARY))
            line_y += 1
        displays = gpu.get("displays", "")
        if displays and line_y < y + h - 1:
            _rect_add(screen, line_y, x + 2, f"driving: {displays}", w - 4,
                      color_pair(PAIR_PRIMARY))
            line_y += 1
        if gpu_index < len(gpus) - 1 and line_y < y + h - 1:
            _rect_add(screen, line_y, x + 2, "-" * min(w - 4, 28),
                      w - 4, color_pair(PAIR_STRUCTURE))
            line_y += 1


def draw_io_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    disk: DiskInfo,
    activity: DiskActivity,
    net_down: Optional[float],
    net_up: Optional[float],
    history: MetricHistory,
    phase: int = 0,
) -> None:
    draw_box(screen, y, x, h, w, "I/O", color_pair(PAIR_STRUCTURE))
    disk_pct = disk_percent(disk)
    activity_age = ""
    if activity.sample_age > DISK_ACTIVITY_REFRESH_SECONDS * 2:
        activity_age = f" · {activity.sample_age:.0f}s old"
    if activity.source in ("checking", "pending"):
        activity_line = "disk activity CHECKING"
        activity_attr = color_pair(PAIR_PRIMARY) | curses.A_BOLD
    elif activity.source == "unavailable":
        activity_line = "disk activity unavailable · UNAVAILABLE"
        activity_attr = color_pair(PAIR_SECONDARY) | curses.A_BOLD
    else:
        activity_line = (
            f"disk live [{activity.source}] {human_bytes(activity.bytes_per_sec)}/s  "
            f"{activity.iops:.0f} iops{activity_age} · LIVE"
        )
        activity_attr = color_pair(PAIR_INFO) | curses.A_BOLD

    network_ready = (
        net_down is not None and net_up is not None
        and net_down >= 0.0 and net_up >= 0.0
    )
    network_unavailable = (
        net_down is not None and net_up is not None
        and (net_down < 0.0 or net_up < 0.0)
    )
    if network_ready:
        network_total = net_down + net_up
        net_peak = max(history.net) if history.net else network_total
        net_peak_text = f"pk {human_bytes(net_peak)}/s"
        net_spark_width = max(
            6, w - len("net 60s  ") - len(net_peak_text) - 6,
        )
        network_line = (
            f"net d {human_bytes(net_down)}/s  "
            f"u {human_bytes(net_up)}/s · LIVE"
        )
        network_attr = color_pair(PAIR_INFO) | curses.A_BOLD
        network_history = (
            f"net 60s  {sparkline(history.net, net_spark_width, None)} "
            f"{net_peak_text}"
        )
    elif network_unavailable:
        network_line = "net UNAVAILABLE"
        network_attr = color_pair(PAIR_SECONDARY) | curses.A_BOLD
        network_history = "net history unavailable"
    else:
        network_line = "net CHECKING · awaiting rate baseline"
        network_attr = color_pair(PAIR_PRIMARY) | curses.A_BOLD
        network_history = "net history awaiting baseline"
    lines = [
        (pulse_bar(disk_pct, max(8, w - 18), phase, "disk "), color_for_percent(disk_pct)),
        (f"{disk.label} {human_bytes(disk.used)} / {human_bytes(disk.total)}  free {human_bytes(disk.free)}", color_pair(PAIR_PRIMARY)),
        (activity_line, activity_attr),
        (network_line, network_attr),
        (network_history, color_pair(PAIR_STRUCTURE)),
        (f"disk 60s {sparkline(history.disk, max(8, w - 18), 100.0)}", color_pair(PAIR_INFO)),
    ]
    if h > 8 and network_ready:
        lines.append((f"rx {comet(max(8, w - 9), phase, min(1.0, net_down / 1_000_000 + 0.35))}", color_pair(PAIR_STRUCTURE)))
        lines.append((f"tx {comet(max(8, w - 9), phase + 7, min(1.0, net_up / 1_000_000 + 0.35))}", color_pair(PAIR_INFO)))
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        _rect_add(screen, y + index, x + 2, line, w - 4, attr)


def process_empty_message(frame: HealthFrame, config: Optional[HudConfig]) -> str:
    """Keep unavailable process lists distinct from valid empty selections."""

    has_query = config is not None and bool(config.search_query)
    view = frame.source_views[SourceId.PROCESS_TABLE]
    if view.status is not SourceViewStatus.LIVE:
        if view.status is SourceViewStatus.CHECKING and not has_query:
            return "collecting processes..."
        return "Processes " + source_view_text(view)
    return "No matching processes · / edit · Esc clear" if has_query else "No processes reported"


def draw_process_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    rows: Sequence[ProcessRow],
    scroll: int,
    sort_mode: str,
    title: Optional[str] = None,
    *, empty_message: str = "collecting processes...", identity_only: bool = False,
) -> None:
    panel_title = title or "PROCESS GRID sort=%s" % sort_mode
    draw_box(screen, y, x, h, w, panel_title, color_pair(PAIR_STRUCTURE))
    if h < 5:
        return
    body_height = process_body_height(h)
    scroll = max(0, min(scroll, max(0, len(rows) - body_height)))
    visible_rows = rows[scroll : scroll + body_height]
    show_gpu = any(row.gpu is not None for row in visible_rows)
    compact = w < 58
    if compact:
        header = " CPU%% MEM%% %sIDENTITY" % ("GPU% " if show_gpu else "")
    else:
        header = " PID   CPU%% MEM%% %sST ELAPSED   IDENTITY" % (
            "GPU% " if show_gpu else "",
        )
    _rect_add(screen, y + 1, x + 1, header, w - 2,
              color_pair(PAIR_STRUCTURE) | curses.A_BOLD)
    _rect_add(screen, y + 2, x + 1, "-" * (w - 2), w - 2,
              color_pair(PAIR_STRUCTURE))

    if not rows and body_height > 0:
        _rect_add(screen, y + 3, x + 2, empty_message, w - 4,
                  color_pair(PAIR_PRIMARY))
    for index, row in enumerate(visible_rows):
        line_y = y + 3 + index
        command = process_identity(row.command) if identity_only else row.command
        if compact:
            fixed_width = 13 + (5 if show_gpu else 0)
            command_width = max(8, w - fixed_width - 2)
            gpu = ""
            if show_gpu:
                gpu = "%5s" % ("--" if row.gpu is None else "%.1f" % row.gpu)
            line = "%5.1f %4.1f%s %s" % (
                row.cpu, row.mem, gpu, visible_command(command, command_width),
            )
        else:
            fixed_width = 31 + (6 if show_gpu else 0)
            command_width = max(8, w - fixed_width)
            gpu = ""
            if show_gpu:
                gpu = "%6s" % ("--" if row.gpu is None else "%.1f" % row.gpu)
            line = "%6d %5.1f %4.1f%s %-2.2s %8.8s  %s" % (
                row.pid, row.cpu, row.mem, gpu, row.stat, row.etime,
                visible_command(command, command_width),
            )
        attr = color_pair(PAIR_PRIMARY)
        _rect_add(screen, line_y, x + 1, line, w - 2, attr)

    first = min(len(rows), scroll + 1) if rows else 0
    last = min(len(rows), scroll + body_height)
    keys = "↑↓/Pg" if unicode_ok() else "j/k/Pg"
    if not rows:
        state = ("collecting" if empty_message == "collecting processes..." else
                 "0 matches" if empty_message.startswith("No matching processes") else
                 "0 processes" if empty_message == "No processes reported" else "list unavailable")
        footer = f"{state} · m {sort_mode}" if unicode_ok() else f"{state} | m {sort_mode}"
    elif w < 58:
        footer = f"{len(rows)} · {first}-{last} · {keys} · m {sort_mode}"
    else:
        footer = f"{len(rows)} processes · {first}-{last} · {keys} scroll · m sort"
    _rect_add(screen, y + h - 2, x + 2, footer, w - 4,
              color_pair(PAIR_STRUCTURE) | curses.A_BOLD)


def _body_add(
    screen: curses.window, y: int, x: int, text: str, width: int,
    attr: int = 0,
) -> None:
    _rect_add(screen, y, x, text, max(0, width), attr)


def _clamped_process_scroll(rows: Sequence[ProcessRow], panel_h: int, scroll: int) -> int:
    return max(0, min(scroll, max(0, len(rows) - process_body_height(panel_h))))


def draw_mascot_art(
    screen: curses.window, y: int, x: int, phase: int, config: HudConfig,
) -> None:
    """Draw the shared fixed-size artwork without owning surrounding geometry."""

    mascot = render_mascot(phase, style=config.visual_style,
                          unicode_enabled=unicode_ok())
    roles = {"brand": PAIR_PIG_BODY, "highlight": PAIR_PIG_LIT,
             "shadow": PAIR_PIG_SHADOW, "detail": PAIR_PIG_DETAIL,
             "structure": PAIR_STRUCTURE}
    for row_index, spans in enumerate(mascot.rows):
        column = x
        for span in spans:
            _rect_add(screen, y + row_index, column, span.text,
                      max(0, x + mascot.width - column), color_pair(roles[span.role]))
            column += display_cell_width(span.text)


def _overview_memory_known(mem: Mapping[str, float]) -> bool:
    return all(type(mem.get(key)) in (int, float)
               and math.isfinite(mem[key]) and mem[key] >= 0
               for key in ("used", "total")) and mem["total"] > 0


def _overview_memory_bytes(mem: Mapping[str, float], key: str) -> str:
    value = mem.get(key)
    return (human_bytes(value) if type(value) in (int, float)
            and math.isfinite(value) and value >= 0 else "--")


def _draw_overview_quick_stats(
    screen: curses.window, y: int, width: int, frame: HealthFrame,
    history: MetricHistory, static: Mapping[str, Any], mem: Mapping[str, float],
    battery: Optional[BatteryInfo], sensor: SensorInfo, config: HudConfig,
    phase: int, wide: bool,
) -> None:
    """Trade decorative branding space for seven immediately useful readouts."""

    draw_mascot_art(screen, y, 2, phase, config)
    x, budget = 33, min(44, width - 35) if wide else width - 35
    chip = str(static.get("chip") or "Discovering your Mac")
    memory_size = str(static.get("memory") or "")
    cpu = ("%.1f%%" % frame.cpu_percent if frame.cpu_percent is not None else
           frame.source_views[SourceId.CPU_USAGE].status.value)
    ram = "%.1f%%" % ram_percent(dict(mem)) if _overview_memory_known(mem) else "CHECKING"
    missing_sensor = ("CHECKING" if sensor.thermal_warning in ("checking", "pending")
                      else "LOCKED" if sensor.privileged_locked else "NOT EXPOSED")
    gpu = ("%.1f%%" % sensor.gpu_active_percent
           if sensor.gpu_active_percent is not None else missing_sensor)
    power = (" · package " + format_watts(sensor.package_power_w).strip()
             if sensor.package_power_w is not None else "")
    cpu_temp = format_temp(sensor.cpu_temp_c).strip() if sensor.cpu_temp_c is not None else "--"
    gpu_temp = format_temp(sensor.gpu_temp_c).strip() if sensor.gpu_temp_c is not None else "--"
    if sensor.fans:
        fans = "/".join(str(fan.rpm) for fan in sensor.fans[:2])
        fans += "+" if len(sensor.fans) > 2 else ""
    else:
        fans = str(sensor.fan_rpm) if sensor.fan_rpm is not None else "--"
    temperatures = "CPU %s · GPU %s · fan %s rpm" % (cpu_temp, gpu_temp, fans)
    if display_cell_width(temperatures) > budget:
        temperatures = "CPU %s GPU %s fan %s rpm" % (cpu_temp, gpu_temp, fans)
    battery_line = "Battery CHECKING"
    if battery is not None and battery.state not in ("checking", "pending"):
        if battery.present:
            percent = "--" if battery.percent is None else "%s%%" % battery.percent
            state = "charging" if battery.is_charging else "AC" if battery.external_connected else "battery"
            battery_line = "Battery %s · %s" % (percent, state)
            if battery.remaining and battery.remaining not in ("not charging", "unknown"):
                battery_line += " · " + battery.remaining
        elif battery.source != "Power":
            battery_line = "%s · no internal battery" % battery.source
        else:
            battery_line = "Battery UNAVAILABLE"
    os_line = str(static.get("os") or "macOS CHECKING")
    uptime = static.get("uptime_seconds", static.get("boot_seconds"))
    if type(uptime) is int and uptime >= 0:
        os_line += " · up " + format_uptime(uptime)
    if sensor.sensor_sample_age_s > SENSOR_REFRESH_SECONDS * 2:
        last_line = "Sensors STALE · %.0fs old" % sensor.sensor_sample_age_s
    elif sensor.privileged_locked:
        last_line = "LOCKED · u: temporary unlock"
    else:
        last_line = "CPU 60s " + sparkline(history.cpu, max(1, budget - 8))
    lines = (
        chip + (" · " + memory_size if memory_size else ""),
        "CPU %s · RAM %s" % (cpu, ram), "GPU " + gpu + power,
        temperatures,
        battery_line, os_line, last_line,
    )
    for index, line in enumerate(lines):
        _body_add(screen, y + index, x, line, budget,
                  color_pair(PAIR_PRIMARY if index in (0, 4, 5) else PAIR_INFO)
                  | (curses.A_BOLD if index in (0, 1) else 0))
    if wide:
        gpu_width = 36 if width >= 156 else width - 80
        draw_gpu_panel(screen, y, 79, 7, gpu_width, dict(static), sensor, 0)
        if width >= 156:
            if (battery is not None and battery.state not in ("checking", "pending")
                    and (battery.present or battery.source != "Power")):
                draw_battery_panel(screen, y, 116, 7, width - 117, battery, 0)
            else:
                draw_box(screen, y, 116, 7, width - 117, "POWER CELL", color_pair(PAIR_STRUCTURE))
                _body_add(screen, y + 1, 118, battery_line, width - 121,
                          color_pair(PAIR_SECONDARY))


def _draw_overview_memory(
    screen: curses.window, y: int, x: int, h: int, w: int,
    mem: Mapping[str, float], history: MetricHistory,
) -> None:
    """Compact memory fields keep unobserved values distinct from measured zero."""

    draw_box(screen, y, x, h, w, "MEMORY", color_pair(PAIR_STRUCTURE))
    known = _overview_memory_known(mem)
    lines = [
        ("Used %s / %s · %.1f%%" % (human_bytes(mem["used"]), human_bytes(mem["total"]),
                                   ram_percent(dict(mem))) if known else "RAM CHECKING"),
        "Swap %s / %s" % (_overview_memory_bytes(mem, "swap_used"),
                          _overview_memory_bytes(mem, "swap_total")),
        "Comp %s · cache %s" % (_overview_memory_bytes(mem, "compressed"),
                               _overview_memory_bytes(mem, "cached")),
        "App %s · wired %s" % (_overview_memory_bytes(mem, "app"),
                              _overview_memory_bytes(mem, "wired")),
        "Available " + _overview_memory_bytes(mem, "available"),
        "RAM 60s " + sparkline(history.ram, max(1, w - 12)),
    ]
    for index, line in enumerate(lines[:max(0, h - 2)]):
        _body_add(screen, y + 1 + index, x + 2, line, w - 4,
                  color_pair(PAIR_INFO if index == 0 and known else PAIR_PRIMARY))


def _draw_overview_io(
    screen: curses.window, y: int, x: int, h: int, w: int,
    frame: HealthFrame, disk: Optional[DiskInfo], activity: Optional[DiskActivity],
    net_down: Optional[float], net_up: Optional[float], history: MetricHistory,
) -> None:
    """Always show capacity, transfer activity, and both network directions."""

    draw_box(screen, y, x, h, w, "STORAGE + I/O", color_pair(PAIR_STRUCTURE))
    storage_view = frame.source_views[SourceId.DISK_CAPACITY]
    capacity = frame.disk_capacity
    if capacity is not None:
        free, total, label = capacity.free_bytes, capacity.total_bytes, capacity.volume_name
    elif (disk is not None and disk.label not in ("checking", "pending")
          and storage_view.last_valid_revision is not None):
        free, total, label = disk.free, disk.total, disk.label
    else:
        free = total = None
        label = ""
    storage_line = ("Free %s / %s" % (human_bytes(free), human_bytes(total))
                    if free is not None and storage_view.status is SourceViewStatus.LIVE
                    else "Storage " + source_view_text(storage_view))
    if activity is None or activity.source in ("checking", "pending"):
        disk_line = "Disk I/O CHECKING"
    elif activity.source == "unavailable":
        disk_line = "Disk I/O UNAVAILABLE"
    elif activity.sample_age > DISK_ACTIVITY_REFRESH_SECONDS * 2:
        disk_line = "Disk I/O STALE · %.0fs old" % activity.sample_age
    else:
        disk_line = "Disk %s/s · %.0f IOPS" % (human_bytes(activity.bytes_per_sec), activity.iops)
    network_ready = net_down is not None and net_up is not None and net_down >= 0 and net_up >= 0
    if network_ready:
        network_line = "Net %s/s down · %s/s up" % (human_bytes(net_down), human_bytes(net_up))
    elif net_down is not None and net_up is not None and (net_down < 0 or net_up < 0):
        network_line = "Net UNAVAILABLE"
    else:
        network_line = "Net CHECKING"
    detail = ("%s · %.1f%% used" % (label, (total - free) / total * 100.0)
              if total is not None and total > 0 else "Capacity awaiting a sample")
    if storage_view.status is not SourceViewStatus.LIVE and free is not None:
        detail = "Last good: %s free" % human_bytes(free)
    lines = [storage_line, disk_line, network_line, detail,
             "Net 60s " + sparkline(history.net, max(1, w - 12), None),
             "Used% 60s " + sparkline(history.disk, max(1, w - 14))]
    for index, line in enumerate(lines[:max(0, h - 2)]):
        _body_add(screen, y + 1 + index, x + 2, line, w - 4,
                  color_pair(PAIR_INFO if index in (1, 2) else PAIR_PRIMARY))


def draw_overview_body(
    screen: curses.window, body_y: int, body_h: int, width: int,
    frame: HealthFrame, history: MetricHistory, rows: Sequence[ProcessRow],
    sort_mode: str, sensor: Optional[SensorInfo] = None,
    *, static: Optional[Mapping[str, Any]] = None,
    config: Optional[HudConfig] = None, phase: int = 0,
    per_core: Sequence[float] = (), mem: Optional[Mapping[str, float]] = None,
    battery: Optional[BatteryInfo] = None, disk: Optional[DiskInfo] = None,
    activity: Optional[DiskActivity] = None, net_down: Optional[float] = None,
    net_up: Optional[float] = None, scroll: int = 0,
) -> int:
    """The complete daily dashboard, with compact summaries and richer wide panels."""

    config = config or HudConfig()
    static, mem = static or {}, mem or {}
    sensor = sensor or default_sensor_info()
    wide = width >= 118 and body_y + body_h + 1 >= 34
    _draw_overview_quick_stats(screen, body_y, width, frame, history, static,
                             mem, battery, sensor, config, phase, wide)
    y, remaining = body_y + 7, body_h - 7
    empty_message = process_empty_message(frame, config)
    title = "TOP PROCESSES · " + sort_mode
    if not wide:
        left_w = (width - 3) // 2
        right_x, right_w = left_w + 2, width - left_w - 3
        metric_h = min(9, max(5, remaining // 2))
        _draw_overview_memory(screen, y, 1, metric_h, left_w, mem, history)
        _draw_overview_io(screen, y, right_x, metric_h, right_w, frame, disk,
                          activity, net_down, net_up, history)
        process_h = remaining - metric_h
        effective = _clamped_process_scroll(rows, process_h, scroll)
        draw_process_panel(screen, y + metric_h, 1, process_h, width - 2,
                           rows, effective, sort_mode, title, empty_message=empty_message,
                           identity_only=True)
        return effective
    available = width - 4
    left_w, center_w = available // 3, available // 3
    right_w = available - left_w - center_w
    center_x, right_x = left_w + 2, left_w + center_w + 3
    cpu_h = max(13, remaining * 3 // 5)
    memory_h = max(10, min(12, remaining // 2))
    if per_core and frame.source_views[SourceId.CPU_USAGE].status is SourceViewStatus.LIVE:
        draw_cpu_panel(screen, y, 1, cpu_h, left_w, list(per_core), dict(static), history, 0)
    else:
        draw_box(screen, y, 1, cpu_h, left_w, "CPU WORKER CORES", color_pair(PAIR_STRUCTURE))
        _body_add(screen, y + 1, 3,
                  "CPU " + frame.source_views[SourceId.CPU_USAGE].status.value,
                  left_w - 4, color_pair(PAIR_SECONDARY))
        _body_add(screen, y + 2, 3, "Per-core readings unavailable", left_w - 4,
                  color_pair(PAIR_SECONDARY))
    _draw_overview_io(screen, y + cpu_h, 1, remaining - cpu_h, left_w,
                      frame, disk, activity, net_down, net_up, history)
    if (_overview_memory_known(mem)
            and all(_overview_memory_bytes(mem, key) != "--" for key in
                    ("available", "cached", "app", "wired", "compressed",
                     "swap_used", "swap_total"))):
        draw_memory_panel(screen, y, center_x, memory_h, center_w, dict(mem), history, 0)
    else:
        _draw_overview_memory(screen, y, center_x, memory_h, center_w, mem, history)
    if battery is not None:
        draw_thermal_panel(screen, y + memory_h, center_x, remaining - memory_h,
                           center_w, battery, sensor, 0, frame.thermal)
    else:
        draw_box(screen, y + memory_h, center_x, remaining - memory_h, center_w,
                 "THERMAL", color_pair(PAIR_STRUCTURE))
        _body_add(screen, y + memory_h + 1, center_x + 2,
                  "CPU %s · GPU %s" % (format_temp(sensor.cpu_temp_c).strip()
                                       if sensor.cpu_temp_c is not None else "--",
                                       format_temp(sensor.gpu_temp_c).strip()
                                       if sensor.gpu_temp_c is not None else "--"),
                  center_w - 4, color_pair(PAIR_PRIMARY))
    effective = _clamped_process_scroll(rows, remaining, scroll)
    draw_process_panel(screen, y, right_x, remaining, right_w,
                       rows, effective, sort_mode, title, empty_message=empty_message,
                       identity_only=True)
    return effective


def draw_insights_body(
    screen: curses.window, y: int, h: int, width: int, frame: HealthFrame,
    mem: Mapping[str, float], vm_view: Optional[VmRateView],
    cpu_samples: Sequence[tuple[float, float]], history: MetricHistory,
) -> None:
    """Descriptive measured context, kept separate from the decision policy."""

    left_w = (width - 3) // 2
    right_x = left_w + 2
    right_w = width - right_x - 1
    draw_box(screen, y, 1, h, left_w, "CPU · RECENT 60s", color_pair(PAIR_STRUCTURE))
    draw_box(screen, y, right_x, h, right_w, "MEMORY MECHANICS", color_pair(PAIR_STRUCTURE))
    summary = (summarize_cpu_samples(cpu_samples, frame.captured_mono_s)
               if frame.source_views[SourceId.CPU_USAGE].decision_ready else None)
    left = ["Descriptive · sampled observations"]
    if summary is None:
        cpu_view = frame.source_views[SourceId.CPU_USAGE]
        left += ["CHECKING · collecting samples" if cpu_view.decision_ready else
                 cpu_view.status.value + " · recent samples unavailable"]
    else:
        left += ["Mean    %.1f%%" % summary.mean_percent,
                 "Peak    %.1f%%" % summary.peak_percent,
                 "Samples %d · span %.1fs" % (summary.sample_count, summary.coverage_s)]
    left += ["", sparkline(history.cpu, max(1, left_w - 4)), "",
             "Aggregate CPU capacity: 0–100%", "Process CPU: 100% = one core",
             "Averages describe observed samples.", "Health uses its own 15s window."]
    rates = vm_view.rates if vm_view is not None else None
    status = vm_view.status.value if vm_view is not None else "CHECKING"
    right = ["%s · live page-counter deltas" % status]
    if rates is None:
        right += [vm_view.detail if vm_view is not None else "Waiting for two valid samples",
                  "Missing data never becomes zero."]
    else:
        right += ["Compress     %8.1f pages/s" % rates.compressions_per_s,
                  "Decompress   %8.1f pages/s" % rates.decompressions_per_s,
                  "Swap in      %8.1f pages/s" % rates.swapins_per_s,
                  "Swap out     %8.1f pages/s" % rates.swapouts_per_s,
                  "Sample window %.2fs" % rates.window_s]
    right += ["", "Compressed footprint " + human_bytes(mem.get("compressed", 0))
              if "compressed" in mem else "Compressed footprint unavailable",
              "Swap allocated " + human_bytes(mem.get("swap_used", 0))
              if "swap_used" in mem else "Swap allocation unavailable",
              "", "Pages/s measures VM activity.", "It is not physical disk throughput.",
              "macOS pressure drives health."]
    for x, w, lines in ((3, left_w - 4, left), (right_x + 2, right_w - 4, right)):
        for index, line in enumerate(lines[:max(0, h - 2)]):
            _body_add(screen, y + 1 + index, x, line, w,
                      color_pair(PAIR_INFO if index in (1, 2, 3, 4) else PAIR_SECONDARY))


EXPLORE_OPTIONS = (
    ("balanced", "1", "Overview", "All key readings and top processes"),
    ("compute", "2", "CPU & memory", "Worker cores, RAM and busy processes"),
    ("thermals", "3", "Temperatures & fans", "Sensors, fan speed and power"),
    ("io", "4", "Storage & traffic", "Free space, disk activity and network rates"),
    ("processes", "5", "Processes", "Search, sort and scroll running processes"),
    ("cinema", "6", "Cinema", "Wide dashboard · needs 118×34"),
    ("insights", "7", "Memory activity", "CPU trends, compression and swap rates"),
    ("devices", "8", "Connected devices", "USB, Thunderbolt and displays"),
    ("network", "9", "Network", "Interfaces, addresses, route and DNS"),
    ("connections", "0", "TCP connections", "Listeners, peers and connection states"),
    ("system", "i", "This Mac", "Hardware, macOS and battery details"),
)
def _inventory_probe(argv: Sequence[str], *, timeout_s: float,
                     max_output_bytes: int,
                     cancel_event: Optional[threading.Event] = None) -> ProbeResult:
    """Adapt bounded command outcomes without exposing stderr or argv."""

    if cancel_event is not None and cancel_event.is_set():
        return ProbeResult(status=InventoryStatus.ERROR, detail="inventory closed")
    options = {"cancel_event": cancel_event} if cancel_event is not None else {}
    result = run_structured_command(argv, "inventory", timeout_s,
                                    max_output_bytes, 16 * 1024, **options)
    statuses = {
        AttemptOutcome.SUCCESS: InventoryStatus.LIVE,
        AttemptOutcome.TIMEOUT: InventoryStatus.TIMEOUT,
        AttemptOutcome.PERMISSION_DENIED: InventoryStatus.PERMISSION_DENIED,
        AttemptOutcome.MISSING_DEPENDENCY: InventoryStatus.MISSING,
        AttemptOutcome.UNSUPPORTED: InventoryStatus.UNSUPPORTED,
    }
    status = statuses.get(result.outcome, InventoryStatus.ERROR)
    # Some commands report a proved denial as a nonzero exit. Its raw text is
    # examined only here and never projected into inventory or UI records.
    if result.failure is FailureCode.PERMISSION_DENIED or (
            result.outcome is AttemptOutcome.COMMAND_ERROR
            and re.search(r"permission denied|operation not permitted|must be root",
                          result.stderr, re.IGNORECASE)):
        status = InventoryStatus.PERMISSION_DENIED
    return ProbeResult(result.stdout if status is InventoryStatus.LIVE else "", status,
                       "" if status is InventoryStatus.LIVE else "bounded macOS probe unavailable")


INVENTORY_COLLECTORS = {
    "devices": collect_devices, "network": collect_network,
    "connections": collect_connections,
}


def inventory_lines(mode: str, result: Any) -> list[str]:
    """Readable row projection over structured, privacy-limited native facts."""

    if mode == "devices":
        return ["%s · %s" % (item.kind, item.name) +
                (" | " + " · ".join(value for value in
                 (item.vendor, item.product_id, item.speed, item.parent, item.port) if value)
                 if any((item.vendor, item.product_id, item.speed, item.parent, item.port)) else "")
                for item in result.items]
    if mode == "network":
        lines = ["IPv4 default route · %s · interface %s [%s]" %
                 (result.gateway or "unknown", result.default_interface or "unknown",
                  result.route_status.value),
                 "DNS · %s [%s]" % (", ".join(result.dns_servers) or "none reported",
                                    result.dns_status.value),
                 "Hardware port mapping [%s]" % result.hardware_status.value, ""]
        for item in result.items:
            lines.append("%s · %s · %s · MTU %s" %
                         (item.name, " · ".join(value for value in (item.hardware_port, item.medium) if value) or "interface",
                          item.state, item.mtu if item.mtu is not None else "unknown"))
            addresses = ["IPv4 " + value for value in item.ipv4]
            addresses += ["IPv6 " + value for value in item.ipv6]
            if item.mac:
                addresses.append("MAC " + item.mac)
            lines.append("  " + (" · ".join(addresses) or "No addresses reported"))
        return lines
    if mode == "connections":
        return ["%-12s %s  →  %s · Rx/Tx queue bytes %s/%s" %
                (item.state, item.local, item.remote, item.recv_queue, item.send_queue)
                for item in result.items]
    raise ValueError("unknown inventory view")


def draw_inventory_body(screen: curses.window, mode: str, y: int, h: int,
                        width: int, result: Any, age_s: float, error: str,
                        scroll: int, retained: bool = False,
                        *, config: Optional[HudConfig] = None) -> int:
    title = {"devices": "CONNECTED DEVICES", "network": "NETWORK ENVIRONMENT",
             "connections": "TCP CONNECTIONS"}[mode]
    draw_box(screen, y, 1, h, width - 2, title, color_pair(PAIR_STRUCTURE))
    if result is None:
        lines = ["ERROR · collection unavailable" if error else "CHECKING · collecting bounded macOS inventory"]
        status = "ERROR" if error else "CHECKING"
    else:
        query = config.search_query if config is not None else ""
        connection_state = config.connection_filter if config is not None and mode == "connections" else "all"
        filtered = filter_inventory(result, query, connection_state=connection_state)
        summary = summarize_inventory(result)
        status = "ERROR" if error else ("STALE" if age_s > 30.0 and
                 result.status is InventoryStatus.LIVE else result.status.value)
        lines = inventory_lines(mode, filtered)
        if not lines:
            if result.status is not InventoryStatus.LIVE and not retained:
                lines = []
            else:
                lines = ["No matches · / edit search · Esc clear" if query or connection_state != "all"
                         else result.detail or "No matching records reported"]
        if result.status is not InventoryStatus.LIVE:
            lines.insert(0, result.detail or "Inventory unavailable")
        if mode == "connections":
            totals = "%d/%d matches · totals: %d listeners · %d connected · %d other" % (
                len(filtered.items), summary.total_count, summary.listeners,
                summary.established, summary.other_connections)
        elif mode == "network":
            totals = "%d active · %d link unknown · %d/%d interfaces" % (
                summary.active_interfaces, summary.unknown_link_interfaces,
                len(filtered.items), summary.total_count)
        else:
            totals = "%d/%d devices · %s" % (len(filtered.items), summary.total_count,
                      " · ".join("%s %d" % pair for pair in summary.kind_counts))
        if result.status is not InventoryStatus.LIVE and not retained:
            totals = "Counts unavailable · no valid list"
    if result is not None and status == "LIVE" and not result.complete:
        status = "PARTIAL"
    _body_add(screen, y + 1, 3,
              "%s · observed locally · %s" % (status,
               "retained sample · %.0fs" % age_s if result is not None and
               (retained or result.status is InventoryStatus.LIVE and (error or age_s > 30.0))
               else "read-only"),
              width - 6, color_pair(PAIR_INFO if status == "LIVE" else PAIR_SECONDARY))
    _body_add(screen, y + 2, 3, totals if result is not None else "Discovery runs in the background",
              width - 6, color_pair(PAIR_INFO))
    # Wrap at display-cell boundaries so narrow viewports retain every field.
    wrapped = []
    for line in lines:
        projected = semantic_text(line, unicode_ok())
        while display_cell_width(projected) > width - 6:
            segment = truncate_display_cells(projected, width - 8)
            if not segment:
                break
            wrapped.append(segment)
            projected = "  " + projected[len(segment):]
        wrapped.append(projected)
    lines = wrapped
    available = max(0, h - 6)
    effective = max(0, min(scroll, max(0, len(lines) - available)))
    for index, line in enumerate(lines[effective:effective + available]):
        _body_add(screen, y + 3 + index, 3, line, width - 6, color_pair(PAIR_PRIMARY))
    note = ("Protected sockets may be hidden" if mode == "connections" else
            "No connectivity test or network changes" if mode == "network" else
            "Reported topology · serial numbers omitted")
    limited = " · TRUNCATED" if result is not None and result.truncated else ""
    _body_add(screen, y + h - 2, 3, "%d–%d / %d%s · %s" %
              (effective + 1 if lines else 0, min(len(lines), effective + available),
               len(lines), limited, note), width - 6, color_pair(PAIR_SECONDARY))
    return effective


def draw_explore_menu(screen: curses.window, config: HudConfig) -> None:
    height, width = screen.getmaxyx()
    w, h = min(70, width - 6), len(EXPLORE_OPTIONS) + 5
    y, x = max(3, (height - h) // 2), (width - w) // 2
    fill_rect(screen, y, x, h, w, color_pair(PAIR_PRIMARY))
    draw_box(screen, y, x, h, w, "ALL TOOLS", color_pair(PAIR_STRUCTURE))
    for index, (_mode, key, label, description) in enumerate(EXPLORE_OPTIONS):
        selected = index == config.menu_index
        _rect_add(screen, y + 2 + index, x + 3,
                  (("› " if unicode_ok() else "> ") if selected else "  ") + key + "  " +
                  "%-21s %s" % (label, description), w - 6,
                  color_pair(PAIR_INFO if selected else PAIR_PRIMARY) |
                  (curses.A_BOLD if selected else 0))
    _rect_add(screen, y + h - 2, x + 3, "↑/↓ select · Enter open · 1–0/i jump · Esc close", w - 6,
              color_pair(PAIR_SECONDARY))


def draw_system_details_body(screen: curses.window, y: int, h: int, width: int,
                             static: Mapping[str, Any], mem: Mapping[str, float],
                             battery: BatteryInfo, sensor: SensorInfo) -> None:
    """Present already collected Mac details without starting another probe."""

    left_w = (width - 3) // 2
    right_x = left_w + 2
    right_w = width - right_x - 1
    draw_box(screen, y, 1, h, left_w, "THIS MAC", color_pair(PAIR_STRUCTURE))
    uptime = static.get("uptime_seconds")
    total = mem.get("total")
    lines = [
        str(static.get("chip") or "Hardware CHECKING"),
        "Model: " + str(static.get("model") or "CHECKING"),
        "Identifier: " + str(static.get("model_id") or "not reported"),
        "System: " + str(static.get("os") or "CHECKING"),
        "Build: " + str(static.get("build") or "not reported"),
        "Kernel: Darwin " + str(static.get("kernel") or "CHECKING"),
        "CPU cores: " + str(static.get("cpu_cores") or static.get("logical_cpu") or "CHECKING"),
        "Memory: " + (human_bytes(total) if total is not None else str(static.get("memory") or "CHECKING")),
        "GPU cores: " + str(static.get("gpu_core_count") or "not reported"),
        "Uptime: " + (format_uptime(int(uptime)) if uptime is not None else "CHECKING"),
        "", "8 Devices · 9 Network · 0 Connections",
    ]
    for index, line in enumerate(lines[:h - 2], 1):
        _body_add(screen, y + index, 3, line, left_w - 4, color_pair(PAIR_PRIMARY))
    power_h = min(11, max(6, h // 2))
    draw_battery_panel(screen, y, right_x, power_h, right_w, battery, 0)
    draw_gpu_panel(screen, y + power_h, right_x, h - power_h, right_w,
                   dict(static), sensor, 0)


def draw_mode_body(
    screen: curses.window, mode: str, body_y: int, body_h: int, width: int,
    frame: HealthFrame, per_core: Sequence[float], static: Mapping[str, Any],
    mem: Mapping[str, float], battery: BatteryInfo, sensor: SensorInfo,
    disk: DiskInfo, activity: DiskActivity, net_down: float, net_up: float,
    history: MetricHistory, rows: Sequence[ProcessRow], scroll: int,
    sort_mode: str, phase: int, *, config: Optional[HudConfig] = None,
    vm_view: Optional[VmRateView] = None,
    cpu_samples: Sequence[tuple[float, float]] = (),
    inventory: Any = None, inventory_age: float = 0.0, inventory_error: str = "", inventory_retained: bool = False,
) -> int:
    """Dispatch one deterministic body inside the global header/footer geometry."""

    if mode not in LAYOUTS:
        raise ValueError("unknown display mode")
    if (type(body_y) is not int or type(body_h) is not int or type(width) is not int
            or body_y < 0 or body_h < 0 or width < 2
            or body_y + body_h + 1 < 22 or width < 76):
        raise ValueError("body geometry must describe a supported viewport")
    body_w = width - 2
    if body_w < 8:
        return 0
    per_core_values = list(per_core)
    static_values = dict(static)
    memory_values = dict(mem)
    row_values = rows
    empty_processes = process_empty_message(frame, config)
    operational_phase = 0
    if mode == "balanced":
        return draw_overview_body(
            screen, body_y, body_h, width, frame, history, row_values, sort_mode,
            sensor, static=static_values, config=config, phase=phase,
            per_core=per_core_values, mem=memory_values, battery=battery, disk=disk,
            activity=activity, net_down=net_down, net_up=net_up, scroll=scroll,
        )
    if mode == "system":
        draw_system_details_body(screen, body_y, body_h, width, static_values,
                                 memory_values, battery, sensor)
        return 0
    if mode == "insights":
        draw_insights_body(screen, body_y, body_h, width, frame, memory_values,
                           vm_view, cpu_samples, history)
        return 0
    if mode in INVENTORY_COLLECTORS:
        return draw_inventory_body(screen, mode, body_y, body_h, width,
                                   inventory, inventory_age, inventory_error, scroll, inventory_retained,
                                   config=config)
    if mode == "cinema":
        if width < 118 or body_y + body_h + 1 < 34 or body_h < 7:
            raise ValueError("Cinema requires 118×34")
        draw_signature(screen, body_y, width, static_values, config or HudConfig(), phase)
        operational_y = body_y + 7
        operational_h = body_h - 7
        left_w, center_w, right_w = wide_column_widths(width, "cinema")
        left_x = 1
        center_x = left_x + left_w + 1
        right_x = center_x + center_w + 1
        left_top = max(3, operational_h // 2)
        center_top = max(3, operational_h * 3 // 5)
        draw_thermal_panel(screen, operational_y, left_x, left_top, left_w,
                           battery, sensor, phase, frame.thermal)
        draw_io_panel(screen, operational_y + left_top, left_x,
                      operational_h - left_top, left_w, disk, activity,
                      net_down, net_up, history, phase)
        draw_cpu_panel(screen, operational_y, center_x, center_top, center_w,
                       per_core_values, static_values, history, phase)
        draw_memory_panel(screen, operational_y + center_top, center_x,
                          operational_h - center_top, center_w, memory_values,
                          history, phase)
        effective = _clamped_process_scroll(row_values, operational_h, scroll)
        draw_process_panel(screen, operational_y, right_x, operational_h,
                           right_w, row_values, effective, sort_mode,
                           empty_message=empty_processes)
        return effective

    gap = 1
    left_w = (body_w - gap) // 2
    right_w = body_w - gap - left_w
    left_x = 1
    right_x = left_x + left_w + gap
    if mode == "compute":
        cpu_h = min(max(3, 10), max(3, body_h - 3))
        memory_h = body_h - cpu_h
        draw_cpu_panel(screen, body_y, left_x, cpu_h, left_w, per_core_values,
                       static_values, history, operational_phase)
        _body_add(
            screen, body_y + 4, left_x + 2,
            "15s  %s" % sparkline(list(history.cpu)[-15:], max(8, left_w - 10), 100.0),
            left_w - 4, color_pair(PAIR_STRUCTURE),
        )
        draw_memory_panel(screen, body_y + cpu_h, left_x, memory_h, left_w,
                          memory_values, history, operational_phase)
        effective = _clamped_process_scroll(row_values, body_h, scroll)
        draw_process_panel(screen, body_y, right_x, body_h, right_w, row_values,
                           effective, "CPU", "TOP CPU", empty_message=empty_processes)
        return effective
    if mode == "thermals":
        draw_thermal_panel(screen, body_y, left_x, body_h, left_w,
                           battery, sensor, operational_phase, frame.thermal)
        draw_sensors_panel(screen, body_y, right_x, body_h, right_w,
                           battery, sensor, operational_phase)
        return 0
    if mode == "io":
        draw_io_panel(screen, body_y, 1, body_h, body_w, disk, activity,
                      net_down, net_up, history, operational_phase)
        return 0
    effective = _clamped_process_scroll(row_values, body_h, scroll)
    draw_process_panel(screen, body_y, 1, body_h, body_w, row_values,
                       effective, sort_mode, empty_message=empty_processes)
    return effective


def set_message(config: HudConfig, text: str, ttl: float = 3.0) -> None:
    config.message = text
    config.message_until = time.monotonic() + ttl


def shortcut_status(width: int, config: Optional[HudConfig] = None) -> str:
    _projection_width(width)
    if config is not None and config.search_editing:
        text = "Find: %s_ · Enter apply · Esc clear" % truncate_display_cells(config.search_query, width - 40)
    elif config is not None and config.search_query:
        text = "Find: %s · / edit · Esc clear · Tab · d tools · q quit" % truncate_display_cells(config.search_query, width - 56)
    elif config is not None and config.layout_name == "connections":
        text = "Tab · d tools · / find · f %s · Esc clear · r refresh · q quit" % config.connection_filter
    else:
        text = "1–5 views · d all tools · Tab/Shift-Tab · / find · h help · q quit"
    return semantic_text(text, unicode_ok())


def draw_help_overlay(screen: curses.window, config: HudConfig, sensor: SensorInfo) -> None:
    height, width = screen.getmaxyx()
    w = min(72, max(48, width - 8))
    h = min(21, height - 4)
    y = max(2, (height - h) // 2)
    x = max(2, (width - w) // 2)
    fill_rect(screen, y, x, h, w, color_pair(PAIR_PRIMARY))
    draw_box(screen, y, x, h, w, "SHORTCUTS", color_pair(PAIR_STRUCTURE) | curses.A_BOLD)
    if sensor.thermal_warning in ("checking", "pending"):
        unlock_state = "checking"
    elif not sensor.privileged_locked:
        unlock_state = "online"
    elif sensor.raw_hint.startswith("smc"):
        unlock_state = "SMC online / power locked"
    else:
        unlock_state = "locked"
    lines = [
        "Tab/Shift-Tab next/previous view · l next",
        "1–0 / i       choose view · d all tools",
        "/             find processes or inventory",
        "Enter / Esc   apply / clear search",
        "Backspace/Ctrl-U edit / erase query",
        "f             TCP: all / listeners / established / other",
        "arrows/j/k    scroll processes or inventory",
        "PgUp/PgDn/Home/End  page or jump to either end",
        "m             sort CPU / memory / GPU",
        "r             refresh telemetry and selected inventory",
        "t / s         theme / sculpted, outline, ASCII artwork",
        "a             motion: off / calm / vivid",
        "Esc / h / ?   close this panel · q quit",
    ]
    if width >= 118 and height >= 34:
        lines.append("6  Cinema     select wide Cinema mode")
    if sensor.privileged_locked:
        lines.append("u             unlock advanced macOS sensors with sudo")
    lines.extend([
        f"sensors: {unlock_state} · theme: {config.theme_name} · layout: {config.layout_name}",
        COPYRIGHT_TEXT,
    ])
    unicode_enabled = unicode_ok()
    for index, line in enumerate(lines[: h - 2], start=1):
        attr = color_pair(PAIR_PRIMARY)
        if line.startswith(("theme:", "sensors:")) or line == COPYRIGHT_TEXT:
            attr = color_pair(PAIR_SECONDARY) | curses.A_BOLD
        safe_add(
            screen, y + index, x + 2,
            truncate_display_cells(semantic_text(line, unicode_enabled), w - 4),
            attr,
        )


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
    for pair_id, foreground in {**theme, **MASCOT_COLORS}.items():
        try:
            curses.init_pair(pair_id, color_id(foreground), background)
        except (curses.error, ValueError):
            pass


def draw_small_screen(screen: curses.window, height: int, width: int) -> None:
    unicode_enabled = unicode_ok()
    safe_add(
        screen, 1, 2, "PorkyHUD needs a larger terminal.",
        color_pair(PAIR_STRUCTURE) | curses.A_BOLD,
    )
    safe_add(
        screen, 3, 2,
        semantic_text(
            "Current: %d×%d. Resize to at least 76×22." % (width, height),
            unicode_enabled,
        ),
        color_pair(PAIR_PRIMARY),
    )
    safe_add(screen, 5, 2, "Press q to quit.", color_pair(PAIR_PRIMARY))


def draw_decision_header(
    screen: curses.window, frame: HealthFrame, config: HudConfig,
    now_mono_s: float,
) -> int:
    """Draw rows zero and one without owning erase or refresh."""

    _height, width = screen.getmaxyx()
    lines = decision_header_lines(
        frame, config.layout_name, width, now_mono_s,
    )
    unicode_enabled = unicode_ok()
    level = frame.assessment.severity.value
    if _is_bootstrap_checking(frame) or _is_cpu_baseline(frame):
        level = "checking"
    projected = tuple(semantic_text(line, unicode_enabled) for line in lines)
    if any(display_cell_width(line) > width - 1 for line in projected):
        raise ValueError("projected decision header exceeds the viewport")
    safe_add(screen, 0, 0, projected[0], read_attr(level))
    safe_add(screen, 1, 0, projected[1], color_pair(PAIR_PRIMARY))
    return 2


def draw_evidence_ribbon(
    screen: curses.window, y: int, frame: HealthFrame, now_mono_s: float,
) -> int:
    """Draw the one-row core evidence ribbon without erase or refresh."""

    if type(y) is not int or y < 0:
        raise ValueError("ribbon row must be a nonnegative int")
    _height, width = screen.getmaxyx()
    unicode_enabled = unicode_ok()
    segments, separator = _evidence_ribbon_projection(frame, width, now_mono_s)
    projected_separator = semantic_text(separator, unicode_enabled)
    projected = tuple(
        (semantic_text(text, unicode_enabled), role)
        for text, role in segments
    )
    line = projected_separator.join(text for text, _role in projected)
    if display_cell_width(line) > width - 1:
        raise ValueError("projected evidence ribbon exceeds the viewport")
    x = 0
    for index, (text, role) in enumerate(projected):
        suffix = projected_separator if index + 1 < len(projected) else ""
        segment = text + suffix
        safe_add(screen, y, x, segment, color_pair(role) | curses.A_BOLD)
        x += display_cell_width(segment)
    return y + 1


def draw_initial_shell(screen: curses.window, config: HudConfig) -> int:
    """Draw the effect-free progressive startup shell before any collection begins."""

    del config
    height, width = screen.getmaxyx()
    screen.erase()
    if height < 22 or width < 76:
        draw_small_screen(screen, height, width)
        return 3
    _projection_width(width)
    unicode_enabled = unicode_ok()
    lines = (
        "PorkyHUD · 1 OVERVIEW · CHECKING 0/4 core sources",
        "Loading system readings.",
        "CPU CHECKING · MEM CHECKING · THERM CHECKING · DISK CHECKING",
    )
    projected = tuple(semantic_text(line, unicode_enabled) for line in lines)
    if any(display_cell_width(line) > width - 1 for line in projected):
        raise ValueError("initial shell does not fit the supported viewport")
    safe_add(screen, 0, 0, projected[0], read_attr("checking"))
    safe_add(screen, 1, 0, projected[1], color_pair(PAIR_PRIMARY))
    safe_add(screen, 2, 0, projected[2], color_pair(PAIR_INFO) | curses.A_BOLD)
    return 3


def _frame_render_key(frame: HealthFrame) -> Tuple[Any, ...]:
    """Return semantic frame state without continuously advancing ages."""

    assessment = frame.assessment
    assessment_key = (
        assessment.severity, assessment.evidence_state, assessment.reason,
        assessment.evidence, assessment.active_rules,
        assessment.contributor_name, assessment.blockers,
        assessment.cpu_coverage_s,
    )
    source_key = tuple(
        (
            source_id, view.status, view.refreshing, view.decision_ready,
            view.last_valid_revision, view.failure,
        )
        for source_id, view in frame.source_views.items()
    )
    return (
        assessment_key, source_key, frame.cpu_percent,
        frame.memory_pressure, frame.thermal, frame.disk_capacity,
        frame.contributor, frame.origin, frame.decisive_triggered_mono_s,
    )


def _render_age_key(
    frame: HealthFrame, sensor: SensorInfo, activity: DiskActivity,
    now_mono_s: float,
) -> Tuple[Any, ...]:
    """Return only integer age buckets whose visible copy can change."""

    elapsed_s = max(0.0, now_mono_s - frame.captured_mono_s)

    def displayed_age(
        value: Optional[float], extra: float = 0.0,
    ) -> Optional[int]:
        if value is None:
            return None
        return int(math.floor(max(0.0, value + extra)))

    return (
        displayed_age(frame.assessment.evidence_age_s, elapsed_s),
        tuple(displayed_age(view.age_s, elapsed_s)
              for view in frame.source_views.values()),
        displayed_age(
            None if frame.decisive_triggered_mono_s is None
            else now_mono_s - frame.decisive_triggered_mono_s,
        ),
        displayed_age(sensor.sensor_sample_age_s),
        displayed_age(activity.sample_age),
    )


def hud(screen: curses.window, config: Optional[HudConfig] = None) -> None:
    """Own terminal-session cleanup even when input or drawing raises."""

    coordinators: list[LiveHealthCoordinator] = []
    inventory_jobs: list[tuple[AsyncPoller, threading.Event]] = []
    try:
        _hud_impl(screen, coordinators, config, inventory_jobs)
    finally:
        for _poller, cancellation in inventory_jobs:
            cancellation.set()
        try:
            if coordinators:
                coordinators[0].close()
        finally:
            if inventory_jobs:
                deadline = time.monotonic() + 1.5
                for poller, _cancellation in inventory_jobs:
                    poller.close(max(0.0, deadline - time.monotonic()))


def _hud_impl(
    screen: curses.window, coordinators: list[LiveHealthCoordinator],
    initial_config: Optional[HudConfig] = None,
    inventory_jobs: Optional[list[tuple[AsyncPoller, threading.Event]]] = None,
) -> None:
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
    config = initial_config or HudConfig()
    inventory_jobs = inventory_jobs if inventory_jobs is not None else []
    init_colors(config.theme_index)
    apply_dark_screen(screen)
    draw_initial_shell(screen, config)
    screen.refresh()

    coordinator = LiveHealthCoordinator()
    coordinators.append(coordinator)
    static: dict[str, Any] = {}

    def collect_live_static() -> dict[str, Any]:
        result = collect_static_info()
        result["boot_seconds"] = boot_seconds()
        return result

    static_poller: Optional[AsyncPoller] = None
    uptime_at_start = 0
    uptime_clock_start = time.monotonic()
    intro_started = uptime_clock_start
    per_core: list[float] = []
    mem: dict[str, float] = {}
    battery = BatteryInfo(None, False, "Power", "checking", "checking", False,
                          False, False, None, None, None, None, None, None, None,
                          None, None)
    disk = DiskInfo("checking", "/", 1, 0, 1)
    net_down: Optional[float] = None
    net_up: Optional[float] = None
    raw_rows: tuple[ProcessRow, ...] = ()
    rows: list[ProcessRow] = []
    cpu_rows: list[ProcessRow] = []
    rows_sort_mode = ""
    sort_mode = "CPU"
    scroll = 0
    sensor = default_sensor_info()
    latest_thermal: list[Optional[OsThermalState]] = [None]
    activity = DiskActivity()
    memory_poller = AsyncPoller(MEMORY_DETAIL_REFRESH_SECONDS, memory_detail, mem)
    swap_poller = AsyncPoller(SWAP_REFRESH_SECONDS, swap_stats)
    network_poller = AsyncPoller(NETWORK_REFRESH_SECONDS, network_bytes)
    battery_poller = AsyncPoller(BATTERY_REFRESH_SECONDS, battery_info, battery)
    sensor_poller = AsyncPoller(
        OPTIONAL_SENSOR_REFRESH_SECONDS,
        lambda: sensor_info(latest_thermal[0]),
        sensor,
    )
    disk_activity_poller = AsyncPoller(DISK_ACTIVITY_REFRESH_SECONDS, disk_activity, activity)
    history = MetricHistory()
    history_source_generation = coordinator.source_generation()
    last_history_cpu_revision: Optional[int] = None
    cpu_samples: deque[tuple[float, float]] = deque(maxlen=120)
    vm_state = VmRateState()
    vm_view = VmRateView()
    memory_sample_revision = 0
    network_sample_revision = 0
    memory_sample_completed = 0.0
    force_live = True
    force_memory = True
    force_swap = True
    force_network = True
    force_battery = True
    force_sensor = True
    force_disk_activity = True
    network_baseline: Optional[tuple[tuple[int, int], float]] = None
    last_render = 0.0
    descriptive_revision = 0
    last_render_state: Optional[tuple[Any, ...]] = None
    last_render_age_key: Optional[tuple[Any, ...]] = None
    inventory_pollers: dict[str, AsyncPoller] = {}
    inventory = None
    inventory_age = 0.0
    inventory_error = ""
    inventory_retained = False
    inventory_render_key = None
    force_inventory = False
    searched_rows: Optional[Sequence[ProcessRow]] = None
    searched_query: Optional[str] = None
    matching_rows: tuple[ProcessRow, ...] = ()
    process_search_cache = _ProcessSearchCache()

    def quit_hud() -> None:
        # The outer hud() finally owns the single aggregate close.
        return None

    while True:
        now = time.monotonic()
        height, width = screen.getmaxyx()
        key = read_hud_key(screen, config.search_editing)
        ui_changed = False
        if edit_search(config, key):
            scroll = 0
            ui_changed = True
            key = -1
        elif (key == 27 and config.search_query
              and not config.show_help and not config.show_menu):
            config.searches.pop(config.layout_name, None)
            scroll = 0
            ui_changed = True
            key = -1
        elif (key == 27 and config.layout_name == "connections" and config.connection_filter != "all"
              and not config.show_help and not config.show_menu):
            config.connection_filter = "all"
            scroll = 0
            ui_changed = True
            key = -1
        if key in (ord("q"), ord("Q")) or (
                key == 27 and not config.show_help and not config.show_menu):
            quit_hud()
            return
        if key != -1:
            config.intro_enabled = False
        controls_enabled = height >= 22 and width >= 76
        if not controls_enabled and key != 27:
            key = -1
        if config.show_menu:
            if key in (27, ord("d"), ord("D"), ord("h"), ord("?")):
                config.show_menu = False
            elif key in (curses.KEY_DOWN, ord("j")):
                config.menu_index = (config.menu_index + 1) % len(EXPLORE_OPTIONS)
            elif key in (curses.KEY_UP, ord("k")):
                config.menu_index = (config.menu_index - 1) % len(EXPLORE_OPTIONS)
            elif key in (10, 13, curses.KEY_ENTER):
                message = select_mode(config, EXPLORE_OPTIONS[config.menu_index][0], height, width)
                set_message(config, message)
                if message.endswith(" selected"):
                    config.show_menu = False
                    scroll = 0
            elif key >= 0 and chr(key).lower() in DIRECT_MODE_KEYS:
                message = select_mode(config, DIRECT_MODE_KEYS[chr(key).lower()], height, width)
                set_message(config, message)
                if message.endswith(" selected"):
                    config.show_menu = False
                    scroll = 0
            if key != -1:
                ui_changed = True
            key = -1
        if config.show_help:
            if key in (27, ord("h"), ord("H"), ord("?")):
                config.show_help = False
                set_message(config, "shortcut panel closed")
                ui_changed = True
        elif key in (ord("d"), ord("D")):
            config.show_menu = True
            config.menu_index = next(index for index, option in enumerate(EXPLORE_OPTIONS)
                                     if option[0] == config.layout_name)
            ui_changed = True
        elif key in (ord("h"), ord("H"), ord("?")):
            config.show_help = True
            set_message(config, "shortcut panel opened")
            ui_changed = True
        elif key in tuple(ord(value) for value in DIRECT_MODE_KEYS) + (ord("I"),):
            message = select_mode(
                config, DIRECT_MODE_KEYS[chr(key).lower()], height, width,
            )
            if message.endswith(" selected"):
                scroll = 0
            set_message(config, message)
            ui_changed = True
        elif key in (ord("t"), ord("T")):
            config.theme_index = (config.theme_index + 1) % len(THEMES)
            init_colors(config.theme_index)
            apply_dark_screen(screen)
            set_message(config, f"theme switched to {config.theme_name}")
            ui_changed = True
        elif key in (ord("s"), ord("S")):
            config.visual_style = VISUAL_STYLES[(VISUAL_STYLES.index(config.visual_style) + 1)
                                                % len(VISUAL_STYLES)]
            set_message(config, "visual style: " + config.visual_style)
            ui_changed = True
        elif key in (ord("l"), ord("L"), 9, curses.KEY_BTAB):
            set_message(config, cycle_mode(config, height, width, -1 if key == curses.KEY_BTAB else 1))
            scroll = 0
            ui_changed = True
        elif key == ord("/"):
            if config.layout_name in SEARCHABLE_MODES:
                config.search_editing = True
            else:
                set_message(config, "Search is available in Processes, Devices, Network and TCP")
            ui_changed = True
        elif key in (ord("f"), ord("F")) and config.layout_name == "connections":
            config.connection_filter = CONNECTION_FILTERS[(CONNECTION_FILTERS.index(config.connection_filter) + 1)
                                                         % len(CONNECTION_FILTERS)]
            scroll = 0
            set_message(config, "TCP filter: " + config.connection_filter)
            ui_changed = True
        elif key in (ord("a"), ord("A")):
            config.animation_mode = (config.animation_mode + 1) % 3
            labels = ["off", "calm", "vivid"]
            set_message(config, f"motion {labels[config.animation_mode]}")
            ui_changed = True
        elif key in (ord("u"), ord("U")) and sensor.privileged_locked:
            if unlock_privileged_sensors(screen):
                force_sensor = True
                set_message(config, "sensor access unlocked · refreshing")
            else:
                set_message(config, "advanced sensor unlock skipped")
            ui_changed = True
        elif key in (ord("m"), ord("M")):
            sort_modes = ["CPU", "MEM", "GPU"]
            sort_mode = sort_modes[(sort_modes.index(sort_mode) + 1) % len(sort_modes)]
            rows_sort_mode = ""
            scroll = 0
            set_message(config, f"process sort: {sort_mode}")
            ui_changed = True
        elif key in (curses.KEY_DOWN, ord("j")):
            scroll += 1
            ui_changed = True
        elif key in (curses.KEY_UP, ord("k")):
            scroll -= 1
            ui_changed = True
        elif key == curses.KEY_NPAGE:
            scroll += 10
            ui_changed = True
        elif key == curses.KEY_PPAGE:
            scroll -= 10
            ui_changed = True
        elif key == curses.KEY_HOME:
            scroll = 0
            ui_changed = True
        elif key == curses.KEY_END:
            scroll = 10**9
            ui_changed = True
        elif key in (ord("r"), ord("R")):
            force_live = True
            force_inventory = True
            force_memory = force_swap = force_network = True
            force_battery = force_sensor = force_disk_activity = True
            set_message(config, "refreshing live telemetry")
            ui_changed = True

        coordinator.observe_time(now, utc_now())
        current_source_generation = coordinator.source_generation()
        if current_source_generation != history_source_generation:
            history.clear()
            cpu_samples.clear()
            vm_state, vm_view = VmRateState(), VmRateView()
            network_baseline = None
            net_down = net_up = None
            last_history_cpu_revision = None
            history_source_generation = current_source_generation
        frame = coordinator.tick(now, force_live)
        latest_thermal[0] = frame.thermal
        per_core = list(coordinator.per_core())
        if frame.disk_capacity is not None:
            capacity = frame.disk_capacity
            disk = DiskInfo(capacity.volume_name.lower(), capacity.mount_point,
                            capacity.total_bytes, capacity.total_bytes - capacity.free_bytes,
                            capacity.free_bytes)
        sampled_rows = tuple(coordinator.process_rows(now))
        if sampled_rows != raw_rows:
            raw_rows = sampled_rows
            cpu_rows = sort_process_rows(raw_rows, "CPU")
            rows = (cpu_rows if sort_mode == "CPU"
                    else sort_process_rows(raw_rows, sort_mode))
            rows_sort_mode = sort_mode
        elif sort_mode != rows_sort_mode:
            rows = (cpu_rows if sort_mode == "CPU"
                    else sort_process_rows(raw_rows, sort_mode))
            rows_sort_mode = sort_mode

        if static_poller is None:
            static_poller = _start_static_discovery(collect_live_static, now)
        sampled_static, _static_age, _static_error = static_poller.snapshot(now)
        if sampled_static and sampled_static != static:
            static = sampled_static
            uptime_at_start = int(static.get("boot_seconds", uptime_at_start) or 0)
            uptime_clock_start = now - max(0.0, _static_age)
            descriptive_revision += 1
        memory_poller.tick(now, force_memory)
        memory_sample = memory_poller.sample_snapshot(now)
        sampled_memory = memory_sample.value
        if memory_sample.revision != memory_sample_revision:
            memory_sample_revision = memory_sample.revision
            memory_sample_completed = memory_sample.completed_mono_s
            try:
                counters = vm_counters_from_mapping(sampled_memory or {})
            except (TypeError, ValueError):
                counters = None
            vm_state, vm_view = update_vm_rates(
                vm_state, counters, memory_sample_completed,
                source_error=bool(memory_sample.error),
            )
            descriptive_revision += 1
        elif (memory_sample_completed and now - memory_sample_completed > 6.0
              and vm_view.status is InsightStatus.LIVE):
            vm_view = VmRateView(InsightStatus.STALE, None, "memory activity sample delayed")
            descriptive_revision += 1
        if sampled_memory and sampled_memory != mem:
            mem = sampled_memory
            descriptive_revision += 1
        swap_poller.tick(now, force_swap)
        sampled_swap, _swap_age, _swap_error = swap_poller.snapshot(now)
        if sampled_swap is not None:
            merged_memory = merge_memory_stats(mem, sampled_swap)
            if merged_memory != mem:
                mem = merged_memory
                descriptive_revision += 1
        network_poller.tick(now, force_network)
        network_sample = network_poller.sample_snapshot(now)
        if network_sample.revision != network_sample_revision:
            network_sample_revision = network_sample.revision
            if network_sample.error or network_sample.value is None:
                network_baseline = None
                sampled_rates = (-1.0, -1.0)
            else:
                try:
                    network_baseline, sampled_rates = _network_rate_update(
                        network_baseline, network_sample.value, network_sample.completed_mono_s,
                    )
                except (TypeError, ValueError):
                    network_baseline = None
                    sampled_rates = (-1.0, -1.0)
            next_rates = sampled_rates if sampled_rates is not None else (None, None)
            if next_rates != (net_down, net_up):
                net_down, net_up = next_rates
                descriptive_revision += 1
        elif (network_baseline is not None and now - network_baseline[1] > 5.0
              and (net_down, net_up) != (-1.0, -1.0)):
            network_baseline = None
            net_down = net_up = -1.0
            descriptive_revision += 1
        battery_poller.tick(now, force_battery)
        sampled_battery, _battery_age, _battery_error = battery_poller.snapshot(now)
        if sampled_battery is not None and sampled_battery != battery:
            battery = sampled_battery
            descriptive_revision += 1

        sensor_poller.tick(now, force_sensor)
        sampled_sensor, sensor_age, _sensor_error = sensor_poller.snapshot(now)
        if sampled_sensor is not None:
            candidate_sensor = replace(sampled_sensor, sensor_sample_age_s=sensor_age)
            if (replace(candidate_sensor, sensor_sample_age_s=0.0)
                    != replace(sensor, sensor_sample_age_s=0.0)):
                descriptive_revision += 1
            sensor = candidate_sensor
            coordinator.set_system_gpu_percent(sensor.gpu_active_percent)

        disk_activity_poller.tick(now, force_disk_activity)
        sampled_activity, activity_age, _activity_error = disk_activity_poller.snapshot(now)
        if sampled_activity is not None:
            candidate_activity = replace(sampled_activity, sample_age=activity_age)
            if (replace(candidate_activity, sample_age=0.0)
                    != replace(activity, sample_age=0.0)):
                descriptive_revision += 1
            activity = candidate_activity

        if config.layout_name in INVENTORY_COLLECTORS:
            mode = config.layout_name
            if mode not in inventory_pollers:
                collector = INVENTORY_COLLECTORS[mode]
                cancellation = threading.Event()
                def inventory_runner(argv, *, timeout_s, max_output_bytes,
                                     cancellation=cancellation):
                    return _inventory_probe(argv, timeout_s=timeout_s,
                                            max_output_bytes=max_output_bytes,
                                            cancel_event=cancellation)
                observation = [InventoryObservation()]
                def collect_observation(collector=collector, runner=inventory_runner,
                                        observation=observation):
                    result = collector(runner)
                    observation[0] = observe_inventory(observation[0], result, time.monotonic())
                    return observation[0]
                poller = AsyncPoller(
                    10.0 if mode == "connections" else 30.0, collect_observation,
                )
                inventory_pollers[mode] = poller
                inventory_jobs.append((poller, cancellation))
            poller = inventory_pollers[mode]
            poller.tick(now, force_inventory)
            observed, _inventory_attempt_age, inventory_error = poller.snapshot(now)
            inventory = None
            inventory_age = 0.0
            inventory_retained = False
            if isinstance(observed, InventoryObservation):
                latest = observed.latest
                inventory = latest
                if observed.last_valid is not None:
                    inventory_age = max(0.0, now - observed.last_valid_completed_mono_s)
                    inventory_retained = latest.status is not InventoryStatus.LIVE
                    inventory = (replace(observed.last_valid, status=latest.status,
                                         detail=latest.detail, complete=False)
                                 if inventory_retained else observed.last_valid)
            inventory_key = (mode, inventory, int(inventory_age), inventory_error, inventory_retained)
            if inventory_key != inventory_render_key:
                inventory_render_key = inventory_key
                descriptive_revision += 1
        force_inventory = False

        network_bps = (
            net_down + net_up
            if net_down is not None and net_up is not None
            and net_down >= 0.0 and net_up >= 0.0
            else 0.0
        )
        previous_history_revision = last_history_cpu_revision
        last_history_cpu_revision = _record_history_frame(
            history,
            frame,
            last_history_cpu_revision,
            per_core,
            mem,
            network_bps,
            disk,
        )
        if (last_history_cpu_revision != previous_history_revision
                and frame.cpu_percent is not None):
            age = frame.source_views[SourceId.CPU_USAGE].age_s or 0.0
            completed = max(0.0, frame.captured_mono_s - age)
            if not cpu_samples or completed > cpu_samples[-1][0]:
                cpu_samples.append((completed, frame.cpu_percent))
        force_live = False
        force_memory = False
        force_swap = False
        force_network = False
        force_battery = False
        force_sensor = False
        force_disk_activity = False

        # Collection can overlap a terminal resize. Reacquire one geometry for
        # mode normalization, render-state comparison, body layout, and footer.
        height, width = screen.getmaxyx()
        resized_message = normalize_mode_for_viewport(config, height, width)
        if resized_message:
            scroll = 0
            set_message(config, resized_message)
            ui_changed = True

        intro_active = config.intro_enabled and now - intro_started < 0.8
        active_message = (
            config.message if config.message and now < config.message_until else ""
        )
        render_state = (
            height, width, _frame_render_key(frame), descriptive_revision,
            config.theme_index, config.animation_mode, config.layout_index,
            config.show_help, config.show_menu, config.menu_index, config.visual_style, intro_active, active_message, sort_mode, scroll,
            config.search_editing, config.search_query, config.connection_filter,
        )
        render_age_key = _render_age_key(frame, sensor, activity, now)
        should_render = (
            last_render_state is None or render_state != last_render_state
            or ui_changed
        )
        if intro_active:
            should_render = should_render or now - last_render >= 0.08
        elif config.animation_mode == 0:
            should_render = should_render or render_age_key != last_render_age_key
        else:
            should_render = (
                should_render
                or now - last_render >= render_interval(config.animation_mode)
            )
        idle_delay = (0.01 if config.search_editing else
                      0.04 if intro_active or config.animation_mode == 2 else 0.10)
        if not should_render:
            time.sleep(idle_delay)
            continue
        last_render = now
        last_render_state = render_state
        last_render_age_key = render_age_key
        if height < 22 or width < 76:
            screen.erase()
            draw_small_screen(screen, height, width)
            screen.refresh()
            time.sleep(idle_delay)
            continue

        if intro_active:
            # A single shallow turn resolves to the exact motion-off rest pose.
            phase = int((now - intro_started) / 0.8 * 96)
        elif config.animation_mode == 0:
            phase = 0
        elif config.animation_mode == 2:
            phase = int(now * 16)
        else:
            phase = int(now * 3)
        for _render_attempt in range(3):
            height, width = screen.getmaxyx()
            resized_message = normalize_mode_for_viewport(config, height, width)
            if resized_message:
                scroll = 0
                set_message(config, resized_message)
            screen.erase()
            if height < 22 or width < 76:
                draw_small_screen(screen, height, width)
            else:
                header_h = draw_decision_header(screen, frame, config, now)
                header_h = draw_evidence_ribbon(screen, header_h, frame, now)
                body_rows = cpu_rows if config.layout_name == "compute" else rows
                if body_rows is not searched_rows or config.search_query != searched_query:
                    searched_rows, searched_query = body_rows, config.search_query
                    matching_rows = process_search_cache.filter(body_rows, searched_query)
                render_static = dict(static)
                if "boot_seconds" in static:
                    render_static["uptime_seconds"] = uptime_at_start + max(0, int(now - uptime_clock_start))
                scroll = draw_mode_body(
                    screen, config.layout_name, header_h, height - 4, width, frame,
                    per_core, render_static, mem, battery, sensor, disk, activity, net_down,
                    net_up, history, matching_rows, scroll, sort_mode, phase,
                    config=config, vm_view=vm_view, cpu_samples=tuple(cpu_samples),
                    inventory=inventory, inventory_age=inventory_age, inventory_error=inventory_error, inventory_retained=inventory_retained,
                )

                if config.show_menu:
                    draw_explore_menu(screen, config)
                if config.show_help:
                    draw_help_overlay(screen, config, sensor)

                status = shortcut_status(width, config)
                if config.message and now < config.message_until and not config.search_editing and not config.search_query:
                    status = semantic_text(config.message + " · q quit", unicode_ok())
                safe_add(
                    screen, height - 1, 1,
                    truncate_display_cells(status, width - 2), color_pair(PAIR_STRUCTURE),
                )
            if screen.getmaxyx() == (height, width):
                screen.refresh()
                break
        time.sleep(idle_delay)


def _snapshot_adapter_attempt(
    collector: Callable[[], AdapterResult[T]],
) -> AdapterResult[T]:
    """Turn ordinary typed-collector faults into explicit unavailable evidence."""

    started_mono_s = time.monotonic()
    try:
        result = collector()
    except Exception:
        completed_mono_s = time.monotonic()
        return AdapterResult(
            None, started_mono_s, completed_mono_s, utc_now(),
            AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
        )
    if isinstance(result, AdapterResult):
        return result
    completed_mono_s = time.monotonic()
    return AdapterResult(
        None, started_mono_s, completed_mono_s, utc_now(),
        AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
    )


def _snapshot_unknown_result() -> AdapterResult[Any]:
    """Represent an ordinary adapter setup failure without exposing its details."""

    started_mono_s = time.monotonic()
    return AdapterResult(
        None, started_mono_s, time.monotonic(), utc_now(),
        AttemptOutcome.UNKNOWN_ERROR, FailureCode.UNEXPECTED_EXCEPTION,
    )


def _snapshot_cpu_result(cpu_sampler: MacCpuSampler) -> tuple[AdapterResult[CpuUsage], tuple[float, ...]]:
    """Prime one native CPU delta and reject windows that outlive its freshness budget."""

    time.sleep(0.25)
    result = _snapshot_adapter_attempt(cpu_sampler.sample_usage)
    if result.outcome is AttemptOutcome.SUCCESS and result.value is not None:
        window_s = result.value.window_completed_mono_s - result.value.window_started_mono_s
        cpu_stale_after_s = next(
            stale_after_s for source_id, _cadence_s, stale_after_s in LIVE_SOURCE_SPECS
            if source_id is SourceId.CPU_USAGE
        )
        if window_s <= 0 or window_s > cpu_stale_after_s:
            result = AdapterResult(
                None, result.started_mono_s, result.completed_mono_s,
                result.completed_at_utc, AttemptOutcome.PARSE_ERROR,
                FailureCode.PARSER_REJECTED,
            )
    per_core = tuple(getattr(cpu_sampler, "last_per_core", ()))
    return result, per_core if result.outcome is AttemptOutcome.SUCCESS else ()


def _snapshot_system_fallback() -> dict[str, Any]:
    try:
        hostname = socket.gethostname().split(".")[0]
    except Exception:
        hostname = "unavailable"
    return {
        "host": hostname,
        "os": "unavailable", "build": "unavailable", "kernel": "unavailable",
        "model": "unavailable", "chip": "unavailable", "physical_cpu": None,
        "logical_cpu": None, "memory": "unavailable", "cpu_clusters": [],
        "core_labels": [], "cpu_index_map": [], "core_counts": {}, "gpus": [],
        "gpu_core_count": None, "max_gpu_freq_mhz": None, "model_id": "unavailable",
        "cpu_cores": "unavailable", "gpu_fp32_tflops": None,
    }


def _snapshot_finite_number(value: Any, *, nonnegative: bool = False) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return False
    return finite and (not nonnegative or value >= 0)


def _snapshot_system_projection(value: Any) -> dict[str, Any]:
    """Return only validated static fields in the fixed snapshot wire shape."""

    projected = _snapshot_system_fallback()
    if not isinstance(value, Mapping):
        return projected
    try:
        static = dict(value)
    except Exception:
        return projected

    for key in ("host", "build", "kernel", "model", "chip", "model_id",
                "cpu_cores"):
        candidate = static.get(key)
        if isinstance(candidate, str) and candidate.strip():
            projected[key] = candidate
    os_name = static.get("os")
    if isinstance(os_name, str) and re.fullmatch(r"macOS \S+", os_name):
        projected["os"] = os_name
    memory_text = static.get("memory")
    memory_match = (
        re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:B|KB|MB|GB|TB|PB)", memory_text)
        if isinstance(memory_text, str) else None
    )
    if (memory_match is not None
            and any(character in "123456789" for character in memory_match.group(1))):
        projected["memory"] = memory_text
    for key in ("physical_cpu", "logical_cpu"):
        candidate = static.get(key)
        if ((isinstance(candidate, str) and candidate)
                or (type(candidate) is int and candidate >= 0)):
            projected[key] = candidate
    for key in ("gpu_core_count", "max_gpu_freq_mhz", "gpu_fp32_tflops"):
        candidate = static.get(key)
        if _snapshot_finite_number(candidate, nonnegative=True):
            projected[key] = candidate

    labels = static.get("core_labels")
    if isinstance(labels, list) and all(isinstance(label, str) for label in labels):
        projected["core_labels"] = list(labels)
    index_map = static.get("cpu_index_map")
    if (isinstance(index_map, list)
            and all(type(index) is int and index >= 0 for index in index_map)):
        projected["cpu_index_map"] = list(index_map)
    counts = static.get("core_counts")
    if isinstance(counts, Mapping):
        try:
            projected["core_counts"] = {
                key: count for key in ("E", "P", "S")
                if type(count := counts.get(key)) is int and count >= 0
            }
        except Exception:
            projected["core_counts"] = {}

    clusters = static.get("cpu_clusters")
    if isinstance(clusters, list):
        projected_clusters = []
        for row in clusters:
            if not isinstance(row, Mapping):
                continue
            try:
                code = row.get("code")
                label = row.get("label")
                logical = row.get("logical")
                source = row.get("source")
            except Exception:
                continue
            if (not isinstance(code, str) or re.fullmatch(r"[A-Z]", code) is None
                    or not isinstance(label, str) or not label
                    or type(logical) is not int or logical <= 0
                    or not isinstance(source, str) or not source):
                continue
            projected_clusters.append({
                "code": code, "label": label, "logical": logical, "source": source,
            })
        projected["cpu_clusters"] = projected_clusters

    gpus = static.get("gpus")
    if isinstance(gpus, list):
        projected_gpus = []
        for row in gpus:
            if not isinstance(row, Mapping):
                continue
            try:
                candidate = {
                    key: row.get(key)
                    for key in ("name", "cores", "metal", "displays", "max_freq_mhz")
                }
            except Exception:
                continue
            if not all(isinstance(value, str) for value in candidate.values()):
                continue
            projected_gpus.append(candidate)
        projected["gpus"] = projected_gpus
    return projected


def _snapshot_battery_fallback() -> dict[str, Any]:
    return {
        "percent": None, "present": None, "source": "unavailable", "state": "unavailable",
        "remaining": "unavailable", "external_connected": None, "is_charging": None,
        "fully_charged": None, "cycle_count": None, "design_cycles": None,
        "health_percent": None, "temp_c": None, "virtual_temp_c": None,
        "voltage_v": None, "amperage_a": None, "charger_watts": None,
        "charger_name": "unavailable",
    }


def _snapshot_battery_projection(battery: Any) -> dict[str, Any]:
    if not isinstance(battery, BatteryInfo):
        return _snapshot_battery_fallback()
    numeric_fields = (
        battery.percent, battery.cycle_count, battery.design_cycles,
        battery.health_percent, battery.temp_c, battery.virtual_temp_c,
        battery.voltage_v, battery.amperage_a, battery.charger_watts,
    )
    if (any(value is not None and not _snapshot_finite_number(value)
            for value in numeric_fields)
            or any(type(value) is not bool for value in (
                battery.present, battery.external_connected, battery.is_charging,
                battery.fully_charged,
            ))
            or any(not isinstance(value, str) for value in (
                battery.source, battery.state, battery.remaining, battery.charger_name,
            ))):
        return _snapshot_battery_fallback()
    if (not battery.present and battery.source == "Power"
            and battery.state == "unknown" and battery.remaining == "unknown"):
        return _snapshot_battery_fallback()
    return asdict(battery)


def _snapshot_sensor_fallback() -> dict[str, Any]:
    return {
        "thermal_warning": "unavailable", "performance_warning": "unavailable",
        "cpu_power_w": None, "gpu_power_w": None, "cpu_temp_c": None,
        "gpu_temp_c": None, "fan_rpm": None, "privileged_locked": None,
        "raw_hint": "unavailable", "ane_power_w": None, "dram_power_w": None,
        "gpu_sram_power_w": None, "package_power_w": None, "gpu_active_percent": None,
        "gpu_freq_mhz": None, "e_cluster_active": None, "p_cluster_active": None,
        "s_cluster_active": None, "e_cluster_freq_mhz": None, "p_cluster_freq_mhz": None,
        "s_cluster_freq_mhz": None, "dram_read_gbs": None, "dram_write_gbs": None,
        "fan_count": None, "fans": [], "temp_sensors": [], "sensor_sample_age_s": None,
    }


def _snapshot_sensor_projection(sensor: Any) -> dict[str, Any]:
    if not isinstance(sensor, SensorInfo):
        return _snapshot_sensor_fallback()
    numeric_fields = (
        sensor.cpu_power_w, sensor.gpu_power_w, sensor.cpu_temp_c, sensor.gpu_temp_c,
        sensor.fan_rpm, sensor.ane_power_w, sensor.dram_power_w,
        sensor.gpu_sram_power_w, sensor.package_power_w, sensor.gpu_active_percent,
        sensor.gpu_freq_mhz, sensor.e_cluster_active, sensor.p_cluster_active,
        sensor.s_cluster_active, sensor.e_cluster_freq_mhz, sensor.p_cluster_freq_mhz,
        sensor.s_cluster_freq_mhz, sensor.dram_read_gbs, sensor.dram_write_gbs,
    )
    valid_fans = (
        isinstance(sensor.fans, list)
        and all(isinstance(fan, FanReading) for fan in sensor.fans)
    )
    valid_temps = (
        isinstance(sensor.temp_sensors, list)
        and all(isinstance(reading, TempReading) for reading in sensor.temp_sensors)
    )
    if (not isinstance(sensor.thermal_warning, str)
            or not isinstance(sensor.performance_warning, str)
            or type(sensor.privileged_locked) is not bool
            or not isinstance(sensor.raw_hint, str)
            or type(sensor.fan_count) is not int or sensor.fan_count < 0
            or not _snapshot_finite_number(sensor.sensor_sample_age_s, nonnegative=True)
            or any(value is not None and not _snapshot_finite_number(value)
                   for value in numeric_fields)
            or not valid_fans or not valid_temps):
        return _snapshot_sensor_fallback()
    try:
        projected = asdict(sensor)
        json.dumps(projected)
    except (Exception, TypeError, ValueError):
        return _snapshot_sensor_fallback()
    if set(projected) != set(_snapshot_sensor_fallback()):
        return _snapshot_sensor_fallback()
    return projected


def _snapshot_memory_fallback() -> dict[str, Optional[float]]:
    return {
        "used_percent": None, "used_bytes": None, "total_bytes": None,
        "swap_used_bytes": None, "swap_total_bytes": None,
    }


def _snapshot_memory_projection(memory: Any) -> dict[str, Optional[float]]:
    projected = _snapshot_memory_fallback()
    if not isinstance(memory, Mapping):
        return projected
    try:
        values = dict(memory)
    except Exception:
        return projected
    total = values.get("total")
    used = values.get("used")
    valid_detail = (
        _snapshot_finite_number(total)
        and total > 0
        and _snapshot_finite_number(used, nonnegative=True)
        and used <= total
    )
    swap_used = values.get("swap_used")
    swap_total = values.get("swap_total")
    valid_swap = (
        _snapshot_finite_number(swap_used, nonnegative=True)
        and _snapshot_finite_number(swap_total, nonnegative=True)
        and swap_used <= swap_total
    )
    if valid_detail:
        projected.update({
            "used_percent": float(used) / float(total) * 100.0,
            "used_bytes": used,
            "total_bytes": total,
        })
    if valid_swap:
        projected.update({
            "swap_used_bytes": swap_used,
            "swap_total_bytes": swap_total,
        })
    return projected


def _snapshot_vm_projection(memory: Any) -> dict[str, Any]:
    try:
        counters = vm_counters_from_mapping(memory)
    except (TypeError, ValueError):
        counters = None
    return {
        "source": "vm_stat", "counter_unit": "pages",
        "status": "OBSERVED" if counters is not None else "UNAVAILABLE",
        "cumulative": asdict(counters) if counters is not None else None,
        "rates": None, "rate_status": "LIVE_ONLY",
    }


def _snapshot_activity_fallback() -> dict[str, Any]:
    return {
        "bytes_per_sec": None, "iops": None, "sample_age": None,
        "source": "unavailable",
    }


def _snapshot_activity_projection(activity: Any) -> dict[str, Any]:
    if (not isinstance(activity, DiskActivity)
            or not isinstance(activity.source, str)
            or not activity.source or activity.source == "unavailable"
            or any(not _snapshot_finite_number(value, nonnegative=True) for value in (
                activity.bytes_per_sec, activity.iops, activity.sample_age,
            ))):
        return _snapshot_activity_fallback()
    return asdict(activity)


def _snapshot_result(future: Any, fallback: Callable[[], T]) -> T:
    try:
        return future.result()
    except Exception:
        return fallback()


def _snapshot_frame(
    states: Mapping[SourceId, SourceState[Any]], now_mono_s: float,
) -> HealthFrame:
    """Build a snapshot frame through the common validated policy boundary."""

    _policy, frame = build_health_frame(initial_policy_state(), states, now_mono_s)
    return replace(frame, origin=FrameOrigin.SNAPSHOT)


def collect_snapshot() -> dict[str, Any]:
    """Collect one truthful, isolated snapshot without creating live pollers."""

    futures: list[Any] = []
    try:
        cpu_sampler: Optional[MacCpuSampler] = MacCpuSampler()
    except Exception:
        cpu_sampler = None
    try:
        gpu_sampler: Optional[GPUProcessSampler] = GPUProcessSampler()
    except Exception:
        gpu_sampler = None
    pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="porkyhud-snapshot")
    primary_error: Optional[BaseException] = None
    try:
        memory_pressure_future = pool.submit(_snapshot_adapter_attempt, collect_memory_pressure)
        thermal_future = pool.submit(_snapshot_adapter_attempt, collect_os_thermal_state)
        capacity_future = pool.submit(_snapshot_adapter_attempt, collect_disk_capacity)
        processes_future = pool.submit(_snapshot_adapter_attempt, collect_process_rows)
        agx_first_future = (
            pool.submit(_snapshot_adapter_attempt,
                        lambda: collect_gpu_process_sample(gpu_sampler, None))
            if gpu_sampler is not None else None
        )
        static_future = pool.submit(collect_static_info)
        memory_future = pool.submit(_snapshot_memory_stats)
        battery_future = pool.submit(_snapshot_battery_info)
        activity_future = pool.submit(disk_activity)
        futures.extend((static_future, memory_future, battery_future, activity_future,
                        memory_pressure_future, thermal_future, capacity_future,
                        processes_future))
        if agx_first_future is not None:
            futures.append(agx_first_future)

        cpu_result, per_core = (
            _snapshot_cpu_result(cpu_sampler)
            if cpu_sampler is not None else (_snapshot_unknown_result(), ())
        )
        memory_pressure_result = memory_pressure_future.result()
        thermal_result = thermal_future.result()
        capacity_result = capacity_future.result()
        processes_result = processes_future.result()
        agx_first_result = (
            agx_first_future.result()
            if agx_first_future is not None else _snapshot_unknown_result()
        )
        if gpu_sampler is not None:
            agx_second_future = pool.submit(
                _snapshot_adapter_attempt,
                lambda: collect_gpu_process_sample(gpu_sampler, None),
            )
            futures.append(agx_second_future)
            agx_second_result = agx_second_future.result()
        else:
            agx_second_result = _snapshot_unknown_result()
        thermal_value = thermal_result.value if thermal_result.outcome is AttemptOutcome.SUCCESS else None
        sensor_future = pool.submit(sensor_info, thermal_value)
        futures.append(sensor_future)

        states: dict[SourceId, SourceState[Any]] = dict(_registry_states())
        states[SourceId.CPU_USAGE] = source_state_from_result(states[SourceId.CPU_USAGE], cpu_result)
        states[SourceId.MEMORY_PRESSURE] = source_state_from_result(
            states[SourceId.MEMORY_PRESSURE], memory_pressure_result,
        )
        states[SourceId.THERMAL_OS] = source_state_from_result(states[SourceId.THERMAL_OS], thermal_result)
        states[SourceId.DISK_CAPACITY] = source_state_from_result(states[SourceId.DISK_CAPACITY], capacity_result)
        states[SourceId.PROCESS_TABLE] = source_state_from_result(
            states[SourceId.PROCESS_TABLE], processes_result,
        )
        gpu_state = source_state_from_result(states[SourceId.PROCESS_GPU], agx_first_result)
        states[SourceId.PROCESS_GPU] = source_state_from_result(gpu_state, agx_second_result)

        now_mono_s = time.monotonic()
        latest_completion = max(
            state.last_attempt.completed_mono_s
            for state in states.values() if state.last_attempt is not None
        )
        if now_mono_s < latest_completion:
            raise ValueError("snapshot clock precedes typed evidence completion")
        frame = _snapshot_frame(states, now_mono_s)

        static = _snapshot_system_projection(_snapshot_result(static_future, lambda: None))
        memory = _snapshot_result(memory_future, lambda: None)
        battery = _snapshot_result(battery_future, lambda: None)
        activity = _snapshot_result(activity_future, lambda: None)
        sensor = _snapshot_result(sensor_future, lambda: None)
        rows = tuple(processes_result.value or ()) if processes_result.outcome is AttemptOutcome.SUCCESS else ()
        if rows:
            rows = merge_gpu_process_rows(rows, states[SourceId.PROCESS_GPU], now_mono_s)
        rows = tuple(sort_process_rows(rows, "CPU")[:12])
        capacity = capacity_result.value if capacity_result.outcome is AttemptOutcome.SUCCESS else None
        capacity_projection = (
            {"label": capacity.volume_name.lower(), "mount": capacity.mount_point,
             "total": capacity.total_bytes, "used": capacity.total_bytes - capacity.free_bytes,
             "free": capacity.free_bytes}
            if capacity is not None else
            {"label": None, "mount": None, "total": None, "used": None, "free": None}
        )
        activity_projection = _snapshot_activity_projection(activity)
        cpu_ready = frame.cpu_percent is not None
        core_values, core_labels = display_core_usage(list(per_core), static)
        return {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "read": legacy_read_projection(frame.assessment),
            "system": static,
            "cpu": {
                "average_percent": frame.cpu_percent,
                "cores": ([{"label": label, "percent": percent}
                           for percent, label in zip(core_values, core_labels)]
                          if cpu_ready else []),
            },
            "memory": _snapshot_memory_projection(memory),
            "memory_activity": _snapshot_vm_projection(memory),
            "battery": _snapshot_battery_projection(battery),
            "sensors": _snapshot_sensor_projection(sensor),
            "disk": {"capacity": capacity_projection, "activity": activity_projection},
            "processes": [
                {**asdict(row), "command": sanitize_command(row.command)}
                for row in rows
            ],
        }
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error: Optional[BaseException] = None
        for future in futures:
            try:
                future.cancel()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        while True:
            try:
                pool.shutdown(wait=True, cancel_futures=True)
                break
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


def print_text_snapshot(snapshot: dict[str, Any]) -> None:
    system = snapshot["system"]
    cpu = snapshot["cpu"]
    memory = snapshot["memory"]
    sensors = snapshot["sensors"]
    disk = snapshot["disk"]
    print("PorkyHUD snapshot")
    read = snapshot.get("read")
    message = read.get("message") if isinstance(read, Mapping) else None
    print(message if isinstance(message, str) and message
          else "read: health assessment unavailable.")
    print(f"Mac: {system.get('chip', 'Mac')} / {system.get('model', '')}")
    average = cpu.get("average_percent")
    if average is None:
        print("CPU: unavailable")
    else:
        print(f"CPU: {average:.1f}% across {len(cpu.get('cores') or [])} sampled workers")
    if memory.get("used_bytes") is None or memory.get("total_bytes") is None or memory.get("used_percent") is None:
        print("RAM: unavailable")
    else:
        print(f"RAM: {human_bytes(memory['used_bytes'])} / {human_bytes(memory['total_bytes'])} ({memory['used_percent']:.1f}%)")
    package_power = sensors.get("package_power_w")
    package_text = format_watts(package_power) if package_power is not None else "--"
    print(f"Thermal: {sensors.get('thermal_warning', 'unavailable')}  Package: {package_text}")
    fan_rows = sensors.get("fans") or []
    if fan_rows:
        print("Fans: " + ", ".join(f"{fan['name']} {fan['rpm']}rpm" for fan in fan_rows[:4]))
    temp_rows = sensors.get("temp_sensors") or []
    if temp_rows:
        hottest = ", ".join(f"{temp['name']} {temp['value_c']:.1f}C" for temp in temp_rows[:4])
        print(f"Hottest sensors: {hottest}")
    activity = disk.get("activity") or {}
    if activity.get("bytes_per_sec") is None or activity.get("iops") is None:
        print("Disk: unavailable")
    else:
        print(f"Disk: {human_bytes(activity['bytes_per_sec'])}/s, {activity['iops']:.0f} iops")
    vm = snapshot.get("memory_activity") or {}
    if vm.get("cumulative"):
        counters = vm["cumulative"]
        print("VM activity: cumulative pages · compressions %s / decompressions %s / swap in %s / out %s (rates require live sampling)" %
              (counters["compressions"], counters["decompressions"], counters["swapins"], counters["swapouts"]))
    print("Top processes:")
    for row in snapshot["processes"][:8]:
        gpu = row.get("gpu")
        gpu_text = f"{gpu:4.1f}%" if gpu is not None else "  --"
        print(f"  {row['pid']:>6} CPU {row['cpu']:5.1f}% MEM {row['mem']:4.1f}% GPU {gpu_text}  {visible_command(row['command'], 72)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PorkyHUD macOS system monitor and tools.",
    )
    parser.add_argument("--version", action="version", version=f"PorkyHUD {VERSION}")
    parser.add_argument(
        "--json", action="store_true",
        help="print one unversioned machine-readable snapshot and exit",
    )
    parser.add_argument(
        "--snapshot", action="store_true",
        help="print one unversioned text snapshot and exit",
    )
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
    parser.add_argument("--theme", choices=[theme["name"].lower().replace(" ", "-")
                                            for theme in THEMES],
                        help="live palette (default: graphite)")
    parser.add_argument("--style", choices=VISUAL_STYLES,
                        help="mascot and wordmark style (default: sculpted)")
    parser.add_argument("--motion", choices=("off", "calm", "vivid"),
                        help="continuous live motion; explicit off also skips the intro (default: off)")
    parser.add_argument("--mode", choices=("overview", "compute", "thermals", "io",
                                         "processes", "insights", "devices", "network", "connections", "system", "cinema"),
                        help="initial dashboard view (default: overview)")
    parser.add_argument("--inspect", choices=("devices", "network", "connections"),
                        help="print one read-only inventory; combine with --json for structured output")
    parser.add_argument("--search", type=normalize_search_query, default="",
                        help="find matching processes or inventory records")
    parser.add_argument("--state", choices=CONNECTION_FILTERS,
                        help="TCP state filter; selects Connections unless a mode is specified")
    parser.add_argument("--no-intro", action="store_true",
                        help="skip the brief launch flourish")
    args = parser.parse_args(argv)
    if args.setup_sensors:
        return install_sensor_access()
    if args.remove_sensor_access:
        return remove_sensor_access()
    if args.sensor_access_status:
        return print_sensor_access_status()
    if args.state is not None:
        if args.inspect and args.inspect != "connections":
            parser.error("--state applies to --inspect connections")
        if not args.inspect and args.mode not in (None, "connections"):
            parser.error("--state applies to --mode connections")
    if (args.search or args.state is not None) and (args.json or args.snapshot) and not args.inspect:
        parser.error("search and TCP filters need --inspect or a live view")
    if args.search and not args.inspect:
        requested = args.mode or ("connections" if args.state is not None else "overview")
        if ("balanced" if requested == "overview" else requested) not in SEARCHABLE_MODES:
            parser.error("--search needs Overview, Compute, Processes, Cinema or an inventory view")
    if args.inspect:
        original = INVENTORY_COLLECTORS[args.inspect](_inventory_probe)
        summary = summarize_inventory(original)
        result = filter_inventory(original, args.search, connection_state=args.state or "all")
        if args.json:
            payload = asdict(result)
            payload["summary"] = asdict(summary)
            payload["filter"] = {"query": args.search, "connection_state": args.state or "all",
                                 "matched_records": len(result.items), "returned_records": summary.total_count}
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("PorkyHUD · %s · %s" % (args.inspect, result.status.value))
            if result.status is InventoryStatus.LIVE:
                print("%d matches / %d returned%s" % (len(result.items), summary.total_count,
                                                    " · PARTIAL" if not original.complete else ""))
            else:
                print("Counts unavailable · no valid list")
            if result.detail:
                print(result.detail)
            for line in inventory_lines(args.inspect, result):
                print(line)
            if result.truncated:
                print("TRUNCATED · record limit reached")
        return 0 if result.status is InventoryStatus.LIVE else 1
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
        if any((args.theme, args.style, args.motion, args.mode, args.no_intro, args.search, args.state)):
            config = HudConfig(intro_enabled=not args.no_intro and args.motion != "off")
            if args.theme:
                config.theme_index = next(index for index, theme in enumerate(THEMES)
                                          if theme["name"].lower().replace(" ", "-") == args.theme)
            if args.style:
                config.visual_style = args.style
            if args.motion:
                config.animation_mode = ("off", "calm", "vivid").index(args.motion)
            if args.mode:
                config.layout_index = LAYOUTS.index("balanced" if args.mode == "overview" else args.mode)
            elif args.state is not None:
                config.layout_index = LAYOUTS.index("connections")
            if args.search:
                config.searches[config.layout_name] = args.search
            if args.state is not None:
                config.connection_filter = args.state
            curses.wrapper(lambda screen: hud(screen, config))
        else:
            curses.wrapper(hud)
        return 0
    except KeyboardInterrupt:
        return 0
    except TerminationSignal as exc:
        return 128 + exc.signum
    except Exception as exc:
        print("PorkyHUD crashed: " + plain_reading_text(str(exc)))
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
