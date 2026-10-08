"""Optional workbench state and CLI composition, with no native actions."""

import argparse
from concurrent.futures import Future, TimeoutError
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from porkyhud_inventory import ProbeResult
from porkyhud_tools import cli, controller, network, profiles, service, sessions
from porkyhud_tools.model import ToolReport


def reading(elapsed=0, value=20):
    return {"schema_version": 1, "timestamp": "2026-10-08T12:00:00+00:00",
            "elapsed_s": elapsed,
            "metrics": {"cpu_pct": {"value": value, "unit": "%", "status": "LIVE"}},
            "processes": [], "events": [],
            "health": {"severity": "UNKNOWN", "readiness": "INCOMPLETE"}}


def arguments(values):
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", choices=("devices", "network", "connections"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--search", default="")
    parser.add_argument("--state", choices=("all", "listeners", "established", "other"))
    cli.add_arguments(parser)
    return parser.parse_args(values), parser


class WorkbenchFlowTests(unittest.TestCase):
    def setUp(self):
        self.runner = mock.Mock(return_value=ProbeResult())
        self.apply = mock.Mock()
        self.workbench = controller.Workbench(lambda _cancel: self.runner, lambda: {"mode": "balanced"}, self.apply)
        self.addCleanup(self.workbench.close)

    def deliver(self, report, effect=None):
        future = Future()
        future.set_result((report, effect))
        self.workbench._job_generation = self.workbench._generation
        self.workbench._future = future
        self.workbench.poll()

    def test_prompt_text_precedes_quit_navigation_and_pause_shortcuts(self):
        self.workbench.open("mark")
        for key in ("q", "5", "r", " ", "é"):
            self.assertTrue(self.workbench.handle_key(key))
        self.assertEqual(self.workbench.text, "q5r é")
        self.assertFalse(self.workbench.paused)
        self.workbench.handle_key(127)
        self.assertEqual(self.workbench.text, "q5r ")
        self.workbench.handle_key(21)
        self.assertEqual(self.workbench.text, "")
        self.workbench.handle_key("q")
        with mock.patch.object(self.workbench, "_submit") as submit:
            self.workbench.handle_key("\n")
        submit.assert_called_once_with("mark", "q")

    def test_fresh_process_inspector_pins_identity_when_the_cached_table_is_unavailable(self):
        report = ToolReport("process", "PARTIAL", "Process details", data={"identity": {"pid": 42, "start_id": "start-a"}})
        with mock.patch.object(controller, "ToolService") as service:
            service.return_value.run.return_value = report
            result, effect = self.workbench._execute("process", "42", (), (), {})
        self.assertIs(result, report)
        self.assertEqual(effect, ("pin", (42, "start-a", None)))

    def test_escape_closes_prompt_and_invalidates_a_pending_completion(self):
        self.workbench.open("port")
        old_generation = self.workbench._generation
        self.workbench.handle_key(27)
        self.assertFalse(self.workbench.visible)
        self.assertFalse(self.workbench.editing)
        self.assertTrue(self.workbench._cancel.is_set())
        self.assertEqual(self.workbench._generation, old_generation + 1)
        future = Future()
        future.set_result((ToolReport("late", "LIVE", "outdated"), ("pin", (42, "old", None))))
        self.workbench._future = future
        self.workbench._job_generation = old_generation
        self.workbench.poll()
        self.assertIsNone(self.workbench.pinned_pid)
        self.assertNotEqual(self.workbench.report.summary, "outdated")

    def test_escape_stops_a_cancelled_new_recorder_instead_of_leaking_it(self):
        recorder = mock.Mock()
        future = Future()
        future.set_result((ToolReport("Recording", "RECORDING", "started"), ("record", recorder)))
        self.workbench.visible = True
        self.workbench._future = future
        self.workbench.handle_key(27)
        self.workbench.poll()
        if self.workbench._future is not None:
            self.workbench._future.result(timeout=1)
            self.workbench.poll()
        self.assertIn(mock.call(timeout_s=0), recorder.stop.call_args_list)
        self.assertIn(mock.call(), recorder.stop.call_args_list)
        self.assertIsNone(self.workbench.recording)

    def test_cancelled_record_join_runs_off_hud_thread(self):
        entered, release = threading.Event(), threading.Event()
        caller_thread = threading.get_ident()
        joined_threads = []

        def stop(timeout_s=1.5):
            if timeout_s == 0:
                return {"writer_finished": False}
            joined_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Cleanup fixture did not release")
            return {"writer_finished": True}

        recorder = mock.Mock(stop=mock.Mock(side_effect=stop))
        future = Future()
        future.set_result((ToolReport("Recording", "RECORDING", "started"), ("record", recorder)))
        self.workbench.visible = True
        self.workbench._future = future
        self.workbench.handle_key(27)
        try:
            self.workbench.poll()
            self.assertTrue(entered.wait(1))
            self.assertTrue(joined_threads)
            self.assertNotIn(caller_thread, joined_threads)
            self.assertFalse(self.workbench.visible)
            self.assertIsNone(self.workbench.recording)
            self.assertFalse(self.workbench._future.done())
        finally:
            release.set()
            if self.workbench._future is not None:
                self.workbench._future.result(timeout=1)
                self.workbench.poll()

    def test_record_created_after_close_timeout_stops_without_later_poll(self):
        entered, release = threading.Event(), threading.Event()
        created = []
        original = sessions.SessionRecorder

        def delayed_recorder(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Recorder fixture did not release")
            recorder = original(*args, **kwargs)
            created.append(recorder)
            return recorder

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(sessions, "SessionRecorder", side_effect=delayed_recorder):
            self.workbench.open("record", str(Path(directory) / "late.jsonl"))
            self.assertTrue(entered.wait(1))
            future = self.workbench._future
            try:
                with mock.patch.object(future, "result", side_effect=TimeoutError()):
                    self.workbench.close()
                self.assertIsNone(self.workbench.recording)
            finally:
                release.set()
            future.result(timeout=1)
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0]._stop_event.is_set())
            self.assertFalse(created[0]._worker.is_alive())
            self.assertIsNone(self.workbench.recording)

    def test_invalid_record_seed_stops_created_writer_on_worker_error(self):
        recorder = mock.Mock()
        recorder.append.side_effect = ValueError("Invalid sample")
        self.workbench.latest = reading()
        with mock.patch.object(sessions, "SessionRecorder", return_value=recorder), self.assertRaises(ValueError):
            self.workbench._execute("record", "chosen.jsonl", (), (), {})
        recorder.stop.assert_called_once_with()

    def test_cancelled_stop_record_still_clears_finished_recording(self):
        recorder = mock.Mock()
        self.workbench.recording = recorder
        future = Future()
        future.set_result((ToolReport("Recording", "SAVED", "stopped"), ("stop_record", None)))
        self.workbench.visible = True
        self.workbench._future = future
        self.workbench.handle_key(27)
        self.workbench.poll()
        self.assertIsNone(self.workbench.recording)

    def test_busy_action_never_queues_a_second_worker(self):
        unfinished = Future()
        self.workbench._future = unfinished
        self.workbench._submit("port", "443")
        self.assertEqual(self.workbench.report.status, "BUSY")
        self.assertIs(self.workbench._future, unfinished)
        self.assertIsNone(self.workbench._executor)
        unfinished.set_result((ToolReport("port", "LIVE", "done"), None))

    def test_actual_worker_has_one_slot_and_poll_applies_result(self):
        started, release = threading.Event(), threading.Event()

        def execute(*_):
            started.set()
            if not release.wait(1):
                raise RuntimeError("test worker timeout")
            return ToolReport("mock", "LIVE", "finished"), None

        with mock.patch.object(self.workbench, "_execute", side_effect=execute):
            self.workbench.open("interfaces")
            self.assertTrue(started.wait(1))
            first = self.workbench._future
            self.workbench.open("interfaces")
            self.assertEqual(self.workbench.report.status, "BUSY")
            self.assertIs(self.workbench._future, first)
            release.set()
            first.result(timeout=1)
            self.workbench.poll()
        self.assertEqual(self.workbench.report.summary, "finished")

    def test_timeline_updates_after_observation_without_more_native_probes(self):
        self.workbench.observe(reading(0), ())
        self.deliver(self.workbench.history.to_report())
        self.workbench.visible = True
        self.workbench.observe(reading(1, 50), ())
        if self.workbench._future is not None:
            self.workbench._future.result(timeout=1)
            self.workbench.poll()
        self.assertEqual(len(self.workbench.report.data["samples"]), 2)
        self.assertEqual(self.workbench.report.data["samples"][-1]["metrics"]["cpu_pct"]["value"], 50)
        self.runner.assert_not_called()

    def test_pause_freezes_display_but_collection_continues(self):
        self.workbench.observe(reading(0), ())
        self.deliver(self.workbench.history.to_report())
        self.workbench.visible = True
        original = self.workbench.report
        self.workbench.handle_key(" ")
        self.workbench.observe(reading(1), ())
        self.assertTrue(self.workbench.paused)
        self.assertIs(self.workbench.report, original)
        self.assertEqual(len(self.workbench.history.samples), 2)

    def test_replay_boundaries_and_escape_restore_live_without_collection(self):
        session = {"samples": [reading(0), reading(1)], "complete": True}
        self.deliver(controller.replay_report(session, 0), ("replay", session))
        self.workbench.visible = True
        for key in (260, 260):
            self.workbench.handle_key(key)
        self.assertEqual(self.workbench.replay_index, 0)
        for key in (261, 261, 261):
            self.workbench.handle_key(key)
        self.assertEqual(self.workbench.replay_index, 1)
        self.assertIn("frame 2/2", self.workbench.report.summary)
        self.workbench.handle_key(27)
        self.assertIsNone(self.workbench.replay)
        self.assertFalse(self.workbench.visible)
        self.runner.assert_not_called()

    def test_empty_replay_can_be_opened_and_closed(self):
        session = {"samples": [], "complete": True}
        self.deliver(controller.replay_report(session), ("replay", session))
        self.workbench.visible = True
        self.workbench.handle_key(261)
        self.assertEqual(self.workbench.report.status, "EMPTY")
        self.workbench.handle_key(27)
        self.assertIsNone(self.workbench.replay)

    def test_loaded_profile_applies_settings_and_replaces_watch_state(self):
        settings = {"mode": "processes", "searches": {"processes": "worker"},
                    "pinned_pid": 42, "pinned_start_id": "start-a",
                    "watches": [{"field": "cpu_pct", "operator": ">", "threshold": 90, "duration_s": 5}]}
        self.deliver(ToolReport("Workspace", "LOADED", "loaded"), ("profile", ("development", settings)))
        self.apply.assert_called_once_with(settings)
        self.assertEqual(self.workbench.profile_name, "development")
        self.assertEqual(self.workbench.pinned_start_id, "start-a")
        self.assertEqual(len(self.workbench.watch_rules), 1)
        self.assertEqual(self.workbench.watch_rules[0].field, "cpu_pct")

    def test_contextual_job_refresh_rejects_a_reused_pinned_pid(self):
        self.workbench.visible = True
        self.workbench.pinned_pid, self.workbench.pinned_start_id = 42, "start-a"
        self.workbench.rows = ({"pid": 42, "start_id": "start-b"},)
        for key in ("r", "o"):
            with mock.patch.object(self.workbench, "open") as action:
                self.workbench.handle_key(key)
            action.assert_not_called()
            self.assertIn(self.workbench.report.status, ("PID CHANGED", "UNAVAILABLE"))
            self.assertEqual(self.workbench.pinned_start_id, "start-a")

    def test_contextual_job_refresh_retains_explicit_progress_path_for_same_identity(self):
        self.workbench.visible = True
        self.workbench.pinned_pid, self.workbench.pinned_start_id = 42, "start-a"
        self.workbench.job_progress = "/chosen/progress.json"
        self.workbench.rows = ({"pid": 42, "start_id": "start-a"},)
        with mock.patch.object(self.workbench, "open") as action:
            self.workbench.handle_key("r")
        action.assert_called_once_with("job", "42 | /chosen/progress.json")

    def test_support_export_omits_private_process_fields_and_addresses(self):
        private = reading(0)
        private["hostname"] = "PRIVATE_HOST"
        private["addresses"] = ["PRIVATE_ADDRESS"]
        rows = ({"pid": 42, "start_id": "start-a", "ppid": 1, "name": "PRIVATE_PROCESS",
                 "cpu_pct": 10, "rss_bytes": 1000, "state": "S", "command": "PRIVATE_COMMAND"},)
        self.workbench.observe(private, rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            report, effect = self.workbench._execute("export", str(path), self.workbench.rows, self.workbench.history.samples, {})
            text = path.read_text()
        self.assertEqual(report.status, "SAVED")
        self.assertIsNone(effect)
        self.assertNotIn("PRIVATE_", text)
        self.assertNotIn(str(path), text)

    def test_process_trends_stay_selected_after_job_and_refresh_without_probes(self):
        process = {"pid": 42, "start_id": "start-a", "ppid": 1, "name": "worker",
                   "cpu_pct": 10, "rss_bytes": 1000, "state": "S"}
        self.workbench.observe(reading(0), (process,), observed_mono_s=0)
        self.workbench.visible = True
        self.workbench.pinned_pid, self.workbench.pinned_start_id = 42, "start-a"
        self.workbench.active_key, self.workbench.active_value = "job", "42 | /chosen/progress.json"
        self.workbench.handle_key("v")
        self.workbench._future.result(timeout=1)
        self.workbench.poll()
        self.assertEqual(self.workbench.active_key, "trends")
        self.assertEqual(self.workbench.report.tool, "process-trends")
        self.workbench.observe(reading(1), (process,), observed_mono_s=1)
        self.workbench._future.result(timeout=1)
        self.workbench.poll()
        self.assertEqual(self.workbench.active_key, "trends")
        self.assertEqual(self.workbench.report.tool, "process-trends")
        self.assertEqual(len(self.workbench.report.rows), 2)
        self.runner.assert_not_called()

    def test_record_opt_in_summary_names_included_process_details(self):
        recorder = mock.Mock()
        self.workbench.include_processes = True
        with mock.patch.object(sessions, "SessionRecorder", return_value=recorder):
            report, effect = self.workbench._execute("record", "chosen.jsonl", (), (), {})
        self.assertIn("selected process details included", report.summary)
        self.assertNotIn("private fields omitted", report.summary)
        self.assertEqual(effect, ("record", recorder))


class ServiceAndCliTests(unittest.TestCase):
    def test_target_split_and_validation_accept_ipv6_and_reject_argv_injection(self):
        self.assertEqual(service.split_target("[2001:db8::1]:8443"), ("2001:db8::1", 8443))
        self.assertEqual(service.split_target("fe80::1%en0"), ("fe80::1%en0", 443))
        runner = mock.Mock()
        for value in ("-n:443", "https://example.invalid:443", "[::1]oops", "example.invalid:0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.ToolService(runner).run("diagnose", value)
        runner.assert_not_called()

    def test_quality_requires_typed_start_in_ui_service(self):
        runner = mock.Mock()
        with mock.patch.object(network, "network_quality", return_value=ToolReport("quality", "LIVE", "done")) as action:
            for value in ("", "start", "yes"):
                self.assertEqual(service.ToolService(runner).run("quality", value).status, "CANCELLED")
            action.assert_not_called()
            self.assertEqual(service.ToolService(runner).run("quality", "START").status, "LIVE")
            action.assert_called_once_with(runner)

    def test_job_dispatch_uses_only_explicit_progress_file(self):
        rows = ({"pid": 42},)
        with mock.patch("porkyhud_tools.processes.watch_job_report", return_value=ToolReport("job", "LIVE", "done")) as action:
            service.ToolService(mock.Mock()).run("job", "42 | /chosen/progress.json", rows=rows)
        action.assert_called_once_with(42, rows, "/chosen/progress.json")

    def test_cli_rejects_missing_targets_nonfinite_duration_and_conflicting_actions(self):
        bad = (("--tool", "port"), ("--target", "443"), ("--progress-file", "p.json"),
               ("--record", "session", "--duration", "nan"),
               ("--tool", "interfaces", "--replay", "session"),
               ("--record", "session", "--save-workspace", "work"),
               ("--record", "session", "--list-workspaces"),
               ("--replay", "session", "--inspect", "network"),
               ("--compare", "a", "b", "--inspect", "devices"),
               ("--export", "report", "--inspect", "connections"),
               ("--tool", "port", "--target", "443", "--snapshot"),
               ("--replay", "session", "--snapshot"),
               ("--export", "report", "--snapshot"))
        for values in bad:
            with self.subTest(values=values), redirect_stderr(io.StringIO()):
                args, parser = arguments(values)
                with self.assertRaises(SystemExit):
                    cli.validate_arguments(args, parser)

    def test_cli_json_tool_report_does_not_start_unrelated_collectors(self):
        args, parser = arguments(("--tool", "port", "--target", "8080", "--json"))
        cli.validate_arguments(args, parser)
        report = ToolReport("port-owner", "LIVE", "No visible owner", complete=False)
        callbacks = {name: mock.Mock(side_effect=AssertionError("unrelated collector")) for name in
                     ("process_rows", "capture_sample", "record_stream", "replay_ui")}
        output = io.StringIO()
        with mock.patch.object(service.ToolService, "run", return_value=report) as action, redirect_stdout(output):
            status = cli.handle(args, runner=mock.Mock(), settings={}, **callbacks)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["schema_version"], 1)
        self.assertFalse(json.loads(output.getvalue())["complete"])
        action.assert_called_once_with("port", "8080", rows=())

    def test_cli_replay_json_starts_no_native_collectors_or_ui(self):
        args, _ = arguments(("--replay", "session", "--json"))
        callbacks = {name: mock.Mock(side_effect=AssertionError("unexpected action")) for name in
                     ("process_rows", "capture_sample", "record_stream", "replay_ui")}
        with mock.patch.object(sessions, "load_session", return_value={"samples": [reading(0)], "complete": True}), redirect_stdout(io.StringIO()) as output:
            status = cli.handle(args, runner=mock.Mock(), settings={}, **callbacks)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "REPLAY")

    def test_cli_quality_flag_is_the_explicit_start_action(self):
        args, _ = arguments(("--tool", "quality", "--json"))
        callbacks = {name: mock.Mock(side_effect=AssertionError("unexpected action")) for name in
                     ("process_rows", "capture_sample", "record_stream", "replay_ui")}
        with mock.patch.object(service.ToolService, "run", return_value=ToolReport("quality", "LIVE", "done")) as action, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.handle(args, runner=mock.Mock(), settings={}, **callbacks), 0)
        action.assert_called_once_with("quality", "START", rows=())

    def test_personal_watch_can_be_parsed_before_first_sample(self):
        if not hasattr(controller, "parse_watch_rule"):
            self.fail("initial watches need a parser independent of live samples")
        rule = controller.parse_watch_rule("cpu_pct >= 90 for 30")
        self.assertEqual(rule, profiles.WatchRule("cpu_pct", ">=", 90, 30))


class HudIntegrationTests(unittest.TestCase):
    def run_mocked_hud(self, config, keys):
        import porkyhud
        from tests.test_porkyhud import FakeScreen, make_health_frame_for_test
        coordinator = mock.Mock()
        coordinator.source_generation.return_value = 0
        coordinator.tick.return_value = make_health_frame_for_test()
        coordinator.per_core.return_value = (10.0,)
        coordinator.process_rows.return_value = ()
        poller = mock.Mock()
        poller.tick.return_value = None
        poller.snapshot.return_value = (None, 0.0, "")
        poller.sample_snapshot.return_value = porkyhud.DescriptiveSample(None, 0.0, "", 0, 0.0)
        screen = FakeScreen(24, 80)
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", return_value=poller), \
                mock.patch.object(porkyhud, "_start_static_discovery", return_value=poller), \
                mock.patch.object(porkyhud, "init_colors"), \
                mock.patch.object(porkyhud, "apply_dark_screen"), \
                mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=21.0), \
                mock.patch.object(porkyhud.time, "sleep"), \
                mock.patch.object(porkyhud, "read_hud_key", side_effect=list(keys)) as input_reader:
            porkyhud.hud(screen, config)
        coordinator.close.assert_called_once_with()
        self.assertEqual(input_reader.call_count, len(keys))
        return screen

    def test_menu_search_q_is_text_and_opens_only_explicit_quality_prompt(self):
        import porkyhud
        config = porkyhud.HudConfig(show_menu=True, menu_search=True, intro_enabled=False)
        self.run_mocked_hud(config, ("q", 10, 10, 27, ord("q")))
        self.assertEqual(config.menu_query, "q")
        self.assertEqual(config.layout_name, "balanced")
        self.assertFalse(config.show_menu)

    def test_empty_menu_search_enter_preserves_selected_view(self):
        import porkyhud
        config = porkyhud.HudConfig(show_menu=True, menu_search=True, menu_query="DOES_NOT_EXIST", intro_enabled=False)
        screen = self.run_mocked_hud(config, (10, 10, 27, ord("q")))
        self.assertEqual(config.layout_name, "balanced")
        self.assertEqual(config.message, "No matching tools")
        self.assertFalse(config.show_menu)
        self.assertIn("OVERVIEW", screen.text())

    def test_result_overlay_wide_character_quit_reaches_global_handler(self):
        import porkyhud
        from tests.test_porkyhud import FakeScreen
        workbench = controller.Workbench(mock.Mock(), lambda: {}, mock.Mock())
        workbench.visible = True
        workbench.report = ToolReport("Details", "LIVE", "selected row")
        coordinator = mock.Mock()
        coordinator.source_generation.return_value = 0
        screen = FakeScreen(24, 80)
        with mock.patch.object(porkyhud, "Workbench", return_value=workbench), \
                mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller"), \
                mock.patch.object(porkyhud, "init_colors"), \
                mock.patch.object(porkyhud, "apply_dark_screen"), \
                mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0), \
                mock.patch.object(porkyhud, "read_hud_key", side_effect=["q", AssertionError("q was not handled")]):
            porkyhud.hud(screen)
        coordinator.close.assert_called_once_with()

    def test_main_workspace_returns_flat_settings_and_explicit_flags_override_it(self):
        import porkyhud
        settings = {"mode": "connections", "theme": "neon-grid", "style": "outline",
                    "motion": "calm", "searches": {"connections": "listener"},
                    "state": "listeners", "sort": "memory", "watches": []}
        with mock.patch.object(profiles, "load_profile", return_value=settings), \
                mock.patch.object(porkyhud, "terminal_ui_problem", return_value=None), \
                mock.patch.object(porkyhud, "enforce_dark_terminal"), \
                mock.patch.object(porkyhud, "restore_terminal_colors"), \
                mock.patch.object(porkyhud.curses, "wrapper", side_effect=lambda callback: callback(object())), \
                mock.patch.object(porkyhud, "hud") as hud, \
                mock.patch.object(porkyhud.signal, "signal"), \
                mock.patch.object(porkyhud.sys.stdout, "isatty", return_value=False):
            status = porkyhud.main(["--workspace", "development", "--mode", "processes", "--style", "ascii", "--motion", "off"])
        self.assertEqual(status, 0)
        config = hud.call_args.args[1]
        self.assertEqual(config.layout_name, "processes")
        self.assertEqual(config.visual_style, "ascii")
        self.assertEqual(config.animation_mode, 0)
        self.assertFalse(config.intro_enabled)
        self.assertEqual(config.searches["connections"], "listener")
        self.assertEqual(config.connection_filter, "listeners")

    def test_headless_missing_battery_uses_typed_fallback_without_native_retry(self):
        import porkyhud
        from tests.test_porkyhud import make_health_frame_for_test
        coordinator = mock.Mock()
        coordinator.tick.return_value = make_health_frame_for_test()
        poller = mock.Mock()
        poller.sample_snapshot.return_value = porkyhud.DescriptiveSample(None, 0, "failed", 1, 0)
        with mock.patch.object(porkyhud, "LiveHealthCoordinator", return_value=coordinator), \
                mock.patch.object(porkyhud, "AsyncPoller", return_value=poller), \
                mock.patch.object(porkyhud.time, "monotonic", return_value=21.0), \
                mock.patch.object(porkyhud, "_snapshot_battery_info", side_effect=AssertionError("duplicate battery probe")) as retry:
            stream = porkyhud._record_stream(1)
            try:
                sample = next(stream)
            finally:
                stream.close()
        retry.assert_not_called()
        self.assertIsNone(sample["metrics"]["battery_pct"]["value"])
        self.assertEqual(sample["metrics"]["battery_pct"]["status"], "ERROR")
        coordinator.close.assert_called_once_with()

    def test_cli_support_snapshot_maps_actual_legacy_memory_keys(self):
        from porkyhud_tools.bridge import snapshot_sample
        snapshot = {"cpu": {"average_percent": 20},
                    "memory": {"used_bytes": 4_000_000_000, "total_bytes": 8_000_000_000,
                               "swap_used_bytes": 1_000_000, "swap_total_bytes": 2_000_000},
                    "battery": {"percent": 60}, "disk": {"capacity": {"free": 100_000_000_000}}}
        sample = snapshot_sample(snapshot)
        self.assertEqual(sample["metrics"]["memory_used_bytes"]["value"], 4_000_000_000)
        self.assertEqual(sample["metrics"]["memory_total_bytes"]["value"], 8_000_000_000)
        self.assertEqual(sample["metrics"]["memory_swap_used_bytes"]["value"], 1_000_000)
        self.assertEqual(sample["metrics"]["memory_used_bytes"]["status"], "LIVE")
        self.assertIsNone(sample["metrics"]["memory_compressed_bytes"]["value"])


if __name__ == "__main__":
    unittest.main()
