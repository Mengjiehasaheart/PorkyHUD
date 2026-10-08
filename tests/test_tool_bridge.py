"""Collector-to-tool contracts, headless cleanup, and CLI output boundaries."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import porkyhud
from porkyhud_inventory import InventoryStatus, ProbeResult
from porkyhud_tools.bridge import live_sample, process_projection, snapshot_sample
from porkyhud_tools.processes import workload_report
from porkyhud_tools.sessions import load_session, validate_sample
from tests.test_porkyhud import FakeScreen, make_health_frame_for_test


START = "Thu Oct 8 10:00:00 2026"
EXTENDED = "42 25.0 1.0 S 00:20 1 2048 Thu Oct 8 10:00:00 2026 /usr/bin/python3 train.py --token PRIVATE_TOKEN\n"
IDENTITY = "42 1 S 2048 Thu Oct 8 10:00:00 2026 /usr/bin/python3\n"
FILES = "p42\0\nf3r\0tREG\0n/Users/PRIVATE_USER/dataset.csv\0"
PORT = "p42\0cpython3\0f4u\0PTCP\0n127.0.0.1:8080\0TST=LISTEN\0"


def checking_battery():
    return porkyhud.BatteryInfo(None, False, "Power", "checking", "checking", False,
                               False, False, None, None, None, None, None, None, None, None, "")


def snapshot_fixture():
    return {
        "generated_at": "2026-10-08T10:00:00-0500",
        "read": {"severity": "UNKNOWN", "status": "INCOMPLETE"},
        "system": {"hostname": "PRIVATE_HOST", "chip": "Test Mac"},
        "cpu": {"average_percent": 25.0, "cores": []},
        "memory": porkyhud._snapshot_memory_projection({"total": 4096, "used": 1024, "swap_used": 0, "swap_total": 2048}),
        "disk": {"capacity": {"free": 123456, "mount": "/Users/PRIVATE_USER"}},
        "battery": {"percent": None, "charger_name": "PRIVATE_CHARGER"},
        "processes": [{"pid": 42, "command": "python --token PRIVATE_TOKEN"}],
        "connections": [{"remote": "PRIVATE_ENDPOINT"}],
    }


class ProcessBridgeTests(unittest.TestCase):
    def test_extended_process_collection_preserves_arguments_and_start_identity(self):
        rows = porkyhud.parse_extended_process_rows(EXTENDED)
        self.assertEqual(rows[0].ppid, 1)
        self.assertEqual(rows[0].rss_bytes, 2097152)
        self.assertEqual(rows[0].start_id, START)
        self.assertIn("train.py --token PRIVATE_TOKEN", rows[0].command)
        projected = process_projection(rows, porkyhud.process_identity)
        self.assertEqual(projected[0]["start_id"], START)
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(projected))
        self.assertNotIn("command", projected[0])

    def test_extended_table_failure_is_atomic(self):
        for suffix in (EXTENDED.replace("1 2048 Thu", "-1 2048 Thu"),
                       EXTENDED.replace("1 2048 Thu", "1 -1 Thu"),
                       EXTENDED.replace("10:00:00", "99:00:00"), EXTENDED):
            with self.assertRaises(ValueError):
                porkyhud.parse_extended_process_rows(EXTENDED + suffix)

    def test_legacy_rows_retain_unknown_identity_and_memory(self):
        rows = porkyhud.parse_process_rows("42 2.0 1.0 S 00:20 python3\n")
        normalized = process_projection(rows, porkyhud.process_identity)
        self.assertIsNone(normalized[0]["start_id"])
        self.assertIsNone(normalized[0]["rss_bytes"])
        report = workload_report(42, normalized)
        self.assertIn(report.data["start_id"], (None, ""))
        self.assertIsNone(report.data["rss_bytes_sum"])


class ObservationBridgeTests(unittest.TestCase):
    def sample(self, frame=None, **kwargs):
        return live_sample(frame or make_health_frame_for_test(), {"used": 1024, "total": 4096},
                           checking_battery(), porkyhud.default_sensor_info(),
                           porkyhud.DiskActivity(source="unavailable"), None, None, 5, **kwargs)

    def test_real_health_frame_values_and_health_status_round_trip(self):
        frame = make_health_frame_for_test("memory_critical")
        sample = self.sample(frame, statuses={"memory.stats": "LIVE", "battery": "CHECKING", "sensors": "CHECKING", "disk.activity": "CHECKING"})
        validated = validate_sample(sample)
        self.assertEqual(validated["health"], {"severity": "CRITICAL", "readiness": "LIVE"})
        self.assertEqual(validated["metrics"]["memory_pressure"]["value"], 4)
        self.assertEqual(validated["metrics"]["cpu_pct"]["value"], frame.cpu_percent)
        self.assertEqual(validated["metrics"]["disk_free_bytes"]["value"], frame.disk_capacity.free_bytes)

    def test_source_failure_and_age_survive_retained_reading_projection(self):
        sample = self.sample(statuses={"memory.stats": "STALE", "battery": "CHECKING", "sensors": "ERROR", "disk.activity": "CHECKING"},
                             revisions={"memory.stats": 7}, ages={"memory.stats": 20})
        metric = validate_sample(sample)["metrics"]["memory_used_bytes"]
        self.assertEqual(metric["value"], 1024)
        self.assertEqual(metric["status"], "STALE")
        self.assertEqual(metric["revision"], 7)
        self.assertEqual(metric["age_s"], 20)
        self.assertGreater(metric["age_s"], metric["max_gap_s"])
        self.assertEqual(sample["metrics"]["cpu_temp_c"]["status"], "ERROR")

    def test_checking_sources_do_not_fabricate_battery_or_network_readings(self):
        sample = self.sample(make_health_frame_for_test("checking"), statuses={"battery": "CHECKING"})
        metrics = sample["metrics"]
        for key in ("cpu_pct", "memory_pressure", "disk_free_bytes", "battery_pct", "battery_present", "network_down_bytes_per_s", "network_up_bytes_per_s"):
            self.assertIsNone(metrics[key]["value"], key)
        self.assertEqual(metrics["cpu_pct"]["status"], "CHECKING")
        self.assertEqual(sample["health"]["severity"], "UNKNOWN")

    def test_unavailable_activity_is_not_reported_as_measured_idle_zero(self):
        sample = self.sample(statuses={"disk.activity": "LIVE"})
        self.assertIsNone(sample["metrics"]["disk_bytes_per_s"]["value"])
        self.assertNotEqual(sample["metrics"]["disk_bytes_per_s"]["status"], "LIVE")

    def test_unavailable_battery_sentinel_does_not_affirm_a_batteryless_mac(self):
        battery = replace(checking_battery(), state="unknown", remaining="unknown")
        sample = live_sample(make_health_frame_for_test(), {}, battery, porkyhud.default_sensor_info(),
                             porkyhud.DiskActivity(), None, None, 5, statuses={"battery": "LIVE"})
        self.assertIsNone(sample["metrics"]["battery_present"]["value"])

    def test_snapshot_export_reads_the_actual_legacy_memory_fields(self):
        sample = snapshot_sample(snapshot_fixture())
        self.assertEqual(sample["metrics"]["memory_used_bytes"]["value"], 1024)
        self.assertEqual(sample["metrics"]["memory_total_bytes"]["value"], 4096)
        self.assertEqual(sample["metrics"]["memory_swap_used_bytes"]["value"], 0)
        self.assertEqual(sample["metrics"]["disk_free_bytes"]["value"], 123456)
        self.assertIsNone(sample["metrics"]["battery_pct"]["value"])
        self.assertEqual(sample["metrics"]["battery_pct"]["status"], "UNAVAILABLE")
        validate_sample(sample)


class HeadlessRecorderTests(unittest.TestCase):
    def fixtures(self, battery=None, *, empty=False):
        coordinator = mock.Mock()
        coordinator.tick.return_value = make_health_frame_for_test()
        pollers = []
        values = {porkyhud.memory_detail: {"used": 1024, "total": 4096},
                  porkyhud.battery_info: battery or checking_battery(), porkyhud.network_bytes: (100, 200),
                  porkyhud.disk_activity: porkyhud.DiskActivity(bytes_per_sec=10), porkyhud.sensor_info: porkyhud.default_sensor_info()}
        def factory(interval, collector, *args, **kwargs):
            poller = mock.Mock()
            poller.sample_snapshot.return_value = porkyhud.DescriptiveSample(None, 0, "", 0, 0) if empty else porkyhud.DescriptiveSample(values[collector], 0, "", 1, 20)
            pollers.append(poller)
            return poller
        return coordinator, pollers, factory

    def test_opt_in_recording_omits_retained_process_rows_from_a_failed_source(self):
        coordinator, _pollers, factory = self.fixtures()
        coordinator.process_rows.return_value = porkyhud.parse_extended_process_rows(EXTENDED)
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=20):
            stream = porkyhud._record_stream(10, include_processes=True)
            try:
                sample = next(stream)
            finally:
                stream.close()
        self.assertEqual(sample["processes"], [])
        self.assertEqual(sample["process_source"]["status"], "CHECKING")
        coordinator.process_rows.assert_not_called()

    def test_opt_in_recording_keeps_fresh_process_identity_without_commands(self):
        coordinator, _pollers, factory = self.fixtures()
        frame = coordinator.tick.return_value
        views = dict(frame.source_views)
        views[porkyhud.SourceId.PROCESS_TABLE] = replace(
            views[porkyhud.SourceId.PROCESS_TABLE], status=porkyhud.SourceViewStatus.LIVE,
            age_s=0.5, last_valid_revision=7)
        coordinator.tick.return_value = replace(frame, source_views=views)
        coordinator.process_rows.return_value = porkyhud.parse_extended_process_rows(EXTENDED)
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=20):
            stream = porkyhud._record_stream(10, include_processes=True)
            try:
                sample = next(stream)
            finally:
                stream.close()
        self.assertEqual(sample["processes"][0]["start_id"], START)
        self.assertEqual(sample["process_source"]["revision"], 7)
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(sample))

    def test_failed_cpu_attempt_emits_a_frame_and_clears_a_personal_watch(self):
        from porkyhud_tools.profiles import WatchEngine, WatchRule
        coordinator, _pollers, factory = self.fixtures()
        frame = coordinator.tick.return_value
        views = dict(frame.source_views)
        views[porkyhud.SourceId.CPU_USAGE] = replace(views[porkyhud.SourceId.CPU_USAGE],
                                                   status=porkyhud.SourceViewStatus.ERROR, decision_ready=False)
        coordinator.tick.side_effect = [frame, replace(frame, source_views=views, cpu_percent=None)]
        current = [20]
        watches = WatchEngine([WatchRule("cpu_pct", ">", 0, 0)])
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", side_effect=lambda: current[0]), \
                mock.patch.object(porkyhud.time, "sleep"):
            stream = porkyhud._record_stream(10)
            try:
                initial = next(stream)
                self.assertTrue(watches.update(initial))
                current[0] = 21
                failed = next(stream)
                self.assertEqual(failed["metrics"]["cpu_pct"]["status"], "ERROR")
                self.assertEqual(failed["metrics"]["cpu_pct"]["revision"], initial["metrics"]["cpu_pct"]["revision"])
                self.assertEqual(watches.update(failed), ())
            finally:
                stream.close()

    def test_closing_a_headless_generator_closes_all_existing_pollers(self):
        coordinator, pollers, factory = self.fixtures()
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=20), \
                mock.patch.object(porkyhud.time, "sleep"):
            stream = porkyhud._record_stream(10)
            sample = next(stream)
            validate_sample(sample)
            stream.close()
        coordinator.close.assert_called_once()
        self.assertEqual(len(pollers), 5)
        for poller in pollers:
            poller.close.assert_called_once()

    def test_initial_headless_sample_starts_no_synchronous_battery_fallback(self):
        coordinator, pollers, factory = self.fixtures(empty=True)
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=20), \
                mock.patch.object(porkyhud, "_snapshot_battery_info", side_effect=AssertionError("Unexpected duplicate battery collection")) as fallback:
            stream = porkyhud._record_stream(10)
            try:
                sample = next(stream)
                self.assertIsNone(sample["metrics"]["battery_present"]["value"])
                self.assertIsNone(sample["metrics"]["disk_bytes_per_s"]["value"])
                self.assertEqual(sample["metrics"]["battery_pct"]["status"], "CHECKING")
            finally:
                stream.close()
        fallback.assert_not_called()
        for poller in pollers:
            poller.close.assert_called_once()

    def test_interrupt_during_sampling_closes_coordinator_and_all_pollers(self):
        coordinator, pollers, factory = self.fixtures()
        coordinator.tick.side_effect = KeyboardInterrupt
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=20):
            with self.assertRaises(KeyboardInterrupt):
                next(porkyhud._record_stream(10))
        coordinator.close.assert_called_once()
        for poller in pollers:
            poller.close.assert_called_once()

    def test_retained_reading_age_does_not_reset_after_a_failed_attempt(self):
        coordinator, pollers, original = self.fixtures()
        def factory(interval, collector, *args, **kwargs):
            poller = original(interval, collector, *args, **kwargs)
            if collector is porkyhud.memory_detail:
                poller.sample_snapshot.return_value = porkyhud.DescriptiveSample({"used": 1024, "total": 4096}, 100, "collector failed", 1, 20)
            return poller
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=20):
            stream = porkyhud._record_stream(10)
            try:
                sample = next(stream)
                metric = sample["metrics"]["memory_used_bytes"]
                self.assertEqual(metric["age_s"], 100)
                self.assertEqual(metric["status"], "ERROR")
            finally:
                stream.close()

    def test_network_failure_resets_the_headless_rate_baseline_without_a_new_revision(self):
        coordinator, pollers, original = self.fixtures()
        frames = []
        for revision in range(1, 5):
            frame = make_health_frame_for_test()
            views = dict(frame.source_views)
            views[porkyhud.SourceId.CPU_USAGE] = replace(views[porkyhud.SourceId.CPU_USAGE], last_valid_revision=revision)
            frames.append(replace(frame, source_views=views))
        coordinator.tick.side_effect = frames
        network = iter((porkyhud.DescriptiveSample((100, 200), 0, "", 1, 20),
                        porkyhud.DescriptiveSample((200, 300), 0, "", 2, 21),
                        porkyhud.DescriptiveSample((200, 300), 1, "collector failed", 2, 22),
                        porkyhud.DescriptiveSample((300, 400), 0, "", 3, 23)))
        def factory(interval, collector, *args, **kwargs):
            poller = original(interval, collector, *args, **kwargs)
            if collector is porkyhud.network_bytes:
                poller.sample_snapshot.side_effect = lambda now: next(network)
            return poller
        current = [20]
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", side_effect=lambda: current[0]), \
                mock.patch.object(porkyhud.time, "sleep"):
            stream = porkyhud._record_stream(10)
            try:
                initial = next(stream)
                current[0] = 21
                measured = next(stream)
                current[0] = 22
                failed = next(stream)
                current[0] = 23
                recovered = next(stream)
                self.assertIsNone(initial["metrics"]["network_down_bytes_per_s"]["value"])
                self.assertEqual(measured["metrics"]["network_down_bytes_per_s"]["value"], 100)
                self.assertEqual(failed["metrics"]["network_down_bytes_per_s"]["status"], "ERROR")
                self.assertIsNone(recovered["metrics"]["network_down_bytes_per_s"]["value"])
            finally:
                stream.close()

    def test_coordinator_cleanup_error_does_not_skip_descriptive_cleanup(self):
        coordinator, pollers, factory = self.fixtures()
        coordinator.close.side_effect = RuntimeError("coordinator close failed")
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", side_effect=factory), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=20):
            stream = porkyhud._record_stream(10)
            next(stream)
            with self.assertRaises(RuntimeError):
                stream.close()
        for poller in pollers:
            poller.close.assert_called_once()

    def test_headless_cli_interrupt_finishes_recording_and_closes_the_stream(self):
        completed = []
        sample = snapshot_sample(snapshot_fixture())
        def interrupted(duration):
            try:
                yield sample
                raise KeyboardInterrupt
            finally:
                completed.append(True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            with mock.patch.object(porkyhud, "_record_stream", side_effect=interrupted), \
                    mock.patch.object(porkyhud, "collect_snapshot") as capture, \
                    redirect_stdout(io.StringIO()) as output:
                code = porkyhud.main(["--record", str(path), "--duration", "10", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(completed, [True])
            self.assertEqual(len(load_session(path)["samples"]), 1)
            self.assertEqual(json.loads(output.getvalue())["schema_version"], 1)
            capture.assert_not_called()

    def test_headless_cli_closes_the_collector_stream_when_the_recorder_stops_accepting(self):
        class Stream:
            closed = False
            def __iter__(self):
                return self
            def __next__(self):
                return snapshot_sample(snapshot_fixture())
            def close(self):
                self.closed = True
        stream = Stream()
        recorder = mock.Mock()
        recorder.append.return_value = False
        recorder.stop.return_value = {"complete": False}
        with mock.patch.object(porkyhud, "_record_stream", return_value=stream), \
                mock.patch("porkyhud_tools.sessions.SessionRecorder", return_value=recorder), \
                redirect_stdout(io.StringIO()):
            code = porkyhud.main(["--record", "/unused/test.jsonl", "--duration", "10", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(stream.closed)
        recorder.stop.assert_called_once()


class ToolCliBridgeTests(unittest.TestCase):
    def test_process_cli_uses_shared_inspector_without_snapshot_or_terminal(self):
        replies = iter((ProbeResult(IDENTITY), ProbeResult(FILES), ProbeResult(IDENTITY)))
        runner = mock.Mock(side_effect=lambda argv, timeout: next(replies))
        rows = porkyhud.parse_extended_process_rows(EXTENDED)
        with mock.patch.object(porkyhud, "_tool_runner", return_value=runner), \
                mock.patch.object(porkyhud, "collect_process_rows", return_value=SimpleNamespace(value=rows)), \
                mock.patch.object(porkyhud, "collect_snapshot") as snapshot, \
                mock.patch.object(porkyhud, "terminal_ui_problem") as terminal, \
                redirect_stdout(io.StringIO()) as output:
            code = porkyhud.main(["--tool", "process", "--target", "42", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["status"], "LIVE")
        self.assertEqual(payload["data"]["identity"]["start_id"], START)
        self.assertNotIn("PRIVATE_TOKEN", output.getvalue())
        self.assertNotIn("PRIVATE_USER", output.getvalue())
        snapshot.assert_not_called()
        terminal.assert_not_called()

    def test_port_cli_does_not_collect_an_unrelated_process_or_snapshot_table(self):
        runner = mock.Mock(return_value=ProbeResult(PORT))
        with mock.patch.object(porkyhud, "_tool_runner", return_value=runner), \
                mock.patch.object(porkyhud, "collect_process_rows") as process, \
                mock.patch.object(porkyhud, "collect_snapshot") as snapshot, \
                redirect_stdout(io.StringIO()) as output:
            code = porkyhud.main(["--tool", "port", "--target", "8080", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema_version"], 1)
        self.assertFalse(payload["complete"])
        self.assertIn("127.0.0.1:8080", output.getvalue())
        process.assert_not_called()
        snapshot.assert_not_called()
        self.assertEqual(runner.call_count, 1)

    def test_snapshot_export_writes_only_the_aggregate_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with mock.patch.object(porkyhud, "collect_snapshot", return_value=snapshot_fixture()), \
                    mock.patch.object(porkyhud, "terminal_ui_problem") as terminal, \
                    redirect_stdout(io.StringIO()) as output:
                code = porkyhud.main(["--export", str(path), "--json"])
            self.assertEqual(code, 0)
            saved = path.read_text()
            for value in ("PRIVATE_HOST", "PRIVATE_USER", "PRIVATE_TOKEN", "PRIVATE_ENDPOINT", "PRIVATE_CHARGER"):
                self.assertNotIn(value, saved)
            payload = json.loads(saved)
            self.assertEqual(payload["sample"]["processes"], [])
            self.assertEqual(payload["sample"]["metrics"]["memory_used_bytes"]["value"], 1024)
            self.assertEqual(payload["sample"]["health"], {"severity": "UNKNOWN", "readiness": "INCOMPLETE"})
            terminal.assert_not_called()


class NavigationContractTests(unittest.TestCase):
    def test_reopening_a_filtered_menu_highlights_its_actual_first_match(self):
        config = porkyhud.HudConfig(show_menu=True, menu_query="counters", menu_index=0)
        screen = FakeScreen(28, 100)
        with mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_explore_menu(screen, config)
        selected = [text for _y, _x, text, _limit, attr in screen.writes
                    if attr & porkyhud.curses.A_BOLD and "Interface counters" in text]
        self.assertEqual(len(selected), 1)

    def test_wrapped_network_rows_keep_the_selected_interface_visible(self):
        from porkyhud_inventory import NetworkInterface, NetworkInventory
        interfaces = tuple(NetworkInterface("en%d" % index, "active", ("192.0.2.%d" % index,),
                                            ("2001:db8::%d" % index,), medium="Ethernet " * 12)
                           for index in range(12))
        result = NetworkInventory(InventoryStatus.LIVE, interfaces, complete=True)
        config = porkyhud.HudConfig(inventory_cursor=11)
        screen = FakeScreen(26, 80)
        with mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_inventory_body(screen, "network", 4, 18, 80, result, 0, "", 0, config=config)
        selected = [text for _y, _x, text, _limit, attr in screen.writes if attr & porkyhud.curses.A_REVERSE]
        self.assertTrue(any("en11" in line for line in selected))
        self.assertTrue(any("192.0.2.11" in line for line in selected))
        self.assertFalse(any("en10" in line for line in selected))
        self.assertEqual(config.inventory_cursor, 11)

    def test_standalone_replay_preserves_scroll_and_uses_quit_copy(self):
        session = {"samples": [snapshot_sample(snapshot_fixture())], "complete": True, "truncated": False}
        session["samples"][0]["metrics"].update({"reading_%d" % index: {"value": index, "unit": "bytes", "status": "LIVE"}
                                                for index in range(25)})
        screen = FakeScreen(24, 80, keys=[porkyhud.curses.KEY_NPAGE, -1, ord("q")])
        screen.timeout = mock.Mock()
        scrolls = []
        draw = porkyhud.draw_workbench
        def capture(screen, state):
            scrolls.append(state.scroll)
            draw(screen, state)
        with mock.patch.object(porkyhud, "terminal_ui_problem", return_value=None), \
                mock.patch.object(porkyhud, "enforce_dark_terminal"), \
                mock.patch.object(porkyhud, "restore_terminal_colors"), \
                mock.patch.object(porkyhud, "init_colors"), \
                mock.patch.object(porkyhud, "color_pair", return_value=0), \
                mock.patch.object(porkyhud, "draw_workbench", side_effect=capture), \
                mock.patch.object(porkyhud.curses, "wrapper", side_effect=lambda render: render(screen)):
            self.assertTrue(porkyhud._run_replay(session))
        self.assertEqual(scrolls[:2], [0, 10])
        self.assertGreater(scrolls[2], 0)
        self.assertTrue(any("Esc quit" in text for _y, _x, text, _limit, _attr in screen.writes))

    def test_replay_termination_restores_palette_and_signal_handlers(self):
        import signal
        previous = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(porkyhud, "terminal_ui_problem", return_value=None), \
                mock.patch.object(porkyhud, "enforce_dark_terminal"), \
                mock.patch.object(porkyhud, "restore_terminal_colors") as restore, \
                mock.patch.dict(porkyhud.os.environ, {}, clear=True), \
                mock.patch.object(porkyhud.curses, "wrapper", side_effect=porkyhud.TerminationSignal(signal.SIGTERM)):
            with self.assertRaises(SystemExit) as raised:
                porkyhud._run_replay({"samples": []})
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        restore.assert_called_once()

    def test_report_number_guard_handles_an_unrepresentable_integer(self):
        from porkyhud_tools.model import finite_number
        self.assertIsNone(finite_number(10**1000))
        self.assertEqual(finite_number(42), 42)


if __name__ == "__main__":
    unittest.main()
