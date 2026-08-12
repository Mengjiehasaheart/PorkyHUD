from __future__ import annotations

import json
import os
import signal
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
    """Minimal curses-like character buffer for deterministic rendering tests."""

    def __init__(self, height: int, width: int, fill: str = " ") -> None:
        self.height = height
        self.width = width
        self.cells = [[fill for _ in range(width)] for _ in range(height)]
        self.background: tuple[str, int] | None = None

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def addnstr(self, y: int, x: int, text: str, limit: int, _attr: int = 0) -> None:
        for offset, character in enumerate(str(text)[: max(0, limit)]):
            column = x + offset
            if 0 <= y < self.height and 0 <= column < self.width:
                self.cells[y][column] = character

    def bkgd(self, character: str, attr: int) -> None:
        self.background = (character, attr)

    def erase(self) -> None:
        self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]

    def row(self, y: int) -> str:
        return "".join(self.cells[y])


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
            "time.sleep(0.03); sys.stdout.buffer.write(b'\\x82\\xac')"
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
                "time.sleep(0.03); sys.stdout.buffer.write(b'X')"
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
            self.assert_abnormal_or_private_unknown(
                result, core.AttemptOutcome.TIMEOUT, core.FailureCode.COMMAND_TIMEOUT,
            )
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
        return porkyhud.CommandResult(
            "fixture", stdout, "", 0 if outcome is core.AttemptOutcome.SUCCESS else None,
            10.0, 10.2, datetime(2026, 8, 11, 12, tzinfo=timezone.utc), outcome, failure,
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
        message, level = porkyhud.system_read([], {"total": 1.0, "used": 0.0},
                                               porkyhud.DiskInfo("root", "/", 100, 0, 100),
                                               porkyhud.DiskActivity(), make_battery(), unavailable, [])
        self.assertEqual(level, "good")
        self.assertIn("Quiet", message)

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

        message, level = porkyhud.system_read(
            [45.0],
            {"total": 100.0, "used": 50.0, "swap_total": 0.0},
            porkyhud.DiskInfo("data", "/", 100, 50, 50),
            porkyhud.DiskActivity(),
            battery,
            settled_sensor(),
            [],
        )
        self.assertNotIn("battery", message.lower())
        self.assertEqual(level, "good")

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


class LayoutAndRenderingTests(unittest.TestCase):
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
        with mock.patch.object(porkyhud, "color_pair", return_value=0), mock.patch.object(
            porkyhud.time,
            "strftime",
            return_value="2026-07-11 10:15:00",
        ):
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
        self.assertLessEqual(len(porkyhud.shortcut_status(76)), 74)
        self.assertIn("u sensor", porkyhud.shortcut_status(76))
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
        overlay_height = 16
        overlay_y = (40 - overlay_height) // 2
        overlay_x = (120 - overlay_width) // 2
        for row in range(overlay_y, overlay_y + overlay_height):
            self.assertNotIn(
                sentinel,
                screen.row(row)[overlay_x : overlay_x + overlay_width],
            )
        self.assertEqual(screen.cells[overlay_y][overlay_x - 1], sentinel)
        blank_content_row = overlay_y + 11
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

    def test_renderer_and_text_snapshot_show_unknown_gpu_as_dashes(self) -> None:
        screen = FakeScreen(10, 70)
        rows = [porkyhud.ProcessRow(1, 2.0, 3.0, None, "S", "00:01", "/bin/task")]
        with mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_process_panel(screen, 1, 1, 8, 64, rows, 0, "GPU")
        self.assertIn("--", "\n".join(screen.row(index) for index in range(1, 9)))
        snapshot = {"system": {}, "cpu": {"average_percent": 0.0, "cores": []},
                    "memory": {"used_bytes": 0, "total_bytes": 0, "used_percent": 0.0},
                    "sensors": {}, "disk": {"activity": {"bytes_per_sec": 0, "iops": 0}},
                    "processes": [{"pid": 1, "cpu": 2.0, "mem": 3.0, "gpu": None, "command": "/bin/task"}]}
        with mock.patch("builtins.print") as output:
            porkyhud.print_text_snapshot(snapshot)
        self.assertIn("GPU   --", "\n".join(str(call.args[0]) for call in output.call_args_list))


class RuntimeProcessAndSnapshotTests(unittest.TestCase):
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

        class FakeCpuSampler:
            def sample(self) -> list[float]:
                return [25.0, 75.0]

        with mock.patch.object(porkyhud, "collect_static_info", return_value=static), mock.patch.object(
            porkyhud,
            "MacCpuSampler",
            FakeCpuSampler,
        ), mock.patch.object(porkyhud.time, "sleep"), mock.patch.object(
            porkyhud,
            "memory_stats",
            return_value=memory,
        ), mock.patch.object(porkyhud, "battery_info", return_value=battery), mock.patch.object(
            porkyhud,
            "sensor_info",
            return_value=sensor,
        ), mock.patch.object(porkyhud, "disk_info", return_value=disk), mock.patch.object(
            porkyhud,
            "disk_activity",
            return_value=activity,
        ), mock.patch.object(porkyhud,
            "process_rows",
            return_value=[process],
        ), mock.patch.object(porkyhud.time, "strftime", return_value="2026-01-01T00:00:00+0000"):
            snapshot = porkyhud.collect_snapshot()

        self.assertEqual(
            set(snapshot),
            {"generated_at", "read", "system", "cpu", "memory", "battery", "sensors", "disk", "processes"},
        )
        self.assertEqual(snapshot["cpu"]["average_percent"], 50.0)
        self.assertEqual(
            [row["label"] for row in snapshot["cpu"]["cores"]],
            ["P0", "P1"],
        )
        command = snapshot["processes"][0]["command"]
        self.assertNotIn("snapshot-secret", command)
        self.assertIn("<redacted>", command)
        self.assertIsNone(snapshot["processes"][0]["gpu"])
        self.assertFalse(snapshot["battery"]["present"])
        self.assertIsNone(json.loads(json.dumps(snapshot))["processes"][0]["gpu"])


if __name__ == "__main__":
    unittest.main()
