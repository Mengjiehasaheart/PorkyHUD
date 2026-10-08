from __future__ import annotations

import json
import itertools
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone, tzinfo
from unittest import mock

import porkyhud
import porkyhud_core as core


class OffsetTimezone(tzinfo):
    """Timezone fixture for missing, exceptional, and interrupted offsets."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure

    def utcoffset(self, _value: datetime | None) -> timedelta | None:
        if self.failure is not None:
            raise self.failure
        return None


class DiskStatFixture:
    """Complete statvfs fixture with one optional faulting metadata field."""

    def __init__(
        self,
        *,
        f_flag: int = 0,
        f_fsid: int = 1,
        faulting_field: str | None = None,
        fault: BaseException | None = None,
    ) -> None:
        self._f_flag = f_flag
        self._f_fsid = f_fsid
        self.faulting_field = faulting_field
        self.fault = fault

    @property
    def f_flag(self) -> int:
        if self.faulting_field == "f_flag" and self.fault is not None:
            raise self.fault
        return self._f_flag

    @property
    def f_fsid(self) -> int:
        if self.faulting_field == "f_fsid" and self.fault is not None:
            raise self.fault
        return self._f_fsid


class DiskUsageFixture:
    """Complete disk_usage fixture with one optional faulting result field."""

    def __init__(
        self,
        *,
        total: int = 100,
        used: int = 25,
        free: int = 75,
        faulting_field: str | None = None,
        fault: BaseException | None = None,
    ) -> None:
        self._total = total
        self.used = used
        self._free = free
        self.faulting_field = faulting_field
        self.fault = fault

    @property
    def total(self) -> int:
        if self.faulting_field == "total" and self.fault is not None:
            raise self.fault
        return self._total

    @property
    def free(self) -> int:
        if self.faulting_field == "free" and self.fault is not None:
            raise self.fault
        return self._free


class FakeScreen:
    """Display-cell-aware curses double with observable terminal side effects."""

    def __init__(self, height: int, width: int, fill: str = " ",
                 keys: list[int] | None = None, on_refresh=None) -> None:
        self.height = height
        self.width = width
        self.cells = [[fill for _ in range(width)] for _ in range(height)]
        self.background: tuple[str, int] | None = None
        self.keys = list(keys or [])
        self.on_refresh = on_refresh
        self.writes: list[tuple[int, int, str, int, int]] = []
        self.attributes = [[0 for _ in range(width)] for _ in range(height)]
        self.erase_count = 0
        self.refresh_count = 0
        self.nodelay_enabled: bool | None = None
        self.keypad_enabled: bool | None = None

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def addnstr(self, y: int, x: int, text: str, limit: int, _attr: int = 0) -> None:
        rendered = str(text)[: max(0, limit)]
        self.writes.append((y, x, rendered, limit, _attr))
        column = x
        for character in rendered:
            category = porkyhud.unicodedata.category(character)
            if category in {"Cc", "Cf"}:
                continue
            if porkyhud.unicodedata.combining(character):
                if 0 <= y < self.height and x < column:
                    self.cells[y][column - 1] += character
                continue
            cell_width = (
                2 if porkyhud.unicodedata.east_asian_width(character) in {"F", "W"}
                else 1
            )
            if not (0 <= y < self.height) or column < 0 or column + cell_width > self.width:
                break
            self.cells[y][column] = character
            self.attributes[y][column] = _attr
            if cell_width == 2:
                self.cells[y][column + 1] = ""
                self.attributes[y][column + 1] = _attr
            column += cell_width

    def bkgd(self, character: str, attr: int) -> None:
        self.background = (character, attr)

    def erase(self) -> None:
        self.erase_count += 1
        self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]
        self.attributes = [[0 for _ in range(self.width)] for _ in range(self.height)]

    def nodelay(self, _enabled: bool) -> None:
        self.nodelay_enabled = _enabled

    def keypad(self, _enabled: bool) -> None:
        self.keypad_enabled = _enabled

    def getch(self) -> int:
        return self.keys.pop(0) if self.keys else -1

    def refresh(self) -> None:
        self.refresh_count += 1
        if self.on_refresh is not None:
            self.on_refresh()

    def row(self, y: int) -> str:
        return "".join(self.cells[y])

    def text(self) -> str:
        return "\n".join(self.row(y) for y in range(self.height))


def make_battery(**changes: object) -> porkyhud.BatteryInfo:
    values: dict[str, object] = {
        "percent": 75,
        "present": True,
        "source": "AC Power",
        "state": "AC attached",
        "remaining": "not charging",
        "external_connected": True,
        "is_charging": False,
        "fully_charged": False,
        "cycle_count": 10,
        "design_cycles": 1000,
        "health_percent": 98,
        "temp_c": 31.0,
        "virtual_temp_c": 31.2,
        "voltage_v": 12.4,
        "amperage_a": -0.2,
        "charger_watts": 96,
        "charger_name": "USB-C Power Adapter",
    }
    values.update(changes)
    return porkyhud.BatteryInfo(**values)


def settled_sensor() -> porkyhud.SensorInfo:
    return replace(
        porkyhud.default_sensor_info(),
        thermal_warning="nominal",
        performance_warning="nominal",
        privileged_locked=False,
        raw_hint="test sensors",
    )


def make_health_frame_for_test(severity: str = "normal") -> porkyhud.HealthFrame:
    """Build literal, immutable decision-presentation fixtures at capture t=20."""
    core_ids = (core.SourceId.CPU_USAGE, core.SourceId.MEMORY_PRESSURE,
                core.SourceId.THERMAL_OS, core.SourceId.DISK_CAPACITY)
    views = {
        source_id: core.SourceView(source_id, core.SourceViewStatus.LIVE, 1.0,
                                   False, True, 1, None)
        for source_id in core_ids
    }
    for source_id in (core.SourceId.PROCESS_TABLE, core.SourceId.PROCESS_GPU):
        views[source_id] = core.SourceView(source_id, core.SourceViewStatus.CHECKING,
                                           None, False, False, None, None)
    evidence = tuple(
        core.EvidenceRef(source_id, 1, core.EvidenceRole.CURRENT)
        for source_id in core_ids
    )
    assessment = core.HealthAssessment(
        core.HealthSeverity.NORMAL, core.EvidenceState.LIVE,
        "Fresh core evidence shows no active CPU, memory, thermal, or storage pressure.",
        evidence, (), None, 1.0, (), 15.0,
    )
    cpu_percent: float | None = 10.0
    memory: core.MemoryPressure | None = core.MemoryPressure(core.MemoryPressureLevel.NORMAL)
    thermal: core.OsThermalState | None = core.OsThermalState(0, 0)
    disk: core.DiskCapacity | None = core.DiskCapacity(
        "disk-1", "Data", "/System/Volumes/Data", 100 * 1024 ** 3, 50 * 1024 ** 3,
    )
    origin, triggered = porkyhud.FrameOrigin.LIVE, None
    active_cases = {
        "memory_watch": (core.HealthSeverity.WATCH, core.RuleId.MEMORY_WATCH,
                         "macOS reports elevated memory pressure."),
        "memory_critical": (core.HealthSeverity.CRITICAL, core.RuleId.MEMORY_CRITICAL,
                            "macOS reports critical memory pressure."),
        "thermal_watch": (core.HealthSeverity.WATCH, core.RuleId.THERMAL_WATCH,
                          "macOS reports an active thermal or performance warning."),
        "cpu_watch": (core.HealthSeverity.WATCH, core.RuleId.CPU_WATCH,
                      "CPU capacity has averaged at least 90% for 15 seconds."),
        "storage_watch": (core.HealthSeverity.WATCH, core.RuleId.STORAGE_WATCH,
                          "Storage headroom is low: 8.0 GiB remains on Data."),
    }
    recovery_cases = {
        "memory_recovery": (core.RuleId.MEMORY_WATCH,
                            "macOS memory pressure is now normal; recovery confirmation is in progress."),
        "memory_critical_recovery": (core.RuleId.MEMORY_CRITICAL,
                                     "macOS memory pressure has eased from critical to elevated; recovery confirmation is in progress."),
        "thermal_recovery": (core.RuleId.THERMAL_WATCH,
                             "macOS warnings are now clear; thermal recovery confirmation is in progress."),
        "cpu_recovery": (core.RuleId.CPU_WATCH,
                         "CPU use has eased; recovery confirmation is in progress."),
        "storage_recovery": (core.RuleId.STORAGE_WATCH,
                             "Storage headroom has improved; recovery confirmation is in progress."),
    }
    rule_sources = {
        core.RuleId.MEMORY_WATCH: core.SourceId.MEMORY_PRESSURE,
        core.RuleId.MEMORY_CRITICAL: core.SourceId.MEMORY_PRESSURE,
        core.RuleId.THERMAL_WATCH: core.SourceId.THERMAL_OS,
        core.RuleId.CPU_WATCH: core.SourceId.CPU_USAGE,
        core.RuleId.STORAGE_WATCH: core.SourceId.DISK_CAPACITY,
    }
    if severity in active_cases:
        health_severity, rule, reason = active_cases[severity]
        source_id = rule_sources[rule]
        assessment = core.HealthAssessment(
            health_severity, core.EvidenceState.LIVE, reason,
            (core.EvidenceRef(source_id, 1, core.EvidenceRole.CURRENT),),
            (rule,), None, 1.0, (), 15.0,
        )
        memory = ({"memory_watch": core.MemoryPressure(core.MemoryPressureLevel.WARN),
                   "memory_critical": core.MemoryPressure(core.MemoryPressureLevel.CRITICAL)}
                  .get(severity, memory))
        thermal = core.OsThermalState(1, 0) if severity == "thermal_watch" else thermal
        cpu_percent = 95.0 if severity == "cpu_watch" else cpu_percent
        disk = replace(disk, free_bytes=8 * 1024 ** 3) if severity == "storage_watch" else disk
    elif severity in recovery_cases:
        rule, reason = recovery_cases[severity]
        source_id = rule_sources[rule]
        refs = tuple(core.EvidenceRef(source_id, 1, role)
                     for role in (core.EvidenceRole.CURRENT, core.EvidenceRole.TRIGGER))
        assessment = core.HealthAssessment(
            core.HealthSeverity.CRITICAL if rule is core.RuleId.MEMORY_CRITICAL
            else core.HealthSeverity.WATCH, core.EvidenceState.LIVE, reason, refs,
            (rule,), None, 1.0, (), 15.0,
        )
        triggered = 2.0
        memory = (core.MemoryPressure(core.MemoryPressureLevel.WARN)
                  if severity == "memory_critical_recovery" else memory)
        cpu_percent = 60.0 if severity == "cpu_recovery" else cpu_percent
        disk = (replace(disk, free_bytes=30 * 1024 ** 3)
                if severity == "storage_recovery" else disk)
    elif severity in {"checking", "cpu_baseline", "snapshot", "incomplete", "delayed"}:
        blockers, coverage = core_ids, 0.0
        reason = "No recommendation is available from incomplete evidence."
        if severity == "checking":
            for source_id in core_ids:
                views[source_id] = core.SourceView(source_id, core.SourceViewStatus.CHECKING,
                                                   None, False, False, None, None)
            cpu_percent = memory = thermal = disk = None
        elif severity in {"cpu_baseline", "snapshot"}:
            blockers, coverage = (core.SourceId.CPU_USAGE,), 7.0
            reason = "CPU trend requires live monitoring."
            origin = (porkyhud.FrameOrigin.SNAPSHOT if severity == "snapshot"
                      else porkyhud.FrameOrigin.LIVE)
        elif severity == "incomplete":
            blockers = (core.SourceId.MEMORY_PRESSURE,)
            views[core.SourceId.MEMORY_PRESSURE] = core.SourceView(
                core.SourceId.MEMORY_PRESSURE, core.SourceViewStatus.MISSING,
                None, False, False, None, core.FailureCode.MISSING_EXECUTABLE)
            memory = None
        else:
            blockers = (core.SourceId.CPU_USAGE,)
            views[core.SourceId.CPU_USAGE] = core.SourceView(
                core.SourceId.CPU_USAGE, core.SourceViewStatus.STALE,
                18.0, False, False, 1, None)
            cpu_percent = None
        assessment = core.HealthAssessment(
            core.HealthSeverity.UNKNOWN,
            core.EvidenceState.DELAYED if severity == "delayed"
            else core.EvidenceState.INCOMPLETE,
            reason, (), (), None, None, blockers, coverage,
        )
    elif severity != "normal":
        raise ValueError("unknown test severity")

    return porkyhud.HealthFrame(assessment, views, cpu_percent, memory, thermal, disk,
                                core.CpuContributor(core.ContributorState.NO_CLEAR_LEADER),
                                origin, 20.0, triggered)


class FormatterAndParserTests(unittest.TestCase):
    def test_basic_formatters_and_clamping(self) -> None:
        self.assertEqual(porkyhud.clamp(-4.0), 0.0)
        self.assertEqual(porkyhud.clamp(104.0), 100.0)
        self.assertEqual(porkyhud.human_bytes(0), "0B")
        self.assertEqual(porkyhud.human_bytes(1024), "1.0KB")
        self.assertEqual(porkyhud.format_uptime(90_061), "1d 1h 1m")
        self.assertEqual(porkyhud.format_temp(42.25), "42.2C")
        self.assertEqual(porkyhud.format_watts(0.5).strip(), "500mW")
        self.assertEqual(porkyhud.format_watts(12.25).strip(), "12.2W")

    def test_ioreg_and_temperature_parsers(self) -> None:
        raw = """
        "CycleCount" = 42
        "Negative" = 18446744073709551614
        "ExternalConnected" = Yes
        "Name" = "96W USB-C Power Adapter"
        """
        self.assertEqual(porkyhud.parse_ioreg_int(raw, "CycleCount"), 42)
        self.assertEqual(porkyhud.parse_ioreg_int(raw, "Negative"), -2)
        self.assertTrue(porkyhud.parse_ioreg_bool(raw, "ExternalConnected"))
        self.assertFalse(porkyhud.parse_ioreg_bool(raw, "Missing"))
        self.assertEqual(
            porkyhud.parse_ioreg_string(raw, "Name"),
            "96W USB-C Power Adapter",
        )
        self.assertAlmostEqual(porkyhud.apple_battery_temp(3000), 26.85)
        self.assertAlmostEqual(porkyhud.apple_virtual_battery_temp(3120), 31.2)

    def test_powermetrics_value_parsers(self) -> None:
        raw = """
        CPU Power: 1250 mW
        GPU Power: 18.5 W
        GPU active residency: 127.0 %
        GPU active frequency: 1.42 GHz
        """
        self.assertEqual(porkyhud.parse_power_value(raw, "CPU Power"), 1.25)
        self.assertEqual(porkyhud.parse_power_value(raw, "GPU Power"), 18.5)
        self.assertEqual(
            porkyhud.parse_any_power_value(raw, ["Missing", "GPU Power"]),
            18.5,
        )
        self.assertEqual(
            porkyhud.parse_percent_value(raw, ["GPU active residency"]),
            100.0,
        )
        self.assertEqual(
            porkyhud.parse_frequency_mhz(raw, ["GPU active frequency"]),
            1420,
        )

    def test_fan_and_temperature_collection_parsers(self) -> None:
        raw = """
        Fan 0: 1800 rpm min 1200 rpm max 4200 rpm target 2000 rpm auto
        Fan 0: 1800 rpm min 1200 rpm max 4200 rpm target 2000 rpm auto
        CPU temperature: 54.2 C
        GPU temp: 61.0 C
        nonsense temperature: 200 C
        """
        fans = porkyhud.parse_fan_readings(raw)
        self.assertEqual(len(fans), 1)
        self.assertEqual(fans[0].rpm, 1800)
        self.assertEqual(fans[0].minimum_rpm, 1200)
        self.assertEqual(fans[0].maximum_rpm, 4200)
        self.assertEqual(fans[0].target_rpm, 2000)
        self.assertEqual(fans[0].mode, "auto")

        temperatures = porkyhud.parse_temperature_readings(raw)
        self.assertEqual([reading.value_c for reading in temperatures], [61.0, 54.2])

    def test_topology_parsing_and_formatting(self) -> None:
        self.assertEqual(
            porkyhud.core_topology_counts("proc 18:6:0:12"),
            {"total": 18, "efficiency": 6, "performance": 12},
        )
        self.assertEqual(
            porkyhud.core_topology_counts("12 8 4"),
            {"total": 12, "performance": 8, "efficiency": 4},
        )
        self.assertEqual(
            porkyhud.format_core_topology("proc 18:6:0:12"),
            "18 cores (12P/6E)",
        )

    def test_boot_time_is_collected_once_and_then_derived_from_the_cache(self) -> None:
        with mock.patch.object(porkyhud, "BOOT_EPOCH", None), mock.patch.object(
            porkyhud,
            "run_command",
            return_value="{ sec = 100, usec = 0 }",
        ) as run_command, mock.patch.object(
            porkyhud.time,
            "time",
            side_effect=[200.9, 205.1],
        ):
            self.assertEqual(porkyhud.boot_seconds(), 100)
            self.assertEqual(porkyhud.boot_seconds(), 105)
        run_command.assert_called_once_with(["sysctl", "-n", "kern.boottime"])

    def test_memory_disk_and_network_parsers(self) -> None:
        command_output = {
            ("sysctl", "-n", "hw.memsize"): str(16 * 1024**3),
            ("vm_stat",): """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free: 100.
Pages speculative: 20.
File-backed pages: 200.
Anonymous pages: 300.
Pages wired down: 400.
Pages occupied by compressor: 500.
Pages active: 600.
Pages inactive: 700.
""",
            ("sysctl", "vm.swapusage"): "vm.swapusage: total = 2048.00M used = 512.00M free = 1536.00M",
        }

        def fake_command(args: list[str], timeout: float = 2.0) -> str:
            del timeout
            return command_output.get(tuple(args), "")

        with mock.patch.object(porkyhud, "run_command", side_effect=fake_command):
            memory = porkyhud.memory_stats()

        self.assertEqual(memory["total"], 16 * 1024**3)
        self.assertEqual(memory["used"], (300 + 400 + 500) * 4096)
        self.assertEqual(memory["available"], (100 + 20 + 200) * 4096)
        self.assertEqual(memory["swap_used"], 512 * 1024**2)

        iostat = """disk0 disk1
KB/t xfrs MB KB/t xfrs MB
10.0 100 1.0 20.0 200 2.0
12.0 10 3.0 16.0 20 4.0
"""
        with mock.patch.object(porkyhud, "run_command", return_value=iostat):
            activity = porkyhud.disk_activity()
        self.assertEqual(activity.iops, 30.0)
        self.assertEqual(activity.bytes_per_sec, 7 * 1024**2)

        netstat = """Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
lo0 16384 Link x 1 0 999 1 0 999 0
en0 1500 Link x 1 0 1000 1 0 2000 0
en0 1500 inet x 1 - 1000 1 - 2000 -
utun0 1380 Link x 1 0 300 1 0 400 0
"""
        with mock.patch.object(porkyhud, "run_command", return_value=netstat):
            self.assertEqual(porkyhud.network_bytes(), (1300, 2400))

    def test_disk_activity_requires_complete_numeric_triplets(self) -> None:
        """A truncated iostat sample is unknown while a complete zero row is measured."""

        prefix = "disk0\nKB/t xfrs MB\n0 0 0\n"
        with mock.patch.object(porkyhud, "run_command", return_value=prefix + "1 2\n"):
            malformed = porkyhud.disk_activity()
        self.assertEqual(malformed.source, "unavailable")

        with mock.patch.object(porkyhud, "run_command", return_value=prefix + "0 0 0\n"):
            measured_zero = porkyhud.disk_activity()
        self.assertEqual(measured_zero.source, "iostat")
        self.assertEqual(measured_zero.bytes_per_sec, 0.0)
        self.assertEqual(measured_zero.iops, 0.0)


class StructuredCommandTests(unittest.TestCase):
    def run_fixture(
        self,
        code: str,
        timeout_s: float = 1.0,
        stdout_limit: int = 1024,
        stderr_limit: int = 1024,
    ) -> object:
        return porkyhud.run_structured_command(
            [sys.executable, "-c", code],
            category="fixture",
            timeout_s=timeout_s,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
        )

    def assert_command_error(self, result: object, failure: core.FailureCode) -> None:
        self.assertEqual(result.outcome, core.AttemptOutcome.COMMAND_ERROR)
        self.assertEqual(result.failure, failure)

    def assert_private_unknown(self, result: object) -> None:
        self.assertEqual(result.outcome, core.AttemptOutcome.UNKNOWN_ERROR)
        self.assertEqual(result.failure, core.FailureCode.UNEXPECTED_EXCEPTION)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def assert_abnormal_or_private_unknown(
        self, result: object, outcome: core.AttemptOutcome, failure: core.FailureCode,
    ) -> None:
        if result.outcome is core.AttemptOutcome.UNKNOWN_ERROR:
            self.assert_private_unknown(result)
        else:
            self.assertEqual(result.outcome, outcome)
            self.assertEqual(result.failure, failure)

    def descendant_fixture(
        self,
        directory: str,
        emit_invalid_utf8: bool,
        safety_seconds: float = 1.5,
        parent_lingers: bool = True,
    ) -> tuple[str, str, str]:
        pid_path = os.path.join(directory, "pids")
        heartbeat_path = os.path.join(directory, "heartbeat")
        descendant_code = (
            "import os, pathlib, signal, time\n"
            "os.close(1)\n"
            "os.close(2)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "path = pathlib.Path(%r)\n"
            "deadline = time.monotonic() + %r\n"
            "while time.monotonic() < deadline:\n"
            "    path.write_text(str(time.monotonic()))\n"
            "    time.sleep(0.01)\n"
        ) % (heartbeat_path, safety_seconds)
        parent_code = (
            "import os, pathlib, signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "pids = pathlib.Path(%r)\n"
            "pids.write_text(str(os.getpid()) + '\\n')\n"
            "child = subprocess.Popen([sys.executable, '-c', %r])\n"
            "pids.write_text(pids.read_text() + str(child.pid) + '\\n')\n"
            "heartbeat = pathlib.Path(%r)\n"
            "deadline = time.monotonic() + 1.0\n"
            "while not heartbeat.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
        ) % (pid_path, descendant_code, heartbeat_path)
        if emit_invalid_utf8:
            parent_code += "sys.stdout.buffer.write(b'\\xff'); sys.stdout.flush()"
        elif parent_lingers:
            parent_code += "time.sleep(%r)" % safety_seconds
        return parent_code, pid_path, heartbeat_path

    def wait_for_heartbeat(self, heartbeat_path: str) -> str:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if os.path.exists(heartbeat_path):
                with open(heartbeat_path, encoding="utf-8") as heartbeat:
                    return heartbeat.read()
            time.sleep(0.01)
        self.fail("descendant never became live")

    def assert_heartbeat_stopped(self, heartbeat_path: str) -> None:
        before = self.wait_for_heartbeat(heartbeat_path)
        time.sleep(0.08)
        with open(heartbeat_path, encoding="utf-8") as heartbeat:
            self.assertEqual(heartbeat.read(), before)

    def assert_recorded_processes_are_gone(self, pid_path: str, timeout_s: float = 1.0) -> None:
        with open(pid_path, encoding="utf-8") as pids:
            values = [int(value) for value in pids.read().splitlines() if value]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining = []
            for pid in values:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                remaining.append(pid)
            if not remaining:
                return
            time.sleep(0.02)
        self.fail("recorded process remained after structured cleanup")

    def assert_traceback_contains(self, actual: object, expected: object) -> None:
        current = actual
        while current is not None:
            if current is expected:
                return
            current = current.tb_next
        self.fail("the original interruption traceback was not preserved")

    def assert_no_command_workers(self) -> None:
        workers = [
            thread.name for thread in threading.enumerate()
            if thread.name.startswith("PorkyHUD-command-")
        ]
        self.assertEqual(workers, [])

    def assert_command_workers_eventually_gone(self, timeout_s: float = 1.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not any(
                thread.name.startswith("PorkyHUD-command-")
                for thread in threading.enumerate()
            ):
                return
            time.sleep(0.01)
        self.assert_no_command_workers()

    def run_capturing_base_exception(self, action: object) -> tuple[BaseException, object]:
        try:
            action()
        except BaseException as caught:
            return caught, sys.exc_info()[2]
        self.fail("expected a BaseException")

    def raise_with_traceback(
        self, interruption: BaseException, tracebacks: list[object],
    ) -> None:
        try:
            raise interruption
        except BaseException:
            tracebacks.append(sys.exc_info()[2])
            raise

    def test_thread_start_interruptions_have_no_spawn_ownership_gap(self) -> None:
        real_start = porkyhud._command_start_thread
        for timing in ("before", "after", "delayed", "ordinary-after"):
            with self.subTest(timing=timing):
                fd_before = len(os.listdir("/dev/fd"))
                interruption = (
                    RuntimeError("ordinary start failure")
                    if timing == "ordinary-after"
                    else KeyboardInterrupt("thread start interruption")
                )
                original_tb: list[object] = []
                popen_calls: list[object] = []
                delayed_start_done = threading.Event()

                def interrupted_start(thread: threading.Thread) -> None:
                    if timing in ("after", "ordinary-after"):
                        real_start(thread)
                    elif timing == "delayed":
                        def start_later() -> None:
                            time.sleep(0.03)
                            real_start(thread)
                            delayed_start_done.set()

                        threading.Thread(target=start_later, daemon=True).start()
                    self.raise_with_traceback(interruption, original_tb)

                def forbidden_popen(*_args: object, **_kwargs: object) -> object:
                    popen_calls.append(object())
                    raise AssertionError("worker spawned despite interrupted start")

                with mock.patch.object(
                    porkyhud, "_command_start_thread", side_effect=interrupted_start,
                ), mock.patch.object(
                    porkyhud, "_command_worker_popen", side_effect=forbidden_popen,
                ):
                    if isinstance(interruption, Exception):
                        self.assert_private_unknown(
                            self.run_fixture("print('must not spawn')")
                        )
                    else:
                        caught_exception, caught_tb = self.run_capturing_base_exception(
                            lambda: self.run_fixture("print('must not spawn')")
                        )
                        self.assertIs(caught_exception, interruption)
                        self.assert_traceback_contains(caught_tb, original_tb[0])
                self.assertEqual(popen_calls, [])
                if timing == "delayed":
                    self.assertTrue(delayed_start_done.wait(1.0))
                self.assert_command_workers_eventually_gone()
                self.assertEqual(len(os.listdir("/dev/fd")), fd_before)

    def test_each_event_setup_failure_closes_the_cancel_socketpair(self) -> None:
        real_event = threading.Event
        for failure_index in (1, 2):
            with self.subTest(failure_index=failure_index):
                calls = 0
                fd_before = len(os.listdir("/dev/fd"))

                def fail_selected_event() -> threading.Event:
                    nonlocal calls
                    calls += 1
                    if calls == failure_index:
                        raise RuntimeError("private event construction failure")
                    return real_event()

                with mock.patch.object(
                    porkyhud.threading, "Event", side_effect=fail_selected_event,
                ), mock.patch.object(porkyhud, "_command_worker_popen") as popen:
                    result = self.run_fixture("print('must not spawn')")
                self.assert_private_unknown(result)
                self.assertEqual(len(os.listdir("/dev/fd")), fd_before)
                popen.assert_not_called()

        interruption = KeyboardInterrupt("event construction interruption")
        calls = 0

        def interrupt_second_event() -> threading.Event:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise interruption
            return real_event()

        fd_before = len(os.listdir("/dev/fd"))
        with mock.patch.object(
            porkyhud.threading, "Event", side_effect=interrupt_second_event,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.run_fixture("print('must not spawn')")
        self.assertIs(raised.exception, interruption)
        self.assertEqual(len(os.listdir("/dev/fd")), fd_before)

    def test_first_caller_interruption_wins_while_publication_cleans_group(self) -> None:
        first = KeyboardInterrupt("first caller interruption")
        later = SystemExit("later caller interruption")
        original_tb: list[object] = []
        wait_calls = 0
        spawned = threading.Event()
        real_popen = porkyhud._command_worker_popen
        real_wait = porkyhud._command_wait_publication
        real_notify = porkyhud._command_notify_cancel
        notify_calls = 0

        def tracked_popen(*args: object, **kwargs: object) -> object:
            process = real_popen(*args, **kwargs)
            spawned.set()
            return process

        def interrupting_wait(event: threading.Event, timeout_s: float) -> bool:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                self.assertTrue(spawned.wait(1.0))
                self.wait_for_heartbeat(heartbeat_path)
                self.raise_with_traceback(first, original_tb)
            if wait_calls == 2:
                raise later
            return real_wait(event, timeout_s)

        def interrupt_notify_once(cancel_writer: object) -> bool:
            nonlocal notify_calls
            notify_calls += 1
            if notify_calls == 1:
                raise SystemExit("later cancel notification interruption")
            return real_notify(cancel_writer)

        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, heartbeat_path = self.descendant_fixture(
                directory, emit_invalid_utf8=False,
            )
            with mock.patch.object(
                porkyhud, "_command_worker_popen", side_effect=tracked_popen,
            ), mock.patch.object(
                porkyhud, "_command_wait_publication", side_effect=interrupting_wait,
            ), mock.patch.object(
                porkyhud, "_command_notify_cancel", side_effect=interrupt_notify_once,
            ):
                caught_exception, caught_tb = self.run_capturing_base_exception(
                    lambda: self.run_fixture(code, timeout_s=1.0)
                )
            self.assertIs(caught_exception, first)
            self.assert_traceback_contains(caught_tb, original_tb[0])
            self.assert_recorded_processes_are_gone(pid_path)
        self.assertGreaterEqual(wait_calls, 3)
        self.assertGreaterEqual(notify_calls, 2)
        self.assert_no_command_workers()

    def test_normal_start_waits_for_publication_and_join_despite_interrupt(self) -> None:
        interruption = KeyboardInterrupt("join interruption")
        real_entry = porkyhud._command_worker_entry
        real_join = porkyhud._command_join_thread
        join_calls = 0

        def publish_then_linger(*args: object, **kwargs: object) -> None:
            real_entry(*args, **kwargs)
            time.sleep(0.05)

        def interrupt_join_once(thread: threading.Thread, timeout_s: float) -> None:
            nonlocal join_calls
            join_calls += 1
            if join_calls == 1:
                raise interruption
            real_join(thread, timeout_s)

        with mock.patch.object(
            porkyhud, "_command_worker_entry", side_effect=publish_then_linger,
        ), mock.patch.object(
            porkyhud, "_command_join_thread", side_effect=interrupt_join_once,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.run_fixture("print('published')")
        self.assertIs(raised.exception, interruption)
        self.assertGreaterEqual(join_calls, 2)
        self.assert_no_command_workers()

    def test_first_worker_interruption_wins_over_later_publication_interruption(self) -> None:
        first = KeyboardInterrupt("first worker interruption")
        later = SystemExit("later publication interruption")
        real_publish = porkyhud._command_publish_report
        select_calls = 0
        publish_calls = 0

        def interrupt_select_once(selector: object, timeout_s: float) -> object:
            nonlocal select_calls
            select_calls += 1
            if select_calls == 1:
                raise first
            return selector.select(timeout_s)

        def interrupt_publish_once(
            request: object, reports: object, report: object,
        ) -> None:
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 1:
                raise later
            real_publish(request, reports, report)

        with mock.patch.object(
            porkyhud, "_command_worker_select", side_effect=interrupt_select_once,
        ), mock.patch.object(
            porkyhud, "_command_publish_report", side_effect=interrupt_publish_once,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.run_fixture("import time; time.sleep(0.4)")
        self.assertIs(raised.exception, first)
        self.assertGreaterEqual(publish_calls, 2)
        self.assert_no_command_workers()

    def test_hundred_run_lifecycle_stress_has_no_worker_fd_or_child_growth(self) -> None:
        def process_counts() -> tuple[int, int]:
            completed = subprocess.run(
                ["ps", "-axo", "ppid=,stat="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=True,
            )
            children = 0
            zombies = 0
            for line in completed.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 2 and int(fields[0]) == os.getpid():
                    children += 1
                    zombies += fields[1].startswith("Z")
            return children, zombies

        fd_before = len(os.listdir("/dev/fd"))
        children_before = process_counts()
        for index in range(100):
            kind = index % 5
            with mock.patch.object(
                porkyhud,
                "_command_worker_signal_group",
                return_value=porkyhud._CommandGroupState.ABSENT,
            ):
                if kind == 0:
                    result = self.run_fixture("print('ok')")
                    self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
                elif kind == 1:
                    result = self.run_fixture("import time; time.sleep(0.03)", timeout_s=0.005)
                    self.assertEqual(result.outcome, core.AttemptOutcome.TIMEOUT)
                elif kind == 2:
                    result = self.run_fixture(
                        "import sys; sys.stdout.buffer.write(b'x' * 9)",
                        stdout_limit=8,
                    )
                    self.assert_command_error(result, core.FailureCode.OUTPUT_LIMIT)
                elif kind == 3:
                    result = self.run_fixture(
                        "import sys; sys.stdout.buffer.write(b'\\xff')"
                    )
                    self.assert_command_error(result, core.FailureCode.DECODE_ERROR)
                else:
                    interruption = KeyboardInterrupt("stress cancellation")
                    spawned = threading.Event()
                    real_popen = porkyhud._command_worker_popen
                    real_wait = porkyhud._command_wait_publication
                    wait_calls = 0

                    def tracked_popen(*args: object, **kwargs: object) -> object:
                        process = real_popen(*args, **kwargs)
                        spawned.set()
                        return process

                    def cancel_once(event: threading.Event, timeout_s: float) -> bool:
                        nonlocal wait_calls
                        wait_calls += 1
                        if wait_calls == 1:
                            self.assertTrue(spawned.wait(1.0))
                            raise interruption
                        return real_wait(event, timeout_s)

                    with mock.patch.object(
                        porkyhud, "_command_worker_popen", side_effect=tracked_popen,
                    ), mock.patch.object(
                        porkyhud, "_command_wait_publication", side_effect=cancel_once,
                    ):
                        with self.assertRaises(KeyboardInterrupt) as raised:
                            self.run_fixture("import time; time.sleep(0.03)")
                    self.assertIs(raised.exception, interruption)
            self.assert_no_command_workers()

        self.assertEqual(len(os.listdir("/dev/fd")), fd_before)
        self.assertEqual(process_counts(), children_before)

    def test_group_probe_uses_only_captured_pgid_and_exact_errno_semantics(self) -> None:
        with mock.patch.object(porkyhud.os, "getpgid", side_effect=AssertionError("unsafe getpgid")):
            with mock.patch.object(porkyhud.os, "killpg", return_value=None) as killpg:
                self.assertIs(
                    porkyhud._command_worker_probe_group(12345),
                    porkyhud._CommandGroupState.EXISTS,
                )
                killpg.assert_called_once_with(12345, 0)
            with mock.patch.object(porkyhud.os, "killpg", side_effect=ProcessLookupError()):
                self.assertIs(
                    porkyhud._command_worker_probe_group(12345),
                    porkyhud._CommandGroupState.ABSENT,
                )
            with mock.patch.object(porkyhud.os, "killpg", side_effect=PermissionError()):
                self.assertIs(
                    porkyhud._command_worker_probe_group(12345),
                    porkyhud._CommandGroupState.UNCONFIRMED,
                )
                self.assertIs(
                    porkyhud._command_worker_signal_group(12345, signal.SIGTERM),
                    porkyhud._CommandGroupState.UNCONFIRMED,
                )

    def test_kqueue_exit_observation_does_not_reap_the_leader(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        owner = mock.Mock(exit_watch=None)
        try:
            observed = porkyhud._command_worker_open_exit_watch(owner, process.pid)
            deadline = time.monotonic() + 1.0
            while not observed and time.monotonic() < deadline:
                observed = porkyhud._command_worker_observe_exit(
                    owner.exit_watch, process.pid,
                )
                if not observed:
                    time.sleep(0.01)
            self.assertTrue(observed)
            self.assertIsNone(process.returncode)
        finally:
            if owner.exit_watch is not None:
                owner.exit_watch.close()
            process.wait(timeout=1.0)

    def test_cancellation_before_normal_commit_cleans_waitable_leader_group(self) -> None:
        entered_commit = threading.Event()
        cancellation_recorded = threading.Event()
        real_commit = porkyhud._command_try_normal_commit
        real_cancel = porkyhud._command_request_cancellation
        real_wait = porkyhud._command_wait_publication
        wait_calls = 0
        interruption = KeyboardInterrupt("cancel before normal commit")

        def commit_after_cancellation(control: object) -> bool:
            entered_commit.set()
            self.assertTrue(cancellation_recorded.wait(1.0))
            return real_commit(control)

        def record_cancellation(control: object) -> bool:
            result = real_cancel(control)
            cancellation_recorded.set()
            return result

        def cancel_during_commit(event: threading.Event, timeout_s: float) -> bool:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                self.assertTrue(entered_commit.wait(1.0))
                raise interruption
            return real_wait(event, timeout_s)

        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, heartbeat_path = self.descendant_fixture(
                directory,
                emit_invalid_utf8=False,
                safety_seconds=0.4,
                parent_lingers=False,
            )
            with mock.patch.object(
                porkyhud, "_command_try_normal_commit", side_effect=commit_after_cancellation,
            ), mock.patch.object(
                porkyhud, "_command_request_cancellation", side_effect=record_cancellation,
            ), mock.patch.object(
                porkyhud, "_command_wait_publication", side_effect=cancel_during_commit,
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    self.run_fixture(code, timeout_s=1.0)
            self.assertIs(raised.exception, interruption)
            self.assert_heartbeat_stopped(heartbeat_path)
            self.assert_recorded_processes_are_gone(pid_path)

    def test_cancellation_after_normal_commit_does_not_signal_freed_group(self) -> None:
        committed = threading.Event()
        cancellation_recorded = threading.Event()
        real_commit = porkyhud._command_try_normal_commit
        real_cancel = porkyhud._command_request_cancellation
        real_reap = porkyhud._command_worker_reap_leader
        real_wait = porkyhud._command_wait_publication
        interruption = KeyboardInterrupt("cancel after normal commit")
        wait_calls = 0
        signals: list[tuple[int, int]] = []

        def commit_then_block(control: object) -> bool:
            result = real_commit(control)
            if result:
                committed.set()
            return result

        def delayed_reap(process: object, deadline: float) -> bool:
            self.assertTrue(cancellation_recorded.wait(1.0))
            return real_reap(process, deadline)

        def record_cancellation(control: object) -> bool:
            result = real_cancel(control)
            cancellation_recorded.set()
            return result

        def interrupt_after_commit(event: threading.Event, timeout_s: float) -> bool:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                self.assertTrue(committed.wait(1.0))
                raise interruption
            return real_wait(event, timeout_s)

        def forbidden_signal(pgid: int, sig: int) -> object:
            signals.append((pgid, sig))
            raise AssertionError("normal-committed command was signaled")

        with mock.patch.object(
            porkyhud, "_command_try_normal_commit", side_effect=commit_then_block,
        ), mock.patch.object(
            porkyhud, "_command_worker_reap_leader", side_effect=delayed_reap,
        ), mock.patch.object(
            porkyhud, "_command_request_cancellation", side_effect=record_cancellation,
        ), mock.patch.object(
            porkyhud, "_command_wait_publication", side_effect=interrupt_after_commit,
        ), mock.patch.object(
            porkyhud, "_command_worker_signal_group", side_effect=forbidden_signal,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.run_fixture("print('committed')")
        self.assertIs(raised.exception, interruption)
        self.assertEqual(signals, [])
        self.assert_no_command_workers()

    def test_success_is_bounded_classified_and_uses_c_locale(self) -> None:
        original_locale = os.environ.get("LC_ALL")
        result = self.run_fixture(
            "import os; print(os.environ.get('LC_ALL')); print('ok')"
        )
        self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
        self.assertEqual(result.stdout, "C\nok\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.failure)
        self.assertEqual(os.environ.get("LC_ALL"), original_locale)
        self.assertLessEqual(result.started_mono_s, result.completed_mono_s)
        self.assertIsNotNone(result.completed_at_utc.tzinfo)
        self.assertEqual(result.completed_at_utc.utcoffset(), timedelta(0))

    def test_command_result_is_frozen_validated_and_normalizes_utc(self) -> None:
        result = porkyhud.CommandResult(
            category="fixture",
            stdout="",
            stderr="",
            returncode=0,
            started_mono_s=1.0,
            completed_mono_s=2.0,
            completed_at_utc=datetime(
                2026, 8, 12, 2, 0, tzinfo=timezone(timedelta(hours=-5)),
            ),
            outcome=core.AttemptOutcome.SUCCESS,
            failure=None,
        )
        self.assertEqual(result.completed_at_utc.utcoffset(), timedelta(0))
        with self.assertRaises(FrozenInstanceError):
            result.stdout = "changed"

        invalid_overrides = (
            {"category": "unsafe category"},
            {"stdout": b"bytes"},
            {"returncode": True},
            {"started_mono_s": float("nan")},
            {"completed_mono_s": 0.5},
            {"completed_at_utc": datetime(2026, 8, 12)},
            {"outcome": core.AttemptOutcome.SUCCESS, "failure": core.FailureCode.NONZERO_EXIT},
            {"outcome": core.AttemptOutcome.COMMAND_ERROR, "failure": None},
        )
        values = {
            "category": "fixture",
            "stdout": "",
            "stderr": "",
            "returncode": 0,
            "started_mono_s": 1.0,
            "completed_mono_s": 2.0,
            "completed_at_utc": datetime.now(timezone.utc),
            "outcome": core.AttemptOutcome.SUCCESS,
            "failure": None,
        }
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides), self.assertRaises((TypeError, ValueError)):
                porkyhud.CommandResult(**{**values, **overrides})

    def test_command_result_requires_a_real_utc_offset(self) -> None:
        values = {
            "category": "fixture",
            "stdout": "",
            "stderr": "",
            "returncode": 0,
            "started_mono_s": 1.0,
            "completed_mono_s": 2.0,
            "outcome": core.AttemptOutcome.SUCCESS,
            "failure": None,
        }
        missing_offset = datetime(2026, 8, 12, 2, 0, tzinfo=OffsetTimezone())
        with self.assertRaises(ValueError):
            porkyhud.CommandResult(**values, completed_at_utc=missing_offset)

        exceptional = datetime(
            2026, 8, 12, 2, 0, tzinfo=OffsetTimezone(RuntimeError("bad timezone")),
        )
        with self.assertRaises(ValueError):
            porkyhud.CommandResult(**values, completed_at_utc=exceptional)

        interruption = KeyboardInterrupt("stop timezone validation")
        interrupted = datetime(2026, 8, 12, 2, 0, tzinfo=OffsetTimezone(interruption))
        with self.assertRaises(KeyboardInterrupt) as raised:
            porkyhud.CommandResult(**values, completed_at_utc=interrupted)
        self.assertIs(raised.exception, interruption)

    def test_missing_executable_is_classified_without_echoing_arguments(self) -> None:
        secret = "never-display-this-argument"
        result = porkyhud.run_structured_command(
            ["/definitely/not/a/porkyhud-command", secret],
            category="fixture",
            timeout_s=1.0,
            stdout_limit=1024,
            stderr_limit=1024,
        )
        self.assertEqual(result.outcome, core.AttemptOutcome.MISSING_DEPENDENCY)
        self.assertEqual(result.failure, core.FailureCode.MISSING_EXECUTABLE)
        self.assertEqual(result.returncode, None)
        self.assertNotIn(secret, repr(result))

    def test_nonzero_exit_preserves_bounded_streams_but_is_not_success(self) -> None:
        result = self.run_fixture(
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)"
        )
        self.assert_command_error(result, core.FailureCode.NONZERO_EXIT)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")

    def test_exact_stream_caps_succeed_and_first_extra_byte_invalidates_output(self) -> None:
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream, size="exact"):
                target = "sys.stdout.buffer" if stream == "stdout" else "sys.stderr.buffer"
                result = self.run_fixture(
                    "import sys; %s.write(b'x' * 8)" % target,
                    stdout_limit=8,
                    stderr_limit=8,
                )
                self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
                self.assertEqual(getattr(result, stream), "x" * 8)
            with self.subTest(stream=stream, size="crossing"):
                target = "sys.stdout.buffer" if stream == "stdout" else "sys.stderr.buffer"
                with mock.patch.object(
                    porkyhud,
                    "_command_worker_signal_group",
                    return_value=porkyhud._CommandGroupState.ABSENT,
                ):
                    result = self.run_fixture(
                        "import sys; %s.write(b'x' * 9)" % target,
                        stdout_limit=8,
                        stderr_limit=8,
                    )
                self.assert_command_error(result, core.FailureCode.OUTPUT_LIMIT)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def test_simultaneous_stdout_and_stderr_are_drained_without_deadlock(self) -> None:
        result = self.run_fixture(
            "import sys; sys.stdout.buffer.write(b'o' * 65536); "
            "sys.stderr.buffer.write(b'e' * 65536)",
            timeout_s=2.0,
            stdout_limit=65536,
            stderr_limit=65536,
        )
        self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
        self.assertEqual(result.stdout, "o" * 65536)
        self.assertEqual(result.stderr, "e" * 65536)

    def test_utf8_is_decoded_strictly_across_write_boundaries(self) -> None:
        valid = self.run_fixture(
            "import sys, time; sys.stdout.buffer.write(b'\\xe2'); sys.stdout.flush(); "
            "time.sleep(0.03); sys.stdout.buffer.write(b'\\x82\\xac')",
            timeout_s=2.0,
        )
        self.assertEqual(valid.outcome, core.AttemptOutcome.SUCCESS)
        self.assertEqual(valid.stdout, "€")
        with mock.patch.object(
            porkyhud,
            "_command_worker_signal_group",
            return_value=porkyhud._CommandGroupState.ABSENT,
        ):
            invalid = self.run_fixture(
                "import sys, time; sys.stdout.buffer.write(b'\\xe2'); sys.stdout.flush(); "
                "time.sleep(0.03); sys.stdout.buffer.write(b'X')",
                timeout_s=2.0,
            )
        self.assert_command_error(invalid, core.FailureCode.DECODE_ERROR)
        self.assertEqual(invalid.stdout, "")
        self.assertEqual(invalid.stderr, "")

    def test_decode_failure_terminates_a_live_descendant_after_leader_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, heartbeat_path = self.descendant_fixture(directory, emit_invalid_utf8=True)
            result = self.run_fixture(code, timeout_s=1.0)
            self.assert_abnormal_or_private_unknown(
                result, core.AttemptOutcome.COMMAND_ERROR, core.FailureCode.DECODE_ERROR,
            )
            self.assert_heartbeat_stopped(heartbeat_path)
            self.assert_recorded_processes_are_gone(pid_path)

    def test_selector_interruption_reaps_group_and_reraises_original_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, heartbeat_path = self.descendant_fixture(directory, emit_invalid_utf8=False)
            interruption = KeyboardInterrupt("fixture interruption")
            real_selector = porkyhud.selectors.DefaultSelector

            class InterruptingSelector:
                def __init__(self) -> None:
                    self._selector = real_selector()

                def register(self, *args: object, **kwargs: object) -> object:
                    return self._selector.register(*args, **kwargs)

                def unregister(self, *args: object, **kwargs: object) -> object:
                    return self._selector.unregister(*args, **kwargs)

                def get_map(self) -> object:
                    return self._selector.get_map()

                def select(self, _timeout: float | None = None) -> object:
                    self_outer.wait_for_heartbeat(heartbeat_path)
                    raise interruption

                def close(self) -> None:
                    self._selector.close()

            self_outer = self
            with mock.patch.object(porkyhud.selectors, "DefaultSelector", InterruptingSelector):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    self.run_fixture(code, timeout_s=1.0)
            self.assertIs(raised.exception, interruption)
            self.assert_heartbeat_stopped(heartbeat_path)
            self.assert_recorded_processes_are_gone(pid_path)

    def test_spawn_and_setup_failures_are_private_and_never_echo_arguments(self) -> None:
        secret = "spawn-worker-fixture-secret"
        for spawn_error, outcome, failure in (
            (PermissionError(), core.AttemptOutcome.PERMISSION_DENIED, core.FailureCode.PERMISSION_DENIED),
            (OSError(), core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION),
        ):
            with self.subTest(error=type(spawn_error).__name__), mock.patch.object(
                porkyhud, "_command_worker_popen", side_effect=spawn_error,
            ):
                result = porkyhud.run_structured_command(
                    ["/fixture/command", secret], "fixture", 1.0, 128, 128,
                )
            self.assertEqual(result.outcome, outcome)
            self.assertEqual(result.failure, failure)
            self.assertNotIn(secret, repr(result))

    def test_popen_helper_publishes_process_ownership_before_post_spawn_interruption(self) -> None:
        interruption = KeyboardInterrupt("post-spawn interruption")
        created: list[object] = []
        real_popen = porkyhud.subprocess.Popen
        signal_returncodes: list[int | None] = []
        real_signal = porkyhud._command_worker_signal_group

        def spawn_then_interrupt(
            owner: object, args: object, **kwargs: object,
        ) -> object:
            process = real_popen(args, **kwargs)
            created.append(process)
            owner.process = process
            raise interruption

        def trace_signal(pgid: int, sig: int) -> object:
            signal_returncodes.append(created[0].returncode)
            return real_signal(pgid, sig)

        with mock.patch.object(
            porkyhud, "_command_worker_popen_owned", side_effect=spawn_then_interrupt,
        ), mock.patch.object(
            porkyhud, "_command_worker_signal_group", side_effect=trace_signal,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.run_fixture("import time; time.sleep(1.5)")
        self.assertIs(raised.exception, interruption)
        self.assertTrue(created)
        self.assertTrue(signal_returncodes)
        self.assertTrue(all(returncode is None for returncode in signal_returncodes))
        self.assertIsNotNone(created[0].returncode)
        self.assert_no_command_workers()

        for error in (FileNotFoundError(), PermissionError(), OSError()):
            with self.subTest(error=type(error).__name__):
                created = []

                def spawn_then_error(
                    owner: object, args: object, **kwargs: object,
                ) -> object:
                    process = real_popen(args, **kwargs)
                    created.append(process)
                    owner.process = process
                    raise error

                try:
                    with mock.patch.object(
                        porkyhud, "_command_worker_popen_owned", side_effect=spawn_then_error,
                    ):
                        result = self.run_fixture("import time; time.sleep(0.4)")
                    self.assert_private_unknown(result)
                    self.assertTrue(created)
                    self.assertIsNotNone(created[0].returncode)
                finally:
                    for process in created:
                        process.wait(timeout=1.0)
                self.assert_no_command_workers()

    def test_selector_register_select_read_and_unregister_faults_are_private(self) -> None:
        stages = (
            "_command_worker_register",
            "_command_worker_select",
            "_command_worker_read",
            "_command_worker_unregister",
            "_command_worker_close_stream",
            "_command_worker_close_socket",
            "_command_worker_close_exit_watch",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                target = getattr(porkyhud, stage)
                calls = 0

                def fail_once(*args: object, **kwargs: object) -> object:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError("private worker I/O failure")
                    return target(*args, **kwargs)

                with mock.patch.object(porkyhud, stage, side_effect=fail_once):
                    result = self.run_fixture("print('private output')")
                self.assert_private_unknown(result)
                self.assertGreaterEqual(calls, 1)
                self.assert_no_command_workers()

        with mock.patch.object(
            porkyhud.selectors, "DefaultSelector", side_effect=OSError("private selector failure"),
        ), mock.patch.object(porkyhud, "_command_worker_popen") as popen:
            result = self.run_fixture("print('must not spawn')")
        self.assertEqual(result.outcome, core.AttemptOutcome.UNKNOWN_ERROR)
        popen.assert_not_called()

    def test_timeout_terminates_a_child_ignoring_sigterm(self) -> None:
        started = time.monotonic()
        result = self.run_fixture(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1.5)",
            timeout_s=0.15,
        )
        self.assert_abnormal_or_private_unknown(
            result, core.AttemptOutcome.TIMEOUT, core.FailureCode.COMMAND_TIMEOUT,
        )
        self.assertIsNotNone(result.returncode)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_confirmed_cleanup_preserves_prepared_timeout_classification(self) -> None:
        with mock.patch.object(
            porkyhud,
            "_command_worker_signal_group",
            return_value=porkyhud._CommandGroupState.ABSENT,
        ):
            result = self.run_fixture("import time; time.sleep(0.12)", timeout_s=0.02)
        self.assertEqual(result.outcome, core.AttemptOutcome.TIMEOUT)
        self.assertEqual(result.failure, core.FailureCode.COMMAND_TIMEOUT)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_timeout_terminates_descendant_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = os.path.join(directory, "grandchild.pid")
            child_code = "import time; time.sleep(1.5)"
            code = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', %r]); "
                "pathlib.Path(%r).write_text(str(child.pid)); time.sleep(1.5)"
            ) % (child_code, pid_path)
            result = self.run_fixture(code, timeout_s=0.2)
            self.assertEqual(result.outcome, core.AttemptOutcome.TIMEOUT)
            self.assertEqual(result.failure, core.FailureCode.COMMAND_TIMEOUT)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertTrue(os.path.exists(pid_path))
            with open(pid_path, encoding="utf-8") as pid_file:
                grandchild_pid = int(pid_file.read())
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    self.fail("timed-out descendant remained alive")
                time.sleep(0.02)

    def test_rejects_invalid_structured_command_inputs(self) -> None:
        invalid_calls = (
            ([], "fixture", 1.0, 1, 1),
            ([""], "fixture", 1.0, 1, 1),
            ([b"bytes"], "fixture", 1.0, 1, 1),
            (["has" + chr(0) + "nul"], "fixture", 1.0, 1, 1),
            ([sys.executable], "unsafe category", 1.0, 1, 1),
            ([sys.executable], "fixture", 0.0, 1, 1),
            ([sys.executable], "fixture", float("inf"), 1, 1),
            ([sys.executable], "fixture", True, 1, 1),
            ([sys.executable], "fixture", 1.0, True, 1),
            ([sys.executable], "fixture", 1.0, 1, False),
        )
        for args, category, timeout_s, stdout_limit, stderr_limit in invalid_calls:
            with self.subTest(args=args, category=category, timeout_s=timeout_s):
                with self.assertRaises((TypeError, ValueError)):
                    porkyhud.run_structured_command(
                        args,
                        category=category,
                        timeout_s=timeout_s,
                        stdout_limit=stdout_limit,
                        stderr_limit=stderr_limit,
                    )


class DecisionAdapterTests(unittest.TestCase):
    """Regression contracts for macOS evidence adapters, not policy decisions."""

    def command_result(
        self,
        stdout: str = "",
        outcome: core.AttemptOutcome = core.AttemptOutcome.SUCCESS,
        failure: core.FailureCode | None = None,
    ) -> porkyhud.CommandResult:
        now = time.monotonic()
        return porkyhud.CommandResult(
            "fixture", stdout, "", 0 if outcome is core.AttemptOutcome.SUCCESS else None,
            now - 0.2, now - 0.1,
            datetime(2026, 8, 11, 12, tzinfo=timezone.utc), outcome, failure,
        )

    def test_adapter_result_accepts_only_core_outcome_failure_pairs(self) -> None:
        completed = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
        result = porkyhud.AdapterResult("value", 1.0, 2.0, completed,
                                        core.AttemptOutcome.SUCCESS, None)
        self.assertEqual(result.value, "value")
        with self.assertRaises(FrozenInstanceError):
            result.value = "other"  # type: ignore[misc]
        legal_pairs = {
            core.AttemptOutcome.NOT_APPLICABLE: (core.FailureCode.NOT_APPLICABLE,),
            core.AttemptOutcome.UNSUPPORTED: (core.FailureCode.UNSUPPORTED_CAPABILITY,),
            core.AttemptOutcome.PERMISSION_DENIED: (core.FailureCode.PERMISSION_DENIED,),
            core.AttemptOutcome.MISSING_DEPENDENCY: (core.FailureCode.MISSING_EXECUTABLE,),
            core.AttemptOutcome.TIMEOUT: (core.FailureCode.COMMAND_TIMEOUT, core.FailureCode.DEADLINE_EXCEEDED),
            core.AttemptOutcome.COMMAND_ERROR: (
                core.FailureCode.NONZERO_EXIT, core.FailureCode.DECODE_ERROR, core.FailureCode.OUTPUT_LIMIT,
            ),
            core.AttemptOutcome.PARSE_ERROR: (core.FailureCode.EMPTY_OUTPUT, core.FailureCode.PARSER_REJECTED),
            core.AttemptOutcome.CANCELLED: (core.FailureCode.USER_CANCELLED,),
            core.AttemptOutcome.UNKNOWN_ERROR: (core.FailureCode.UNEXPECTED_EXCEPTION,),
        }
        for outcome, failures in legal_pairs.items():
            for failure in failures:
                with self.subTest(outcome=outcome, failure=failure):
                    failed = porkyhud.AdapterResult(None, 1.0, 2.0, completed, outcome, failure)
                    self.assertEqual((failed.outcome, failed.failure), (outcome, failure))
        with self.assertRaises(ValueError):
            porkyhud.AdapterResult(None, 1.0, 2.0, completed,
                                   core.AttemptOutcome.PARSE_ERROR, None)
        with self.assertRaises(ValueError):
            porkyhud.AdapterResult(None, 1.0, 2.0, completed,
                                   core.AttemptOutcome.SUCCESS,
                                   core.FailureCode.PARSER_REJECTED)

    def test_adapter_result_requires_a_real_offset_and_normalizes_failed_attempts(self) -> None:
        missing_offset = datetime(2026, 8, 11, 7, tzinfo=OffsetTimezone())
        with self.assertRaises(ValueError):
            porkyhud.AdapterResult(
                "value", 1.0, 2.0, missing_offset, core.AttemptOutcome.SUCCESS, None,
            )

        exceptional = datetime(
            2026, 8, 11, 7, tzinfo=OffsetTimezone(RuntimeError("bad timezone")),
        )
        with self.assertRaises(ValueError):
            porkyhud.AdapterResult(
                "value", 1.0, 2.0, exceptional, core.AttemptOutcome.SUCCESS, None,
            )

        interruption = KeyboardInterrupt("stop timezone validation")
        interrupted = datetime(2026, 8, 11, 7, tzinfo=OffsetTimezone(interruption))
        with self.assertRaises(KeyboardInterrupt) as raised:
            porkyhud.AdapterResult(
                "value", 1.0, 2.0, interrupted, core.AttemptOutcome.SUCCESS, None,
            )
        self.assertIs(raised.exception, interruption)

        failed = porkyhud.AdapterResult(
            None,
            1.0,
            2.0,
            datetime(2026, 8, 11, 7, tzinfo=timezone(timedelta(hours=-5))),
            core.AttemptOutcome.TIMEOUT,
            core.FailureCode.COMMAND_TIMEOUT,
        )
        self.assertEqual(
            failed.completed_at_utc,
            datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        )

    def test_memory_pressure_accepts_only_public_levels(self) -> None:
        accepted = {
            "1\n": core.MemoryPressureLevel.NORMAL,
            "2": core.MemoryPressureLevel.WARN,
            "4\n": core.MemoryPressureLevel.CRITICAL,
        }
        for raw, expected in accepted.items():
            with self.subTest(raw=raw):
                self.assertEqual(porkyhud.parse_memory_pressure_level(raw).level, expected)
        for raw in ("", "0", "3", "8", "normal", "1 warning", "01", "+1", "1\n2"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    porkyhud.parse_memory_pressure_level(raw)

    def test_memory_pressure_collector_preserves_command_failure_and_classifies_parse_errors(self) -> None:
        with mock.patch.object(porkyhud, "run_structured_command", return_value=self.command_result("2\n")) as run:
            result = porkyhud.collect_memory_pressure()
        self.assertEqual(result.value, core.MemoryPressure(core.MemoryPressureLevel.WARN))
        run.assert_called_once_with(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            category="memory-pressure", timeout_s=1.0, stdout_limit=8192, stderr_limit=8192,
        )
        timeout = self.command_result(
            outcome=core.AttemptOutcome.TIMEOUT, failure=core.FailureCode.COMMAND_TIMEOUT,
        )
        with mock.patch.object(porkyhud, "run_structured_command", return_value=timeout):
            failed = porkyhud.collect_memory_pressure()
        self.assertEqual((failed.outcome, failed.failure),
                         (core.AttemptOutcome.TIMEOUT, core.FailureCode.COMMAND_TIMEOUT))
        with mock.patch.object(porkyhud, "run_structured_command", return_value=self.command_result("")):
            empty = porkyhud.collect_memory_pressure()
        self.assertEqual((empty.outcome, empty.failure),
                         (core.AttemptOutcome.PARSE_ERROR, core.FailureCode.EMPTY_OUTPUT))
        with mock.patch.object(porkyhud, "run_structured_command", return_value=self.command_result("3")):
            rejected = porkyhud.collect_memory_pressure()
        self.assertEqual((rejected.outcome, rejected.failure),
                         (core.AttemptOutcome.PARSE_ERROR, core.FailureCode.PARSER_REJECTED))

    def test_thermal_requires_both_explicit_signals(self) -> None:
        clear = ("Note: No thermal warning level has been recorded\n"
                 "Note: No performance warning level has been recorded\n")
        self.assertEqual(porkyhud.parse_os_thermal_state(clear), core.OsThermalState(0, 0))
        self.assertEqual(porkyhud.parse_os_thermal_state(
            "Thermal Warning Level = 0\nPerformance Warning Level = 3\n"),
            core.OsThermalState(0, 3))
        self.assertEqual(porkyhud.parse_os_thermal_state(
            "Thermal Warning Level = 1\nPerformance Warning Level = 2\n"),
            core.OsThermalState(1, 2))
        for raw in ("Thermal Warning Level = 1\n", "localized unknown output\n", "",
                    "Thermal Warning Level = -1\nPerformance Warning Level = 0\n",
                    "Thermal Warning Level = 1\nThermal Warning Level = 2\n"
                    "Performance Warning Level = 0\n"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    porkyhud.parse_os_thermal_state(raw)

    def test_thermal_collector_uses_exact_public_command_and_failure_classes(self) -> None:
        raw = "Thermal Warning Level = 0\nPerformance Warning Level = 1\n"
        with mock.patch.object(porkyhud, "run_structured_command", return_value=self.command_result(raw)) as run:
            result = porkyhud.collect_os_thermal_state()
        self.assertEqual(result.value, core.OsThermalState(0, 1))
        run.assert_called_once_with(
            ["/usr/bin/pmset", "-g", "therm"], category="thermal-os", timeout_s=1.5,
            stdout_limit=8192, stderr_limit=8192,
        )
        nonzero = self.command_result(
            outcome=core.AttemptOutcome.COMMAND_ERROR, failure=core.FailureCode.NONZERO_EXIT,
        )
        with mock.patch.object(porkyhud, "run_structured_command", return_value=nonzero):
            failed = porkyhud.collect_os_thermal_state()
        self.assertEqual((failed.outcome, failed.failure),
                         (core.AttemptOutcome.COMMAND_ERROR, core.FailureCode.NONZERO_EXIT))

    def test_disk_capacity_prefers_writable_data_and_preserves_provenance(self) -> None:
        stat = type("Stat", (), {"f_flag": 0, "f_fsid": 44})()
        usage = type("Usage", (), {"total": 1000, "used": 250, "free": 750})()
        with mock.patch.object(porkyhud.os, "statvfs", return_value=stat), \
             mock.patch.object(porkyhud.shutil, "disk_usage", return_value=usage):
            result = porkyhud.collect_disk_capacity()
        self.assertEqual(result.value, core.DiskCapacity("fsid:44", "Data", "/System/Volumes/Data", 1000, 750))

    def test_disk_capacity_uses_writable_root_and_reports_absence_or_inspection_error(self) -> None:
        readonly = type("Stat", (), {"f_flag": porkyhud.os.ST_RDONLY, "f_fsid": 1})()
        root = type("Stat", (), {"f_flag": 0, "f_fsid": 2})()
        usage = type("Usage", (), {"total": 200, "used": 50, "free": 150})()
        with mock.patch.object(porkyhud.os, "statvfs", side_effect=[readonly, root]), \
             mock.patch.object(porkyhud.shutil, "disk_usage", return_value=usage):
            fallback = porkyhud.collect_disk_capacity()
        self.assertEqual(fallback.value, core.DiskCapacity("fsid:2", "Root", "/", 200, 150))
        with mock.patch.object(
            porkyhud.os, "statvfs",
            side_effect=[FileNotFoundError("Data absent"), readonly],
        ):
            absent = porkyhud.collect_disk_capacity()
        self.assertEqual((absent.outcome, absent.failure),
                         (core.AttemptOutcome.NOT_APPLICABLE, core.FailureCode.NOT_APPLICABLE))
        with mock.patch.object(porkyhud.os, "statvfs", side_effect=OSError("boom")):
            broken = porkyhud.collect_disk_capacity()
        self.assertEqual((broken.outcome, broken.failure),
                         (core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION))

    def test_disk_capacity_does_not_treat_inaccessible_mounts_as_absent(self) -> None:
        with mock.patch.object(
            porkyhud.os, "statvfs", side_effect=PermissionError("denied"),
        ):
            result = porkyhud.collect_disk_capacity()
        self.assertEqual(
            (result.outcome, result.failure),
            (core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION),
        )

    def test_disk_capacity_contains_unexpected_inspection_exceptions(self) -> None:
        with mock.patch.object(
            porkyhud.os, "statvfs", side_effect=RuntimeError("unexpected"),
        ):
            try:
                result = porkyhud.collect_disk_capacity()
            except RuntimeError as exc:
                self.fail("disk inspection exception escaped: %s" % exc)
        self.assertEqual(
            (result.outcome, result.failure),
            (core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION),
        )

    def test_disk_capacity_prefers_writable_root_after_inaccessible_data(self) -> None:
        root = type("Stat", (), {"f_flag": 0, "f_fsid": 22})()
        usage = type("Usage", (), {"total": 400, "used": 100, "free": 300})()

        def inspect(mount: str) -> object:
            if mount == "/System/Volumes/Data":
                raise PermissionError("denied")
            return root

        with mock.patch.object(
            porkyhud.os, "statvfs", side_effect=inspect,
        ), mock.patch.object(
            porkyhud.shutil, "disk_usage", return_value=usage,
        ):
            result = porkyhud.collect_disk_capacity()
        self.assertEqual(
            result.value,
            core.DiskCapacity("fsid:22", "Root", "/", 400, 300),
        )

    def test_disk_capacity_retries_root_after_data_metadata_failure(self) -> None:
        root = DiskStatFixture(f_fsid=22)
        usage = DiskUsageFixture(total=400, used=100, free=300)
        for field in ("f_flag", "f_fsid"):
            with self.subTest(field=field), mock.patch.object(
                porkyhud.os,
                "statvfs",
                side_effect=[
                    DiskStatFixture(
                        faulting_field=field,
                        fault=RuntimeError("broken Data metadata"),
                    ),
                    root,
                ],
            ), mock.patch.object(porkyhud.shutil, "disk_usage", return_value=usage):
                try:
                    result = porkyhud.collect_disk_capacity()
                except Exception as exc:
                    self.fail("Data metadata failure escaped instead of trying Root: %s" % exc)
                self.assertEqual(
                    result.value,
                    core.DiskCapacity("fsid:22", "Root", "/", 400, 300),
                )

    def test_disk_capacity_contains_the_full_candidate_inspection_boundary(self) -> None:
        valid_stat = DiskStatFixture(f_fsid=7)
        valid_usage = DiskUsageFixture()
        scenarios = (
            (
                "f_flag metadata",
                DiskStatFixture(faulting_field="f_flag", fault=RuntimeError("bad flags")),
                valid_usage,
            ),
            (
                "f_fsid metadata",
                DiskStatFixture(faulting_field="f_fsid", fault=RuntimeError("bad fsid")),
                valid_usage,
            ),
            (
                "usage total",
                valid_stat,
                DiskUsageFixture(faulting_field="total", fault=RuntimeError("bad total")),
            ),
            (
                "usage free",
                valid_stat,
                DiskUsageFixture(faulting_field="free", fault=RuntimeError("bad free")),
            ),
            (
                "capacity validation",
                valid_stat,
                DiskUsageFixture(total=0, used=0, free=0),
            ),
            (
                "late missing metadata",
                DiskStatFixture(
                    faulting_field="f_fsid",
                    fault=FileNotFoundError("metadata vanished"),
                ),
                valid_usage,
            ),
            (
                "late missing usage",
                valid_stat,
                DiskUsageFixture(
                    faulting_field="total",
                    fault=FileNotFoundError("usage vanished"),
                ),
            ),
        )
        for name, filesystem, usage in scenarios:
            with self.subTest(name=name), mock.patch.object(
                porkyhud.os, "statvfs", side_effect=[filesystem, filesystem],
            ), mock.patch.object(porkyhud.shutil, "disk_usage", return_value=usage):
                try:
                    result = porkyhud.collect_disk_capacity()
                except Exception as exc:
                    self.fail("candidate inspection failure escaped: %s" % exc)
                self.assertEqual(
                    (result.outcome, result.failure),
                    (core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION),
                )

    def test_disk_capacity_candidate_boundary_propagates_process_control(self) -> None:
        interruption = KeyboardInterrupt("stop disk metadata inspection")
        with mock.patch.object(
            porkyhud.os,
            "statvfs",
            return_value=DiskStatFixture(faulting_field="f_flag", fault=interruption),
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                porkyhud.collect_disk_capacity()
        self.assertIs(raised.exception, interruption)

        termination = SystemExit("stop disk capacity construction")
        with mock.patch.object(
            porkyhud.os, "statvfs", return_value=DiskStatFixture(),
        ), mock.patch.object(
            porkyhud.shutil, "disk_usage", return_value=DiskUsageFixture(),
        ), mock.patch.object(
            porkyhud, "DiskCapacity", side_effect=termination,
        ):
            with self.assertRaises(SystemExit) as raised:
                porkyhud.collect_disk_capacity()
        self.assertIs(raised.exception, termination)

    def test_cpu_usage_uses_counter_read_window_and_resets_on_bad_counters(self) -> None:
        sampler = object.__new__(porkyhud.MacCpuSampler)
        sampler.available = True
        sampler.previous = [(100, 0, 100, 0), (10, 0, 10, 0)]
        sampler.last_read_completed_mono_s = 5.0
        sampler.last_per_core = ()
        sampler._read_raw = mock.Mock(return_value=[(130, 0, 110, 0), (20, 0, 20, 0)])
        with mock.patch.object(porkyhud.time, "monotonic", side_effect=[7.0, 7.5]), \
             mock.patch.object(porkyhud, "utc_now", return_value=datetime(2026, 8, 11, 12, tzinfo=timezone.utc)):
            result = sampler.sample_usage()
        self.assertAlmostEqual(result.value.percent, 40 / 60 * 100)
        self.assertEqual((result.value.window_started_mono_s, result.value.window_completed_mono_s), (5.0, 7.5))
        self.assertEqual(sampler.last_per_core, (75.0, 50.0))
        sampler._read_raw = mock.Mock(return_value=[(1, 0, 1, 0), (1, 0, 1, 0)])
        with mock.patch.object(porkyhud.time, "monotonic", side_effect=[8.0, 8.5]), \
             mock.patch.object(porkyhud, "utc_now", return_value=datetime(2026, 8, 11, 12, tzinfo=timezone.utc)):
            reset = sampler.sample_usage()
        self.assertEqual((reset.outcome, reset.failure),
                         (core.AttemptOutcome.PARSE_ERROR, core.FailureCode.PARSER_REJECTED))
        self.assertIsNone(reset.value)
        self.assertEqual(sampler.last_per_core, ())
        sampler._read_raw = mock.Mock(return_value=[])
        with mock.patch.object(porkyhud.time, "monotonic", side_effect=[9.0, 9.5]), \
             mock.patch.object(porkyhud, "utc_now", return_value=datetime(2026, 8, 11, 12, tzinfo=timezone.utc)):
            missing = sampler.sample_usage()
        self.assertEqual((missing.outcome, missing.failure),
                         (core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION))
        self.assertIsNone(missing.value)

    def test_cpu_transient_initial_read_retries_and_uses_successful_read_window(self) -> None:
        baseline = [(100, 10, 90, 0), (10, 0, 90, 0)]
        current = [(130, 20, 100, 0), (20, 10, 110, 0)]
        completed_at = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
        for initial in ([], RuntimeError("transient native read")):
            with self.subTest(initial=type(initial).__name__):
                reads: list[object] = [initial, baseline, current]

                def read_raw(_sampler: object) -> list[tuple[int, int, int, int]]:
                    value = reads.pop(0)
                    if isinstance(value, Exception):
                        raise value
                    return value  # type: ignore[return-value]

                clock_values = iter((10.0, 10.5, 11.0, 12.0))
                with mock.patch.object(porkyhud.ctypes, "CDLL", return_value=mock.Mock()), \
                     mock.patch.object(porkyhud.MacCpuSampler, "_read_raw", autospec=True,
                                       side_effect=read_raw), \
                     mock.patch.object(porkyhud.time, "monotonic",
                                       side_effect=lambda: next(clock_values, 99.0)), \
                     mock.patch.object(porkyhud, "utc_now", return_value=completed_at):
                    sampler = porkyhud.MacCpuSampler()
                    primed = sampler.sample_usage()
                    result = sampler.sample_usage()

                self.assertEqual(
                    (primed.outcome, primed.failure, primed.value),
                    (core.AttemptOutcome.PARSE_ERROR, core.FailureCode.PARSER_REJECTED, None),
                )
                self.assertEqual(
                    (result.value.window_started_mono_s, result.value.window_completed_mono_s),
                    (10.5, 12.0),
                )
                self.assertAlmostEqual(result.value.percent, 60 / 90 * 100)
                self.assertEqual(sampler.last_per_core, (80.0, 50.0))

    def test_cpu_native_setup_failure_remains_unavailable(self) -> None:
        with mock.patch.object(
            porkyhud.ctypes, "CDLL", side_effect=OSError("native library unavailable"),
        ), mock.patch.object(
            porkyhud.time, "monotonic", side_effect=[1.0, 1.5],
        ), mock.patch.object(
            porkyhud, "utc_now", return_value=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        ):
            sampler = porkyhud.MacCpuSampler()
            result = sampler.sample_usage()
        self.assertFalse(sampler.available)
        self.assertEqual(
            (result.outcome, result.failure),
            (core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION),
        )

    def test_sensor_os_thermal_is_independent_and_missing_state_is_unavailable(self) -> None:
        os_state = core.OsThermalState(0, 2)
        with mock.patch.object(porkyhud, "run_command") as run, \
             mock.patch.object(porkyhud, "thermal_level_from_sysctl") as private_probe, \
             mock.patch.object(porkyhud, "read_smc_sensors", return_value=([], [])), \
             mock.patch.object(porkyhud, "powermetrics_sample", return_value="thermal pressure: nominal"):
            sensor = porkyhud.sensor_info(os_state)
        run.assert_not_called()
        private_probe.assert_not_called()
        self.assertEqual((sensor.thermal_warning, sensor.performance_warning), ("nominal", "active"))
        with mock.patch.object(porkyhud, "read_smc_sensors", return_value=([], [])), \
             mock.patch.object(porkyhud, "powermetrics_sample", return_value="thermal pressure: active"):
            unavailable = porkyhud.sensor_info()
        self.assertEqual((unavailable.thermal_warning, unavailable.performance_warning),
                         ("unavailable", "unavailable"))

    def test_sudoers_rule_has_intrinsic_timeout_and_check_only_validation(self) -> None:
        expected = (
            "# PorkyHUD advanced sensor access.\n"
            "# Allows admin users to run PorkyHUD's bounded powermetrics samples without a password.\n"
            "Cmnd_Alias PORKYHUD_POWERMETRICS = /usr/bin/powermetrics --samplers thermal\\,cpu_power\\,gpu_power\\,ane_power\\,battery\\,smc --show-extra-power-info -n 1 -i 1000, /usr/bin/powermetrics --samplers thermal\\,cpu_power\\,gpu_power\\,ane_power\\,battery --show-extra-power-info -n 1 -i 1000, /usr/bin/powermetrics --samplers all --show-extra-power-info -n 1 -i 1000\n"
            "%admin ALL=(root) TIMEOUT=3s NOPASSWD: PORKYHUD_POWERMETRICS\n"
        )
        self.assertEqual(porkyhud.sudoers_contents(), expected)
        with mock.patch.object(porkyhud.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(porkyhud.validate_sudoers_file("/tmp/porkyhud-rule"))
        run.assert_called_once_with([porkyhud.VISUDO_PATH, "-c", "-s", "-f", "/tmp/porkyhud-rule"])

    def test_invalid_generated_sudoers_rule_fails_closed_before_privileged_actions(self) -> None:
        fake_file = mock.mock_open()
        with mock.patch.object(porkyhud.platform, "system", return_value="Darwin"), \
             mock.patch.object(porkyhud.os.path, "exists", return_value=True), \
             mock.patch.object(porkyhud.tempfile, "mkstemp",
                               return_value=(123, "/tmp/porkyhud-test-rule")), \
             mock.patch.object(porkyhud.os, "fdopen", fake_file), \
             mock.patch.object(porkyhud.os, "chmod"), \
             mock.patch.object(porkyhud.os, "unlink"), \
             mock.patch.object(porkyhud, "validate_sudoers_file",
                               return_value=False) as validate, \
             mock.patch.object(porkyhud.subprocess, "run") as privileged_run, \
             mock.patch.object(porkyhud, "remove_sensor_access") as remove_access, \
             mock.patch.object(porkyhud, "powermetrics_sample") as sample_power, \
             mock.patch.object(porkyhud, "advanced_sensor_access_available") as access_check, \
             mock.patch("builtins.print"):
            result = porkyhud.install_sensor_access()

        self.assertEqual(result, 1)
        validate.assert_called_once_with("/tmp/porkyhud-test-rule")
        privileged_run.assert_not_called()
        remove_access.assert_not_called()
        sample_power.assert_not_called()
        access_check.assert_not_called()

    @unittest.skipUnless(porkyhud.platform.system() == "Darwin", "visudo syntax is macOS-specific")
    def test_generated_sudoers_timeout_passes_macos_check_only_validation(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as rule:
            rule.write(porkyhud.sudoers_contents())
            rule.flush()
            self.assertTrue(porkyhud.validate_sudoers_file(rule.name))


class CommandPrivacyTests(unittest.TestCase):
    def test_sensitive_arguments_are_redacted_in_common_forms(self) -> None:
        command = (
            "/usr/bin/tool --api-key=alpha --token beta "
            "--password gamma /secret=delta "
            "--authorization \"Bearer epsilon\" --access-token='zeta eta' "
            "--mode analyze"
        )
        sanitized = porkyhud.sanitize_command(command)
        for secret in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"):
            self.assertNotIn(secret, sanitized)
        self.assertEqual(sanitized.count("<redacted>"), 6)
        self.assertIn("--mode analyze", sanitized)

    def test_visible_command_keeps_identity_and_truncates_from_the_right(self) -> None:
        command = "/usr/local/bin/python3 --api-key SECRET --mode analyze-a-long-input"
        visible = porkyhud.visible_command(command, 24)
        self.assertTrue(visible.startswith("python3"))
        self.assertTrue(visible.endswith("..."))
        self.assertLessEqual(len(visible), 24)
        self.assertNotIn("SECRET", visible)

        app = (
            "/Applications/RenderPro.app/Contents/MacOS/RenderPro "
            "--token SECRET --scene alpha"
        )
        app_visible = porkyhud.visible_command(app, 28)
        self.assertTrue(app_visible.startswith("RenderPro"))
        self.assertTrue(app_visible.endswith("..."))
        self.assertNotIn("SECRET", app_visible)

    def test_tiny_visible_command_is_left_anchored(self) -> None:
        self.assertEqual(porkyhud.visible_command("/usr/bin/python3 --flag", 2), "py")


class BatteryBehaviorTests(unittest.TestCase):
    def test_batteryless_mac_is_presented_as_desktop_ac_power(self) -> None:
        def fake_command(args: list[str], timeout: float = 2.0) -> str:
            del timeout
            if args == ["pmset", "-g", "batt"]:
                return "Now drawing from 'AC Power'"
            if args[:4] == ["ioreg", "-r", "-c", "AppleSmartBattery"]:
                return ""
            return ""

        with mock.patch.object(porkyhud, "run_command", side_effect=fake_command):
            battery = porkyhud.battery_info()

        self.assertFalse(battery.present)
        self.assertTrue(battery.external_connected)
        self.assertIsNone(battery.percent)
        self.assertEqual(
            porkyhud.battery_summary(battery),
            "AC Power: no internal battery",
        )

    def test_batteryless_power_panel_has_no_empty_battery_bar(self) -> None:
        screen = FakeScreen(12, 60)
        battery = make_battery(
            percent=None,
            present=False,
            source="AC Power",
            external_connected=True,
        )
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), mock.patch.object(
            porkyhud,
            "color_pair",
            return_value=0,
        ):
            porkyhud.draw_battery_panel(screen, 1, 2, 7, 40, battery, 0)
        rendered = "\n".join(screen.row(row) for row in range(1, 8))
        self.assertIn("desktop power", rendered)
        self.assertIn("internal battery: not present", rendered)
        self.assertNotIn("0.0%", rendered)

    def test_present_battery_fields_are_parsed(self) -> None:
        pmset = """Now drawing from 'AC Power'
-InternalBattery-0 72%; AC attached; not charging present: true
"""
        ioreg = """
        "ExternalConnected" = Yes
        "IsCharging" = No
        "FullyCharged" = No
        "CycleCount" = 12
        "DesignCycleCount9C" = 1000
        "MaxCapacity" = 96
        "Temperature" = 3000
        "VirtualTemperature" = 3120
        "Voltage" = 12400
        "InstantAmperage" = 18446744073709551416
        "Watts" = 96
        "Name" = "USB-C Adapter"
        """

        def fake_command(args: list[str], timeout: float = 2.0) -> str:
            del timeout
            return pmset if args == ["pmset", "-g", "batt"] else ioreg

        with mock.patch.object(porkyhud, "run_command", side_effect=fake_command):
            battery = porkyhud.battery_info()

        self.assertTrue(battery.present)
        self.assertEqual(battery.percent, 72)
        self.assertTrue(battery.external_connected)
        self.assertFalse(battery.is_charging)
        self.assertEqual(battery.remaining, "not charging")
        self.assertEqual(battery.cycle_count, 12)
        self.assertEqual(battery.health_percent, 96)
        self.assertAlmostEqual(battery.temp_c or 0, 26.85)
        self.assertAlmostEqual(battery.virtual_temp_c or 0, 31.2)
        self.assertAlmostEqual(battery.voltage_v or 0, 12.4)
        self.assertAlmostEqual(battery.amperage_a or 0, -0.2)


class AsyncPollerTests(unittest.TestCase):
    def test_error_keeps_last_good_value_and_snapshot_reports_age(self) -> None:
        outcomes: list[object] = ["fresh", RuntimeError("collector failed")]

        def collector() -> object:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        poller = porkyhud.AsyncPoller(5.0, collector, initial="initial")
        with mock.patch.object(porkyhud.time, "monotonic", side_effect=[10.0, 11.0]):
            poller._run()
        self.assertEqual(poller.value, "fresh")
        self.assertEqual(poller.last_success, 10.0)
        self.assertEqual(poller.last_finish, 11.0)

        with mock.patch.object(porkyhud.time, "monotonic", return_value=20.0):
            poller._run()
        self.assertEqual(poller.value, "fresh")
        self.assertEqual(poller.error, "collector failed")
        self.assertEqual(poller.last_success, 10.0)

        value, age, error = poller.snapshot(25.0)
        self.assertEqual(value, "fresh")
        self.assertEqual(age, 15.0)
        self.assertEqual(error, "collector failed")

    def test_poll_cadence_is_measured_from_completion_and_is_single_flight(self) -> None:
        class FakeThread:
            instances: list["FakeThread"] = []

            def __init__(self, target: object, daemon: bool) -> None:
                self.target = target
                self.daemon = daemon
                self.started = False
                self.__class__.instances.append(self)

            def start(self) -> None:
                self.started = True

        poller = porkyhud.AsyncPoller(5.0, lambda: "sample")
        poller.last_start = 10.0
        poller.last_finish = 20.0

        with mock.patch.object(porkyhud.threading, "Thread", FakeThread):
            poller.tick(24.999)
            self.assertEqual(FakeThread.instances, [])

            poller.tick(25.0)
            self.assertEqual(len(FakeThread.instances), 1)
            self.assertTrue(FakeThread.instances[0].started)
            self.assertTrue(poller._running)
            self.assertEqual(poller.last_start, 25.0)

            poller.tick(100.0, force=True)
            self.assertEqual(len(FakeThread.instances), 1)
            self.assertTrue(poller._force_pending)

            first_target = FakeThread.instances[0].target
            self.assertTrue(callable(first_target))
            first_target()
            self.assertEqual(len(FakeThread.instances), 2)
            self.assertTrue(FakeThread.instances[1].started)
            self.assertFalse(poller._force_pending)

    def test_failed_initial_samples_age_from_the_first_attempt(self) -> None:
        poller = porkyhud.AsyncPoller(5.0, lambda: None)
        poller.first_start = 10.0
        poller.last_start = 25.0
        poller.last_finish = 26.0
        poller.error = "collector failed"
        self.assertEqual(
            poller.snapshot(30.0),
            (None, 20.0, "collector failed"),
        )

    def test_fresh_initial_value_uses_initialization_as_completion_time(self) -> None:
        with mock.patch.object(porkyhud.time, "monotonic", return_value=50.0):
            poller = porkyhud.AsyncPoller(
                5.0,
                lambda: "later",
                initial="ready",
                initial_is_fresh=True,
            )
        self.assertEqual(poller.snapshot(52.0), ("ready", 2.0, ""))
        self.assertEqual(poller.last_start, 50.0)
        self.assertEqual(poller.last_finish, 50.0)


class LiveOrchestrationTests(unittest.TestCase):
    UTC = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)

    def command_result(
        self, *, stdout: str = "", stderr: str = "", outcome=core.AttemptOutcome.SUCCESS,
        failure=None, returncode: int | None = 0,
    ) -> porkyhud.CommandResult:
        return porkyhud.CommandResult(
            "sensor-powermetrics", stdout, stderr, returncode, 1.0, 1.1,
            self.UTC, outcome, failure,
        )

    def adapter_success(self, value, started: float, completed: float):
        return porkyhud.AdapterResult(
            value, started, completed, self.UTC,
            core.AttemptOutcome.SUCCESS, None,
        )

    def live_cpu_frame(
        self, revision, status=core.SourceViewStatus.LIVE, ready=True,
    ):
        frame = porkyhud.initial_health_frame(float(max(0, revision or 0)))
        views = dict(frame.source_views)
        views[core.SourceId.CPU_USAGE] = core.SourceView(
            core.SourceId.CPU_USAGE, status, 0.0, False, ready, revision, None,
        )
        return replace(frame, source_views=views)

    def threaded_call(self, callback):
        done, errors = threading.Event(), []
        def run():
            try:
                callback()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, done, errors

    def wait_for_revision(self, poller, revision):
        with poller._condition:
            return poller._condition.wait_for(
                lambda: poller._state.active_revision is None
                and poller._state.last_attempt is not None
                and poller._state.last_attempt.revision == revision,
                1.0,
            )

    class SyncPoller:
        def __init__(self, _interval, collector, initial=None, initial_is_fresh=False):
            del initial_is_fresh
            self.collector, self.value, self.started = collector, initial, False
        def tick(self, _now, force=False):
            del force
            if not self.started:
                self.started = True
                if self.value is None:
                    self.value = self.collector()
            return self.value
        def snapshot(self, _now=None):
            return self.value, 0.0, ""
        def sample_snapshot(self, _now=None):
            return porkyhud.DescriptiveSample(self.value, 0.0, "", 0, 0.0)

    def test_initial_health_frame_is_pristine_and_registry_ordered(self) -> None:
        frame = porkyhud.initial_health_frame(10.0)
        self.assertEqual(
            tuple(frame.source_views),
            (
                core.SourceId.CPU_USAGE,
                core.SourceId.MEMORY_PRESSURE,
                core.SourceId.THERMAL_OS,
                core.SourceId.DISK_CAPACITY,
                core.SourceId.PROCESS_TABLE,
                core.SourceId.PROCESS_GPU,
            ),
        )
        self.assertEqual(frame.captured_mono_s, 10.0)
        self.assertEqual(frame.origin, porkyhud.FrameOrigin.LIVE)
        self.assertEqual(frame.assessment.severity, core.HealthSeverity.UNKNOWN)

    def test_memory_detail_uses_cached_size_and_merge_does_not_mutate(self) -> None:
        raw = "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 2.\nAnonymous pages: 0.\nPages wired down: 0.\nPages occupied by compressor: 0.\n"
        with mock.patch.object(porkyhud, "MEMORY_SIZE_BYTES", None), \
                mock.patch.object(porkyhud, "sysctl_value", return_value="8192") as size, \
                mock.patch.object(porkyhud, "run_command", return_value=raw):
            first = porkyhud.memory_detail()
            second = porkyhud.memory_detail()
        self.assertEqual((first["total"], second["total"]), (8192.0, 8192.0))
        self.assertEqual(size.call_count, 1)
        merged = porkyhud.merge_memory_stats(first, (3.0, 4.0))
        self.assertNotIn("swap_used", first)
        self.assertEqual((merged["swap_used"], merged["swap_total"]), (3.0, 4.0))

    def test_network_rates_prime_then_compute_and_reset_on_counter_reversal(self) -> None:
        baseline, rates = porkyhud._network_rate_update(
            None, (1_000, 2_000), 10.0,
        )
        self.assertEqual(baseline, ((1_000, 2_000), 10.0))
        self.assertIsNone(rates)

        unchanged, rates = porkyhud._network_rate_update(
            baseline, (1_000, 2_000), 10.5,
        )
        self.assertEqual(unchanged, ((1_000, 2_000), 10.5))
        self.assertEqual(rates, (0.0, 0.0))

        baseline, rates = porkyhud._network_rate_update(
            baseline, (1_600, 2_300), 11.0,
        )
        self.assertEqual(baseline, ((1_600, 2_300), 11.0))
        self.assertEqual(rates, (600.0, 300.0))

        baseline, rates = porkyhud._network_rate_update(
            baseline, (100, 2_500), 12.0,
        )
        self.assertEqual(baseline, ((100, 2_500), 12.0))
        self.assertIsNone(rates)

        baseline, rates = porkyhud._network_rate_update(
            baseline, (300, 2_900), 14.0,
        )
        self.assertEqual(rates, (100.0, 200.0))

    def test_steady_state_launch_budget_is_exactly_bounded(self) -> None:
        budget = porkyhud.steady_state_launch_budget()
        self.assertEqual(budget["scheduled"], 228)
        self.assertEqual(budget["retry_reserve"], 12)
        self.assertEqual(budget["total"], 240)

    def test_powermetrics_attempt_uses_one_child_and_only_exact_sudo_proves_lock(self) -> None:
        denied = self.command_result(
            stderr="sudo: a password is required\n",
            outcome=core.AttemptOutcome.COMMAND_ERROR,
            failure=core.FailureCode.NONZERO_EXIT,
            returncode=1,
        )
        with mock.patch.object(porkyhud, "POWER_METRICS_SAMPLER_INDEX", 0), \
                mock.patch.object(porkyhud, "run_structured_command", return_value=denied) as runner:
            attempt = porkyhud._collect_powermetrics_attempt()
        self.assertEqual(attempt, porkyhud._PowermetricsAttempt("", True))
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.kwargs["category"], "sensor-powermetrics")
        self.assertEqual(runner.call_args.kwargs["timeout_s"], 5.5)

    def test_empty_powermetrics_advances_candidate_for_next_attempt_only(self) -> None:
        empty = self.command_result(stdout=" \n\t")
        good = self.command_result(stdout="thermal pressure: nominal")
        with mock.patch.object(porkyhud, "POWER_METRICS_SAMPLER_INDEX", 0), \
                mock.patch.object(porkyhud, "run_structured_command", side_effect=[empty, good]) as runner:
            first = porkyhud._collect_powermetrics_attempt()
            self.assertEqual(first.text, "")
            self.assertEqual(runner.call_count, 1)
            second = porkyhud._collect_powermetrics_attempt()
        self.assertEqual(second.text, "thermal pressure: nominal")
        self.assertEqual(runner.call_count, 2)
        first_args = runner.call_args_list[0].args[0]
        second_args = runner.call_args_list[1].args[0]
        self.assertNotEqual(first_args, second_args)

    def test_powermetrics_advances_only_for_exact_attempted_sampler_rejection(self) -> None:
        samplers = porkyhud.POWER_METRICS_SAMPLER_SETS[0]
        cases = (
            ("powermetrics: unrecognized sampler: smc\n", 1),
            ("powermetrics: internal I/O error", 0),
            ("powermetrics: unrecognized sampler: smc (disabled)", 0),
            ("error: powermetrics: unrecognized sampler: smc", 0),
            ("powermetrics: unrecognized sampler: tasks", 0),
        )
        for stderr, expected_index in cases:
            with self.subTest(stderr=stderr):
                rejected = self.command_result(
                    stderr=stderr,
                    outcome=core.AttemptOutcome.COMMAND_ERROR,
                    failure=core.FailureCode.NONZERO_EXIT,
                    returncode=1,
                )
                with mock.patch.object(
                    porkyhud, "POWER_METRICS_SAMPLER_INDEX", 0,
                ), mock.patch.object(
                    porkyhud, "run_structured_command", return_value=rejected,
                ):
                    porkyhud._collect_powermetrics_attempt()
                    self.assertEqual(
                        porkyhud.POWER_METRICS_SAMPLER_INDEX, expected_index,
                    )

    def test_frame_rejects_missing_extra_and_wrong_registry_states(self) -> None:
        states = dict(porkyhud._registry_states())
        missing = dict(states)
        missing.pop(core.SourceId.PROCESS_GPU)
        with self.assertRaises(ValueError):
            porkyhud.build_health_frame(core.initial_policy_state(), missing, 1.0)
        extra = dict(states)
        extra[core.SourceId.BATTERY] = core.new_source(core.SourceId.BATTERY, 1.0, 1.0)
        with self.assertRaises(ValueError):
            porkyhud.build_health_frame(core.initial_policy_state(), extra, 1.0)
        wrong = dict(states)
        wrong[core.SourceId.CPU_USAGE] = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        with self.assertRaises(ValueError):
            porkyhud.build_health_frame(core.initial_policy_state(), wrong, 1.0)
        wrong_cadence = dict(states)
        wrong_cadence[core.SourceId.CPU_USAGE] = core.new_source(
            core.SourceId.CPU_USAGE, 2.0, 2.5,
        )
        with self.assertRaises(ValueError):
            porkyhud.build_health_frame(
                core.initial_policy_state(), wrong_cadence, 1.0,
            )

    def test_default_sensor_is_not_locked_without_permission_proof(self) -> None:
        self.assertFalse(porkyhud.default_sensor_info().privileged_locked)

    def test_checking_shell_refreshes_before_any_collection(self) -> None:
        events: list[str] = []
        screen = FakeScreen(24, 80, keys=[ord("q")],
                            on_refresh=lambda: events.append("refresh"))
        coordinator = mock.Mock()
        with mock.patch.object(porkyhud, "init_colors"), \
                mock.patch.object(porkyhud, "apply_dark_screen"), \
                mock.patch.object(porkyhud, "draw_initial_shell",
                                  side_effect=lambda *_args: events.append("shell")), \
                mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "collect_static_info", side_effect=lambda: events.append("static")), \
                mock.patch.object(porkyhud, "run_command", side_effect=lambda *_args, **_kwargs: events.append("command")):
            porkyhud.hud(screen)
        self.assertEqual(events[:2], ["shell", "refresh"])
        self.assertNotIn("static", events)
        self.assertNotIn("command", events)
        coordinator.close.assert_called_once_with()

    def test_hud_closes_coordinator_when_input_raises_after_shell(self) -> None:
        class FailingScreen(FakeScreen):
            def getch(self) -> int:
                raise RuntimeError("input failed")

        coordinator = mock.Mock()
        screen = FailingScreen(24, 80)
        with mock.patch.object(porkyhud, "init_colors"), \
                mock.patch.object(porkyhud, "apply_dark_screen"), \
                mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator):
            with self.assertRaisesRegex(RuntimeError, "input failed"):
                porkyhud.hud(screen)
        coordinator.close.assert_called_once_with()

    def test_health_frame_rejects_forged_scalar_contributor_and_trigger_values(self) -> None:
        frame = porkyhud.initial_health_frame(1.0)
        with self.assertRaises(ValueError):
            porkyhud.HealthFrame(
                frame.assessment, frame.source_views, 101.0, frame.memory_pressure,
                frame.thermal, frame.disk_capacity, frame.contributor, frame.origin,
                frame.captured_mono_s, None,
            )

        with self.assertRaises(ValueError):
            porkyhud.HealthFrame(
                frame.assessment, frame.source_views, 10, frame.memory_pressure,
                frame.thermal, frame.disk_capacity, frame.contributor, frame.origin,
                frame.captured_mono_s, None,
            )
        ready_views = dict(frame.source_views)
        ready_views[core.SourceId.CPU_USAGE] = core.SourceView(
            core.SourceId.CPU_USAGE, core.SourceViewStatus.LIVE, 0.0,
            False, True, 1, None,
        )
        accepted = porkyhud.HealthFrame(
            frame.assessment, ready_views, 10, frame.memory_pressure,
            frame.thermal, frame.disk_capacity, frame.contributor, frame.origin,
            frame.captured_mono_s, None,
        )
        self.assertEqual(accepted.cpu_percent, 10)
        with self.assertRaises(ValueError):
            porkyhud.HealthFrame(
                frame.assessment, frame.source_views, True, frame.memory_pressure,
                frame.thermal, frame.disk_capacity, frame.contributor, frame.origin,
                frame.captured_mono_s, None,
            )
        with self.assertRaises(ValueError):
            porkyhud.HealthFrame(
                frame.assessment, frame.source_views, None, frame.memory_pressure,
                frame.thermal, frame.disk_capacity, object(), frame.origin,
                frame.captured_mono_s, None,
            )
        with self.assertRaises(ValueError):
            porkyhud.HealthFrame(
                frame.assessment, frame.source_views, None, frame.memory_pressure,
                frame.thermal, frame.disk_capacity, frame.contributor, frame.origin,
                frame.captured_mono_s, 2.0,
            )
        with self.assertRaises(ValueError):
            porkyhud.HealthFrame(
                frame.assessment, frame.source_views, None, frame.memory_pressure,
                frame.thermal, frame.disk_capacity,
                core.CpuContributor(core.ContributorState.LEADING, "python", 1),
                frame.origin, frame.captured_mono_s, None,
            )
        with self.assertRaises(ValueError):
            porkyhud.HealthFrame(
                frame.assessment, frame.source_views, None, frame.memory_pressure,
                frame.thermal, frame.disk_capacity, frame.contributor, frame.origin,
                frame.captured_mono_s, 0.5,
            )

    def test_process_state_uses_canonical_minimized_facts_and_preserves_metadata(self) -> None:
        command = "/usr/bin/python --token top-secret --mode run"
        state = core.new_source(core.SourceId.PROCESS_TABLE, 2.5, 7.5)
        state, revision = core.start_attempt(state, 1.0)
        state = core.complete_success(
            state, revision,
            (porkyhud.ProcessRow(10, 31.0, 1.0, None, "R", "00:03", command),),
            1.1, self.UTC,
        )
        safe = porkyhud._sanitized_process_state(state, 2.0)
        self.assertEqual(safe.last_valid.revision, state.last_valid.revision)
        self.assertEqual(safe.last_valid.completed_mono_s, state.last_valid.completed_mono_s)
        self.assertNotIn("top-secret", safe.last_valid.value[0].name)
        self.assertEqual(safe.last_valid.value, porkyhud.process_facts(state.last_valid.value))

    def test_discontinuity_boundaries_and_wall_only_reset_are_exact(self) -> None:
        coordinator = object.__new__(porkyhud.LiveHealthCoordinator)
        coordinator._lock = threading.RLock()
        coordinator._closed = False
        coordinator._previous_time = None
        coordinator._policy = core.initial_policy_state()
        coordinator._generation = 0
        coordinator._frame_states = porkyhud._registry_states()
        coordinator._per_core = ()
        coordinator._per_core_revision = 0
        coordinator._cpu_per_core_result = None
        coordinator._pollers = {}
        coordinator._create_generation = mock.Mock()
        coordinator._close_poller_set = mock.Mock(return_value=None)
        self.assertFalse(coordinator.observe_time(0.0, self.UTC))
        self.assertFalse(coordinator.observe_time(30.0, self.UTC + timedelta(seconds=30)))
        self.assertFalse(coordinator.observe_time(35.0, self.UTC + timedelta(seconds=40)))
        self.assertTrue(coordinator.observe_time(35.1, self.UTC + timedelta(seconds=46.0)))
        coordinator._create_generation.assert_not_called()
        self.assertTrue(coordinator.observe_time(66.0, self.UTC + timedelta(seconds=76.0)))
        coordinator._create_generation.assert_called_once()

    def test_powermetrics_timeout_and_permission_do_not_wrap_candidates(self) -> None:
        timeout = self.command_result(
            outcome=core.AttemptOutcome.TIMEOUT,
            failure=core.FailureCode.COMMAND_TIMEOUT, returncode=None,
        )
        denied = self.command_result(
            stderr="sudo: a password is required",
            outcome=core.AttemptOutcome.COMMAND_ERROR,
            failure=core.FailureCode.NONZERO_EXIT, returncode=1,
        )
        with mock.patch.object(porkyhud, "POWER_METRICS_SAMPLER_INDEX", 1), \
                mock.patch.object(porkyhud, "run_structured_command", side_effect=[timeout, denied]) as runner:
            self.assertEqual(porkyhud._collect_powermetrics_attempt().text, "")
            self.assertTrue(porkyhud._collect_powermetrics_attempt().permission_denied)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[0], runner.call_args_list[1].args[0])

    def test_draw_functions_do_not_call_collectors_or_policy_reducer(self) -> None:
        shell = FakeScreen(24, 80)
        resize = FakeScreen(10, 60)
        with mock.patch.object(porkyhud, "run_command", side_effect=AssertionError), \
                mock.patch.object(porkyhud, "run_structured_command", side_effect=AssertionError), \
                mock.patch.object(porkyhud, "reduce_health", side_effect=AssertionError):
            porkyhud.draw_initial_shell(shell, porkyhud.HudConfig())
            porkyhud.draw_small_screen(resize, 10, 60)
        self.assertEqual((shell.erase_count, shell.refresh_count), (1, 0))
        self.assertEqual((resize.erase_count, resize.refresh_count), (0, 0))

    def test_construction_rolls_back_created_pollers(self) -> None:
        created = []
        class Poller:
            def __init__(self, *_args, **_kwargs):
                created.append(self)
                if len(created) == 3:
                    raise RuntimeError("third")
            def close(self, _timeout_s):
                self.closed = True
        with mock.patch.object(porkyhud, "SourcePoller", Poller):
            with self.assertRaisesRegex(RuntimeError, "third"):
                porkyhud.LiveHealthCoordinator()
        self.assertTrue(all(getattr(poller, "closed", False) for poller in created[:2]))

    def test_failed_discontinuity_rebuild_preserves_the_live_generation(self) -> None:
        class Poller:
            created = []
            fail_at = None

            def __init__(self, state, *_args, **_kwargs):
                Poller.created.append(self)
                if Poller.fail_at == len(Poller.created):
                    raise RuntimeError("rebuild failed")
                self.state = state
                self.closed_with = None

            def tick(self, _now, _force=False):
                return self.state

            def snapshot(self):
                return self.state

            def close(self, timeout_s):
                self.closed_with = timeout_s

        with mock.patch.object(porkyhud, "SourcePoller", Poller):
            coordinator = porkyhud.LiveHealthCoordinator()
            old_generation = coordinator.source_generation()
            old_pollers = coordinator._pollers
            old_cpu_sampler = coordinator._cpu_sampler
            old_gpu_sampler = coordinator._gpu_sampler
            old_policy = coordinator._policy
            old_frame_states = coordinator._frame_states
            coordinator._per_core = (42.0,)
            coordinator._per_core_revision = 7
            coordinator._cpu_per_core_result = (old_generation, 8, (43.0,))
            coordinator.observe_time(0.0, self.UTC)

            Poller.fail_at = len(Poller.created) + 3
            with self.assertRaisesRegex(RuntimeError, "rebuild failed"):
                coordinator.observe_time(31.0, self.UTC + timedelta(seconds=31))

            self.assertEqual(coordinator.source_generation(), old_generation)
            self.assertIs(coordinator._pollers, old_pollers)
            self.assertIs(coordinator._cpu_sampler, old_cpu_sampler)
            self.assertIs(coordinator._gpu_sampler, old_gpu_sampler)
            self.assertIs(coordinator._policy, old_policy)
            self.assertIs(coordinator._frame_states, old_frame_states)
            self.assertEqual(coordinator.per_core(), (42.0,))
            self.assertEqual(
                coordinator._cpu_per_core_result,
                (old_generation, 8, (43.0,)),
            )
            self.assertEqual(coordinator._previous_time, (0.0, self.UTC))
            staged = Poller.created[6:8]
            self.assertEqual(len(staged), 2)
            self.assertTrue(all(item.closed_with is not None for item in staged))
            self.assertTrue(all(0.0 <= item.closed_with <= 1.0 for item in staged))
            self.assertIs(coordinator.tick(1.0), coordinator._frame)
            coordinator.close()

    def test_attempt_context_may_equal_the_internal_absence_sentinel(self) -> None:
        seen = []

        def collector(context):
            seen.append(context)
            return self.adapter_success("ok", 0.0, 0.1)

        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
            collector,
            attempt_context_factory=lambda _revision: porkyhud._NO_ATTEMPT_CONTEXT,
        )
        try:
            poller.tick(0.0)
            self.assertTrue(self.wait_for_revision(poller, 1))
            self.assertEqual(len(seen), 1)
            self.assertIs(seen[0], porkyhud._NO_ATTEMPT_CONTEXT)
            self.assertEqual(
                poller.snapshot().last_attempt.outcome,
                core.AttemptOutcome.SUCCESS,
            )
        finally:
            poller.close()

    def test_gpu_calibration_is_captured_at_attempt_acceptance(self) -> None:
        observed = []
        state = core.new_source(core.SourceId.PROCESS_GPU, 5.0, 15.0)
        poller = porkyhud.SourcePoller(
            state,
            lambda _context: (_ for _ in ()).throw(RuntimeError("stop")),
            attempt_context_factory=lambda revision: observed.append(revision),
        )
        try:
            poller.tick(1.0)
            self.assertEqual(observed, [1])
        finally:
            poller.close()

    def test_attempt_context_is_linearized_before_worker_collection(self) -> None:
        events = []
        done = threading.Event()
        def collector(context):
            events.append(("collect", context))
            done.set()
            return porkyhud.AdapterResult(
                core.GpuProcessSample({}, {}, True, False), 1.0, 1.1, self.UTC,
                core.AttemptOutcome.SUCCESS, None,
            )
        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.PROCESS_GPU, 5.0, 15.0), collector,
            attempt_context_factory=lambda revision: events.append("accepted") or revision,
        )
        try:
            poller.tick(1.0)
            self.assertTrue(done.wait(1.0))
            self.assertEqual(events[:2], ["accepted", ("collect", 1)])
        finally:
            poller.close()

    def test_cpu_completion_cache_is_generation_and_revision_scoped(self) -> None:
        coordinator = object.__new__(porkyhud.LiveHealthCoordinator)
        coordinator._lock = threading.RLock()
        coordinator._closed = False
        coordinator._generation = 2
        coordinator._per_core_revision = 3
        coordinator._per_core = (30.0,)
        coordinator._cpu_per_core_result = (1, 4, (99.0,))
        coordinator._commit_cpu_per_core(1, 5, (90.0,))
        self.assertEqual(coordinator._per_core, (30.0,))
        self.assertEqual(coordinator._cpu_per_core_result, (1, 4, (99.0,)))
        coordinator._commit_cpu_per_core(2, 5, (50.0,))
        self.assertEqual(coordinator._cpu_per_core_result, (2, 5, (50.0,)))

    def test_static_collector_is_session_once_and_history_only_updates_on_fresh_frame(self) -> None:
        history = porkyhud.MetricHistory()
        last_revision = None
        for revision in range(1, 61):
            for _render in range(30):
                last_revision = porkyhud._record_history_frame(
                    history, self.live_cpu_frame(revision), last_revision,
                    [float(revision)],
                    {"total": 100.0, "used": 50.0}, 0.0,
                    porkyhud.DiskInfo("data", "/", 100, 50, 50),
                )
        self.assertEqual(len(history.cpu), 60)
        self.assertEqual((history.cpu[0], history.cpu[-1]), (1.0, 60.0))
        self.assertEqual(last_revision, 60)

    def test_history_rejects_non_live_nonpositive_or_unready_cpu_views(self) -> None:
        history = porkyhud.MetricHistory()
        cases = (
            (core.SourceViewStatus.STALE, True, 1),
            (core.SourceViewStatus.LIVE, False, 1),
            (core.SourceViewStatus.LIVE, True, 0),
            (core.SourceViewStatus.LIVE, True, -1),
            (core.SourceViewStatus.LIVE, True, True),
        )
        for status, ready, revision in cases:
            with self.subTest(status=status, ready=ready, revision=revision):
                self.assertIsNone(porkyhud._record_history_frame(
                    history, self.live_cpu_frame(revision, status, ready),
                    None, [50.0],
                    {"total": 100.0, "used": 50.0}, 1.0,
                    porkyhud.DiskInfo("data", "/", 100, 50, 50),
                ))
        self.assertEqual(len(history.cpu), 0)

    def test_hard_reset_clears_history_and_allows_new_revision_one(self) -> None:
        history = porkyhud.MetricHistory()
        disk = porkyhud.DiskInfo("data", "/", 100, 50, 50)
        last_revision = porkyhud._record_history_frame(
            history, self.live_cpu_frame(1), None, [40.0],
            {"total": 100.0, "used": 50.0}, 1.0, disk,
        )
        checking = porkyhud.initial_health_frame(2.0)
        last_revision = porkyhud._record_history_frame(
            history, checking, last_revision, [], {}, 0.0, disk,
        )
        self.assertIsNone(last_revision)
        self.assertEqual(tuple(history.cpu), ())
        last_revision = porkyhud._record_history_frame(
            history, self.live_cpu_frame(1), last_revision, [60.0],
            {"total": 100.0, "used": 70.0}, 2.0, disk,
        )
        self.assertEqual(last_revision, 1)
        self.assertEqual(tuple(history.cpu), (60.0,))

        # A policy-only clock reset retains the same live revision and chart.
        last_revision = porkyhud._record_history_frame(
            history, self.live_cpu_frame(1), last_revision, [99.0],
            {"total": 100.0, "used": 99.0}, 9.0, disk,
        )
        self.assertEqual(last_revision, 1)
        self.assertEqual(tuple(history.cpu), (60.0,))

    def test_hud_refresh_never_restarts_static_and_records_sixty_fresh_revisions(self) -> None:
        frames = [self.live_cpu_frame(revision) for revision in range(1, 61)]
        frames.append(self.live_cpu_frame(1))

        class Coordinator:
            def __init__(self):
                self.index = 0
                self.current = 0
                self.generation = 1
                self.closed = False
            def observe_time(self, _now, _utc):
                if self.index == 60:
                    self.generation = 2
                return False
            def source_generation(self):
                return self.generation
            def tick(self, _now, _force=False):
                frame = frames[self.index]
                self.index += 1
                self.current = frame.source_views[core.SourceId.CPU_USAGE].last_valid_revision
                return frame
            def per_core(self):
                return (float(self.current),)
            def process_rows(self, _now):
                return ()
            def set_system_gpu_percent(self, _percent):
                pass
            def close(self):
                self.closed = True

        histories = []
        real_history = porkyhud.MetricHistory

        def make_history():
            history = real_history()
            histories.append(history)
            return history

        static_calls = []
        coordinator = Coordinator()
        screen = FakeScreen(
            10, 80,
            keys=[ord("r") if index % 7 == 0 else -1 for index in range(61)]
            + [ord("q")],
        )
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", self.SyncPoller), \
                mock.patch.object(porkyhud, "MetricHistory", side_effect=make_history), \
                mock.patch.object(porkyhud, "collect_static_info", side_effect=lambda: static_calls.append(1) or {"model": "Mac"}), \
                mock.patch.object(porkyhud, "boot_seconds", return_value=12), \
                mock.patch.object(porkyhud, "init_colors"), \
                mock.patch.object(porkyhud, "apply_dark_screen"), \
                mock.patch.object(porkyhud.curses, "curs_set"), \
                mock.patch.object(porkyhud.time, "sleep"):
            porkyhud.hud(screen)
        self.assertEqual(static_calls, [1])
        self.assertEqual(len(histories), 1)
        self.assertEqual(tuple(histories[0].cpu), (1.0,))
        self.assertTrue(coordinator.closed)

    def test_attempt_context_failure_retries_without_publishing_stale_gpu_value(self) -> None:
        factory_calls = []
        used_contexts = []
        calibration = [10.0]
        collected = threading.Event()

        def context_factory(revision: int):
            context = (revision, calibration[0])
            factory_calls.append(context)
            if len(factory_calls) == 1:
                raise RuntimeError("capture failed")
            return context

        def collector(context):
            used_contexts.append(context)
            collected.set()
            return self.adapter_success("ready", 1.0, 1.1)

        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
            collector, attempt_context_factory=context_factory,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                poller.tick(1.0)
            state = poller.snapshot()
            self.assertEqual(state.revision, 0)
            self.assertIsNone(state.active_revision)
            calibration[0] = 25.0
            poller.tick(1.0)
            self.assertTrue(collected.wait(1.0))
            self.assertEqual(factory_calls, [(1, 10.0), (1, 25.0)])
            self.assertEqual(used_contexts, [(1, 25.0)])
        finally:
            poller.close()

    def test_context_factory_reentrant_snapshot_and_close_do_not_deadlock(self) -> None:
        for action in ("snapshot", "close"):
            with self.subTest(action=action):
                holder, observed = [], []
                collector = mock.Mock(return_value=self.adapter_success("ready", 0.0, 0.1))
                def context_factory(revision, action=action):
                    if action == "snapshot":
                        observed.append(holder[0].snapshot().revision)
                    else:
                        holder[0].close()
                    return revision
                poller = porkyhud.SourcePoller(
                    core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
                    collector, attempt_context_factory=context_factory,
                )
                holder.append(poller)
                worker, done, errors = self.threaded_call(lambda: poller.tick(0.0))
                self.assertTrue(done.wait(1.0))
                worker.join(1.0)
                if action == "snapshot":
                    self.assertEqual((errors, observed), ([], [0]))
                    poller.close()
                else:
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], RuntimeError)
                    self.assertEqual(poller.snapshot().revision, 0)
                    collector.assert_not_called()

    def test_close_while_context_factory_is_blocked_never_publishes_work(self) -> None:
        factory_started = threading.Event()
        release_factory = threading.Event()
        collector = mock.Mock()

        def context_factory(revision: int):
            factory_started.set()
            if not release_factory.wait(1.0):
                raise RuntimeError("context factory was not released")
            return (revision, None)

        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.PROCESS_GPU, 5.0, 15.0),
            collector, attempt_context_factory=context_factory,
        )

        worker, tick_finished, errors = self.threaded_call(lambda: poller.tick(0.0))
        self.assertTrue(factory_started.wait(1.0))
        poller.close()
        release_factory.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(tick_finished.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(poller.snapshot().revision, 0)
        collector.assert_not_called()

    def test_force_during_rejected_acceptance_preserves_prior_backoff(self) -> None:
        state = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        state, revision = core.start_attempt(state, 0.0)
        state = core.complete_failure(
            state, revision, core.AttemptOutcome.TIMEOUT,
            core.FailureCode.COMMAND_TIMEOUT, 0.2, self.UTC,
        )
        entered, release, captures = threading.Event(), threading.Event(), []
        def context_factory(revision):
            captures.append(revision); entered.set(); release.wait(1.0)
            raise RuntimeError("capture rejected")
        poller = porkyhud.SourcePoller(state, mock.Mock(), context_factory)
        worker, done, errors = self.threaded_call(lambda: poller.tick(0.3, force=True))
        self.assertTrue(entered.wait(1.0))
        poller.tick(0.4, force=True)
        release.set(); worker.join(1.0)
        self.assertTrue(done.is_set())
        self.assertEqual(len(errors), 1)
        poller.tick(1.199)
        self.assertEqual(captures, [2])
        poller.close()

    def test_forced_followup_binds_context_and_coalesces_force_at_acceptance(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        accepted_contexts = []
        used_contexts = []
        calibration = [10.0]
        calls = [0]

        def context_factory(revision: int):
            context = (revision, calibration[0])
            accepted_contexts.append(context)
            return context

        def collector(context):
            used_contexts.append(context)
            calls[0] += 1
            if calls[0] == 1:
                first_started.set()
                self.assertTrue(release_first.wait(1.0))
                return self.adapter_success("first", 1.0, 1.1)
            second_finished.set()
            return self.adapter_success("second", 1.1, 1.2)

        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
            collector, attempt_context_factory=context_factory,
        )
        try:
            poller.tick(1.0)
            self.assertTrue(first_started.wait(1.0))
            calibration[0] = 25.0
            poller.tick(1.05, force=True)
            poller.tick(1.06, force=True)
            release_first.set()
            self.assertTrue(second_finished.wait(1.0))
            self.assertEqual(accepted_contexts, [(1, 10.0), (2, 25.0)])
            self.assertEqual(used_contexts, accepted_contexts)
        finally:
            release_first.set()
            poller.close()

    def test_queued_followup_context_failure_keeps_worker_recoverable(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        rejected_followup = threading.Event()
        recovered = threading.Event()
        factory_calls = []
        used_contexts = []
        collector_calls = [0]

        def context_factory(revision: int):
            context = (revision, len(factory_calls) + 1)
            factory_calls.append(context)
            if len(factory_calls) == 2:
                rejected_followup.set()
                raise RuntimeError("queued context failed")
            return context

        def collector(context):
            used_contexts.append(context)
            collector_calls[0] += 1
            if collector_calls[0] == 1:
                first_started.set()
                if not release_first.wait(1.0):
                    raise RuntimeError("first attempt was not released")
                return self.adapter_success("first", 0.0, 0.2)
            recovered.set()
            return self.adapter_success("recovered", 1.2, 1.3)

        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
            collector, attempt_context_factory=context_factory,
        )
        try:
            worker = poller._worker
            poller.tick(0.0)
            self.assertTrue(first_started.wait(1.0))
            poller.tick(0.1, force=True)
            release_first.set()
            self.assertTrue(rejected_followup.wait(1.0))
            state = poller.snapshot()
            self.assertEqual(state.revision, 1)
            self.assertIsNone(state.active_revision)
            self.assertTrue(worker.is_alive())

            poller.tick(1.199)
            self.assertEqual(factory_calls, [(1, 1), (2, 2)])
            self.assertFalse(recovered.is_set())
            poller.tick(1.2)
            self.assertTrue(recovered.wait(1.0))
            with poller._condition:
                self.assertTrue(poller._condition.wait_for(
                    lambda: poller._state.active_revision is None
                    and poller._state.last_attempt is not None
                    and poller._state.last_attempt.revision == 2,
                    1.0,
                ))
            self.assertEqual(factory_calls, [(1, 1), (2, 2), (2, 3)])
            self.assertEqual(used_contexts, [(1, 1), (2, 3)])
        finally:
            release_first.set()
            poller.close()

    def test_cpu_completion_cache_is_exact_revision_scoped_and_pruned(self) -> None:
        adapter_success = self.adapter_success

        class FakeCpuSampler:
            instances = []

            def __init__(self):
                self.last_per_core = ()
                self.calls = 0
                self.first_started = threading.Event()
                self.release_first = threading.Event()
                FakeCpuSampler.instances.append(self)

            def sample_usage(self):
                self.calls += 1
                if self.calls == 1:
                    self.last_per_core = (10.0,)
                    self.first_started.set()
                    if not self.release_first.wait(1.0):
                        raise RuntimeError("first CPU sample was not released")
                    return adapter_success(
                        core.CpuUsage(10.0, 0.0, 1.0), 1.0, 20.0,
                    )
                self.last_per_core = (20.0,)
                return adapter_success(
                    core.CpuUsage(20.0, 1.0, 2.0), 20.0, 20.0,
                )

        class FakeGpuSampler:
            pass

        with mock.patch.object(porkyhud, "MacCpuSampler", FakeCpuSampler), \
                mock.patch.object(porkyhud, "GPUProcessSampler", FakeGpuSampler):
            coordinator = porkyhud.LiveHealthCoordinator()
        cpu_poller = coordinator._pollers[core.SourceId.CPU_USAGE]
        sampler = FakeCpuSampler.instances[0]
        try:
            cpu_poller.tick(1.0)
            self.assertTrue(sampler.first_started.wait(1.0))
            cpu_poller.tick(2.0, force=True)
            sampler.release_first.set()
            with cpu_poller._condition:
                self.assertTrue(cpu_poller._condition.wait_for(
                    lambda: cpu_poller._state.active_revision is None
                    and cpu_poller._state.last_attempt is not None
                    and cpu_poller._state.last_attempt.revision == 2,
                    1.0,
                ))
            self.assertEqual(
                coordinator._cpu_per_core_result,
                (coordinator._generation, 2, (20.0,)),
            )
            coordinator._consume_cpu_per_core_revision(1)
            self.assertEqual(coordinator.per_core(), ())
            self.assertEqual(
                coordinator._cpu_per_core_result,
                (coordinator._generation, 2, (20.0,)),
            )
            coordinator._consume_cpu_per_core_revision(2)
            self.assertEqual(coordinator.per_core(), (20.0,))
            self.assertIsNone(coordinator._cpu_per_core_result)
        finally:
            sampler.release_first.set()
            coordinator.close()

    def test_failed_followup_does_not_hide_cached_per_core_for_last_valid_cpu(self) -> None:
        states = dict(porkyhud._registry_states())
        cpu = states[core.SourceId.CPU_USAGE]
        cpu, first = core.start_attempt(cpu, 0.0)
        cpu = core.complete_success(
            cpu, first, core.CpuUsage(25.0, 0.0, 1.0), 1.0, self.UTC,
        )
        cpu, second = core.start_attempt(cpu, 1.1)
        cpu = core.complete_failure(
            cpu, second, core.AttemptOutcome.TIMEOUT,
            core.FailureCode.COMMAND_TIMEOUT, 1.2, self.UTC,
        )
        states[core.SourceId.CPU_USAGE] = cpu

        class FixedPoller:
            def __init__(self, state):
                self.state = state

            def tick(self, _now, _force=False):
                return self.state

            def snapshot(self):
                return self.state

        coordinator = object.__new__(porkyhud.LiveHealthCoordinator)
        coordinator._lock = threading.RLock()
        coordinator._closed = False
        coordinator._generation = 1
        coordinator._policy = core.initial_policy_state()
        coordinator._frame_states = porkyhud._registry_states()
        coordinator._frame = porkyhud.initial_health_frame(0.0)
        coordinator._per_core = ()
        coordinator._per_core_revision = 0
        coordinator._cpu_per_core_result = (1, first, (25.0, 75.0))
        coordinator._pollers = {
            source_id: FixedPoller(states[source_id])
            for source_id, _cadence, _stale in porkyhud.LIVE_SOURCE_SPECS
        }

        frame = coordinator.tick(1.2)
        self.assertIsNone(frame.cpu_percent)
        self.assertEqual(coordinator.per_core(), (25.0, 75.0))
        self.assertEqual(coordinator._per_core_revision, first)
        self.assertIsNone(coordinator._cpu_per_core_result)

    def test_gpu_followup_uses_calibration_from_accepted_revision(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        second_accepted = threading.Event()
        release_second_hook = threading.Event()
        observed = []

        class FakeGpuSampler:
            pass

        def collect_gpu(_sampler, calibration):
            observed.append(calibration)
            if len(observed) == 1:
                first_started.set()
                if not release_first.wait(1.0):
                    raise RuntimeError("first GPU sample was not released")
                return self.adapter_success(
                    core.GpuProcessSample({}, {}, True, False), 1.0, 1.1,
                )
            return self.adapter_success(
                core.GpuProcessSample({}, {}, True, False), 1.1, 1.2,
            )

        with mock.patch.object(porkyhud, "GPUProcessSampler", FakeGpuSampler), \
                mock.patch.object(porkyhud, "collect_gpu_process_sample", collect_gpu):
            coordinator = porkyhud.LiveHealthCoordinator()
            gpu_poller = coordinator._pollers[core.SourceId.PROCESS_GPU]
            original_factory = gpu_poller._attempt_context_factory

            def gated_factory(revision: int):
                context = original_factory(revision)
                if revision == 2:
                    second_accepted.set()
                    if not release_second_hook.wait(1.0):
                        raise RuntimeError("second GPU acceptance was not released")
                return context

            gpu_poller._attempt_context_factory = gated_factory
            try:
                coordinator.set_system_gpu_percent(10.0)
                gpu_poller.tick(1.0)
                self.assertTrue(first_started.wait(1.0))
                coordinator.set_system_gpu_percent(25.0)
                gpu_poller.tick(1.05, force=True)
                release_first.set()
                self.assertTrue(second_accepted.wait(1.0))
                coordinator.set_system_gpu_percent(75.0)
                release_second_hook.set()
                with gpu_poller._condition:
                    self.assertTrue(gpu_poller._condition.wait_for(
                        lambda: gpu_poller._state.active_revision is None
                        and gpu_poller._state.last_attempt is not None
                        and gpu_poller._state.last_attempt.revision == 2,
                        1.0,
                    ))
                self.assertEqual(observed, [10.0, 25.0])
            finally:
                release_first.set()
                release_second_hook.set()
                coordinator.close()

    def test_detached_cpu_generation_cannot_commit_per_core(self) -> None:
        # A completion tagged with an old generation is ignored after rebuild.
        coordinator = object.__new__(porkyhud.LiveHealthCoordinator)
        coordinator._lock = threading.RLock()
        coordinator._generation = 2
        coordinator._closed = False
        coordinator._cpu_per_core_result = None
        coordinator._commit_cpu_per_core(1, 7, (77.0,))
        self.assertIsNone(coordinator._cpu_per_core_result)

    def test_closed_coordinator_wins_two_stage_mutation_races(self) -> None:
        for operation in ("calibration", "clock"):
            with self.subTest(operation=operation):
                coordinator = object.__new__(porkyhud.LiveHealthCoordinator)
                coordinator._lock, coordinator._closed = threading.RLock(), False
                coordinator._pollers = {}
                coordinator._system_gpu_percent, coordinator._previous_time = 10.0, None
                entered, release = threading.Event(), threading.Event()
                if operation == "calibration":
                    def gate(_value):
                        entered.set(); release.wait(1.0); return True
                    patcher = mock.patch.object(porkyhud.math, "isfinite", side_effect=gate)
                    callback = lambda: coordinator.set_system_gpu_percent(25.0)
                else:
                    def gate(value, _label):
                        entered.set(); release.wait(1.0); return value
                    patcher = mock.patch.object(porkyhud, "_normalize_utc_timestamp", side_effect=gate)
                    callback = lambda: coordinator.observe_time(1.0, self.UTC)
                with patcher, mock.patch.object(coordinator, "_close_poller_set", return_value=None):
                    worker, done, errors = self.threaded_call(callback)
                    self.assertTrue(entered.wait(1.0))
                    coordinator.close(); release.set(); worker.join(1.0)
                self.assertTrue(done.is_set())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], RuntimeError)
                self.assertEqual(coordinator._system_gpu_percent, 10.0)
                self.assertIsNone(coordinator._previous_time)

    def test_future_worker_completion_is_deferred_until_frame_time_catches_up(self) -> None:
        source = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        source, revision = core.start_attempt(source, 10.0)
        source = core.complete_success(
            source, revision, core.MemoryPressure(core.MemoryPressureLevel.NORMAL),
            10.1, self.UTC,
        )
        self.assertTrue(porkyhud.LiveHealthCoordinator._is_future_state(source, 10.0))
        self.assertFalse(porkyhud.LiveHealthCoordinator._is_future_state(source, 10.1))

    def test_coordinator_close_attempts_every_child_with_one_aggregate_deadline(self) -> None:
        class Child:
            def __init__(self, error: BaseException | None = None) -> None:
                self.error = error
                self.calls: list[float] = []

            def close(self, timeout_s: float) -> None:
                self.calls.append(timeout_s)
                if self.error is not None:
                    raise self.error

        children = {source_id: Child(RuntimeError("first") if source_id is core.SourceId.CPU_USAGE else None)
                    for source_id, _cadence, _stale in porkyhud.LIVE_SOURCE_SPECS}
        coordinator = object.__new__(porkyhud.LiveHealthCoordinator)
        coordinator._lock = threading.RLock()
        coordinator._closed = False
        coordinator._pollers = children
        with self.assertRaisesRegex(RuntimeError, "first"):
            coordinator.close()
        self.assertTrue(coordinator._closed)
        self.assertTrue(all(child.calls for child in children.values()))

    def test_calibration_rejects_booleans_and_out_of_range_values(self) -> None:
        coordinator = object.__new__(porkyhud.LiveHealthCoordinator)
        coordinator._lock = threading.RLock()
        coordinator._closed = False
        coordinator._system_gpu_percent = None
        for invalid in (True, -0.1, 100.1, float("nan")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                coordinator.set_system_gpu_percent(invalid)
        coordinator.set_system_gpu_percent(50.0)
        self.assertEqual(coordinator._system_gpu_percent, 50.0)

    def test_closed_coordinator_rejects_all_mutating_calls_before_argument_validation(self) -> None:
        coordinator = object.__new__(porkyhud.LiveHealthCoordinator)
        coordinator._lock = threading.RLock()
        coordinator._closed = True
        for call in (
            lambda: coordinator.tick(float("nan")),
            lambda: coordinator.observe_time(float("nan"), self.UTC),
            lambda: coordinator.set_system_gpu_percent(True),
            coordinator.reset_policy,
        ):
            with self.subTest(call=call), self.assertRaises(RuntimeError):
                call()


class SourcePollerTests(unittest.TestCase):
    UTC = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)

    class ScriptedCollector:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls = 0
            self.condition = threading.Condition()

        def __call__(self):
            with self.condition:
                index = self.calls
                self.calls += 1
                self.condition.notify_all()
            outcome = self.outcomes[index]
            if isinstance(outcome, BaseException):
                raise outcome
            if callable(outcome):
                return outcome()
            return outcome

        def wait_for_calls(self, count, timeout=2.0):
            with self.condition:
                return self.condition.wait_for(lambda: self.calls >= count, timeout)

    class BlockingCollector:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls = 0
            self.started = threading.Event()
            self.release = threading.Event()
            self.condition = threading.Condition()

        def __call__(self):
            with self.condition:
                index = self.calls
                self.calls += 1
                self.condition.notify_all()
            if index == 0:
                self.started.set()
                if not self.release.wait(2.0):
                    raise RuntimeError("test collector was not released")
            return self.outcomes[index]

        def wait_for_calls(self, count, timeout=2.0):
            with self.condition:
                return self.condition.wait_for(lambda: self.calls >= count, timeout)

    @classmethod
    def success(cls, value, started, completed):
        return porkyhud.AdapterResult(
            value,
            started,
            completed,
            cls.UTC,
            core.AttemptOutcome.SUCCESS,
            None,
        )

    @classmethod
    def failure(cls, outcome, failure, started, completed):
        return porkyhud.AdapterResult(
            None,
            started,
            completed,
            cls.UTC,
            outcome,
            failure,
        )

    def register_poller(self, poller, release=None):
        self.addCleanup(poller.close)
        if release is not None:
            self.addCleanup(release.set)
        return poller

    def wait_for_state(self, poller, predicate, timeout=2.0):
        with poller._condition:
            return poller._condition.wait_for(
                lambda: predicate(poller._state), timeout
            )

    def assert_completed_revision(self, poller, revision):
        self.assertTrue(
            self.wait_for_state(
                poller,
                lambda state: state.active_revision is None
                and state.last_attempt is not None
                and state.last_attempt.revision == revision,
            )
        )

    def test_pristine_source_starts_immediately_and_cadence_boundary_is_inclusive(self):
        collector = self.ScriptedCollector(
            [self.success("first", 0.0, 0.25), self.success("second", 1.25, 1.5)]
        )
        poller = self.register_poller(
            porkyhud.SourcePoller(
                core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
                collector,
            )
        )

        active = poller.tick(0.0)
        self.assertEqual(active.active_revision, 1)
        self.assert_completed_revision(poller, 1)
        self.assertEqual(poller.snapshot().last_valid.value, "first")

        poller.tick(1.249)
        self.assertEqual(collector.calls, 1)
        poller.tick(1.25)
        self.assertTrue(collector.wait_for_calls(2))
        self.assert_completed_revision(poller, 2)
        self.assertEqual(poller.snapshot().last_valid.value, "second")

    def test_retry_backoff_is_one_two_four_ten_ten_and_success_resets_it(self):
        failure = (core.AttemptOutcome.TIMEOUT, core.FailureCode.COMMAND_TIMEOUT)
        results = [
            self.failure(*failure, 0.0, 0.1),
            self.failure(*failure, 1.1, 1.2),
            self.failure(*failure, 3.2, 3.3),
            self.failure(*failure, 7.3, 7.4),
            self.failure(*failure, 17.4, 17.5),
            self.failure(*failure, 27.5, 27.6),
            self.success("recovered", 37.6, 37.7),
            self.failure(*failure, 38.7, 38.8),
            self.success("reset", 39.8, 39.9),
        ]
        collector = self.ScriptedCollector(results)
        poller = self.register_poller(
            porkyhud.SourcePoller(
                core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
                collector,
            )
        )

        poller.tick(0.0)
        self.assert_completed_revision(poller, 1)
        due_times = [1.1, 3.2, 7.3, 17.4, 27.5, 37.6, 38.7, 39.8]
        for expected_calls, due in enumerate(due_times, start=2):
            poller.tick(due - 0.001)
            self.assertEqual(collector.calls, expected_calls - 1)
            poller.tick(due)
            self.assertTrue(collector.wait_for_calls(expected_calls))
            self.assert_completed_revision(poller, expected_calls)
        self.assertEqual(poller.snapshot().last_valid.value, "reset")

    def test_every_nonterminal_failure_retries_at_the_first_backoff_boundary(self):
        failures = (
            (core.AttemptOutcome.PERMISSION_DENIED, core.FailureCode.PERMISSION_DENIED),
            (core.AttemptOutcome.MISSING_DEPENDENCY, core.FailureCode.MISSING_EXECUTABLE),
            (core.AttemptOutcome.TIMEOUT, core.FailureCode.DEADLINE_EXCEEDED),
            (core.AttemptOutcome.COMMAND_ERROR, core.FailureCode.NONZERO_EXIT),
            (core.AttemptOutcome.PARSE_ERROR, core.FailureCode.PARSER_REJECTED),
            (core.AttemptOutcome.CANCELLED, core.FailureCode.USER_CANCELLED),
            (core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION),
        )
        for outcome, failure in failures:
            with self.subTest(outcome=outcome):
                collector = self.ScriptedCollector(
                    [
                        self.failure(outcome, failure, 0.0, 0.2),
                        self.success("ready", 1.2, 1.3),
                    ]
                )
                poller = porkyhud.SourcePoller(
                    core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
                    collector,
                )
                try:
                    poller.tick(0.0)
                    self.assert_completed_revision(poller, 1)
                    poller.tick(1.199)
                    self.assertEqual(collector.calls, 1)
                    poller.tick(1.2)
                    self.assertTrue(collector.wait_for_calls(2))
                    self.assert_completed_revision(poller, 2)
                finally:
                    poller.close()

    def test_terminal_outcomes_stop_automatic_attempts_but_force_recovers(self):
        terminal = (
            (core.AttemptOutcome.UNSUPPORTED, core.FailureCode.UNSUPPORTED_CAPABILITY),
            (core.AttemptOutcome.NOT_APPLICABLE, core.FailureCode.NOT_APPLICABLE),
        )
        for outcome, failure in terminal:
            with self.subTest(outcome=outcome):
                collector = self.ScriptedCollector(
                    [
                        self.failure(outcome, failure, 0.0, 0.2),
                        self.success("rechecked", 100.0, 100.2),
                    ]
                )
                poller = porkyhud.SourcePoller(
                    core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
                    collector,
                )
                try:
                    poller.tick(0.0)
                    self.assert_completed_revision(poller, 1)
                    poller.tick(100.0)
                    self.assertEqual(collector.calls, 1)
                    poller.tick(100.0, force=True)
                    self.assertTrue(collector.wait_for_calls(2))
                    self.assert_completed_revision(poller, 2)
                    self.assertEqual(poller.snapshot().last_valid.value, "rechecked")
                finally:
                    poller.close()

    def test_force_during_backoff_resets_the_failure_sequence(self):
        timeout = (core.AttemptOutcome.TIMEOUT, core.FailureCode.COMMAND_TIMEOUT)
        collector = self.ScriptedCollector(
            [
                self.failure(*timeout, 0.0, 0.2),
                self.failure(*timeout, 0.3, 0.4),
                self.success("ready", 1.4, 1.5),
            ]
        )
        poller = self.register_poller(
            porkyhud.SourcePoller(
                core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
            )
        )
        poller.tick(0.0)
        self.assert_completed_revision(poller, 1)
        poller.tick(0.3, force=True)
        self.assert_completed_revision(poller, 2)
        poller.tick(1.399)
        self.assertEqual(collector.calls, 2)
        poller.tick(1.4)
        self.assert_completed_revision(poller, 3)

    def test_force_while_running_queues_exactly_one_follow_up_including_terminal_result(self):
        collector = self.BlockingCollector(
            [
                self.failure(
                    core.AttemptOutcome.UNSUPPORTED,
                    core.FailureCode.UNSUPPORTED_CAPABILITY,
                    0.0,
                    0.2,
                ),
                self.success("forced", 0.2, 0.3),
            ]
        )
        poller = self.register_poller(
            porkyhud.SourcePoller(
                core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
            ),
            collector.release,
        )
        poller.tick(0.0, force=True)
        self.assertTrue(collector.started.wait(1.0))
        poller.tick(0.1, force=True)
        poller.tick(0.15, force=True)
        collector.release.set()
        self.assertTrue(collector.wait_for_calls(2))
        self.assert_completed_revision(poller, 2)
        self.assertEqual(collector.calls, 2)
        self.assertEqual(poller.snapshot().last_valid.value, "forced")

    def test_force_during_the_forced_follow_up_queues_only_one_more_attempt(self):
        class MultiBlockingCollector:
            def __init__(self, outcomes):
                self.outcomes = outcomes
                self.calls = 0
                self.started = [threading.Event() for _ in outcomes]
                self.release = [threading.Event() for _ in outcomes]
                self.condition = threading.Condition()

            def __call__(self):
                with self.condition:
                    index = self.calls
                    self.calls += 1
                    self.condition.notify_all()
                self.started[index].set()
                if not self.release[index].wait(2.0):
                    raise RuntimeError("test collector was not released")
                return self.outcomes[index]

            def wait_for_calls(self, count):
                with self.condition:
                    return self.condition.wait_for(lambda: self.calls >= count, 2.0)

        collector = MultiBlockingCollector(
            [
                self.success("first", 0.0, 0.2),
                self.success("second", 0.2, 0.4),
                self.success("third", 0.4, 0.6),
            ]
        )
        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
        )
        self.addCleanup(poller.close)
        self.addCleanup(lambda: [event.set() for event in collector.release])

        poller.tick(0.0)
        self.assertTrue(collector.started[0].wait(1.0))
        poller.tick(0.1, force=True)
        collector.release[0].set()
        self.assertTrue(collector.wait_for_calls(2))
        self.assertTrue(collector.started[1].wait(1.0))
        poller.tick(0.3, force=True)
        poller.tick(0.35, force=True)
        collector.release[1].set()
        self.assertTrue(collector.wait_for_calls(3))
        collector.release[2].set()
        self.assert_completed_revision(poller, 3)
        self.assertEqual(collector.calls, 3)

    def test_failed_refresh_retains_last_valid_and_latest_failure(self):
        initial = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        initial, revision = core.start_attempt(initial, 0.0)
        initial = core.complete_success(initial, revision, "old", 0.2, self.UTC)
        collector = self.ScriptedCollector(
            [
                self.failure(
                    core.AttemptOutcome.PARSE_ERROR,
                    core.FailureCode.PARSER_REJECTED,
                    1.2,
                    1.3,
                )
            ]
        )
        poller = self.register_poller(porkyhud.SourcePoller(initial, collector))
        poller.tick(1.2)
        self.assert_completed_revision(poller, 2)
        state = poller.snapshot()
        self.assertEqual((state.last_valid.value, state.last_valid.revision), ("old", 1))
        self.assertEqual(state.last_attempt.outcome, core.AttemptOutcome.PARSE_ERROR)
        self.assertFalse(core.is_decision_ready(state, 1.3))

    def test_exception_wrong_type_and_corrupt_timing_become_bounded_unknown_failures(self):
        corrupt = object.__new__(porkyhud.AdapterResult)
        for name, value in (
            ("value", "bad"),
            ("started_mono_s", 9.0),
            ("completed_mono_s", 10.1),
            ("completed_at_utc", self.UTC),
            ("outcome", core.AttemptOutcome.SUCCESS),
            ("failure", None),
        ):
            object.__setattr__(corrupt, name, value)
        cases = (
            RuntimeError("secret collector detail"),
            object(),
            corrupt,
        )
        for case in cases:
            with self.subTest(case=type(case).__name__):
                collector = self.ScriptedCollector([case])
                poller = porkyhud.SourcePoller(
                    core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
                    collector,
                )
                try:
                    with mock.patch.object(porkyhud.time, "monotonic", return_value=10.5), \
                            mock.patch.object(porkyhud, "utc_now", return_value=self.UTC):
                        poller.tick(10.0)
                        self.assert_completed_revision(poller, 1)
                    attempt = poller.snapshot().last_attempt
                    self.assertEqual(
                        (attempt.outcome, attempt.failure),
                        (
                            core.AttemptOutcome.UNKNOWN_ERROR,
                            core.FailureCode.UNEXPECTED_EXCEPTION,
                        ),
                    )
                    self.assertEqual(attempt.started_mono_s, 10.0)
                    self.assertEqual(attempt.completed_mono_s, 10.5)
                finally:
                    poller.close()

    def test_stale_private_completion_cannot_overwrite_a_newer_active_revision(self):
        returned = threading.Event()
        holder = []

        def stale_collector():
            newer = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
            newer, revision = core.start_attempt(newer, 0.0)
            newer = core.complete_success(newer, revision, "newer", 0.05, self.UTC)
            newer, _ = core.start_attempt(newer, 0.1)
            with holder[0]._condition:
                holder[0]._state = newer
            returned.set()
            return self.success("stale", 0.0, 0.2)

        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), stale_collector
        )
        holder.append(poller)
        self.addCleanup(poller.close)
        poller.tick(0.0)
        self.assertTrue(returned.wait(1.0))
        with poller._condition:
            self.assertTrue(
                poller._condition.wait_for(
                    lambda: poller._state.active_revision == 2, 1.0
                )
            )
        poller.close()
        state = poller.snapshot()
        self.assertEqual(state.active_revision, 2)
        self.assertEqual(state.last_valid.value, "newer")

    def test_invalid_tick_and_close_values_are_rejected_without_closing(self):
        collector = self.ScriptedCollector([self.success("ready", 0.0, 0.1)])
        poller = self.register_poller(
            porkyhud.SourcePoller(
                core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
            )
        )
        for invalid in (True, float("nan"), float("inf"), float("-inf")):
            with self.subTest(kind="tick", invalid=invalid), self.assertRaises(ValueError):
                poller.tick(invalid)
        for invalid in (True, -0.1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(kind="close", invalid=invalid), self.assertRaises(ValueError):
                poller.close(invalid)
        poller.tick(0.0)
        self.assert_completed_revision(poller, 1)

    def test_closed_tick_rejects_lifecycle_before_invalid_monotonic_time(self):
        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
            self.ScriptedCollector([]),
        )
        poller.close()
        with self.assertRaises(RuntimeError):
            poller.tick(float("nan"))

    def test_closed_tick_rejects_lifecycle_before_invalid_force_flag(self):
        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
            self.ScriptedCollector([]),
        )
        poller.close()
        with self.assertRaises(RuntimeError):
            poller.tick(0.0, force=1)

    def test_initial_active_state_is_rejected_and_all_completed_shapes_schedule_correctly(self):
        active = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        active, _ = core.start_attempt(active, 0.0)
        with self.assertRaises(ValueError):
            porkyhud.SourcePoller(active, self.ScriptedCollector([]))

        success = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        success, revision = core.start_attempt(success, 0.0)
        success = core.complete_success(success, revision, "old", 0.2, self.UTC)
        retry = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        retry, revision = core.start_attempt(retry, 0.0)
        retry = core.complete_failure(
            retry,
            revision,
            core.AttemptOutcome.TIMEOUT,
            core.FailureCode.COMMAND_TIMEOUT,
            0.2,
            self.UTC,
        )
        terminal = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        terminal, revision = core.start_attempt(terminal, 0.0)
        terminal = core.complete_failure(
            terminal,
            revision,
            core.AttemptOutcome.NOT_APPLICABLE,
            core.FailureCode.NOT_APPLICABLE,
            0.2,
            self.UTC,
        )

        states_and_boundaries = (
            (success, 1.199, 1.2, True),
            (retry, 1.199, 1.2, True),
            (terminal, 100.0, 100.0, False),
        )
        for state, before, due, starts in states_and_boundaries:
            with self.subTest(outcome=state.last_attempt.outcome):
                start_time = due
                collector = self.ScriptedCollector(
                    [self.success("fresh", start_time, start_time + 0.1)]
                )
                poller = porkyhud.SourcePoller(state, collector)
                try:
                    poller.tick(before)
                    self.assertEqual(collector.calls, 0)
                    poller.tick(due)
                    if starts:
                        self.assertTrue(collector.wait_for_calls(1))
                        self.assert_completed_revision(poller, 2)
                    else:
                        self.assertEqual(collector.calls, 0)
                finally:
                    poller.close()

    def test_one_persistent_worker_stays_single_flight_across_one_thousand_ticks(self):
        collector = self.BlockingCollector([self.success("ready", 0.0, 0.2)])
        poller = self.register_poller(
            porkyhud.SourcePoller(
                core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
            ),
            collector.release,
        )
        worker = poller._worker
        poller.tick(0.0)
        self.assertTrue(collector.started.wait(1.0))
        for index in range(1000):
            poller.tick(index / 1000.0)
        self.assertIs(poller._worker, worker)
        self.assertEqual(collector.calls, 1)
        collector.release.set()
        self.assert_completed_revision(poller, 1)
        poller.close()
        self.assertFalse(worker.is_alive())

    def test_close_suppresses_queued_force_is_idempotent_and_tick_then_raises(self):
        collector = self.BlockingCollector(
            [
                self.success("first", 0.0, 0.2),
                self.success("forbidden", 0.2, 0.3),
            ]
        )
        poller = self.register_poller(
            porkyhud.SourcePoller(
                core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
            ),
            collector.release,
        )
        worker = poller._worker
        poller.tick(0.0)
        self.assertTrue(collector.started.wait(1.0))
        poller.tick(0.1, force=True)
        collector.release.set()
        poller.close(1.0)
        poller.close(1.0)
        self.assertEqual(collector.calls, 1)
        self.assertFalse(worker.is_alive())
        with self.assertRaises(RuntimeError):
            poller.tick(1.0)

    def test_zero_timeout_close_is_bounded_and_worker_exits_after_collector_release(self):
        collector = self.BlockingCollector([self.success("ready", 0.0, 0.2)])
        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
        )
        worker = poller._worker
        poller.tick(0.0)
        self.assertTrue(collector.started.wait(1.0))
        poller.close(0.0)
        self.assertTrue(worker.is_alive())
        collector.release.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())

    def test_close_before_worker_consumes_accepted_work_suppresses_collection(self):
        real_thread = threading.Thread

        class GatedThread:
            def __init__(self, target, daemon):
                self.target = target
                self.gate = threading.Event()
                self.thread = real_thread(target=self.run, daemon=daemon)

            def run(self):
                self.gate.wait(2.0)
                self.target()

            def start(self):
                self.thread.start()

            def join(self, timeout=None):
                self.thread.join(timeout)

            def is_alive(self):
                return self.thread.is_alive()

        collector = self.ScriptedCollector([self.success("forbidden", 0.0, 0.2)])
        with mock.patch.object(porkyhud.threading, "Thread", GatedThread):
            poller = porkyhud.SourcePoller(
                core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
            )
        worker = poller._worker
        try:
            poller.tick(0.0)
            poller.close(0.0)
            worker.gate.set()
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(collector.calls, 0)
            state = poller.snapshot()
            self.assertIsNone(state.active_revision)
            self.assertEqual(
                (state.last_attempt.outcome, state.last_attempt.failure),
                (
                    core.AttemptOutcome.CANCELLED,
                    core.FailureCode.USER_CANCELLED,
                ),
            )
        finally:
            worker.gate.set()
            worker.join(1.0)

    def test_worker_can_close_its_own_poller_without_self_joining(self):
        holder = []
        closed_inside = threading.Event()

        def collector():
            holder[0].close()
            closed_inside.set()
            return self.success("ready", 0.0, 0.2)

        poller = porkyhud.SourcePoller(
            core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5), collector
        )
        holder.append(poller)
        worker = poller._worker
        poller.tick(0.0)
        self.assertTrue(closed_inside.wait(1.0))
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        with self.assertRaises(RuntimeError):
            poller.tick(1.0)


class LayoutAndRenderingTests(unittest.TestCase):
    def run_hud_fixture(
        self, keys: list[int], *, height: int = 24, width: int = 80,
        sensor: porkyhud.SensorInfo | None = None,
        monotonic_values: list[float] | None = None,
        frames: list[porkyhud.HealthFrame] | None = None,
        poller_snapshots: list[tuple[object, float, str]] | None = None,
        on_refresh=None,
        screen: FakeScreen | None = None,
    ) -> tuple[FakeScreen, mock.Mock, mock.Mock, mock.Mock]:
        frame = make_health_frame_for_test()
        coordinator = mock.Mock()
        coordinator.source_generation.return_value = 0
        if frames is None:
            coordinator.tick.return_value = frame
        else:
            coordinator.tick.side_effect = frames
        coordinator.per_core.return_value = (10.0, 20.0)
        coordinator.process_rows.return_value = (
            porkyhud.ProcessRow(42, 70.0, 4.0, None, "R", "00:10",
                                "/Applications/Xcode.app/Contents/MacOS/Xcode"),
            porkyhud.ProcessRow(7, 20.0, 8.0, 0.0, "S", "01:00", "/usr/bin/worker"),
        )
        poller = mock.Mock()
        poller.tick.return_value = None
        if poller_snapshots is None:
            poller.snapshot.return_value = (None, 0.0, "")
        else:
            poller.snapshot.side_effect = poller_snapshots
        def sample_snapshot(now):
            value, age, error = poller.snapshot(now)
            return porkyhud.DescriptiveSample(value, age, error, 0, 0.0)
        poller.sample_snapshot.side_effect = sample_snapshot
        unlock = mock.Mock(return_value=True)
        screen = screen or FakeScreen(height, width, keys=keys)
        if on_refresh is not None:
            screen.on_refresh = lambda: on_refresh(screen)
        default_sensor = sensor or settled_sensor()
        clock = (
            mock.patch.object(porkyhud.time, "monotonic",
                              side_effect=monotonic_values)
            if monotonic_values is not None
            else mock.patch.object(porkyhud.time, "monotonic", return_value=21.0)
        )
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", return_value=poller), \
                mock.patch.object(porkyhud, "_start_static_discovery", return_value=poller), \
                mock.patch.object(porkyhud, "default_sensor_info", return_value=default_sensor), \
                mock.patch.object(porkyhud, "unlock_privileged_sensors", unlock), \
                mock.patch.object(porkyhud, "init_colors"), \
                mock.patch.object(porkyhud, "apply_dark_screen"), \
                mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0), \
                clock, \
                mock.patch.object(porkyhud.time, "sleep"):
            porkyhud.hud(screen, porkyhud.HudConfig(intro_enabled=False))
        return screen, coordinator, poller, unlock

    def test_help_escape_survives_resize_below_minimum(self) -> None:
        def resize(screen: FakeScreen) -> None:
            if screen.refresh_count == 2:
                screen.height, screen.width = 21, 75
            elif screen.refresh_count == 3:
                screen.height, screen.width = 24, 80

        screen, _coordinator, _poller, _unlock = self.run_hud_fixture(
            [ord("h"), 27, -1, ord("q")], on_refresh=resize,
        )
        self.assertNotIn("SHORTCUTS", screen.text())
        self.assertIn("shortcut panel closed", screen.row(23))

    def test_hud_rechecks_geometry_after_collection_before_drawing(self) -> None:
        """A Cinema resize during collection must redraw Overview, not crash."""

        class ResizeBeforeFirstLiveDrawScreen(FakeScreen):
            def __init__(self) -> None:
                super().__init__(34, 118, keys=[ord("6"), ord("q")])
                self.geometry_reads = 0

            def getmaxyx(self) -> tuple[int, int]:
                self.geometry_reads += 1
                if self.geometry_reads == 6:
                    self.height, self.width = 33, 117
                return super().getmaxyx()

        resized = ResizeBeforeFirstLiveDrawScreen()
        screen, _coordinator, _poller, _unlock = self.run_hud_fixture(
            [], screen=resized,
        )

        self.assertIn("1 OVERVIEW", screen.row(0))
        self.assertIn("EVIDENCE DETAIL", screen.text())
        self.assertEqual(
            screen.row(32).strip(),
            "Cinema requires 118x34 | returned to Overview | q quit",
        )

    def test_hud_restarts_a_frame_when_geometry_changes_during_any_draw_stage(self) -> None:
        """Only a frame built for one stable viewport may reach refresh."""

        class GeometryCountingScreen(FakeScreen):
            def __init__(self) -> None:
                super().__init__(34, 118, keys=[ord("6"), ord("q")])
                self.geometry_reads = 0

            def getmaxyx(self) -> tuple[int, int]:
                self.geometry_reads += 1
                return super().getmaxyx()

        baseline = GeometryCountingScreen()
        self.run_hud_fixture([], screen=baseline)
        first_loop_final_read = baseline.geometry_reads - 1

        failures = []
        for resize_read in range(6, first_loop_final_read + 1):
            class ResizeDuringDrawScreen(FakeScreen):
                def __init__(self) -> None:
                    super().__init__(34, 118, keys=[ord("6"), ord("q")])
                    self.geometry_reads = 0
                    self.committed = []

                def getmaxyx(self) -> tuple[int, int]:
                    self.geometry_reads += 1
                    if self.geometry_reads == resize_read:
                        self.height, self.width = 33, 117
                    return super().getmaxyx()

                def refresh(self) -> None:
                    super().refresh()
                    self.committed.append(self.text())

            screen = ResizeDuringDrawScreen()
            try:
                self.run_hud_fixture([], screen=screen)
            except ValueError as error:
                failures.append((resize_read, str(error)))
                continue
            final_frame = screen.committed[-1]
            if ("1 OVERVIEW" not in final_frame
                    or "Cinema requires 118x34 | returned to Overview" not in final_frame
                    or "6 CINEMA" in final_frame):
                failures.append((resize_read, "mixed or stale committed frame"))

        self.assertEqual(failures, [])

    def test_hud_does_not_commit_a_frame_while_geometry_keeps_changing(self) -> None:
        """A resize storm may skip a frame but never refresh mixed geometry."""

        class ChurningGeometryScreen(FakeScreen):
            def __init__(self) -> None:
                super().__init__(34, 118, keys=[ord("6"), ord("q")])
                self.geometry_reads = 0
                self.refresh_geometry = []

            def getmaxyx(self) -> tuple[int, int]:
                self.geometry_reads += 1
                if 6 <= self.geometry_reads <= 180:
                    self.height, self.width = (
                        (33, 117) if self.geometry_reads % 2 else (34, 118)
                    )
                return super().getmaxyx()

            def refresh(self) -> None:
                self.refresh_geometry.append((self.height, self.width, self.text()))
                super().refresh()

        screen = ChurningGeometryScreen()
        self.run_hud_fixture([], screen=screen)

        self.assertGreaterEqual(len(screen.refresh_geometry), 2)
        final_height, final_width, final_text = screen.refresh_geometry[-1]
        self.assertEqual((final_height, final_width), (33, 117))
        self.assertIn("1 OVERVIEW", final_text)
        self.assertIn("returned to Overview", final_text)
        self.assertNotIn("6 CINEMA", final_text)

    def test_hud_direct_keys_change_mode_label_and_body_at_minimum_view(self) -> None:
        expected = {
            "1": ("1 OVERVIEW", "EVIDENCE DETAIL", "Overview"),
            "2": ("2 COMPUTE", "TOP CPU", "Compute"),
            "3": ("3 THERMALS", "FANS + TEMP SENSORS", "Thermals"),
            "4": ("4 I/O", "disk live", "I/O"),
            "5": ("5 PROCESSES", "PROCESS GRID sort=CPU", "Processes"),
        }
        for key, (mode_label, body_signature, display_name) in expected.items():
            with self.subTest(key=key):
                screen, _coordinator, _poller, _unlock = self.run_hud_fixture(
                    [ord(key), ord("q")], height=22, width=76,
                )
                self.assertIn(mode_label, screen.row(0))
                self.assertIn(body_signature, screen.text())
                self.assertEqual(screen.row(21).strip(),
                                 "%s selected | q quit" % display_name)

    def test_hud_control_truth_table_guards_motion_refresh_and_unlock(self) -> None:
        rejected, _coordinator, _poller, _unlock = self.run_hud_fixture(
            [ord("6"), ord("q")], height=24, width=80,
        )
        self.assertIn("1 OVERVIEW", rejected.row(0))
        self.assertIn("Cinema requires 118x34 | staying in Overview | q quit",
                      rejected.row(23))

        motion, _coordinator, _poller, _unlock = self.run_hud_fixture(
            [ord("a"), ord("a"), ord("a"), ord("q")],
        )
        self.assertIn("motion off | q quit", motion.row(23))

        locked_sensor = replace(settled_sensor(), privileged_locked=True,
                                raw_hint="smc direct")
        _screen, _coordinator, _poller, locked_unlock = self.run_hud_fixture(
            [ord("u"), ord("q")], sensor=locked_sensor,
        )
        locked_unlock.assert_called_once()
        _screen, _coordinator, _poller, unlocked_unlock = self.run_hud_fixture(
            [ord("u"), ord("q")], sensor=settled_sensor(),
        )
        unlocked_unlock.assert_not_called()

        _screen, refreshed, poller, _unlock = self.run_hud_fixture(
            [-1, ord("r"), ord("q")],
        )
        self.assertEqual([call.args[1] for call in refreshed.tick.call_args_list],
                         [True, True])
        self.assertTrue(all(call.args[1] for call in poller.tick.call_args_list[:6]))
        self.assertTrue(all(call.args[1] for call in poller.tick.call_args_list[6:12]))

        _small, small_coordinator, _poller, small_unlock = self.run_hud_fixture(
            [ord("u"), ord("r"), ord("q")], height=21, width=75,
        )
        small_unlock.assert_not_called()
        self.assertEqual(sum(call.args[1] is True
                             for call in small_coordinator.tick.call_args_list), 1)

    def test_motion_off_redraws_only_for_state_input_resize_or_age_boundary(self) -> None:
        unchanged, _coordinator, _poller, _unlock = self.run_hud_fixture(
            [-1, -1, -1, ord("q")],
            monotonic_values=[20.0, 21.20, 21.23, 22.01, 22.02],
        )
        self.assertEqual(unchanged.refresh_count, 3)
        self.assertEqual(unchanged.erase_count, 3)

        changed, _coordinator, _poller, _unlock = self.run_hud_fixture(
            [-1, -1, ord("q")],
            monotonic_values=[20.0, 21.20, 21.23, 21.24],
            frames=[make_health_frame_for_test(),
                    make_health_frame_for_test("memory_watch")],
        )
        self.assertEqual(changed.refresh_count, 3)
        self.assertIn("WATCH", changed.row(0))

    def test_motion_off_uses_display_age_boundaries_not_continuous_sample_age(self) -> None:
        sensor = settled_sensor()
        activity = porkyhud.DiskActivity(1024.0, 2.0, 0.0, "iostat")

        def snapshots(age: float) -> list[tuple[object, float, str]]:
            return [
                (None, age, ""), (None, age, ""), (None, age, ""),
                (None, age, ""), (None, age, ""),
                (sensor, age, ""), (activity, age, ""),
            ]

        screen, _coordinator, _poller, _unlock = self.run_hud_fixture(
            [-1, -1, -1, ord("q")],
            monotonic_values=[20.0, 21.20, 21.23, 21.41, 21.42],
            poller_snapshots=(snapshots(0.80) + snapshots(0.83)
                              + snapshots(1.01)),
        )
        self.assertEqual(screen.refresh_count, 3)
        self.assertEqual(screen.erase_count, 3)

    def test_frame_render_key_separates_semantics_from_displayed_age_buckets(self) -> None:
        base = make_health_frame_for_test()
        within_second = replace(
            base,
            assessment=replace(base.assessment, evidence_age_s=1.80),
        )
        base = replace(
            base,
            assessment=replace(base.assessment, evidence_age_s=1.20),
        )
        crossed_second = replace(
            base,
            assessment=replace(base.assessment, evidence_age_s=2.01),
        )
        sensor = replace(settled_sensor(), sensor_sample_age_s=0.80)
        activity = porkyhud.DiskActivity(sample_age=0.80)

        self.assertEqual(
            porkyhud._frame_render_key(base),
            porkyhud._frame_render_key(within_second),
        )
        self.assertEqual(
            porkyhud._render_age_key(base, sensor, activity, 20.0),
            porkyhud._render_age_key(within_second, sensor, activity, 20.0),
        )
        self.assertNotEqual(
            porkyhud._render_age_key(base, sensor, activity, 20.0),
            porkyhud._render_age_key(crossed_second, sensor, activity, 20.0),
        )

    def test_responsive_mode_contract_is_direct_bounded_and_non_mutating(self) -> None:
        operational = ("balanced", "compute", "thermals", "io", "processes",
                       "insights", "devices", "network", "connections")
        all_modes = operational + ("cinema",)
        self.assertEqual(porkyhud.LAYOUTS, list(all_modes))
        self.assertEqual(
            porkyhud.DIRECT_MODE_KEYS,
            {"1": "balanced", "2": "compute", "3": "thermals",
             "4": "io", "5": "processes", "6": "cinema", "7": "insights",
             "8": "devices", "9": "network", "0": "connections"},
        )
        self.assertEqual(porkyhud.HudConfig().animation_mode, 0)

        for height, width, expected in (
            (22, 76, operational), (24, 80, operational),
            (33, 117, operational), (34, 118, all_modes), (42, 132, all_modes),
        ):
            with self.subTest(height=height, width=width):
                self.assertEqual(porkyhud.supported_modes(height, width), expected)

        labels = {
            "balanced": "Overview", "compute": "Compute",
            "thermals": "Thermals", "io": "I/O",
            "processes": "Processes", "insights": "Insights", "devices": "Devices",
            "network": "Network", "connections": "Connections", "cinema": "Cinema",
        }
        config = porkyhud.HudConfig()
        for mode in operational:
            with self.subTest(mode=mode):
                self.assertEqual(
                    porkyhud.select_mode(config, mode, 22, 76),
                    "%s selected" % labels[mode],
                )
                self.assertEqual(config.layout_name, mode)
        before = config.layout_index
        self.assertEqual(
            porkyhud.select_mode(config, "cinema", 33, 117),
            "Cinema requires 118×34 · staying in Connections",
        )
        self.assertEqual(config.layout_index, before)
        for unknown in ("compact", "unknown", None):
            with self.subTest(unknown=unknown), self.assertRaises(ValueError):
                porkyhud.select_mode(config, unknown, 34, 118)
            self.assertEqual(config.layout_index, before)

        for dimensions in ((True, 80), (22, False), (22.0, 80),
                           (22, 80.0), (-1, 80), (22, -1)):
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                porkyhud.supported_modes(*dimensions)

    def test_mode_cycles_resize_normalization_and_motion_are_independent(self) -> None:
        narrow = porkyhud.HudConfig()
        visited = []
        for _ in range(10):
            visited.append(narrow.layout_name)
            porkyhud.cycle_mode(narrow, 24, 80)
        self.assertEqual(set(visited),
                         {"balanced", "compute", "thermals", "io", "processes",
                          "insights", "devices", "network", "connections"})
        self.assertNotIn("compact", visited)
        self.assertNotIn("cinema", visited)

        wide = porkyhud.HudConfig(animation_mode=2)
        visited = []
        for _ in range(10):
            visited.append(wide.layout_name)
            porkyhud.cycle_mode(wide, 34, 118)
        self.assertEqual(visited,
                         ["balanced", "compute", "thermals", "io", "processes",
                          "insights", "devices", "network", "connections", "cinema"])
        self.assertEqual(wide.animation_mode, 2)
        self.assertEqual(wide.layout_name, "balanced")
        self.assertEqual(porkyhud.select_mode(wide, "cinema", 34, 118),
                         "Cinema selected")
        self.assertEqual(wide.animation_mode, 2)
        self.assertEqual(
            porkyhud.normalize_mode_for_viewport(wide, 33, 117),
            "Cinema requires 118×34 · returned to Overview",
        )
        self.assertEqual(wide.layout_name, "balanced")
        self.assertEqual(porkyhud.normalize_mode_for_viewport(wide, 33, 117), "")
        self.assertEqual(porkyhud.normalize_mode_for_viewport(wide, 42, 132), "")
        self.assertEqual(wide.layout_name, "balanced")

    def test_direct_mode_bodies_have_distinct_signatures_and_stay_in_body(self) -> None:
        frame = make_health_frame_for_test()
        history = porkyhud.MetricHistory()
        for value in (12.0, 24.0, 36.0):
            history.add(value, value / 2, value * 1024, value)
        rows = [
            porkyhud.ProcessRow(42, 72.0, 4.0, None, "R", "00:10",
                                "/Applications/Xcode.app/Contents/MacOS/Xcode --token secret"),
            porkyhud.ProcessRow(7, 12.0, 8.0, 0.0, "S", "01:00", "/usr/bin/worker"),
        ]
        static = {"chip": "Test Chip", "logical_cpu": 2, "physical_cpu": 2}
        memory = {"total": 100.0, "used": 60.0, "available": 40.0,
                  "compressed": 4.0, "swap_total": 10.0, "swap_used": 2.0}
        battery = make_battery()
        sensor = settled_sensor()
        disk = porkyhud.DiskInfo("Data", "/System/Volumes/Data", 100, 50, 50)
        activity = porkyhud.DiskActivity(1024.0, 2.0, 0.0, "iostat")
        signatures = {
            "balanced": ("EVIDENCE DETAIL", "TOP PROCESSES"),
            "compute": ("CPU WORKER CORES", "15s", "MEMORY", "TOP CPU"),
            "thermals": ("THERMAL", "FANS + TEMP SENSORS"),
            "io": ("I/O", "disk live", "net d"),
            "processes": ("PROCESS GRID sort=CPU",),
        }
        for mode, expected in signatures.items():
            screen = FakeScreen(22, 76)
            with self.subTest(mode=mode), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                    mock.patch.object(porkyhud, "color_pair", return_value=0):
                scroll = porkyhud.draw_mode_body(
                    screen, mode, 3, 18, 76, frame, [25.0, 50.0], static,
                    memory, battery, sensor, disk, activity, 2048.0, 1024.0,
                    history, rows, 0, "CPU", 0,
                )
            rendered = "\n".join(screen.row(row) for row in range(3, 21))
            for signature in expected:
                self.assertIn(signature, rendered)
            self.assertNotIn("secret", rendered)
            self.assertTrue(all(3 <= write[0] <= 20 for write in screen.writes))
            self.assertTrue(all(1 <= write[1] <= 74 for write in screen.writes))
            self.assertEqual(scroll, 0)

        compute = FakeScreen(22, 76)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_mode_body(
                compute, "compute", 3, 18, 76, frame, [25.0, 50.0], static,
                memory, battery, sensor, disk, activity, 0.0, 0.0, history,
                rows, 0, "CPU", 0,
            )
        self.assertEqual(
            tuple(compute.cells[y][x] for y, x in
                  ((3, 1), (3, 36), (3, 38), (3, 74),
                   (12, 1), (13, 1), (20, 1), (20, 74))),
            ("+", "+", "+", "+", "+", "+", "+", "+"),
        )
        self.assertTrue(all(compute.cells[row][37] == " " for row in range(3, 21)))

        unavailable_io = FakeScreen(22, 76)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_mode_body(
                unavailable_io, "io", 3, 18, 76, frame, (), static, memory,
                battery, sensor, disk,
                porkyhud.DiskActivity(source="unavailable"),
                0.0, 0.0, history, (), 0, "CPU", 0,
            )
        self.assertIn("disk activity unavailable", unavailable_io.text())
        self.assertNotIn("disk live 0B/s", unavailable_io.text())

    def test_overview_process_rows_render_identity_without_arguments(self) -> None:
        screen = FakeScreen(22, 76)
        rows = (
            porkyhud.ProcessRow(
                7, 12.0, 8.0, None, "S", "01:00",
                "/usr/bin/python private_customer_file --customer Acme",
            ),
        )
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_overview_body(
                screen, 3, 18, 76, make_health_frame_for_test(),
                porkyhud.MetricHistory(), rows, "CPU",
            )
        rendered = screen.text()
        self.assertIn("python", rendered)
        self.assertNotIn("private_customer_file", rendered)
        self.assertNotIn("--customer", rendered)
        self.assertNotIn("Acme", rendered)

    def test_specialist_bodies_project_ascii_and_contain_wide_labels(self) -> None:
        frame = make_health_frame_for_test()
        history = porkyhud.MetricHistory()
        battery = make_battery()
        sensor = replace(
            settled_sensor(), raw_hint="传感器🐷" * 20,
            fans=[porkyhud.FanReading("风扇🐷" * 20, 2000, maximum_rpm=4000)],
            fan_count=1,
            temp_sensors=[porkyhud.TempReading("T0", "温度🐷" * 20, 55.0)],
        )
        disk = porkyhud.DiskInfo("数据📁" * 30, "/", 100, 50, 50)
        activity = porkyhud.DiskActivity(0.0, 0.0, 0.0, "测量源🐷")
        rows = [
            porkyhud.ProcessRow(1, 10.0, 2.0, None, "S", "00:01",
                                "/usr/bin/工具🐷 --input 客户文件"),
        ]
        for mode in porkyhud.LAYOUTS:
            height, width = ((34, 118) if mode == "cinema" else (22, 76))
            screen = FakeScreen(height, width)
            with self.subTest(mode=mode), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                    mock.patch.object(porkyhud, "color_pair", return_value=0):
                porkyhud.draw_mode_body(
                    screen, mode, 3, height - 4, width, frame, [99.0],
                    {"chip": "芯片🐷" * 30, "gpus": [{"name": "图形🐷" * 30}]},
                    {"total": 100.0, "used": 99.0}, battery, sensor, disk,
                    activity, 0.0, 0.0, history, rows, 0, "CPU", 0,
                )
            body = "\n".join(screen.row(row) for row in range(3, height - 1))
            self.assertTrue(all(ord(character) < 128 for character in body))

        for mode, gap_columns in (("compute", (37,)), ("thermals", (37,)),
                                  ("cinema", (39, 76))):
            height, width = ((34, 118) if mode == "cinema" else (22, 76))
            screen = FakeScreen(height, width)
            with self.subTest(containment=mode), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=True), \
                    mock.patch.object(porkyhud, "color_pair", return_value=0):
                porkyhud.draw_mode_body(
                    screen, mode, 3, height - 4, width, frame, [99.0],
                    {"chip": "界" * 100}, {"total": 100.0, "used": 99.0},
                    battery, sensor, disk, activity, 0.0, 0.0, history, rows,
                    0, "CPU", 0,
                )
            for column in gap_columns:
                self.assertTrue(all(screen.cells[row][column] == " "
                                    for row in range(10 if mode == "cinema" else 3,
                                                     height - 1)))

    def test_gpu_column_tracks_only_the_clamped_visible_process_slice(self) -> None:
        rows = [
            porkyhud.ProcessRow(index, 1.0, 1.0, None, "S", "00:01", "/bin/task")
            for index in range(1, 5)
        ]
        rows[-1] = replace(rows[-1], gpu=12.5)
        hidden = FakeScreen(10, 70)
        visible = FakeScreen(10, 70)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_process_panel(hidden, 1, 1, 8, 64, rows, 0, "GPU")
            porkyhud.draw_process_panel(visible, 1, 1, 8, 64, rows, 1, "GPU")
        self.assertNotIn("GPU", hidden.row(2))
        self.assertIn("GPU%", visible.row(2))
        self.assertIn("12.5", visible.text())

    def test_overview_lock_is_proof_only_and_actionable(self) -> None:
        frame = make_health_frame_for_test()
        history = porkyhud.MetricHistory()
        locked = FakeScreen(22, 76)
        unlocked = FakeScreen(22, 76)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=True), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_overview_body(
                locked, 3, 18, 76, frame, history, (), "CPU",
                sensor=replace(settled_sensor(), privileged_locked=True),
            )
            porkyhud.draw_overview_body(
                unlocked, 3, 18, 76, frame, history, (), "CPU",
                sensor=settled_sensor(),
            )
        self.assertIn("LOCKED · u: temporary unlock", locked.text())
        self.assertNotIn("LOCKED", unlocked.text())

    def test_typed_thermal_precedes_optional_detail_and_io_requires_evidence(self) -> None:
        sensor = replace(
            settled_sensor(), thermal_warning="legacy warning",
            performance_warning="legacy performance",
        )
        thermal = FakeScreen(16, 60)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_thermal_panel(
                thermal, 1, 1, 12, 50, make_battery(), sensor, 0,
                os_thermal=core.OsThermalState(1, 2),
            )
        self.assertIn("OS thermal: WARN 1", thermal.row(2))
        self.assertIn("OS performance: WARN 2", thermal.row(3))
        self.assertGreater(thermal.text().find("legacy warning"),
                           thermal.text().find("OS thermal"))

        for source, down, up, expected, forbidden in (
            ("checking", None, None, "CHECKING", "LIVE"),
            ("unavailable", -1.0, -1.0, "UNAVAILABLE", "LIVE"),
            ("iostat", 0.0, 0.0, "LIVE", "UNAVAILABLE"),
        ):
            screen = FakeScreen(14, 80)
            with self.subTest(source=source), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                    mock.patch.object(porkyhud, "color_pair", return_value=0):
                porkyhud.draw_io_panel(
                    screen, 1, 1, 10, 74,
                    porkyhud.DiskInfo("Data", "/", 100, 50, 50),
                    porkyhud.DiskActivity(source=source), down, up,
                    porkyhud.MetricHistory(), 0,
                )
            rendered = screen.text()
            self.assertIn(expected, rendered)
            self.assertNotIn(forbidden, rendered)

    def test_draw_helpers_consume_only_preordered_visible_process_rows(self) -> None:
        row = porkyhud.ProcessRow(1, 1.0, 1.0, None, "S", "00:01", "/bin/task")

        class VisibleRows:
            def __len__(self):
                return 10_000

            def __iter__(self):
                raise AssertionError("rendering must not iterate retained rows")

            def __getitem__(self, key):
                if not isinstance(key, slice) or key.stop - key.start > 13:
                    raise AssertionError("rendering may request only a visible slice")
                return [row] * (key.stop - key.start)

        screen = FakeScreen(22, 76)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0), \
                mock.patch.object(porkyhud, "sort_process_rows",
                                  side_effect=AssertionError("sorting belongs to HUD")):
            porkyhud.draw_mode_body(
                screen, "processes", 3, 18, 76, make_health_frame_for_test(),
                (), {}, {}, make_battery(), settled_sensor(),
                porkyhud.DiskInfo("Data", "/", 100, 50, 50),
                porkyhud.DiskActivity(), 0.0, 0.0, porkyhud.MetricHistory(),
                VisibleRows(), 0, "CPU", 0,
            )

        with mock.patch.object(porkyhud, "sort_process_rows",
                              wraps=porkyhud.sort_process_rows) as sorter:
            self.run_hud_fixture(
                [ord("m"), ord("2"), -1, ord("q")], height=22, width=76,
            )
        self.assertEqual(sorter.call_count, 2)

    def test_unavailable_locked_and_stale_details_never_use_nominal_role(self) -> None:
        def attr_for(screen: FakeScreen, fragment: str) -> int:
            return next(write[4] for write in screen.writes if fragment in write[2])

        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", side_effect=lambda pair: pair), \
                mock.patch.object(porkyhud.curses, "A_BOLD", 0):
            cpu = FakeScreen(14, 60)
            porkyhud.draw_cpu_panel(cpu, 1, 1, 10, 50, [], {},
                                    porkyhud.MetricHistory(), 0)
            sensors = FakeScreen(20, 70)
            porkyhud.draw_sensors_panel(
                sensors, 1, 1, 16, 60, make_battery(),
                replace(settled_sensor(), sensor_sample_age_s=30.0), 0,
            )
            gpu = FakeScreen(12, 60)
            porkyhud.draw_gpu_panel(gpu, 1, 1, 8, 50, {}, settled_sensor(), 0)
            locked_gpu = FakeScreen(12, 60)
            porkyhud.draw_gpu_panel(
                locked_gpu, 1, 1, 8, 50, {"gpus": [{"name": "GPU"}]},
                replace(settled_sensor(), privileged_locked=True), 0,
            )
        self.assertEqual(attr_for(cpu, "unavailable"), porkyhud.PAIR_SECONDARY)
        self.assertEqual(attr_for(sensors, "stale"), porkyhud.PAIR_PRIMARY)
        self.assertEqual(attr_for(sensors, "none exposed"), porkyhud.PAIR_SECONDARY)
        self.assertEqual(attr_for(gpu, "GPU scan unavailable"),
                         porkyhud.PAIR_SECONDARY)
        self.assertEqual(attr_for(locked_gpu, "press u"), porkyhud.PAIR_PRIMARY)

    def test_decision_projection_primitives_are_exact_and_validate_inputs(self) -> None:
        self.assertEqual(
            porkyhud.semantic_text(
                "CPU averaged ≥90% for 15s. · 118×34 – evidence ≤2s", False,
            ),
            "CPU averaged >=90% for 15s. | 118x34 - evidence <=2s",
        )
        self.assertEqual(
            porkyhud.semantic_text("Données 📁", False), "Donnees ?",
        )
        names = {"balanced": "1 OVERVIEW", "compact": "1 OVERVIEW",
                 "compute": "2 COMPUTE", "thermals": "3 THERMALS", "io": "4 I/O",
                 "processes": "5 PROCESSES", "cinema": "6 CINEMA"}
        self.assertEqual({mode: porkyhud.display_mode_name(mode) for mode in names}, names)
        with self.assertRaises(ValueError):
            porkyhud.display_mode_name("unknown")

        ages = (
            (0.0, "<1s"), (0.999, "<1s"), (18.9, "18s"),
            (125.9, "2m 05s"), (3_720.9, "1h 02m"),
            (183_600.9, "2d 03h"),
        )
        for age, expected in ages:
            with self.subTest(age=age):
                self.assertEqual(porkyhud.format_evidence_age(age), expected)
        with self.assertRaises(ValueError):
            porkyhud.format_evidence_age(float("inf"))

    def test_compact_reasons_cover_policy_recovery_and_blocked_states(self) -> None:
        reasons = {
            "normal": "No active CPU, memory, thermal, or storage pressure.",
            "memory_watch": "macOS memory pressure is elevated.",
            "memory_critical": "macOS memory pressure is critical.",
            "thermal_watch": "macOS thermal/performance warning is active.",
            "cpu_watch": "CPU averaged ≥90% for 15s.",
            "storage_watch": "Storage low · 8.0GB free on Data.",
            "incomplete": "Health incomplete · MEM MISSING.",
            "delayed": "Health incomplete · CPU STALE · 18s.",
            "memory_recovery": (
                "Memory pressure normal · confirming recovery · triggered 18s ago."
            ),
            "memory_critical_recovery": (
                "Memory eased to elevated · confirming recovery · triggered 18s ago."
            ),
            "thermal_recovery": (
                "macOS warnings clear · confirming thermal recovery · triggered 18s ago."
            ),
            "cpu_recovery": (
                "CPU use eased · confirming recovery · triggered 18s ago."
            ),
            "storage_recovery": (
                "Storage headroom improved · confirming recovery · triggered 18s ago."
            ),
        }
        for case, expected in reasons.items():
            with self.subTest(case=case):
                self.assertEqual(
                    porkyhud.compact_reason(
                        make_health_frame_for_test(case), 80, 20.0,
                    ),
                    expected,
                )

        frame = make_health_frame_for_test("cpu_watch")
        assessment = replace(frame.assessment, contributor_name="Xcode")
        frame = replace(frame, assessment=assessment)
        self.assertEqual(
            porkyhud.compact_reason(frame, 120, 20.0),
            "CPU capacity has averaged at least 90% for 15 seconds. · "
            "Xcode is the leading recent CPU process.",
        )

        storage = make_health_frame_for_test("storage_watch")
        disk = replace(storage.disk_capacity, volume_name="数据" * 80)
        assessment = replace(
            storage.assessment,
            reason="Storage headroom is low: 8.0 GiB remains on %s." % disk.volume_name,
        )
        storage = replace(storage, assessment=assessment, disk_capacity=disk)
        complete = porkyhud.compact_reason(storage, 76, 20.0)
        self.assertEqual(complete, "Storage headroom is low.")
        self.assertLessEqual(porkyhud.display_cell_width(complete), 75)

    def test_recovery_reasons_choose_only_complete_copies_at_every_width(self) -> None:
        recovery_copy = {
            "memory_recovery": "Memory pressure normal · confirming recovery",
            "memory_critical_recovery": "Memory eased to elevated · confirming recovery",
            "thermal_recovery": "macOS warnings clear · confirming thermal recovery",
            "cpu_recovery": "CPU use eased · confirming recovery",
            "storage_recovery": "Storage headroom improved · confirming recovery",
        }
        suffix = " · triggered 18s ago."
        for case, compact in recovery_copy.items():
            frame = make_health_frame_for_test(case)
            exact = frame.assessment.reason + suffix
            compact = compact + suffix
            for width in (76, 80, 90, 117, 118, 132):
                expected = (
                    exact if width >= 90
                    and porkyhud.display_cell_width(exact) <= width - 1
                    else compact
                )
                with self.subTest(case=case, width=width):
                    actual = porkyhud.compact_reason(frame, width, 20.0)
                    self.assertEqual(actual, expected)
                    self.assertTrue(actual.endswith(suffix))
                    self.assertLessEqual(
                        porkyhud.display_cell_width(actual), width - 1,
                    )

    def test_reason_fit_is_safe_before_unicode_or_ascii_projection(self) -> None:
        volume_name = "≥" * 23
        storage = make_health_frame_for_test("storage_watch")
        disk = replace(storage.disk_capacity, volume_name=volume_name)
        assessment = replace(
            storage.assessment,
            reason="Storage headroom is low: 8.0 GiB remains on %s." % volume_name,
        )
        storage = replace(storage, assessment=assessment, disk_capacity=disk)

        cpu = make_health_frame_for_test("cpu_watch")
        base_reason = cpu.assessment.reason
        identity_overhead = porkyhud.display_cell_width(
            base_reason + " ·  is the leading recent CPU process.",
        )
        cases = [(storage, 90, "Storage headroom is low.")]
        for width in (100, 117, 118, 132):
            contributor = "≥" * (width - 1 - identity_overhead)
            candidate = replace(
                cpu,
                assessment=replace(cpu.assessment, contributor_name=contributor),
            )
            cases.append((candidate, width, base_reason))

        for frame, width, expected in cases:
            with self.subTest(width=width):
                reason = porkyhud.compact_reason(frame, width, 20.0)
                self.assertEqual(reason, expected)
                for unicode_enabled in (True, False):
                    projected = porkyhud.semantic_text(reason, unicode_enabled)
                    self.assertLessEqual(
                        porkyhud.display_cell_width(projected), width - 1,
                    )
                    screen = FakeScreen(22, width)
                    with mock.patch.object(
                        porkyhud, "unicode_ok", return_value=unicode_enabled,
                    ):
                        porkyhud.draw_decision_header(
                            screen, frame, porkyhud.HudConfig(), 20.0,
                        )

    def test_header_truth_table_distinguishes_bootstrap_baseline_and_snapshot(self) -> None:
        frame = make_health_frame_for_test()
        self.assertEqual(
            porkyhud.decision_header_lines(frame, "balanced", 80, 21.0),
            (
                "PorkyHUD · 1 OVERVIEW · NORMAL · LIVE · evidence 2s",
                "No active CPU, memory, thermal, or storage pressure.",
            ),
        )

        checking = make_health_frame_for_test("checking")
        core_ids = (
            core.SourceId.CPU_USAGE,
            core.SourceId.MEMORY_PRESSURE,
            core.SourceId.THERMAL_OS,
            core.SourceId.DISK_CAPACITY,
        )
        for ready_count in range(4):
            views = dict(checking.source_views)
            for source_id in core_ids[:ready_count]:
                views[source_id] = core.SourceView(
                    source_id, core.SourceViewStatus.LIVE,
                    1.0, False, True, 1, None,
                )
            assessment = replace(checking.assessment, blockers=core_ids[ready_count:])
            candidate = replace(checking, assessment=assessment, source_views=views)
            with self.subTest(ready_count=ready_count):
                self.assertEqual(
                    porkyhud.decision_header_lines(
                        candidate, "balanced", 80, 20.0,
                    ),
                    (
                        "PorkyHUD · 1 OVERVIEW · CHECKING %d/4 core sources"
                        % ready_count,
                        "Waiting for core evidence.",
                    ),
                )

        baseline = make_health_frame_for_test("cpu_baseline")
        for coverage, shown in ((0.0, 0), (7.999, 7), (14.999, 14)):
            assessment = replace(baseline.assessment, cpu_coverage_s=coverage)
            with self.subTest(coverage=coverage):
                self.assertEqual(
                    porkyhud.decision_header_lines(
                        replace(baseline, assessment=assessment),
                        "balanced", 80, 20.0,
                    ),
                    (
                        "PorkyHUD · 1 OVERVIEW · CHECKING CPU baseline %d/15s"
                        % shown,
                        "Building the 15-second CPU baseline.",
                    ),
                )

        snapshot = make_health_frame_for_test("snapshot")
        snapshot_lines = porkyhud.decision_header_lines(
            snapshot, "balanced", 80, 20.0,
        )
        self.assertEqual(
            snapshot_lines,
            (
                "PorkyHUD · 1 OVERVIEW · UNKNOWN · INCOMPLETE",
                "CPU trend requires live monitoring.",
            ),
        )
        self.assertNotIn("CHECKING", " ".join(snapshot_lines))

        failed = make_health_frame_for_test("delayed")
        failed_lines = porkyhud.decision_header_lines(
            failed, "balanced", 80, 20.0,
        )
        self.assertIn("UNKNOWN · DELAYED", failed_lines[0])
        self.assertNotIn("CHECKING", " ".join(failed_lines))
        for invalid_now in (19.999, float("inf"), float("nan"), True):
            with self.subTest(invalid_now=invalid_now), self.assertRaises(ValueError):
                porkyhud.decision_header_lines(frame, "balanced", 80, invalid_now)

    def test_source_view_text_covers_every_status_and_retry_detail(self) -> None:
        cases = (
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.CHECKING, None, False, "CHECKING"),
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.LIVE, 1.0, False, "LIVE"),
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.STALE, 18.0, False, "STALE · 18s"),
            (core.SourceId.SENSOR_POWERMETRICS, core.SourceViewStatus.LOCKED, None, False, "LOCKED · u: temporary unlock"),
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.PERMISSION_DENIED, None, False, "PERMISSION DENIED"),
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.MISSING, None, False, "MISSING"),
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.UNSUPPORTED, None, False, "UNSUPPORTED"),
            (core.SourceId.BATTERY, core.SourceViewStatus.NOT_APPLICABLE, None, False, "NOT APPLICABLE"),
            (core.SourceId.SENSOR_SMC, core.SourceViewStatus.NOT_EXPOSED, None, False, "NOT EXPOSED"),
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.ERROR, 18.0, False, "ERROR · last good 18s ago"),
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.ERROR, None, False, "ERROR · no valid sample"),
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.ERROR, None, True, "ERROR · no valid sample · checking"),
        )
        for source_id, status, age, refreshing, expected in cases:
            view = core.SourceView(
                source_id, status, age, refreshing, status is core.SourceViewStatus.LIVE,
                1 if age is not None else None, None,
            )
            with self.subTest(status=status, age=age, refreshing=refreshing):
                self.assertEqual(porkyhud.source_view_text(view, 0.2), expected)

        invalid_locked = core.SourceView(
            core.SourceId.CPU_USAGE, core.SourceViewStatus.LOCKED,
            None, False, False, None, core.FailureCode.PERMISSION_DENIED,
        )
        with self.assertRaises(ValueError):
            porkyhud.source_view_text(invalid_locked)
        with self.assertRaises(ValueError):
            porkyhud.source_view_text(invalid_locked, -0.1)

    def test_header_and_ribbon_own_rows_zero_through_two_at_every_viewport(self) -> None:
        frame = make_health_frame_for_test()
        for height, width in ((22, 76), (24, 80), (33, 117), (34, 118), (42, 132)):
            for unicode_enabled in (False, True):
                screen = FakeScreen(height, width)
                config = porkyhud.HudConfig()
                with self.subTest(height=height, width=width, unicode=unicode_enabled), \
                        mock.patch.object(porkyhud, "unicode_ok", return_value=unicode_enabled), \
                        mock.patch.object(porkyhud, "color_pair", return_value=0):
                    next_y = porkyhud.draw_decision_header(
                        screen, frame, config, 20.0,
                    )
                    next_y = porkyhud.draw_evidence_ribbon(
                        screen, next_y, frame, 20.0,
                    )
                self.assertEqual(next_y, 3)
                rendered = screen.text()
                for token in ("CPU", "MEM", "THERM", "DISK"):
                    self.assertIn(token, rendered)
                self.assertTrue(screen.row(0).startswith("PorkyHUD"))
                for row in range(3):
                    self.assertLessEqual(
                        porkyhud.display_cell_width(screen.row(row)), width,
                    )

        checking = make_health_frame_for_test("checking")
        views = dict(checking.source_views)
        for source_id in (
            core.SourceId.CPU_USAGE, core.SourceId.MEMORY_PRESSURE,
            core.SourceId.THERMAL_OS, core.SourceId.DISK_CAPACITY,
        ):
            views[source_id] = core.SourceView(
                source_id, core.SourceViewStatus.ERROR, 18.0,
                False, False, 1, core.FailureCode.NONZERO_EXIT,
            )
        failed = replace(
            checking,
            assessment=replace(
                checking.assessment, evidence_state=core.EvidenceState.DELAYED,
            ),
            source_views=views,
        )
        self.assertEqual(
            porkyhud.evidence_ribbon_lines(failed, 76, 20.0),
            ("CPU/MEM/THERM/DISK ERROR",),
        )

        mixed_views = dict(checking.source_views)
        for source_id, status in (
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.PERMISSION_DENIED),
            (core.SourceId.MEMORY_PRESSURE, core.SourceViewStatus.MISSING),
            (core.SourceId.THERMAL_OS, core.SourceViewStatus.UNSUPPORTED),
            (core.SourceId.DISK_CAPACITY, core.SourceViewStatus.NOT_APPLICABLE),
        ):
            mixed_views[source_id] = core.SourceView(
                source_id, status, None, False, False, None, None,
            )
        mixed = replace(checking, source_views=mixed_views)
        self.assertEqual(
            porkyhud.evidence_ribbon_lines(mixed, 76, 20.0),
            ("CPU PERMISSION DENIED/MEM MISSING/THERM UNSUPPORTED/DISK NOT APPLICABLE",),
        )

        repeated_views = dict(checking.source_views)
        for source_id, status in (
            (core.SourceId.CPU_USAGE, core.SourceViewStatus.CHECKING),
            (core.SourceId.MEMORY_PRESSURE, core.SourceViewStatus.PERMISSION_DENIED),
            (core.SourceId.THERMAL_OS, core.SourceViewStatus.NOT_APPLICABLE),
            (core.SourceId.DISK_CAPACITY, core.SourceViewStatus.PERMISSION_DENIED),
        ):
            repeated_views[source_id] = core.SourceView(
                source_id, status, None, False, False, None, None,
            )
        repeated = replace(checking, source_views=repeated_views)
        self.assertEqual(
            porkyhud.evidence_ribbon_lines(repeated, 76, 20.0),
            ("CPU CHECKING/MEM PERMISSION DENIED/THERM NOT APPLICABLE/DISK=MEM",),
        )
        screen = FakeScreen(22, 76)
        with mock.patch.object(porkyhud, "color_pair", side_effect=lambda value: value << 20):
            porkyhud.draw_evidence_ribbon(screen, 2, repeated, 20.0)
        self.assertEqual(screen.writes[-1][2], "DISK=MEM")
        self.assertEqual(
            screen.writes[-1][4],
            (porkyhud.PRIMARY << 20) | porkyhud.curses.A_BOLD,
        )

    def test_width_76_ribbon_covers_every_core_exception_cartesian_product(self) -> None:
        frame = make_health_frame_for_test("checking")
        core_ids = (
            core.SourceId.CPU_USAGE,
            core.SourceId.MEMORY_PRESSURE,
            core.SourceId.THERMAL_OS,
            core.SourceId.DISK_CAPACITY,
        )
        statuses = (
            core.SourceViewStatus.CHECKING,
            core.SourceViewStatus.STALE,
            core.SourceViewStatus.PERMISSION_DENIED,
            core.SourceViewStatus.MISSING,
            core.SourceViewStatus.UNSUPPORTED,
            core.SourceViewStatus.NOT_APPLICABLE,
            core.SourceViewStatus.NOT_EXPOSED,
            core.SourceViewStatus.ERROR,
        )
        for combination in itertools.product(statuses, repeat=4):
            views = dict(frame.source_views)
            for source_id, status in zip(core_ids, combination):
                retained = status in (
                    core.SourceViewStatus.STALE, core.SourceViewStatus.ERROR,
                )
                views[source_id] = core.SourceView(
                    source_id, status, 18.0 if retained else None,
                    False, False, 1 if retained else None, None,
                )
            line = porkyhud.evidence_ribbon_lines(
                replace(frame, source_views=views), 76, 20.0,
            )[0]
            positions = tuple(line.find(label) for label in ("CPU", "MEM", "THERM", "DISK"))
            with self.subTest(combination=combination):
                self.assertEqual(positions, tuple(sorted(positions)))
                self.assertNotIn(-1, positions)
                for status in set(combination):
                    self.assertIn(status.value, line)
                for unicode_enabled in (True, False):
                    projected = porkyhud.semantic_text(line, unicode_enabled)
                    self.assertLessEqual(porkyhud.display_cell_width(projected), 75)

    def test_initial_shell_ascii_no_color_and_display_cells_preserve_meaning(self) -> None:
        screen = FakeScreen(22, 76)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=True), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            self.assertEqual(
                porkyhud.draw_initial_shell(screen, porkyhud.HudConfig()), 3,
            )
        self.assertEqual(
            tuple(screen.row(row).rstrip() for row in range(3)),
            (
                "PorkyHUD · 1 OVERVIEW · CHECKING 0/4 core sources",
                "Waiting for core evidence.",
                "CPU CHECKING · MEM CHECKING · THERM CHECKING · DISK CHECKING",
            ),
        )

        ascii_screen = FakeScreen(22, 76)
        storage = make_health_frame_for_test("storage_watch")
        disk = replace(storage.disk_capacity, volume_name="Données 📁")
        assessment = replace(
            storage.assessment,
            reason="Storage headroom is low: 8.0 GiB remains on Données 📁.",
        )
        storage = replace(storage, assessment=assessment, disk_capacity=disk)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
                mock.patch.object(porkyhud, "unicode_ok", return_value=False):
            y = porkyhud.draw_decision_header(
                ascii_screen, storage, porkyhud.HudConfig(), 20.0,
            )
            porkyhud.draw_evidence_ribbon(ascii_screen, y, storage, 20.0)
        rendered = ascii_screen.text()
        self.assertIn("WATCH", rendered)
        self.assertIn("LIVE", rendered)
        self.assertTrue(all(ord(character) < 128 for character in rendered))

        cells = FakeScreen(3, 10)
        porkyhud.safe_add(cells, 0, 0, "A界B")
        porkyhud.safe_add(cells, 1, 0, "e\u0301x")
        porkyhud.safe_add(cells, 2, 0, "123456789界")
        self.assertEqual(cells.cells[0][1:4], ["界", "", "B"])
        self.assertEqual(cells.cells[1][0], "é")
        self.assertEqual(cells.row(2), "123456789 ")

    def test_semantic_roles_are_stable_and_header_attributes_match_severity(self) -> None:
        self.assertEqual(
            (
                porkyhud.PRIMARY, porkyhud.INFO, porkyhud.NOMINAL,
                porkyhud.WATCH, porkyhud.CRITICAL, porkyhud.STRUCTURE,
                porkyhud.SECONDARY, porkyhud.BRAND,
            ),
            tuple(range(1, 9)),
        )
        self.assertEqual(
            (
                porkyhud.PAIR_PRIMARY, porkyhud.PAIR_INFO,
                porkyhud.PAIR_NOMINAL, porkyhud.PAIR_WATCH,
                porkyhud.PAIR_CRITICAL, porkyhud.PAIR_STRUCTURE,
                porkyhud.PAIR_SECONDARY, porkyhud.PAIR_BRAND,
            ),
            tuple(range(1, 9)),
        )
        expected_theme_roles = {
            "Neon Grid": (231, 51, 46, 220, 196, 39, 45, 201),
            "Catppuccin": (189, 110, 119, 229, 203, 111, 153, 183),
            "Solar Circuit": (255, 87, 82, 226, 202, 33, 87, 165),
            "Graphite": (252, 110, 152, 222, 203, 245, 250, 183),
        }
        self.assertEqual(
            {
                theme["name"]: tuple(
                    theme["colors"][role] for role in range(1, 9)
                )
                for theme in porkyhud.THEMES
            },
            expected_theme_roles,
        )
        self.assertTrue(all(set(theme["colors"]) == set(range(1, 9))
                            for theme in porkyhud.THEMES))
        with mock.patch.object(porkyhud, "color_pair", side_effect=lambda pair: pair << 20):
            for level, pair in (("normal", porkyhud.NOMINAL), ("watch", porkyhud.WATCH),
                                ("critical", porkyhud.CRITICAL), ("checking", porkyhud.PRIMARY)):
                self.assertEqual(porkyhud.read_attr(level),
                                 (pair << 20) | porkyhud.curses.A_BOLD)
            with self.assertRaises(ValueError):
                porkyhud.read_attr("decorative-warning")

        expected_roles = {
            "normal": porkyhud.NOMINAL,
            "memory_watch": porkyhud.WATCH,
            "memory_critical": porkyhud.CRITICAL,
            "checking": porkyhud.PRIMARY,
        }
        for case, pair in expected_roles.items():
            screen = FakeScreen(22, 80)
            with self.subTest(case=case), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                    mock.patch.object(porkyhud, "color_pair", side_effect=lambda value: value << 20):
                porkyhud.draw_decision_header(
                    screen, make_health_frame_for_test(case),
                    porkyhud.HudConfig(), 20.0,
                )
            self.assertEqual(
                screen.writes[0][4], (pair << 20) | porkyhud.curses.A_BOLD,
            )

    def test_alert_pairs_are_reserved_for_typed_decision_and_source_evidence(self) -> None:
        alert_names = {"PAIR_WATCH", "PAIR_CRITICAL"}
        allowed = {"_ribbon_role", "read_attr"}
        offenders = {
            name: sorted(alert_names.intersection(value.__code__.co_names))
            for name, value in vars(porkyhud).items()
            if callable(value) and hasattr(value, "__code__") and name not in allowed
            and alert_names.intersection(value.__code__.co_names)
        }
        self.assertEqual(offenders, {})
        self.assertNotIn("read_attr", porkyhud.draw_small_screen.__code__.co_names)

        screen = FakeScreen(42, 132)
        history = porkyhud.MetricHistory()
        hot_battery = make_battery(
            percent=5, external_connected=False, temp_c=99.0,
        )
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", side_effect=lambda pair: pair), \
                mock.patch.object(porkyhud.curses, "A_BOLD", 0):
            porkyhud.draw_header(
                screen, 132, 0, {"chip": "M", "model": "Mac"},
                porkyhud.HudConfig(),
            )
            porkyhud.draw_cpu_panel(
                screen, 8, 1, 12, 60, [99.0], {}, history, 0,
            )
            porkyhud.draw_battery_panel(
                screen, 21, 1, 10, 60, hot_battery, 0,
            )
        self.assertTrue(screen.writes)
        self.assertTrue({porkyhud.PAIR_INFO, porkyhud.PAIR_STRUCTURE}
                        .intersection(write[4] for write in screen.writes))
        self.assertFalse(
            {porkyhud.PAIR_WATCH, porkyhud.PAIR_CRITICAL}
            .intersection(write[4] for write in screen.writes),
        )

    def test_ribbon_segments_use_semantic_attributes_without_changing_text(self) -> None:
        screen = FakeScreen(22, 80)
        frame = make_health_frame_for_test("memory_critical")
        views = dict(frame.source_views)
        views[core.SourceId.THERMAL_OS] = core.SourceView(
            core.SourceId.THERMAL_OS, core.SourceViewStatus.STALE,
            18.0, False, False, 1, None,
        )
        frame = replace(frame, source_views=views, thermal=None)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=True), \
                mock.patch.object(porkyhud, "color_pair", side_effect=lambda value: value << 20):
            porkyhud.draw_evidence_ribbon(screen, 2, frame, 20.0)
        self.assertEqual(screen.row(2).rstrip(),
                         "CPU 10% · MEM CRITICAL · THERM STALE · 18s · DISK 50% FREE")
        attrs = [write[4] for write in screen.writes]
        self.assertEqual(
            attrs,
            [
                (porkyhud.NOMINAL << 20) | porkyhud.curses.A_BOLD,
                (porkyhud.CRITICAL << 20) | porkyhud.curses.A_BOLD,
                (porkyhud.PRIMARY << 20) | porkyhud.curses.A_BOLD,
                (porkyhud.NOMINAL << 20) | porkyhud.curses.A_BOLD,
            ],
        )

    def test_decision_drawers_are_pure_and_leave_frame_lifecycle_to_hud(self) -> None:
        frame = make_health_frame_for_test()
        screen = FakeScreen(22, 80)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0), \
                mock.patch.object(porkyhud, "run_command", side_effect=AssertionError), \
                mock.patch.object(porkyhud, "run_structured_command", side_effect=AssertionError), \
                mock.patch.object(porkyhud, "collect_static_info", side_effect=AssertionError), \
                mock.patch.object(porkyhud, "reduce_health", side_effect=AssertionError), \
                mock.patch.object(porkyhud.time, "monotonic", side_effect=AssertionError), \
                mock.patch.object(porkyhud.time, "time", side_effect=AssertionError):
            self.assertEqual(
                porkyhud.decision_header_lines(frame, "balanced", 80, 20.0)[0],
                "PorkyHUD · 1 OVERVIEW · NORMAL · LIVE · evidence 1s",
            )
            porkyhud.evidence_ribbon_lines(frame, 80, 20.0)
            next_y = porkyhud.draw_decision_header(
                screen, frame, porkyhud.HudConfig(), 20.0,
            )
            porkyhud.draw_evidence_ribbon(screen, next_y, frame, 20.0)
        self.assertEqual(screen.erase_count, 0)
        self.assertEqual(screen.refresh_count, 0)

    def test_hud_places_decision_header_ribbon_and_overview_before_footer(self) -> None:
        frame = make_health_frame_for_test()
        coordinator = mock.Mock()
        coordinator.source_generation.return_value = 0
        coordinator.tick.return_value = frame
        coordinator.per_core.return_value = (10.0,)
        coordinator.process_rows.return_value = ()
        poller = mock.Mock()
        poller.tick.return_value = None
        poller.snapshot.return_value = (None, 0.0, "")
        poller.sample_snapshot.return_value = porkyhud.DescriptiveSample(None, 0.0, "", 0, 0.0)
        screen = FakeScreen(24, 80, keys=[-1, ord("q")])
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", return_value=poller), \
                mock.patch.object(porkyhud, "_start_static_discovery", return_value=poller), \
                mock.patch.object(porkyhud, "init_colors"), \
                mock.patch.object(porkyhud, "apply_dark_screen"), \
                mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=21.0), \
                mock.patch.object(porkyhud.time, "sleep"):
            porkyhud.hud(screen)
        coordinator.close.assert_called_once_with()
        self.assertTrue(screen.row(0).startswith(
            "PorkyHUD | 1 OVERVIEW | NORMAL | LIVE | evidence 2s"
        ))
        self.assertIn("No active CPU", screen.row(1))
        self.assertIn("CPU 10%", screen.row(2))
        self.assertIn("MEM NORMAL", screen.row(2))
        self.assertIn("THERM CLEAR", screen.row(2))
        self.assertIn("DISK 50% FREE", screen.row(2))
        self.assertNotIn("Fresh core evidence", screen.row(22))
        self.assertIn("EVIDENCE DETAIL", screen.row(10))
        self.assertIn("TOP PROCESSES", screen.row(10))
        self.assertEqual(
            screen.row(23).strip(),
            "1-5 mode | d explore | h help | t theme | a motion | q quit",
        )

    def test_pure_frame_projection_10k_mean_is_under_five_milliseconds(self) -> None:
        frame = make_health_frame_for_test()
        started = time.perf_counter()
        for _ in range(10_000):
            porkyhud.decision_header_lines(frame, "balanced", 80, 20.0)
            porkyhud.evidence_ribbon_lines(frame, 80, 20.0)
        elapsed = time.perf_counter() - started
        self.assertLessEqual(elapsed / 10_000, 0.005)

    def test_narrow_cpu_lane_fallback_is_total_and_shows_100_percent(self) -> None:
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False):
            tiny = porkyhud.cpu_lane_text("P0", 0, 100.0, 8, 0)
            medium = porkyhud.cpu_lane_text("P0", 0, 100.0, 11, 0)
            clamped = porkyhud.cpu_lane_text("P0", 0, 140.0, 11, 0)
        self.assertIsInstance(tiny, str)
        self.assertEqual(tiny, "P0 100%")
        self.assertLessEqual(len(tiny), 8)
        self.assertTrue(medium.endswith("100"))
        self.assertTrue(clamped.endswith("100"))
        self.assertLessEqual(len(medium), 11)

    def test_wide_column_widths_tile_the_available_screen(self) -> None:
        for width in range(118, 241):
            for layout in porkyhud.LAYOUTS:
                with self.subTest(width=width, layout=layout):
                    left, center, right = porkyhud.wide_column_widths(width, layout)
                    self.assertEqual(left + center + right + 4, width)
                    self.assertGreaterEqual(left, 34)
                    self.assertGreaterEqual(center, 34)
                    self.assertGreaterEqual(right, 24)

    def test_short_wide_terminal_uses_the_non_overlapping_compact_header(self) -> None:
        screen = FakeScreen(22, 118)
        with mock.patch.object(porkyhud, "color_pair", return_value=0):
            height = porkyhud.draw_header(
                screen,
                118,
                0,
                {"chip": "Test Chip", "model": "Test Mac"},
                porkyhud.HudConfig(),
            )
        rendered = "\n".join(screen.row(row) for row in range(height))
        self.assertEqual(height, 3)
        self.assertIn("P O R K Y H U D", rendered)
        self.assertIn("Copyright (c) DMS", rendered)
        self.assertNotIn(".-oo-.", rendered)
        self.assertEqual((screen.erase_count, screen.refresh_count), (0, 0))

    def test_layout_height_and_render_interval_invariants(self) -> None:
        specs = [(7, 0), (7, 0), (8, 1), (7, 3)]
        for total in range(25, 101):
            heights = porkyhud.proportional_heights(total, specs)
            self.assertEqual(sum(heights), total)
            self.assertTrue(all(height >= 3 for height in heights))
        self.assertEqual(porkyhud.process_body_height(4), 0)
        self.assertEqual(porkyhud.process_body_height(10), 5)
        self.assertEqual(
            [porkyhud.render_interval(mode) for mode in range(3)],
            [1.00, 0.40, 0.08],
        )
        with mock.patch.object(porkyhud, "unicode_ok", return_value=True):
            self.assertEqual(
                porkyhud.shortcut_status(76),
                "1–5 mode · d explore · h help · t theme · a motion · q quit",
            )
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False):
            self.assertEqual(
                porkyhud.shortcut_status(76),
                "1-5 mode | d explore | h help | t theme | a motion | q quit",
            )
        for total in range(6, 24):
            system_height, power_height = porkyhud.compact_top_heights(total)
            self.assertEqual(system_height + power_height, total)
            self.assertGreaterEqual(system_height, 3)
            self.assertGreaterEqual(power_height, 3)

    def test_help_overlay_clears_every_cell_inside_its_rectangle(self) -> None:
        sentinel = "\x00"
        screen = FakeScreen(40, 120, fill=sentinel)
        config = porkyhud.HudConfig()
        sensor = porkyhud.default_sensor_info()

        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), mock.patch.object(
            porkyhud,
            "color_pair",
            return_value=0,
        ):
            porkyhud.draw_help_overlay(screen, config, sensor)

        overlay_width = 72
        overlay_height = 21
        overlay_y = (40 - overlay_height) // 2
        overlay_x = (120 - overlay_width) // 2
        for row in range(overlay_y, overlay_y + overlay_height):
            self.assertNotIn(
                sentinel,
                screen.row(row)[overlay_x : overlay_x + overlay_width],
            )
        self.assertEqual(screen.cells[overlay_y][overlay_x - 1], sentinel)
        blank_content_row = overlay_y + 13
        self.assertEqual(
            screen.row(blank_content_row)[overlay_x + 1 : overlay_x + overlay_width - 1],
            " " * (overlay_width - 2),
        )

        settled = FakeScreen(40, 120)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), mock.patch.object(
            porkyhud,
            "color_pair",
            return_value=0,
        ):
            porkyhud.draw_help_overlay(
                settled,
                config,
                replace(
                    settled_sensor(),
                    privileged_locked=True,
                    raw_hint="smc direct",
                ),
            )
        rendered = "\n".join(settled.row(row) for row in range(40))
        self.assertIn("sensors: SMC online / power locked", rendered)
        self.assertIn("u             unlock advanced macOS sensors", rendered)

        unlocked = FakeScreen(40, 120)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_help_overlay(unlocked, config, settled_sensor())
        unlocked_text = unlocked.text()
        self.assertNotIn("u             unlock advanced macOS sensors", unlocked_text)
        self.assertIn("6  Cinema", unlocked_text)

    def test_gpu_panel_uses_truthful_static_topology_and_prioritizes_availability(self) -> None:
        screen = FakeScreen(8, 60)
        sensor = replace(
            porkyhud.default_sensor_info(),
            privileged_locked=True,
            thermal_warning="nominal",
        )
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), mock.patch.object(
            porkyhud,
            "color_pair",
            return_value=0,
        ):
            porkyhud.draw_gpu_panel(
                screen,
                1,
                2,
                4,
                42,
                {"gpus": [{"name": "Test GPU", "cores": "40"}]},
                sensor,
                99,
            )
        rendered = "\n".join(screen.row(row) for row in range(1, 5))
        self.assertIn("Test GPU [40 GPU cores]", rendered)
        self.assertIn("activity: press u for live telemetry", rendered)
        self.assertNotIn("cores: [", rendered)

        narrow = FakeScreen(8, 50)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), mock.patch.object(
            porkyhud,
            "color_pair",
            return_value=0,
        ):
            porkyhud.draw_gpu_panel(
                narrow,
                1,
                2,
                4,
                34,
                {"gpus": [{"name": "Test GPU", "cores": "40"}]},
                sensor,
                99,
            )
        narrow_rendered = "\n".join(narrow.row(row) for row in range(1, 5))
        self.assertIn("press u for GPU activity", narrow_rendered)

    def test_process_footer_does_not_overwrite_bottom_border(self) -> None:
        screen = FakeScreen(20, 80)
        rows = [
            porkyhud.ProcessRow(1, 50.0, 1.0, 0.0, "R", "00:01", "/bin/task")
        ]
        y, x, height, width = 2, 5, 10, 40
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), mock.patch.object(
            porkyhud,
            "color_pair",
            return_value=0,
        ):
            porkyhud.draw_process_panel(screen, y, x, height, width, rows, 0, "CPU")

        bottom = screen.row(y + height - 1)[x : x + width]
        self.assertEqual(bottom, "+" + "-" * (width - 2) + "+")
        footer = screen.row(y + height - 2)[x : x + width]
        self.assertIn("1", footer)
        self.assertEqual(footer[0], "|")
        self.assertEqual(footer[-1], "|")

    def test_empty_process_panel_has_a_loading_state(self) -> None:
        screen = FakeScreen(14, 60)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), mock.patch.object(
            porkyhud,
            "color_pair",
            return_value=0,
        ):
            porkyhud.draw_process_panel(screen, 1, 2, 10, 42, [], 0, "CPU")
        rendered = "\n".join(screen.row(row) for row in range(1, 11))
        self.assertIn("collecting processes...", rendered)
        self.assertIn("collecting | m CPU", rendered)

    def test_no_color_short_circuits_terminal_and_curses_color_calls(self) -> None:
        fake_stdout = mock.Mock()
        fake_stdout.isatty.return_value = True
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), mock.patch.object(
            porkyhud.curses,
            "color_pair",
            side_effect=AssertionError("curses.color_pair should not be called"),
        ), mock.patch.object(porkyhud.sys, "stdout", fake_stdout):
            self.assertEqual(porkyhud.color_pair(9), 0)
            self.assertFalse(porkyhud.enforce_dark_terminal())
        fake_stdout.write.assert_not_called()

        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), mock.patch.object(
            porkyhud.curses,
            "has_colors",
            return_value=True,
        ) as has_colors, mock.patch.object(
            porkyhud.curses,
            "start_color",
        ) as start_color, mock.patch.object(
            porkyhud.curses,
            "use_default_colors",
        ) as use_default_colors, mock.patch.object(
            porkyhud.curses,
            "init_pair",
        ) as init_pair:
            porkyhud.init_colors()
        has_colors.assert_called_once_with()
        start_color.assert_called_once_with()
        use_default_colors.assert_called_once_with()
        init_pair.assert_not_called()

    def test_color_helpers_tolerate_uninitialized_curses(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            porkyhud.curses,
            "color_pair",
            side_effect=ValueError("no color pairs"),
        ):
            self.assertEqual(porkyhud.color_pair(3), 0)

        screen = FakeScreen(5, 20)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), mock.patch.object(
            porkyhud.curses,
            "init_pair",
            side_effect=ValueError("no color pairs"),
        ):
            porkyhud.apply_dark_screen(screen)
        self.assertIsNone(screen.background)

    def test_live_dashboard_reports_unsupported_terminal_surfaces(self) -> None:
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch.object(porkyhud.sys, "stdin", tty), mock.patch.object(
            porkyhud.sys,
            "stdout",
            tty,
        ), mock.patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertIn("cursor-addressing", porkyhud.terminal_ui_problem())

        pipe = mock.Mock()
        pipe.isatty.return_value = False
        with mock.patch.object(porkyhud.sys, "stdin", pipe), mock.patch.object(
            porkyhud.sys,
            "stdout",
            pipe,
        ):
            self.assertIn("TTY", porkyhud.terminal_ui_problem())


class ProcessEvidenceTests(unittest.TestCase):
    """Process and AGX evidence contracts keep absence distinct from unknown."""

    PS_FIXTURE = (
        "  42 80.0 2.0 R 00:10 /Applications/Xcode.app/Contents/MacOS/Xcode --token secret\n"
        "   7  4.0 1.5 S 01:02 /usr/bin/worker --mode test\n"
    )
    AGX_FIRST = """
+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x1, !registered, !matched, active, busy 0, retain 5>
{
  \"AppUsage\" = ({\"accumulatedGPUTime\"=40000000},{\"unknown\"=7,\"accumulatedGPUTime\"=60000000})
  \"IOUserClientCreator\" = \"pid 42, Xcode\"
}
"""
    AGX_SECOND = """
+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x2, !registered, !matched, active, busy 0, retain 5>
{
  \"IOUserClientCreator\" = \"pid 42, Xcode\"
  \"AppUsage\" = ({\"accumulatedGPUTime\"=125000000},{\"accumulatedGPUTime\"=75000000})
}
"""
    AGX_EMPTY_USAGE = """
+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x3, !registered, !matched, active, busy 0, retain 5>
{
  \"AppUsage\" = ()
  \"IOUserClientCreator\" = \"pid 7, Cisco Secure Client\"
}
"""

    def command_result(
        self,
        stdout: str,
        outcome: core.AttemptOutcome = core.AttemptOutcome.SUCCESS,
        failure: core.FailureCode | None = None,
    ) -> porkyhud.CommandResult:
        return porkyhud.CommandResult(
            "fixture", stdout, "", 0 if outcome is core.AttemptOutcome.SUCCESS else None,
            10.0, 10.2, datetime(2026, 8, 12, 12, tzinfo=timezone.utc), outcome, failure,
        )

    def successful_gpu_state(
        self, value: object, completed: float = 10.0
    ) -> core.SourceState[object]:
        state = core.new_source(core.SourceId.PROCESS_GPU, 5.0, 15.0)
        state, revision = core.start_attempt(state, completed - 0.1)
        return core.complete_success(
            state, revision, value, completed, datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        )

    def test_process_table_succeeds_without_agx_and_uses_its_own_structured_command(self) -> None:
        with mock.patch.object(
            porkyhud, "run_structured_command", return_value=self.command_result(self.PS_FIXTURE)
        ) as command, mock.patch.object(porkyhud, "run_command", side_effect=AssertionError("legacy command")):
            result = porkyhud.collect_process_rows()

        self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
        self.assertEqual([row.pid for row in result.value or ()], [42, 7])
        self.assertTrue(all(row.gpu is None for row in result.value or ()))
        command.assert_called_once_with(
            ["/bin/ps", "-axo", "pid=,pcpu=,pmem=,stat=,etime=,command="],
            category="process-table", timeout_s=2.0,
            stdout_limit=8 * 1024 * 1024, stderr_limit=64 * 1024,
        )

    def test_process_parser_rejects_malformed_duplicate_and_invalid_rows_as_one_table(self) -> None:
        invalid = (
            "42 1 2 R 00:01 /bin/one\nnot enough\n",
            "42 1 2 R 00:01 /bin/one\n42 1 2 S 00:02 /bin/two\n",
            "0 1 2 R 00:01 /bin/one\n",
            "4 NaN 2 R 00:01 /bin/one\n",
            "4 1 -2 R 00:01 /bin/one\n",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                porkyhud.parse_process_rows(raw)

    def test_process_failures_and_empty_output_preserve_truthful_outcomes(self) -> None:
        failed = self.command_result(
            "", core.AttemptOutcome.TIMEOUT, core.FailureCode.COMMAND_TIMEOUT
        )
        with mock.patch.object(porkyhud, "run_structured_command", return_value=failed):
            result = porkyhud.collect_process_rows()
        self.assertEqual((result.outcome, result.failure), (failed.outcome, failed.failure))
        self.assertEqual((result.started_mono_s, result.completed_mono_s), (10.0, 10.2))
        with mock.patch.object(porkyhud, "run_structured_command", return_value=self.command_result("\n")):
            empty = porkyhud.collect_process_rows()
        self.assertEqual((empty.outcome, empty.failure), (core.AttemptOutcome.PARSE_ERROR, core.FailureCode.EMPTY_OUTPUT))

    def test_elapsed_identity_and_display_cells_are_conservative(self) -> None:
        self.assertEqual(porkyhud.parse_elapsed_seconds("01:02"), 62)
        self.assertEqual(porkyhud.parse_elapsed_seconds("01:02:03"), 3723)
        self.assertEqual(porkyhud.parse_elapsed_seconds("2-01:02:03"), 176523)
        self.assertIsNone(porkyhud.parse_elapsed_seconds("tomorrow"))
        self.assertEqual(
            porkyhud.process_identity('"/Applications/My App.app/Contents/MacOS/My App" --secret yes'),
            "My App",
        )
        self.assertEqual(
            porkyhud.process_identity("/Applications/My App.app/Contents/MacOS/My App --secret yes"),
            "My App",
        )
        self.assertEqual(
            porkyhud.process_identity(
                '"/Users/me/Tools/My App.app/Contents/MacOS/My App" --secret yes'
            ),
            "My App",
        )
        self.assertEqual(porkyhud.process_identity("/usr/bin/wo\u200brker\n--flag"), "worker")
        self.assertEqual(
            porkyhud.process_identity("/usr/bin/python --input /Applications/Decoy.app/file"),
            "python",
        )
        self.assertEqual(porkyhud.display_cell_width("e\u0301界"), 3)
        self.assertEqual(porkyhud.truncate_display_cells("a" * 32 + "界", 32), "a" * 32)
        self.assertEqual(porkyhud.truncate_display_cells("界" * 17, 32), "界" * 16)

    def test_process_facts_remove_raw_arguments_and_cap_only_the_display_name(self) -> None:
        command = '"/Applications/Very Long\u200b Application.app/Contents/MacOS/Main" --api-key leaked'
        facts = porkyhud.process_facts((porkyhud.ProcessRow(3, 4.0, 1.0, None, "R", "bad", command),))
        self.assertEqual(facts[0].name, "Very Long Application")
        self.assertIsNone(facts[0].elapsed_seconds)
        self.assertNotIn("leaked", facts[0].name)
        self.assertLessEqual(porkyhud.display_cell_width(facts[0].name), 32)

    def test_agx_parser_is_complete_immutable_and_all_or_nothing(self) -> None:
        parsed = porkyhud.parse_agx_counters(self.AGX_FIRST)
        self.assertTrue(parsed.complete)
        self.assertEqual(dict(parsed.counters_ns), {42: 100000000})
        with self.assertRaises(TypeError):
            parsed.counters_ns[42] = 1  # type: ignore[index]
        valid_empty = porkyhud.parse_agx_counters(self.AGX_EMPTY_USAGE)
        self.assertTrue(valid_empty.complete)
        self.assertEqual(dict(valid_empty.counters_ns), {7: 0})
        self.assertEqual(dict(valid_empty.identities), {7: "Cisco Secure Client"})
        nested_unknown = self.AGX_EMPTY_USAGE.replace(
            '  "AppUsage" = ()',
            '  "Unknown" = {\n    "nested" = {}\n  }\n  "AppUsage" = ()',
        )
        self.assertEqual(dict(porkyhud.parse_agx_counters(nested_unknown).counters_ns), {7: 0})
        for raw in (
            "\n",
            "nonsense {\n}\n",
            '"IOUserClientCreator" = "pid 42, Xcode"\n' + self.AGX_FIRST,
            '"AppUsage" = ()\n' + self.AGX_FIRST,
            '+-o AGXDeviceUserClient <class AGXDeviceUserClient>\n{\n "IOUserClientCreator" = "pid 42, Xcode"\n',
            '"accumulatedGPUTime" = 9\n',
            '+-o AGXDeviceUserClient <class AGXDeviceUserClient>\n{\n "IOUserClientCreator" = "pid 0, Xcode"\n "AppUsage" = ()\n}\n',
            '+-o AGXDeviceUserClient <class AGXDeviceUserClient>\n{\n "IOUserClientCreator" = "pid 42, Xcode"\n "AppUsage" = ({"accumulatedGPUTime"=-9})\n}\n',
            '+-o AGXDeviceUserClient <class AGXDeviceUserClient>\n{\n "IOUserClientCreator" = "pid 42, Xcode"\n "AppUsage" = ()\n "accumulatedGPUTime" = 9\n}\n',
            '+-o AGXDeviceUserClient <class AGXDeviceUserClient>\n{\n "IOUserClientCreator" = "pid 42, Xcode"\n "AppUsage" = ({"accumulatedGPUTime"=9)\n}\n',
            '+-o AGXDeviceUserClient <class AGXDeviceUserClient>\n{\n "IOUserClientCreator" = "pid 42, Xcode"\n}\n',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                porkyhud.parse_agx_counters(raw)

    def test_agx_parser_sums_matching_clients_and_rejects_identity_conflicts(self) -> None:
        repeated = self.AGX_FIRST + self.AGX_SECOND.replace("125000000", "15").replace("75000000", "10")
        parsed = porkyhud.parse_agx_counters(repeated)
        self.assertEqual(dict(parsed.counters_ns), {42: 100000025})
        conflicting = repeated.replace("Xcode", "Other", 1)
        with self.assertRaises(ValueError):
            porkyhud.parse_agx_counters(conflicting)

    def test_agx_parser_consumes_all_input_and_requires_usage_dictionaries(self) -> None:
        self.assertEqual(
            dict(porkyhud.parse_agx_counters(self.AGX_FIRST).counters_ns),
            {42: 100000000},
        )
        malformed_usage = self.AGX_FIRST.replace(
            '({"accumulatedGPUTime"=40000000},{"unknown"=7,"accumulatedGPUTime"=60000000})',
            '("accumulatedGPUTime"=100000000)',
        )
        invalid = (
            self.AGX_FIRST + "+-o AGXDeviceUserClient\n",
            self.AGX_FIRST + "}\n",
            self.AGX_FIRST + '"truncated" =\n',
            self.AGX_FIRST + "arbitrary suffix\n",
            malformed_usage,
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                porkyhud.parse_agx_counters(raw)

    def test_agx_parser_requires_an_exact_outer_close(self) -> None:
        with self.assertRaises(ValueError):
            porkyhud.parse_agx_counters(self.AGX_EMPTY_USAGE.replace("\n}\n", "\n} trailing\n"))

    def test_agx_parser_rejects_arbitrary_lines_inside_a_client(self) -> None:
        with self.assertRaises(ValueError):
            porkyhud.parse_agx_counters(
                self.AGX_EMPTY_USAGE.replace('  "AppUsage" = ()', "  arbitrary garbage\n  \"AppUsage\" = ()")
            )

    def test_agx_creator_identity_preserves_spaced_labels_for_process_merge(self) -> None:
        parsed = porkyhud.parse_agx_counters(self.AGX_EMPTY_USAGE)
        self.assertEqual(parsed.identities[7], "Cisco Secure Client")
        row = porkyhud.ProcessRow(
            7, 1.0, 1.0, None, "S", "00:01",
            '"/Applications/Cisco Secure Client.app/Contents/MacOS/Cisco Secure Client"',
        )
        sample = porkyhud.GpuProcessSample({7: 0.0}, parsed.identities, True, True)
        self.assertEqual(
            porkyhud.merge_gpu_process_rows((row,), self.successful_gpu_state(sample), 10.5)[0].gpu,
            0.0,
        )

    def test_gpu_merge_fails_closed_for_forged_source_states(self) -> None:
        row = porkyhud.ProcessRow(9, 1.0, 1.0, None, "S", "00:01", "/bin/task")
        sample = porkyhud.GpuProcessSample({}, {}, True, True)
        valid = self.successful_gpu_state(sample)
        forged = (
            replace(valid, source_id=core.SourceId.PROCESS_TABLE),
            replace(valid, expected_cadence_s=0.0),
            replace(valid, stale_after_s=1.0),
            replace(valid, revision=2),
            replace(valid, active_revision=1, active_started_mono_s=None),
            replace(
                valid,
                last_attempt=replace(
                    valid.last_attempt,
                    outcome=core.AttemptOutcome.TIMEOUT,
                    failure=core.FailureCode.COMMAND_TIMEOUT,
                ),
            ),
            replace(valid, last_valid=replace(valid.last_valid, revision=2)),
            replace(valid, last_valid=replace(valid.last_valid, value="not a GPU sample")),
            replace(valid, last_valid=replace(valid.last_valid, started_mono_s=8.0)),
            replace(valid, last_valid=replace(valid.last_valid, completed_at_utc=datetime(2026, 8, 12, 13, tzinfo=timezone.utc))),
        )
        for state in forged:
            with self.subTest(state=state):
                self.assertIsNone(porkyhud.merge_gpu_process_rows((row,), state, 10.5)[0].gpu)

    def test_gpu_sample_defensively_copies_mutable_input_maps(self) -> None:
        percentages = {42: 1.0}
        identities = {42: "Xcode"}
        sample = porkyhud.GpuProcessSample(percentages, identities, True, True)
        percentages[42] = 99.0
        identities[42] = "Other"
        self.assertEqual((sample.percentages[42], sample.identities[42]), (1.0, "Xcode"))

    def test_gpu_collection_uses_its_own_structured_command_and_rejects_parser_failures(self) -> None:
        sampler = porkyhud.GPUProcessSampler()
        with mock.patch.object(
            porkyhud, "run_structured_command", return_value=self.command_result(self.AGX_FIRST)
        ) as command:
            result = porkyhud.collect_gpu_process_sample(sampler, None)
        self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
        command.assert_called_once_with(
            ["/usr/sbin/ioreg", "-r", "-c", "AGXDeviceUserClient", "-l"],
            category="process-gpu", timeout_s=2.0,
            stdout_limit=8 * 1024 * 1024, stderr_limit=64 * 1024,
        )
        with mock.patch.object(porkyhud, "run_structured_command", return_value=self.command_result("unterminated")):
            rejected = porkyhud.collect_gpu_process_sample(sampler, None)
        self.assertEqual((rejected.outcome, rejected.failure),
                         (core.AttemptOutcome.PARSE_ERROR, core.FailureCode.PARSER_REJECTED))

    def test_gpu_sampler_distinguishes_baseline_zero_reversal_and_calibration(self) -> None:
        sampler = porkyhud.GPUProcessSampler()
        first = sampler.update(porkyhud.parse_agx_counters(self.AGX_FIRST), 10.0)
        self.assertFalse(first.baseline_ready)
        second = sampler.update(porkyhud.parse_agx_counters(self.AGX_FIRST), 11.0)
        self.assertTrue(second.baseline_ready)
        self.assertEqual(second.percentages[42], 0.0)
        third = sampler.update(porkyhud.parse_agx_counters(self.AGX_SECOND), 12.0, 50.0)
        self.assertEqual(third.percentages[42], 50.0)
        reversed_sample = sampler.update(porkyhud.parse_agx_counters(self.AGX_FIRST), 13.0)
        self.assertFalse(reversed_sample.baseline_ready)

    def test_gpu_collection_resets_after_failure_and_recovery_cannot_bridge(self) -> None:
        sampler = porkyhud.GPUProcessSampler()
        with mock.patch.object(porkyhud, "run_structured_command", side_effect=[
            self.command_result(self.AGX_FIRST),
            self.command_result("", core.AttemptOutcome.TIMEOUT, core.FailureCode.COMMAND_TIMEOUT),
            self.command_result(self.AGX_SECOND),
        ]):
            first = porkyhud.collect_gpu_process_sample(sampler, None)
            failed = porkyhud.collect_gpu_process_sample(sampler, None)
            recovered = porkyhud.collect_gpu_process_sample(sampler, None)
        self.assertFalse((first.value or object()).baseline_ready)
        self.assertEqual(failed.outcome, core.AttemptOutcome.TIMEOUT)
        self.assertFalse((recovered.value or object()).baseline_ready)

    def test_gpu_merge_requires_fresh_complete_identity_match(self) -> None:
        rows = (porkyhud.ProcessRow(42, 80.0, 2.0, None, "R", "00:10", "/Applications/Xcode.app/Xcode"),)
        gpu = porkyhud.GpuProcessSample({42: 37.5}, {42: "Xcode"}, complete=True, baseline_ready=True)
        state = self.successful_gpu_state(gpu, completed=10.0)
        merged = porkyhud.merge_gpu_process_rows(rows, state, 10.5)
        self.assertEqual(merged[0].gpu, 37.5)
        self.assertEqual(porkyhud.merge_gpu_process_rows(rows, state, 25.0)[0].gpu, 37.5)
        self.assertIsNone(porkyhud.merge_gpu_process_rows(rows, state, 25.01)[0].gpu)
        empty = porkyhud.GpuProcessSample({}, {}, complete=True, baseline_ready=True)
        self.assertEqual(porkyhud.merge_gpu_process_rows(rows, self.successful_gpu_state(empty), 10.5)[0].gpu, 0.0)
        partial = porkyhud.GpuProcessSample({}, {}, complete=False, baseline_ready=True)
        self.assertIsNone(porkyhud.merge_gpu_process_rows(rows, self.successful_gpu_state(partial), 10.5)[0].gpu)
        mismatch = porkyhud.GpuProcessSample({42: 3.0}, {42: "Other"}, complete=True, baseline_ready=True)
        self.assertIsNone(porkyhud.merge_gpu_process_rows(rows, self.successful_gpu_state(mismatch), 10.5)[0].gpu)

    def test_gpu_merge_rejects_latest_failure_and_gpu_sorting_keeps_unknown_last(self) -> None:
        sample = porkyhud.GpuProcessSample({42: 0.0}, {42: "Xcode"}, complete=True, baseline_ready=True)
        state = self.successful_gpu_state(sample)
        active, revision = core.start_attempt(state, 11.0)
        failed = core.complete_failure(active, revision, core.AttemptOutcome.TIMEOUT,
                                       core.FailureCode.COMMAND_TIMEOUT, 11.1,
                                       datetime(2026, 8, 12, 12, tzinfo=timezone.utc))
        rows = [
            porkyhud.ProcessRow(1, 1.0, 1.0, None, "S", "00:01", "/bin/unknown"),
            porkyhud.ProcessRow(2, 1.0, 1.0, 0.0, "S", "00:01", "/bin/zero"),
            porkyhud.ProcessRow(3, 1.0, 1.0, 5.0, "S", "00:01", "/bin/high"),
        ]
        self.assertTrue(all(row.gpu is None for row in porkyhud.merge_gpu_process_rows(rows, failed, 11.2)))
        self.assertEqual([row.pid for row in porkyhud.sort_process_rows(rows, "GPU")], [3, 2, 1])

    def test_renderer_suppresses_unavailable_gpu_but_preserves_measured_zero(self) -> None:
        unknown = FakeScreen(10, 70)
        unknown_rows = [
            porkyhud.ProcessRow(1, 2.0, 3.0, None, "S", "00:01", "/bin/task"),
        ]
        with mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_process_panel(unknown, 1, 1, 8, 64, unknown_rows, 0, "GPU")
        unknown_text = "\n".join(unknown.row(index) for index in range(1, 9))
        self.assertNotIn("GPU", unknown.row(2))
        self.assertNotIn("--", unknown.row(4))

        mixed = FakeScreen(12, 80)
        mixed_rows = [
            porkyhud.ProcessRow(1, 2.0, 3.0, None, "S", "00:01", "/bin/unknown"),
            porkyhud.ProcessRow(2, 2.0, 3.0, 0.0, "S", "00:01", "/bin/zero"),
            porkyhud.ProcessRow(3, 2.0, 3.0, 12.5, "S", "00:01", "/bin/measured"),
        ]
        with mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_process_panel(mixed, 1, 1, 10, 74, mixed_rows, 0, "GPU")
        mixed_text = "\n".join(mixed.row(index) for index in range(1, 11))
        self.assertIn("GPU%", mixed_text)
        self.assertNotIn("GPU%%", mixed_text)
        self.assertIn("--", mixed_text)
        self.assertIn("0.0", mixed_text)
        self.assertIn("12.5", mixed_text)

        snapshot = {"system": {}, "cpu": {"average_percent": 0.0, "cores": []},
                    "memory": {"used_bytes": 0, "total_bytes": 0, "used_percent": 0.0},
                    "sensors": {}, "disk": {"activity": {"bytes_per_sec": 0, "iops": 0}},
                    "processes": [{"pid": 1, "cpu": 2.0, "mem": 3.0, "gpu": None, "command": "/bin/task"}]}
        with mock.patch("builtins.print") as output:
            porkyhud.print_text_snapshot(snapshot)
        self.assertIn("GPU   --", "\n".join(str(call.args[0]) for call in output.call_args_list))


class RuntimeProcessAndSnapshotTests(unittest.TestCase):
    def collect_snapshot_with(
        self,
        *,
        memory_pressure: int = 1,
        thermal: tuple[int, int] = (0, 0),
        free_fraction: float = 0.5,
        cpu_percent: float = 5.0,
        cpu_failure: bool = False,
        cpu_constructor_error: bool = False,
        typed_failure: str | None = None,
        descriptive_failure: str | None = None,
        typed_interrupt: bool = False,
        interrupt_marker: BaseException | None = None,
        process_rows_data: tuple[object, ...] = (),
        agx_results: list[object] | None = None,
        now_mono_s: float = 2.0,
        descriptive_values=None,
        ram_used_percent: float = 50.0,
        compressed_bytes: float = 0.0,
        swap_used_bytes: float = 0.0,
    ) -> dict[str, object]:
        """Collect a deterministic one-shot snapshot through its public boundary."""

        descriptive_values = descriptive_values or {}
        total = 100 * 1024 ** 3
        free = int(total * free_fraction)
        memory = {
            "total": float(total),
            "used": float(total * ram_used_percent / 100.0),
            "compressed": compressed_bytes,
            "swap_total": float(total),
            "swap_used": swap_used_bytes,
        }
        static = {
            "host": "snapshot-test-host",
            "chip": "Test Silicon",
            "model": "Test Mac",
            "core_labels": ["P0"],
            "cpu_index_map": [0],
        }
        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)

        def success(value: object, started: float = 0.25, completed: float = 0.5):
            return porkyhud.AdapterResult(
                value, started, completed, completed_at,
                core.AttemptOutcome.SUCCESS, None,
            )

        class FakeCpuSampler:
            def __init__(self) -> None:
                if cpu_constructor_error:
                    raise RuntimeError("CPU adapter unavailable")
                self.last_per_core = ()
                self.calls = 0

            def sample_usage(self):
                self.calls += 1
                if cpu_failure:
                    return porkyhud.AdapterResult(
                        None, 0.25, 0.5, completed_at,
                        core.AttemptOutcome.UNKNOWN_ERROR,
                        core.FailureCode.UNEXPECTED_EXCEPTION,
                    )
                self.last_per_core = (cpu_percent,)
                return success(core.CpuUsage(cpu_percent, 0.1, 0.5))

            def sample(self) -> list[float]:
                return [cpu_percent]

        sensor = settled_sensor()
        disk = porkyhud.DiskInfo("data", "/System/Volumes/Data", total,
                                 total - free, free)
        with mock.patch.object(porkyhud, "MacCpuSampler", FakeCpuSampler), \
                mock.patch.object(porkyhud.time, "sleep"), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=now_mono_s), \
                mock.patch.object(porkyhud, "collect_static_info",
                                  return_value=descriptive_values.get("system", static),
                                  side_effect=RuntimeError("private") if descriptive_failure == "collect_static_info" else None), \
                mock.patch.object(porkyhud, "_snapshot_memory_stats",
                                  return_value=descriptive_values.get("memory", memory),
                                  side_effect=RuntimeError("private") if descriptive_failure == "memory_stats" else None), \
                mock.patch.object(porkyhud, "_snapshot_battery_info",
                                  return_value=descriptive_values.get("battery", make_battery()),
                                  side_effect=RuntimeError("private") if descriptive_failure == "battery_info" else None), \
                mock.patch.object(porkyhud, "sensor_info",
                                  return_value=descriptive_values.get("sensors", sensor),
                                  side_effect=RuntimeError("private") if descriptive_failure == "sensor_info" else None), \
                mock.patch.object(porkyhud, "disk_info", return_value=disk), \
                mock.patch.object(porkyhud, "disk_activity",
                                  return_value=descriptive_values.get("activity", porkyhud.DiskActivity()),
                                  side_effect=RuntimeError("private") if descriptive_failure == "disk_activity" else None), \
                mock.patch.object(porkyhud, "collect_memory_pressure",
                                  return_value=success(core.MemoryPressure(core.MemoryPressureLevel(memory_pressure))),
                                  side_effect=(interrupt_marker if interrupt_marker is not None else (KeyboardInterrupt() if typed_interrupt else RuntimeError("private"))) if typed_failure == "collect_memory_pressure" else None), \
                mock.patch.object(porkyhud, "collect_os_thermal_state",
                                  return_value=success(core.OsThermalState(*thermal)),
                                  side_effect=RuntimeError("private") if typed_failure == "collect_os_thermal_state" else None), \
                mock.patch.object(porkyhud, "collect_disk_capacity", return_value=success(
                    core.DiskCapacity("test-data", "data", "/System/Volumes/Data", total, free)),
                    side_effect=RuntimeError("private") if typed_failure == "collect_disk_capacity" else None), \
                mock.patch.object(porkyhud, "collect_process_rows", return_value=success(process_rows_data),
                                  side_effect=RuntimeError("private") if typed_failure == "collect_process_rows" else None), \
                mock.patch.object(porkyhud, "collect_gpu_process_sample", side_effect=[
                    success(porkyhud.GpuProcessSample({}, {}, True, False), 0.0, 0.1),
                    success(porkyhud.GpuProcessSample({}, {}, True, True), 0.6, 0.7),
                ] if agx_results is None else agx_results), \
                mock.patch.object(porkyhud, "process_rows", return_value=[]):
            return porkyhud.collect_snapshot()

    def test_one_shot_without_immediate_warning_is_unknown(self) -> None:
        """A one-point CPU sample must not become an all-clear health verdict."""

        snapshot = self.collect_snapshot_with(
            memory_pressure=1, thermal=(0, 0), free_fraction=0.5,
            cpu_percent=5.0,
        )

        self.assertEqual(snapshot["read"], {
            "message": "read: CPU trend requires live monitoring.",
            "level": "unknown",
        })

    def test_normal_pressure_never_advises_closing_apps_from_ram_detail(self) -> None:
        """Detail counters must not override the OS memory-pressure source."""

        snapshot = self.collect_snapshot_with(
            memory_pressure=1, ram_used_percent=99.0,
            compressed_bytes=64 * 1024 ** 3,
            swap_used_bytes=32 * 1024 ** 3,
        )

        self.assertNotIn("close", snapshot["read"]["message"].lower())
        self.assertNotIn("quit", snapshot["read"]["message"].lower())

    def test_immediate_memory_thermal_and_storage_warnings_survive_one_shot_cpu_limits(self) -> None:
        """Fresh decisive sources must surface their own warning without CPU history."""

        for inputs, level, phrase in (
            ({"memory_pressure": 4}, "hot", "critical memory pressure"),
            ({"thermal": (1, 0)}, "watch", "active thermal or performance warning"),
            ({"free_fraction": 0.05}, "watch", "Storage headroom is low"),
        ):
            with self.subTest(inputs=inputs):
                snapshot = self.collect_snapshot_with(**inputs)
                self.assertEqual(snapshot["read"]["level"], level)
                self.assertIn(phrase, snapshot["read"]["message"])

    def test_failed_cpu_projects_null_and_text_never_formats_it_as_zero(self) -> None:
        """A failed CPU adapter is unavailable, never a fabricated idle sample."""

        snapshot = self.collect_snapshot_with(cpu_failure=True)

        self.assertIsNone(snapshot["cpu"]["average_percent"])
        self.assertEqual(snapshot["cpu"]["cores"], [])
        with mock.patch("builtins.print") as output:
            porkyhud.print_text_snapshot(snapshot)
        self.assertIn("CPU: unavailable", "\n".join(str(call.args[0]) for call in output.call_args_list))

    def test_delayed_post_agx_assessment_does_not_serialize_stale_cpu_raw_values(self) -> None:
        """The JSON CPU projection must obey the same freshness decision as health."""

        snapshot = self.collect_snapshot_with(cpu_percent=5.0, now_mono_s=4.0)

        self.assertEqual(snapshot["read"]["level"], "unknown")
        self.assertIsNone(snapshot["cpu"]["average_percent"])
        self.assertEqual(snapshot["cpu"]["cores"], [])

    def test_cpu_adapter_construction_failure_is_an_unavailable_snapshot_projection(self) -> None:
        """Ordinary adapter setup faults stay contained at the one-shot boundary."""

        snapshot = self.collect_snapshot_with(cpu_constructor_error=True)

        self.assertIsNone(snapshot["cpu"]["average_percent"])
        self.assertEqual(snapshot["cpu"]["cores"], [])

    def test_source_state_conversion_preserves_adapter_revision_and_timestamps(self) -> None:
        """Snapshot conversion must carry adapter evidence into the core unchanged."""

        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        result = porkyhud.AdapterResult(
            core.MemoryPressure(core.MemoryPressureLevel.WARN), 3.0, 4.0,
            completed_at, core.AttemptOutcome.SUCCESS, None,
        )
        state = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)

        converted = porkyhud.source_state_from_result(state, result)

        self.assertEqual(converted.revision, 1)
        self.assertEqual(converted.last_attempt.started_mono_s, 3.0)
        self.assertEqual(converted.last_attempt.completed_mono_s, 4.0)
        self.assertEqual(converted.last_valid.completed_at_utc, completed_at)
        active, _revision = core.start_attempt(state, 1.0)
        with self.assertRaises(ValueError):
            porkyhud.source_state_from_result(active, result)

    def test_source_conversion_failure_retains_prior_valid_and_rejects_future_chronology(self) -> None:
        """Failed refreshes retain display history but never repair an impossible clock."""

        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        state = core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        state = porkyhud.source_state_from_result(state, porkyhud.AdapterResult(
            core.MemoryPressure(core.MemoryPressureLevel.NORMAL), 1.0, 2.0,
            completed_at, core.AttemptOutcome.SUCCESS, None,
        ))
        failed = porkyhud.source_state_from_result(state, porkyhud.AdapterResult(
            None, 3.0, 4.0, completed_at, core.AttemptOutcome.TIMEOUT,
            core.FailureCode.COMMAND_TIMEOUT,
        ))
        self.assertEqual(failed.last_valid.revision, 1)
        self.assertEqual(failed.last_attempt.revision, 2)
        with self.assertRaises(ValueError):
            porkyhud.source_state_from_result(state, porkyhud.AdapterResult(
                None, 1.5, 3.0, completed_at, core.AttemptOutcome.TIMEOUT,
                core.FailureCode.COMMAND_TIMEOUT,
            ))

    def test_snapshot_cpu_takes_one_measurement_after_the_short_delay(self) -> None:
        """The sampler constructor owns baseline priming; snapshot takes one delta."""

        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)

        class FakeCpuSampler:
            def __init__(self) -> None:
                self.calls = 0
                self.last_per_core = (25.0,)

            def sample_usage(self):
                self.calls += 1
                return porkyhud.AdapterResult(
                    core.CpuUsage(25.0, 0.0, 0.25), 0.0, 0.25, completed_at,
                    core.AttemptOutcome.SUCCESS, None,
                )

        sampler = FakeCpuSampler()
        with mock.patch.object(porkyhud.time, "sleep"):
            result, per_core = porkyhud._snapshot_cpu_result(sampler)
        self.assertEqual(sampler.calls, 1)
        self.assertEqual(result.value.percent, 25.0)
        self.assertEqual(per_core, (25.0,))

    def test_snapshot_cpu_rejects_a_window_over_its_2_5_second_budget(self) -> None:
        """Long counter intervals cannot manufacture sustained CPU evidence."""

        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        for duration, outcome in ((2.5, core.AttemptOutcome.SUCCESS),
                                  (2.5001, core.AttemptOutcome.PARSE_ERROR)):
            with self.subTest(duration=duration):
                class FakeCpuSampler:
                    last_per_core = (91.0,)

                    def sample_usage(self):
                        return porkyhud.AdapterResult(
                            core.CpuUsage(91.0, 0.0, duration), 0.0, duration,
                            completed_at, core.AttemptOutcome.SUCCESS, None,
                        )

                with mock.patch.object(porkyhud.time, "sleep"):
                    result, cores = porkyhud._snapshot_cpu_result(FakeCpuSampler())
                self.assertEqual(result.outcome, outcome)
                self.assertEqual(cores, (91.0,) if outcome is core.AttemptOutcome.SUCCESS else ())

    def test_snapshot_uses_the_frozen_frame_builder_and_six_worker_budget(self) -> None:
        """One-shot projection shares the frame contract without live coordination."""

        real_executor = porkyhud.ThreadPoolExecutor
        with mock.patch.object(porkyhud, "build_health_frame", wraps=porkyhud.build_health_frame) as builder, \
                mock.patch.object(porkyhud, "ThreadPoolExecutor", wraps=real_executor) as executor:
            self.collect_snapshot_with()
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(executor.call_args.kwargs["max_workers"], 6)

    def test_snapshot_constructs_delta_samplers_before_the_executor(self) -> None:
        """CPU and GPU baselines are established before any snapshot work can start."""

        events = []
        marker = RuntimeError("pool unavailable")

        class CpuSampler:
            def __init__(self):
                events.append("cpu")

        class GpuSampler:
            def __init__(self):
                events.append("gpu")

        class Executor:
            def __init__(self, **_kwargs):
                events.append("pool")
                raise marker

        with mock.patch.object(porkyhud, "MacCpuSampler", CpuSampler), \
                mock.patch.object(porkyhud, "GPUProcessSampler", GpuSampler), \
                mock.patch.object(porkyhud, "ThreadPoolExecutor", Executor), \
                self.assertRaises(RuntimeError) as caught:
            porkyhud.collect_snapshot()
        self.assertIs(caught.exception, marker)
        self.assertEqual(events, ["cpu", "gpu", "pool"])

    def test_snapshot_constructor_baseexception_propagates_before_pool_creation(self) -> None:
        """Sampler interruption keeps exact identity and cannot create executor ownership."""

        marker = KeyboardInterrupt("constructor")
        with mock.patch.object(porkyhud, "MacCpuSampler", side_effect=marker), \
                mock.patch.object(porkyhud, "ThreadPoolExecutor") as executor, \
                self.assertRaises(KeyboardInterrupt) as caught:
            porkyhud.collect_snapshot()
        self.assertIs(caught.exception, marker)
        executor.assert_not_called()

    def test_snapshot_queues_initial_typed_attempts_before_descriptive_work(self) -> None:
        """Slow descriptive futures cannot delay the five health evidence attempts."""

        submitted = []

        class ImmediateFuture:
            def __init__(self, value):
                self.value = value

            def result(self):
                return self.value

            def cancel(self):
                return False

        class ImmediateExecutor:
            def __init__(self, **_kwargs):
                pass

            def submit(self, function, *args):
                submitted.append((function, args))
                return ImmediateFuture(function(*args))

            def shutdown(self, **_kwargs):
                pass

        with mock.patch.object(porkyhud, "ThreadPoolExecutor", ImmediateExecutor):
            self.collect_snapshot_with()
        self.assertEqual(
            [function for function, _args in submitted[:5]],
            [porkyhud._snapshot_adapter_attempt] * 5,
        )
        self.assertEqual(
            [getattr(function, "_mock_name", None) or function.__name__
             for function, _args in submitted[5:9]],
            ["collect_static_info", "_snapshot_memory_stats", "_snapshot_battery_info", "disk_activity"],
        )

    def test_text_snapshot_uses_exact_unknown_fallbacks(self) -> None:
        """Unknown snapshot fields must not masquerade as a privileged reading."""

        snapshot = {
            "system": {}, "cpu": {"average_percent": None, "cores": []},
            "memory": {"used_bytes": None, "total_bytes": None, "used_percent": None},
            "sensors": {"thermal_warning": "unavailable", "package_power_w": None},
            "disk": {"activity": {"bytes_per_sec": None, "iops": None}},
            "processes": [],
        }
        with mock.patch("builtins.print") as output:
            porkyhud.print_text_snapshot(snapshot)
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("read: health assessment unavailable.", text)
        self.assertIn("Package: --", text)

    def test_text_snapshot_treats_null_read_or_message_as_unavailable(self) -> None:
        """Nullable machine input cannot crash or print a literal None assessment."""

        base = {
            "system": {}, "cpu": {"average_percent": None, "cores": []},
            "memory": {"used_bytes": None, "total_bytes": None, "used_percent": None},
            "sensors": {}, "disk": {"activity": {"bytes_per_sec": None, "iops": None}},
            "processes": [],
        }
        for read in (None, {}, {"message": None}):
            with self.subTest(read=read), mock.patch("builtins.print") as output:
                porkyhud.print_text_snapshot({**base, "read": read})
            self.assertEqual(
                output.call_args_list[1].args[0],
                "read: health assessment unavailable.",
            )

    def test_typed_collector_failures_and_descriptive_failures_stay_unavailable(self) -> None:
        """Snapshot boundaries never leak collector errors or substitute legacy disk data."""

        failures = [
            "collect_memory_pressure", "collect_os_thermal_state", "collect_disk_capacity",
            "collect_process_rows",
        ]
        for name in failures:
            with self.subTest(name=name), mock.patch.object(porkyhud, "disk_info") as legacy_disk:
                snapshot = self.collect_snapshot_with(typed_failure=name)
            legacy_disk.assert_not_called()
            self.assertNotIn("private", json.dumps(snapshot))
            if name == "collect_disk_capacity":
                self.assertEqual(snapshot["disk"]["capacity"], {
            "label": None, "mount": None, "total": None, "used": None, "free": None,
                })

        for name, key in (("memory_stats", "memory"), ("battery_info", "battery"),
                          ("collect_static_info", "system"), ("sensor_info", "sensors"),
                          ("disk_activity", "activity")):
            with self.subTest(name=name):
                snapshot = self.collect_snapshot_with(descriptive_failure=name)
            self.assertNotIn("private", json.dumps(snapshot))
            if key == "memory":
                self.assertIsNone(snapshot[key]["used_bytes"])
            elif key == "battery":
                self.assertIsNone(snapshot[key]["present"])
            elif key == "system":
                self.assertEqual(snapshot[key]["host"], socket.gethostname().split(".")[0])
            elif key == "sensors":
                self.assertEqual(snapshot[key]["thermal_warning"], "unavailable")
            else:
                self.assertEqual(snapshot["disk"]["activity"]["source"], "unavailable")

    def test_wrong_type_descriptive_results_use_exact_unavailable_wire_shapes(self) -> None:
        """Returned values are validated as strictly as raised collector failures."""

        system_keys = {
            "host", "os", "build", "kernel", "model", "chip", "physical_cpu",
            "logical_cpu", "memory", "cpu_clusters", "core_labels", "cpu_index_map",
            "core_counts", "gpus", "gpu_core_count", "max_gpu_freq_mhz", "model_id",
            "cpu_cores", "gpu_fp32_tflops",
        }
        memory_keys = {
            "used_percent", "used_bytes", "total_bytes", "swap_used_bytes",
            "swap_total_bytes",
        }
        battery_keys = {
            "percent", "present", "source", "state", "remaining", "external_connected",
            "is_charging", "fully_charged", "cycle_count", "design_cycles",
            "health_percent", "temp_c", "virtual_temp_c", "voltage_v", "amperage_a",
            "charger_watts", "charger_name",
        }
        sensor_keys = {
            "thermal_warning", "performance_warning", "cpu_power_w", "gpu_power_w",
            "cpu_temp_c", "gpu_temp_c", "fan_rpm", "privileged_locked", "raw_hint",
            "ane_power_w", "dram_power_w", "gpu_sram_power_w", "package_power_w",
            "gpu_active_percent", "gpu_freq_mhz", "e_cluster_active", "p_cluster_active",
            "s_cluster_active", "e_cluster_freq_mhz", "p_cluster_freq_mhz",
            "s_cluster_freq_mhz", "dram_read_gbs", "dram_write_gbs", "fan_count",
            "fans", "temp_sensors", "sensor_sample_age_s",
        }
        activity_keys = {"bytes_per_sec", "iops", "sample_age", "source"}
        expected_keys = {
            "system": system_keys, "memory": memory_keys, "battery": battery_keys,
            "sensors": sensor_keys, "activity": activity_keys,
        }
        for name, bad_value in (
            ("system", None), ("memory", "bad"), ("battery", object()),
            ("sensors", None), ("activity", {"bytes_per_sec": 0.0}),
        ):
            with self.subTest(name=name):
                snapshot = self.collect_snapshot_with(
                    descriptive_values={name: bad_value},
                )
                section = snapshot["disk"]["activity"] if name == "activity" else snapshot[name]
                self.assertEqual(set(section), expected_keys[name])
                with mock.patch("builtins.print"):
                    porkyhud.print_text_snapshot(snapshot)

    def test_corrupt_static_and_memory_mappings_fail_closed_per_wire_field(self) -> None:
        """Malformed descriptive mappings cannot reach layout or numeric formatting."""

        snapshot = self.collect_snapshot_with(descriptive_values={
            "system": {"host": "kept-host", "core_labels": object(), "cpu_index_map": "bad"},
            "memory": {"total": 100.0, "used": "bad", "swap_used": 1.0},
        })

        self.assertEqual(snapshot["system"]["host"], "kept-host")
        self.assertEqual(snapshot["system"]["core_labels"], [])
        self.assertEqual(snapshot["system"]["cpu_index_map"], [])
        self.assertIsNone(snapshot["memory"]["used_percent"])
        self.assertIsNone(snapshot["memory"]["used_bytes"])
        self.assertIsNone(snapshot["memory"]["swap_used_bytes"])

    def test_snapshot_rejects_synthetic_static_defaults_from_empty_legacy_probes(self) -> None:
        """Empty commands cannot become an OS or physical-memory observation."""

        with mock.patch.object(porkyhud.socket, "gethostname", return_value="proven-host.local"), \
                mock.patch.object(porkyhud.platform, "release", return_value="26.0.0"), \
                mock.patch.object(porkyhud.platform, "machine", return_value="arm64"), \
                mock.patch.object(porkyhud, "run_command", return_value=""), \
                mock.patch.object(porkyhud, "sysctl_value", return_value=""), \
                mock.patch.object(porkyhud, "build_core_labels", return_value=([], [], {})), \
                mock.patch.object(porkyhud, "gpu_core_count_fast", return_value=None), \
                mock.patch.object(porkyhud, "max_gpu_frequency_mhz", return_value=None):
            legacy_static = porkyhud.collect_static_info()

        snapshot = self.collect_snapshot_with(
            descriptive_values={"system": legacy_static},
        )
        self.assertEqual(snapshot["system"]["host"], "proven-host")
        self.assertEqual(snapshot["system"]["kernel"], "26.0.0")
        for key in ("os", "memory"):
            with self.subTest(key=key):
                self.assertEqual(snapshot["system"][key], "unavailable")

    def test_snapshot_preserves_recognized_static_os_and_memory_fields(self) -> None:
        """Strict static grammar retains nonempty version and positive capacity evidence."""

        snapshot = self.collect_snapshot_with(descriptive_values={
            "system": {
                "host": "proven-host", "kernel": "26.0.0",
                "os": "macOS 26.0", "memory": "16 GB",
            },
        })
        self.assertEqual(snapshot["system"]["os"], "macOS 26.0")
        self.assertEqual(snapshot["system"]["memory"], "16 GB")

    def test_snapshot_static_nested_rows_drop_unknown_private_fields(self) -> None:
        """Untrusted static mappings cannot smuggle arbitrary nested diagnostics."""

        marker = "private-exception-secret"
        snapshot = self.collect_snapshot_with(descriptive_values={
            "system": {
                "host": "proven-host",
                "core_counts": {"E": 4, "P": 6, "private_exception": marker},
                "cpu_clusters": [{
                    "code": "P", "label": "Performance", "logical": 6,
                    "source": "Performance", "stderr": marker,
                }],
                "gpus": [{
                    "name": "Test GPU", "cores": "10", "metal": "Metal 4",
                    "displays": "Built-in", "max_freq_mhz": "1200",
                    "private_exception": marker,
                }],
            },
        })

        self.assertEqual(snapshot["system"]["core_counts"], {"E": 4, "P": 6})
        self.assertEqual(snapshot["system"]["cpu_clusters"], [{
            "code": "P", "label": "Performance", "logical": 6,
            "source": "Performance",
        }])
        self.assertEqual(snapshot["system"]["gpus"], [{
            "name": "Test GPU", "cores": "10", "metal": "Metal 4",
            "displays": "Built-in", "max_freq_mhz": "1200",
        }])
        self.assertNotIn(marker, json.dumps(snapshot))

    def test_snapshot_preserves_canonical_legacy_static_nested_shapes(self) -> None:
        """Every supported legacy nested field survives the strict wire projection."""

        canonical = {
            "core_counts": {"E": 4, "P": 6, "S": 2},
            "cpu_clusters": [{
                "code": "S", "label": "Super", "logical": 2, "source": "Super",
            }],
            "gpus": [{
                "name": "Test GPU", "cores": "12", "metal": "Metal 4",
                "displays": "Built-in Display", "max_freq_mhz": "1400",
            }],
        }
        snapshot = self.collect_snapshot_with(descriptive_values={
            "system": {"host": "proven-host", **canonical},
        })
        for key, expected in canonical.items():
            with self.subTest(key=key):
                self.assertEqual(snapshot["system"][key], expected)

    def test_huge_descriptor_integers_fail_closed_at_every_numeric_projection(self) -> None:
        """Numeric validation is total even for integers too large for float coercion."""

        huge = 10 ** 1000
        cases = (
            ("system", {"host": "kept", "gpu_core_count": huge},
             lambda snapshot: snapshot["system"]["gpu_core_count"]),
            ("memory", {"total": huge, "used": 1},
             lambda snapshot: snapshot["memory"]["total_bytes"]),
            ("battery", make_battery(percent=huge),
             lambda snapshot: snapshot["battery"]["percent"]),
            ("sensors", replace(settled_sensor(), package_power_w=huge),
             lambda snapshot: snapshot["sensors"]["package_power_w"]),
            ("activity", porkyhud.DiskActivity(bytes_per_sec=huge),
             lambda snapshot: snapshot["disk"]["activity"]["bytes_per_sec"]),
        )
        for name, corrupt, projected_value in cases:
            with self.subTest(name=name):
                snapshot = self.collect_snapshot_with(descriptive_values={name: corrupt})
                self.assertIsNone(projected_value(snapshot))

    def test_snapshot_memory_empty_commands_do_not_manufacture_zero_usage(self) -> None:
        """Physical capacity alone is not evidence that used and swap bytes are zero."""

        with mock.patch.object(porkyhud, "_physical_memory_bytes", return_value=16 * 1024 ** 3), \
                mock.patch.object(porkyhud, "run_command", return_value=""):
            memory = porkyhud._snapshot_memory_stats()

        self.assertEqual(porkyhud._snapshot_memory_projection(memory), {
            "used_percent": None,
            "used_bytes": None,
            "total_bytes": None,
            "swap_used_bytes": None,
            "swap_total_bytes": None,
        })

    def test_snapshot_memory_accepts_recognized_detail_and_zero_swap_evidence(self) -> None:
        """A complete legacy response may truthfully report measured zero swap."""

        vm_stat = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free: 10.
Pages speculative: 0.
Anonymous pages: 1.
Pages wired down: 1.
Pages occupied by compressor: 0.
"""

        def command(args, **_kwargs):
            return vm_stat if args == ["vm_stat"] else "total = 0.00M  used = 0.00M  free = 0.00M"

        with mock.patch.object(porkyhud, "_physical_memory_bytes", return_value=40960.0), \
                mock.patch.object(porkyhud, "run_command", side_effect=command):
            memory = porkyhud._snapshot_memory_stats()

        self.assertEqual(porkyhud._snapshot_memory_projection(memory), {
            "used_percent": 20.0,
            "used_bytes": 8192,
            "total_bytes": 40960.0,
            "swap_used_bytes": 0.0,
            "swap_total_bytes": 0.0,
        })

    def test_snapshot_battery_malformed_or_localized_output_is_unknown(self) -> None:
        """Unrecognized command text cannot prove that no battery is installed."""

        for pmset_raw in (
            "Ahora usando 'Alimentacion de CA'\n bateria desconocida",
            "Now drawing from 'AC Power'\n malformed battery row",
        ):
            with self.subTest(pmset_raw=pmset_raw), mock.patch.object(
                porkyhud, "run_command", side_effect=[pmset_raw, ""],
            ):
                battery = porkyhud._snapshot_battery_info()
            projection = porkyhud._snapshot_battery_projection(battery)
            self.assertIsNone(projection["present"])
            self.assertIsNone(projection["external_connected"])
            self.assertIsNone(projection["is_charging"])
            self.assertIsNone(projection["fully_charged"])

    def test_snapshot_battery_accepts_recognized_batteryless_evidence(self) -> None:
        """The canonical pmset AC-only response truthfully proves battery absence."""

        with mock.patch.object(
            porkyhud, "run_command", side_effect=["Now drawing from 'AC Power'\n", ""],
        ):
            battery = porkyhud._snapshot_battery_info()
        projection = porkyhud._snapshot_battery_projection(battery)
        self.assertIs(projection["present"], False)
        self.assertIs(projection["external_connected"], True)

    def test_snapshot_rejects_now_before_any_typed_completion(self) -> None:
        """Clock reversal is an invalid snapshot, not a silently fresh assessment."""

        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        states = dict(porkyhud._registry_states())
        for source_id, value in (
            (core.SourceId.CPU_USAGE, core.CpuUsage(5.0, 0.0, 1.0)),
            (core.SourceId.MEMORY_PRESSURE, core.MemoryPressure(core.MemoryPressureLevel.NORMAL)),
            (core.SourceId.THERMAL_OS, core.OsThermalState(0, 0)),
            (core.SourceId.DISK_CAPACITY, core.DiskCapacity("d", "d", "/", 100, 75)),
            (core.SourceId.PROCESS_TABLE, ()),
        ):
            states[source_id] = porkyhud.source_state_from_result(states[source_id], porkyhud.AdapterResult(
                value, 0.0, 1.0, completed_at, core.AttemptOutcome.SUCCESS, None,
            ))
        with self.assertRaises(ValueError):
            porkyhud._snapshot_frame(states, 0.5)

    def test_snapshot_propagates_interruption_after_executor_cleanup(self) -> None:
        """Interruptions are never converted into a reassuring unavailable snapshot."""

        before = {thread.ident for thread in threading.enumerate()
                  if thread.name.startswith("porkyhud-snapshot")}
        marker = KeyboardInterrupt("stop now")
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.collect_snapshot_with(
                typed_failure="collect_memory_pressure", interrupt_marker=marker,
            )
        self.assertIs(caught.exception, marker)
        self.assertEqual(
            {thread.ident for thread in threading.enumerate()
             if thread.name.startswith("porkyhud-snapshot")}, before,
        )

    def test_snapshot_preserves_primary_interrupt_when_future_cancellation_fails(self) -> None:
        """Cleanup defects must not replace the originating interruption object."""

        real_executor = porkyhud.ThreadPoolExecutor
        interrupt = KeyboardInterrupt("primary")

        class FailingCancelFuture:
            def __init__(self, future):
                self.future = future

            def result(self):
                return self.future.result()

            def cancel(self):
                raise RuntimeError("cleanup failure")

        class FailingCancelExecutor:
            def __init__(self, **kwargs):
                self.executor = real_executor(**kwargs)

            def submit(self, function, *args):
                return FailingCancelFuture(self.executor.submit(function, *args))

            def shutdown(self, **kwargs):
                self.executor.shutdown(**kwargs)

        with mock.patch.object(porkyhud, "ThreadPoolExecutor", FailingCancelExecutor), \
                self.assertRaises(KeyboardInterrupt) as caught:
            self.collect_snapshot_with(
                typed_failure="collect_memory_pressure", interrupt_marker=interrupt,
            )
        self.assertIs(caught.exception, interrupt)

    def test_snapshot_retries_failed_shutdown_and_joins_before_primary_propagates(self) -> None:
        """A shutdown interruption cannot strand worker threads or replace the primary."""

        real_executor = porkyhud.ThreadPoolExecutor
        primary = KeyboardInterrupt("primary")
        shutdown_marker = KeyboardInterrupt("shutdown")
        instances = []
        before = {thread.ident for thread in threading.enumerate()
                  if thread.name.startswith("porkyhud-snapshot")}

        class FailingOnceExecutor:
            def __init__(self, **kwargs):
                self.executor = real_executor(**kwargs)
                self.shutdown_calls = 0
                instances.append(self)

            def submit(self, function, *args):
                return self.executor.submit(function, *args)

            def shutdown(self, **kwargs):
                self.shutdown_calls += 1
                if self.shutdown_calls == 1:
                    raise shutdown_marker
                return self.executor.shutdown(**kwargs)

        residual = None
        try:
            with mock.patch.object(porkyhud, "ThreadPoolExecutor", FailingOnceExecutor), \
                    self.assertRaises(KeyboardInterrupt) as caught:
                self.collect_snapshot_with(
                    typed_failure="collect_memory_pressure", interrupt_marker=primary,
                )
            residual = {thread.ident for thread in threading.enumerate()
                        if thread.name.startswith("porkyhud-snapshot")}
        finally:
            for instance in instances:
                instance.executor.shutdown(wait=True, cancel_futures=True)
        self.assertIs(caught.exception, primary)
        self.assertEqual(instances[0].shutdown_calls, 2)
        self.assertEqual(residual, before)

    def test_snapshot_retries_cleanup_only_shutdown_then_propagates_first_error(self) -> None:
        """Cleanup-only BaseException is delayed until a later join attempt completes."""

        real_executor = porkyhud.ThreadPoolExecutor
        marker = KeyboardInterrupt("shutdown only")
        instances = []
        before = {thread.ident for thread in threading.enumerate()
                  if thread.name.startswith("porkyhud-snapshot")}

        class FailingOnceExecutor:
            def __init__(self, **kwargs):
                self.executor = real_executor(**kwargs)
                self.shutdown_calls = 0
                instances.append(self)

            def submit(self, function, *args):
                return self.executor.submit(function, *args)

            def shutdown(self, **kwargs):
                self.shutdown_calls += 1
                if self.shutdown_calls == 1:
                    raise marker
                return self.executor.shutdown(**kwargs)

        residual = None
        try:
            with mock.patch.object(porkyhud, "ThreadPoolExecutor", FailingOnceExecutor), \
                    self.assertRaises(KeyboardInterrupt) as caught:
                self.collect_snapshot_with()
            residual = {thread.ident for thread in threading.enumerate()
                        if thread.name.startswith("porkyhud-snapshot")}
        finally:
            for instance in instances:
                instance.executor.shutdown(wait=True, cancel_futures=True)
        self.assertIs(caught.exception, marker)
        self.assertEqual(instances[0].shutdown_calls, 2)
        self.assertEqual(residual, before)

    def test_snapshot_retries_a_failed_waiting_shutdown_until_workers_join(self) -> None:
        """Repeated shutdown interruptions cannot cap the mandatory join phase."""

        real_executor = porkyhud.ThreadPoolExecutor
        primary = KeyboardInterrupt("primary")
        cleanup_markers = [KeyboardInterrupt(f"shutdown {index}") for index in range(9)]
        instances = []
        before = {thread.ident for thread in threading.enumerate()
                  if thread.name.startswith("porkyhud-snapshot")}

        class FailingWaitExecutor:
            def __init__(self, **kwargs):
                self.executor = real_executor(**kwargs)
                self.shutdown_calls = 0
                instances.append(self)

            def submit(self, function, *args):
                return self.executor.submit(function, *args)

            def shutdown(self, **kwargs):
                self.shutdown_calls += 1
                if cleanup_markers:
                    raise cleanup_markers.pop(0)
                return self.executor.shutdown(**kwargs)

        residual = None
        try:
            with mock.patch.object(porkyhud, "ThreadPoolExecutor", FailingWaitExecutor), \
                    self.assertRaises(KeyboardInterrupt) as caught:
                self.collect_snapshot_with(
                    typed_failure="collect_memory_pressure", interrupt_marker=primary,
                )
            residual = {thread.ident for thread in threading.enumerate()
                        if thread.name.startswith("porkyhud-snapshot")}
        finally:
            for instance in instances:
                instance.executor.shutdown(wait=True, cancel_futures=True)
        self.assertIs(caught.exception, primary)
        self.assertEqual(instances[0].shutdown_calls, 10)
        self.assertEqual(residual, before)

    def test_snapshot_attempts_every_cancel_and_joins_before_cancel_error_propagates(self) -> None:
        """RuntimeError and BaseException from cancel cannot skip sibling cleanup or join."""

        real_executor = porkyhud.ThreadPoolExecutor
        for marker in (RuntimeError("cancel"), KeyboardInterrupt("cancel")):
            with self.subTest(marker=type(marker).__name__):
                instances = []
                before = {thread.ident for thread in threading.enumerate()
                          if thread.name.startswith("porkyhud-snapshot")}

                class CancelFuture:
                    def __init__(self, future, owner):
                        self.future = future
                        self.owner = owner

                    def result(self):
                        return self.future.result()

                    def cancel(self):
                        self.owner.cancel_calls += 1
                        raise marker

                class CancelExecutor:
                    def __init__(self, **kwargs):
                        self.executor = real_executor(**kwargs)
                        self.submitted = 0
                        self.cancel_calls = 0
                        instances.append(self)

                    def submit(self, function, *args):
                        self.submitted += 1
                        return CancelFuture(self.executor.submit(function, *args), self)

                    def shutdown(self, **kwargs):
                        return self.executor.shutdown(**kwargs)

                with mock.patch.object(porkyhud, "ThreadPoolExecutor", CancelExecutor), \
                        self.assertRaises(type(marker)) as caught:
                    self.collect_snapshot_with()
                self.assertIs(caught.exception, marker)
                self.assertEqual(instances[0].cancel_calls, instances[0].submitted)
                self.assertEqual(
                    {thread.ident for thread in threading.enumerate()
                     if thread.name.startswith("porkyhud-snapshot")},
                    before,
                )

    def test_process_gpu_unknown_zero_and_measurement_keep_distinct_json_and_text_values(self) -> None:
        """GPU projection distinguishes absent evidence, stable zero, and measured use."""

        for gpu, text_value in ((None, "--"), (0.0, "0.0%"), (7.5, "7.5%")):
            with self.subTest(gpu=gpu):
                snapshot = {
                    "read": {"message": "read: unavailable", "level": "unknown"},
                    "system": {}, "cpu": {"average_percent": None, "cores": []},
                    "memory": {"used_bytes": None, "total_bytes": None, "used_percent": None},
                    "sensors": {}, "disk": {"activity": {"bytes_per_sec": None, "iops": None}},
                    "processes": [{"pid": 1, "cpu": 1.0, "mem": 1.0, "gpu": gpu, "command": "/bin/x"}],
                }
                with mock.patch("builtins.print") as output:
                    porkyhud.print_text_snapshot(snapshot)
                self.assertIn(text_value, "\n".join(str(call.args[0]) for call in output.call_args_list))

    def test_snapshot_keeps_processes_when_agx_attempts_fail(self) -> None:
        """GPU attribution failure never discards otherwise valid process evidence."""

        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        row = porkyhud.ProcessRow(42, 20.0, 1.0, None, "R", "00:01", "/bin/task")
        first_failed = porkyhud.AdapterResult(
            None, 0.0, 0.1, completed_at, core.AttemptOutcome.TIMEOUT,
            core.FailureCode.COMMAND_TIMEOUT,
        )
        second_failed = porkyhud.AdapterResult(
            None, 0.2, 0.3, completed_at, core.AttemptOutcome.TIMEOUT,
            core.FailureCode.COMMAND_TIMEOUT,
        )
        for results in ([first_failed, second_failed], [
                porkyhud.AdapterResult(porkyhud.GpuProcessSample({}, {}, True, False), 0.0, 0.1,
                                       completed_at, core.AttemptOutcome.SUCCESS, None), second_failed]):
            with self.subTest(results=results):
                snapshot = self.collect_snapshot_with(
                    process_rows_data=(row,), agx_results=results,
                )
            self.assertEqual(len(snapshot["processes"]), 1)
            self.assertIsNone(snapshot["processes"][0]["gpu"])

    def test_snapshot_merges_actual_agx_attempt_matrix_without_losing_processes(self) -> None:
        """The public snapshot distinguishes failed, absent, new, and measured AGX rows."""

        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        row = porkyhud.ProcessRow(42, 20.0, 1.0, None, "R", "00:01", "/bin/task")

        def result(value, started, completed, outcome=core.AttemptOutcome.SUCCESS,
                   failure=None):
            return porkyhud.AdapterResult(
                value, started, completed, completed_at, outcome, failure,
            )

        failure = result(
            None, 0.0, 0.1, core.AttemptOutcome.TIMEOUT,
            core.FailureCode.COMMAND_TIMEOUT,
        )
        baseline = result(porkyhud.GpuProcessSample({}, {}, True, False), 0.0, 0.1)
        cases = (
            ("baseline failure", [failure, result(
                porkyhud.GpuProcessSample({}, {42: "task"}, True, False), 0.2, 0.3,
            )], None),
            ("measurement failure", [baseline, result(
                None, 0.2, 0.3, core.AttemptOutcome.TIMEOUT,
                core.FailureCode.COMMAND_TIMEOUT,
            )], None),
            ("complete absence", [baseline, result(
                porkyhud.GpuProcessSample({}, {}, True, True), 0.2, 0.3,
            )], 0.0),
            ("new pid", [baseline, result(
                porkyhud.GpuProcessSample({}, {42: "task"}, True, True), 0.2, 0.3,
            )], None),
            ("measured match", [baseline, result(
                porkyhud.GpuProcessSample({42: 7.5}, {42: "task"}, True, True),
                0.2, 0.3,
            )], 7.5),
        )
        for name, attempts, expected_gpu in cases:
            with self.subTest(name=name):
                snapshot = self.collect_snapshot_with(
                    process_rows_data=(row,), agx_results=attempts,
                )
            self.assertEqual(len(snapshot["processes"]), 1)
            self.assertEqual(snapshot["processes"][0]["gpu"], expected_gpu)

    def test_signal_during_palette_setup_still_restores_terminal_colors(self) -> None:
        stdout = mock.Mock()
        stdout.isatty.return_value = True
        stdout.fileno.return_value = 1

        def interrupt_palette_setup() -> bool:
            os.kill(os.getpid(), signal.SIGTERM)
            return True

        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), mock.patch.object(
            porkyhud,
            "terminal_ui_problem",
            return_value="",
        ), mock.patch.object(
            porkyhud,
            "enforce_dark_terminal",
            side_effect=interrupt_palette_setup,
        ), mock.patch.object(
            porkyhud,
            "restore_terminal_colors",
        ) as restore, mock.patch.object(
            porkyhud.sys,
            "stdout",
            stdout,
        ):
            result = porkyhud.main([])

        self.assertEqual(result, 128 + signal.SIGTERM)
        restore.assert_called_once_with()

    def test_signal_during_terminal_capability_check_is_handled(self) -> None:
        stdout = mock.Mock()
        stdout.fileno.return_value = 1

        def interrupt_capability_check() -> bool:
            os.kill(os.getpid(), signal.SIGTERM)
            return True

        stdout.isatty.side_effect = interrupt_capability_check
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), mock.patch.object(
            porkyhud,
            "terminal_ui_problem",
            return_value="",
        ), mock.patch.object(
            porkyhud,
            "enforce_dark_terminal",
        ) as enforce, mock.patch.object(
            porkyhud,
            "restore_terminal_colors",
        ) as restore, mock.patch.object(
            porkyhud.sys,
            "stdout",
            stdout,
        ):
            result = porkyhud.main([])

        self.assertEqual(result, 128 + signal.SIGTERM)
        enforce.assert_not_called()
        restore.assert_not_called()

    def test_collect_runtime_stats_composes_collectors(self) -> None:
        memory = {"total": 100.0, "used": 25.0}
        battery = make_battery()
        disk = porkyhud.DiskInfo("data", "/", 100, 25, 75)
        with mock.patch.object(porkyhud, "memory_stats", return_value=memory), mock.patch.object(
            porkyhud,
            "battery_info",
            return_value=battery,
        ), mock.patch.object(porkyhud, "disk_info", return_value=disk), mock.patch.object(
            porkyhud,
            "network_bytes",
            return_value=(123, 456),
        ), mock.patch.object(porkyhud.time, "monotonic", return_value=99.5):
            runtime = porkyhud.collect_runtime_stats()
        self.assertIs(runtime.memory, memory)
        self.assertIs(runtime.battery, battery)
        self.assertIs(runtime.disk, disk)
        self.assertEqual(runtime.network_totals, (123, 456))
        self.assertEqual(runtime.collected_at, 99.5)

    def test_process_collection_and_sorting_are_separate_and_stable(self) -> None:
        raw = """
          10  12.5  1.2 R  00:01 /usr/bin/alpha --token hidden
          20   5.0  9.5 S  01:02 /usr/bin/beta --mode worker
        """
        result = porkyhud.CommandResult(
            "process-table", raw, "", 0, 10.0, 10.2,
            datetime(2026, 8, 12, 12, tzinfo=timezone.utc), core.AttemptOutcome.SUCCESS, None,
        )
        with mock.patch.object(porkyhud, "run_structured_command", return_value=result):
            rows = list(porkyhud.collect_process_rows().value or ())
        self.assertEqual([row.pid for row in rows], [10, 20])
        self.assertEqual([row.gpu for row in rows], [None, None])

        original_order = [row.pid for row in rows]
        self.assertEqual(
            [row.pid for row in porkyhud.sort_process_rows(rows, "CPU")],
            [10, 20],
        )
        self.assertEqual(
            [row.pid for row in porkyhud.sort_process_rows(rows, "MEM")],
            [20, 10],
        )
        self.assertEqual(
            [row.pid for row in porkyhud.sort_process_rows(rows, "GPU")],
            [10, 20],
        )
        self.assertEqual([row.pid for row in rows], original_order)

    def test_snapshot_schema_is_json_serializable_and_commands_are_sanitized(self) -> None:
        static = {
            "host": "test-mac",
            "chip": "Test Silicon",
            "model": "Test Mac",
            "core_labels": ["P0", "P1"],
            "cpu_index_map": [0, 1],
        }
        memory = {
            "total": 100.0,
            "used": 50.0,
            "swap_total": 10.0,
            "swap_used": 0.0,
        }
        battery = make_battery(present=False, percent=None, source="AC Power")
        sensor = settled_sensor()
        disk = porkyhud.DiskInfo("data", "/", 100, 25, 75)
        activity = porkyhud.DiskActivity(1024.0, 2.0)
        process = porkyhud.ProcessRow(
            42,
            20.0,
            1.0,
            None,
            "R",
            "00:01",
            "/usr/bin/worker --api-key snapshot-secret --mode test",
        )
        completed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)

        def success(value: object, started: float = 0.2, completed: float = 0.5):
            return porkyhud.AdapterResult(value, started, completed, completed_at,
                                          core.AttemptOutcome.SUCCESS, None)

        class FakeCpuSampler:
            def __init__(self) -> None:
                self.last_per_core = ()
                self.calls = 0

            def sample_usage(self):
                self.calls += 1
                self.last_per_core = (25.0, 75.0)
                return success(core.CpuUsage(50.0, 0.1, 0.5))

            def sample(self) -> list[float]:
                return [25.0, 75.0]

        with mock.patch.object(porkyhud, "collect_static_info", return_value=static), mock.patch.object(
            porkyhud,
            "MacCpuSampler",
            FakeCpuSampler,
        ), mock.patch.object(porkyhud.time, "sleep"), mock.patch.object(
            porkyhud.time, "monotonic", return_value=2.0), mock.patch.object(
            porkyhud,
            "_snapshot_memory_stats",
            return_value=memory,
        ), mock.patch.object(porkyhud, "_snapshot_battery_info", return_value=battery), mock.patch.object(
            porkyhud,
            "sensor_info",
            return_value=sensor,
        ), mock.patch.object(porkyhud, "disk_info", return_value=disk), mock.patch.object(
            porkyhud,
            "disk_activity",
            return_value=activity,
        ), mock.patch.object(porkyhud, "collect_memory_pressure", return_value=success(
            core.MemoryPressure(core.MemoryPressureLevel.NORMAL))), mock.patch.object(
            porkyhud, "collect_os_thermal_state", return_value=success(core.OsThermalState(0, 0))), mock.patch.object(
            porkyhud, "collect_disk_capacity", return_value=success(
                core.DiskCapacity("data", "data", "/", 100, 75))), mock.patch.object(
            porkyhud, "collect_process_rows", return_value=success((process,))), mock.patch.object(
            porkyhud, "collect_gpu_process_sample", side_effect=[
                success(porkyhud.GpuProcessSample({}, {}, True, False), 0.0, 0.1),
                success(porkyhud.GpuProcessSample({}, {}, True, True), 0.6, 0.7),
            ]), mock.patch.object(porkyhud.time, "strftime", return_value="2026-01-01T00:00:00+0000"):
            snapshot = porkyhud.collect_snapshot()

        self.assertEqual(
            set(snapshot),
            {"generated_at", "read", "system", "cpu", "memory", "memory_activity", "battery", "sensors", "disk", "processes"},
        )
        self.assertEqual(snapshot["cpu"]["average_percent"], 50.0)
        self.assertEqual(
            [row["label"] for row in snapshot["cpu"]["cores"]],
            ["P0", "P1"],
        )
        command = snapshot["processes"][0]["command"]
        self.assertNotIn("snapshot-secret", command)
        self.assertIn("<redacted>", command)
        self.assertEqual(snapshot["processes"][0]["gpu"], 0.0)
        self.assertFalse(snapshot["battery"]["present"])
        self.assertEqual(json.loads(json.dumps(snapshot))["processes"][0]["gpu"], 0.0)


if __name__ == "__main__":
    unittest.main()
