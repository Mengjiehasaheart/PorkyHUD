from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest import mock

import porkyhud
import porkyhud_core as core


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

    def descendant_fixture(self, directory: str, emit_invalid_utf8: bool) -> tuple[str, str, str]:
        pid_path = os.path.join(directory, "pids")
        heartbeat_path = os.path.join(directory, "heartbeat")
        safety_seconds = 1.5
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
        else:
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
            self.assert_command_error(result, core.FailureCode.DECODE_ERROR)
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

    def test_cleanup_fault_does_not_replace_prepared_timeout_result(self) -> None:
        class CleanupFault(Exception):
            pass

        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, _heartbeat_path = self.descendant_fixture(directory, emit_invalid_utf8=False)
            original_grace = porkyhud._command_terminate_grace_delay

            def grace_then_fault() -> None:
                original_grace()
                events.append("grace")
                raise CleanupFault()

            original_selector_close = porkyhud._close_command_selector

            def close_selector(selector: object) -> None:
                events.append("selector")
                original_selector_close(selector)

            with mock.patch.object(
                porkyhud, "_command_terminate_grace_delay", side_effect=grace_then_fault,
            ), mock.patch.object(
                porkyhud, "_close_command_selector", side_effect=close_selector,
            ):
                result = self.run_fixture(code, timeout_s=0.1)
            self.assertEqual(result.outcome, core.AttemptOutcome.UNKNOWN_ERROR)
            self.assertEqual(result.failure, core.FailureCode.UNEXPECTED_EXCEPTION)
            self.assertEqual(events, ["grace", "selector"])

    def test_cleanup_fault_preserves_pending_keyboard_interrupt_identity(self) -> None:
        class CleanupFault(Exception):
            pass

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
            original_grace = porkyhud._command_terminate_grace_delay

            def grace_then_fault() -> None:
                original_grace()
                raise CleanupFault()

            with mock.patch.object(porkyhud.selectors, "DefaultSelector", InterruptingSelector), mock.patch.object(
                porkyhud, "_command_terminate_grace_delay", side_effect=grace_then_fault,
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    self.run_fixture(code, timeout_s=1.0)
            self.assertIs(raised.exception, interruption)

    def test_ready_success_suppresses_cleanup_faults_and_attempts_every_step(self) -> None:
        class CleanupFault(Exception):
            pass

        events: list[str] = []

        def fail_after_recording(name: str) -> object:
            def fault(*_args: object) -> None:
                events.append(name)
                raise CleanupFault(name)
            return fault

        secret = "cleanup-fixture-secret"
        with mock.patch.object(
            porkyhud, "_unregister_command_stream", side_effect=fail_after_recording("unregister"),
        ), mock.patch.object(
            porkyhud, "_close_command_stream", side_effect=fail_after_recording("stream-close"),
        ), mock.patch.object(
            porkyhud, "_close_command_selector", side_effect=fail_after_recording("selector-close"),
        ):
            result = self.run_fixture("print('ok') # " + secret)
        self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
        self.assertEqual(result.stdout, "ok\n")
        self.assertNotIn(secret, repr(result))
        self.assertEqual(events.count("unregister"), 2)
        self.assertEqual(events.count("stream-close"), 2)
        self.assertEqual(events.count("selector-close"), 1)

    def test_grace_keyboard_interrupt_still_kills_reaps_and_preserves_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, _heartbeat_path = self.descendant_fixture(directory, emit_invalid_utf8=False)
            interruption = KeyboardInterrupt("grace interruption")
            signals: list[int] = []
            original_signal = porkyhud._signal_process_group

            def trace_signal(process: object, sig: int) -> None:
                signals.append(sig)
                original_signal(process, sig)

            def interrupt_grace() -> None:
                raise interruption

            with mock.patch.object(porkyhud, "_command_terminate_grace_delay", side_effect=interrupt_grace), mock.patch.object(
                porkyhud, "_signal_process_group", side_effect=trace_signal,
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    self.run_fixture(code, timeout_s=0.1)
            self.assertIs(raised.exception, interruption)
            self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
            self.assert_recorded_processes_are_gone(pid_path)

    def test_signal_permission_errors_make_timeout_a_private_unknown_after_reap(self) -> None:
        calls: list[str] = []
        original_wait = porkyhud._wait_for_process

        def permission_denied(_process: object, sig: int) -> None:
            calls.append("term" if sig == signal.SIGTERM else "kill")
            raise PermissionError("fixture permission failure")

        def trace_reap(process: object, deadline: float) -> None:
            calls.append("reap")
            original_wait(process, deadline)

        with mock.patch.object(porkyhud, "_signal_process_group", side_effect=permission_denied), mock.patch.object(
            porkyhud, "_wait_for_process", side_effect=trace_reap,
        ):
            result = self.run_fixture(
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.5)",
                timeout_s=0.1,
            )
        self.assertEqual(result.outcome, core.AttemptOutcome.UNKNOWN_ERROR)
        self.assertEqual(result.failure, core.FailureCode.UNEXPECTED_EXCEPTION)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(calls, ["term", "kill", "reap"])
        time.sleep(0.6)

    def test_term_failure_followed_by_kill_is_still_private_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, _heartbeat_path = self.descendant_fixture(directory, emit_invalid_utf8=False)
            original_signal = porkyhud._signal_process_group
            calls: list[int] = []

            def term_fails(process: object, sig: int) -> None:
                calls.append(sig)
                if sig == signal.SIGTERM:
                    raise PermissionError("fixture term failure")
                original_signal(process, sig)

            with mock.patch.object(porkyhud, "_signal_process_group", side_effect=term_fails):
                result = self.run_fixture(code, timeout_s=0.1)
            self.assertEqual(result.outcome, core.AttemptOutcome.UNKNOWN_ERROR)
            self.assertEqual(calls, [signal.SIGTERM, signal.SIGKILL])
            self.assert_recorded_processes_are_gone(pid_path)

    def test_kill_failure_after_term_exit_is_private_unknown_and_reaps_leader(self) -> None:
        original_signal = porkyhud._signal_process_group
        calls: list[int] = []

        def kill_fails_after_term(process: object, sig: int) -> None:
            calls.append(sig)
            if sig == signal.SIGKILL:
                raise PermissionError("fixture kill failure")
            original_signal(process, sig)

        with mock.patch.object(porkyhud, "_signal_process_group", side_effect=kill_fails_after_term):
            result = self.run_fixture("import time; time.sleep(0.5)", timeout_s=0.1)
        self.assertEqual(result.outcome, core.AttemptOutcome.UNKNOWN_ERROR)
        self.assertEqual(result.failure, core.FailureCode.UNEXPECTED_EXCEPTION)
        self.assertEqual(calls, [signal.SIGTERM, signal.SIGKILL])
        self.assertIsNotNone(result.returncode)

    def test_process_lookup_signal_is_benign_after_decode_failure(self) -> None:
        calls: list[int] = []
        original_getpgid = porkyhud.os.getpgid

        def tracked_getpgid(pid: int) -> int:
            return original_getpgid(pid)

        def absent_group(_pgid: int, sig: int) -> None:
            calls.append(sig)
            raise ProcessLookupError()

        with mock.patch.object(porkyhud.os, "getpgid", side_effect=tracked_getpgid), mock.patch.object(
            porkyhud.os, "killpg", side_effect=absent_group,
        ):
            result = self.run_fixture("import sys; sys.stdout.buffer.write(b'\\xff')")
        self.assert_command_error(result, core.FailureCode.DECODE_ERROR)
        self.assertTrue(calls)

    def test_spawn_result_survives_ordinary_selector_close_fault_and_base_propagates(self) -> None:
        class SelectorCloseFault(Exception):
            pass

        class FaultySelector:
            def close(self) -> None:
                raise SelectorCloseFault()

        secret = "spawn-close-fixture-secret"
        with mock.patch.object(porkyhud.selectors, "DefaultSelector", FaultySelector):
            result = porkyhud.run_structured_command(
                ["/definitely/not/a/porkyhud-command", secret], "fixture", 1.0, 128, 128,
            )
        self.assertEqual(result.outcome, core.AttemptOutcome.MISSING_DEPENDENCY)
        self.assertNotIn(secret, repr(result))

        for spawn_error, outcome, failure in (
            (PermissionError(), core.AttemptOutcome.PERMISSION_DENIED, core.FailureCode.PERMISSION_DENIED),
            (OSError(), core.AttemptOutcome.UNKNOWN_ERROR, core.FailureCode.UNEXPECTED_EXCEPTION),
        ):
            with self.subTest(error=type(spawn_error).__name__), mock.patch.object(
                porkyhud.subprocess, "Popen", side_effect=spawn_error,
            ), mock.patch.object(porkyhud.selectors, "DefaultSelector", FaultySelector):
                result = porkyhud.run_structured_command(
                    ["/fixture/command", secret], "fixture", 1.0, 128, 128,
                )
            self.assertEqual(result.outcome, outcome)
            self.assertEqual(result.failure, failure)
            self.assertNotIn(secret, repr(result))

        interruption = KeyboardInterrupt("spawn close interruption")

        class InterruptingSelector:
            def close(self) -> None:
                raise interruption

        with mock.patch.object(porkyhud.selectors, "DefaultSelector", InterruptingSelector):
            with self.assertRaises(KeyboardInterrupt) as raised:
                porkyhud.run_structured_command(
                    ["/definitely/not/a/porkyhud-command", secret], "fixture", 1.0, 128, 128,
                )
        self.assertIs(raised.exception, interruption)

    def test_decode_cleanup_signals_before_the_leader_is_reaped(self) -> None:
        original_signal = porkyhud._signal_process_group

        def traced_result(code: str) -> tuple[object, list[tuple[int, int | None, int | None]]]:
            signals: list[tuple[int, int | None, int | None]] = []

            def trace_signal(process: object, sig: int) -> None:
                try:
                    pgid = os.getpgid(process.pid)
                except ProcessLookupError:
                    pgid = None
                signals.append((sig, process.returncode, pgid))
                original_signal(process, sig)

            with mock.patch.object(porkyhud, "_signal_process_group", side_effect=trace_signal):
                return self.run_fixture(code), signals

        result, signals = traced_result("import sys; sys.stdout.buffer.write(b'\\xff')")
        self.assert_command_error(result, core.FailureCode.DECODE_ERROR)
        self.assertEqual([signal_value for signal_value, _returncode, _pgid in signals], [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(all(returncode is None for _sig, returncode, _pgid in signals))

        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, heartbeat_path = self.descendant_fixture(directory, emit_invalid_utf8=True)
            result, signals = traced_result(code)
            self.assert_command_error(result, core.FailureCode.DECODE_ERROR)
            self.assertEqual(
                [signal_value for signal_value, _returncode, _pgid in signals],
                [signal.SIGTERM, signal.SIGKILL],
            )
            self.assertTrue(all(returncode is None for _sig, returncode, _pgid in signals))
            self.assert_heartbeat_stopped(heartbeat_path)
            self.assert_recorded_processes_are_gone(pid_path)

    def test_timeout_terminates_a_child_ignoring_sigterm(self) -> None:
        started = time.monotonic()
        result = self.run_fixture(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            timeout_s=0.15,
        )
        self.assertEqual(result.outcome, core.AttemptOutcome.TIMEOUT)
        self.assertEqual(result.failure, core.FailureCode.COMMAND_TIMEOUT)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_timeout_terminates_descendant_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = os.path.join(directory, "grandchild.pid")
            child_code = "import time; time.sleep(60)"
            code = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', %r]); "
                "pathlib.Path(%r).write_text(str(child.pid)); time.sleep(60)"
            ) % (child_code, pid_path)
            result = self.run_fixture(code, timeout_s=0.2)
            self.assertEqual(result.outcome, core.AttemptOutcome.TIMEOUT)
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
        invalid row
        """

        class FakeGpuSampler:
            def __init__(self) -> None:
                self.samples: list[float | None] = []

            def sample(self, system_gpu_percent: float | None = None) -> dict[int, float]:
                self.samples.append(system_gpu_percent)
                return {10: 3.0, 20: 70.0}

        sampler = FakeGpuSampler()
        with mock.patch.object(porkyhud, "run_command", return_value=raw):
            rows = porkyhud.collect_process_rows(sampler, 42.0)
        self.assertEqual([row.pid for row in rows], [10, 20])
        self.assertEqual([row.gpu for row in rows], [3.0, 70.0])
        self.assertEqual(sampler.samples, [42.0])

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
            [20, 10],
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
            2.0,
            "R",
            "00:01",
            "/usr/bin/worker --api-key snapshot-secret --mode test",
        )

        class FakeCpuSampler:
            def sample(self) -> list[float]:
                return [25.0, 75.0]

        class FakeGpuSampler:
            def sample(self, _system_gpu_percent: float | None = None) -> dict[int, float]:
                return {}

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
        ), mock.patch.object(porkyhud, "GPUProcessSampler", FakeGpuSampler), mock.patch.object(
            porkyhud,
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
        self.assertFalse(snapshot["battery"]["present"])
        json.dumps(snapshot)


if __name__ == "__main__":
    unittest.main()
