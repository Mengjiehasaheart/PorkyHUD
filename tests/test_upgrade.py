"""User-flow contracts for the refined dashboard and on-demand exploration."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from dataclasses import FrozenInstanceError, replace
import io
import json
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import porkyhud
import porkyhud_core as core
from porkyhud_inventory import (
    ConnectionRecord, DeviceRecord, InventoryResult, InventoryStatus,
    NetworkInterface, NetworkInventory,
)
from tests import test_porkyhud as fixtures
from tests.test_porkyhud import FakeScreen, make_health_frame_for_test, settled_sensor


class DashboardHarness(unittest.TestCase):
    def run_dashboard(self, keys, *, config=None, height=22, width=76,
                      unicode_enabled=False, advance=0.1, collect_inventory=False,
                      process_rows=(), static=None, frame=None):
        config = config or porkyhud.HudConfig()
        screen = FakeScreen(height, width, keys=keys)
        captured = []
        screen.on_refresh = lambda: captured.append(tuple(screen.row(y)
                                                          for y in range(height)))
        coordinator = mock.Mock()
        coordinator.source_generation.return_value = 0
        coordinator.tick.return_value = frame or make_health_frame_for_test()
        coordinator.per_core.return_value = (10.0, 20.0)
        coordinator.process_rows.return_value = tuple(process_rows)
        pollers = []

        class Poller:
            def __init__(self, interval, collector, initial=None, **_kwargs):
                self.value = initial
                self.collector = collector
                self.calls = []
                pollers.append(self)

            def tick(self, now, force=False):
                self.calls.append((now, force))
                if collect_inventory and getattr(self.collector, "__name__", "") == "collect_observation":
                    self.value = self.collector()
                return self.value

            def sample_snapshot(self, _now=None):
                return porkyhud.DescriptiveSample(self.value, 0.0, "", 0, 0.0)

            def snapshot(self, _now=None):
                return self.value, 0.0, ""

            def close(self, _timeout_s=1.0):
                self.closed = True

        static_poller = Poller(30, lambda: None, initial=static)
        clock = [21.0]

        def advance_clock(_duration):
            clock[0] += advance

        with ExitStack() as stack:
            for target, value in (
                ("LiveHealthCoordinator", coordinator),
                ("_start_static_discovery", static_poller),
                ("default_sensor_info", settled_sensor()),
            ):
                stack.enter_context(mock.patch.object(porkyhud, target, return_value=value))
            stack.enter_context(mock.patch.object(porkyhud, "AsyncPoller", Poller))
            for target in ("init_colors", "apply_dark_screen", "unlock_privileged_sensors"):
                stack.enter_context(mock.patch.object(porkyhud, target))
            stack.enter_context(mock.patch.object(porkyhud, "unicode_ok",
                                                  return_value=unicode_enabled))
            stack.enter_context(mock.patch.object(porkyhud, "color_pair", return_value=0))
            stack.enter_context(mock.patch.object(porkyhud.time, "monotonic",
                                                  side_effect=lambda: clock[0]))
            stack.enter_context(mock.patch.object(porkyhud.time, "sleep",
                                                  side_effect=advance_clock))
            body = stack.enter_context(mock.patch.object(porkyhud, "draw_mode_body",
                                                          wraps=porkyhud.draw_mode_body))
            porkyhud.hud(screen, config)
        coordinator.close.assert_called_once_with()
        return screen, config, coordinator, pollers, captured, body.call_args_list


class ExploreFlowTests(DashboardHarness):
    def test_failed_inventory_refresh_retains_rows_with_latest_status_and_age(self):
        first = InventoryResult(InventoryStatus.LIVE,
                                (DeviceRecord("USB", "Fixture keyboard"),),
                                "", False, True)
        failed = InventoryResult(InventoryStatus.TIMEOUT, (), "Fixture timeout")
        with mock.patch.dict(porkyhud.INVENTORY_COLLECTORS,
                             {"devices": mock.Mock(side_effect=(first, failed))}):
            screen, _config, _coordinator, pollers, _captured, _body = self.run_dashboard(
                [ord("8"), -1, ord("q")], advance=1.0, collect_inventory=True,
            )
        self.assertIn("TIMEOUT", screen.row(4))
        self.assertIn("retained sample", screen.row(4))
        self.assertIn("1s", screen.row(4))
        self.assertIn("Fixture keyboard", screen.text())
        self.assertIn("NORMAL", screen.row(0))
        self.assertTrue(pollers[-1].closed)

    def test_initial_inventory_failure_preserves_typed_status_without_fabrication(self):
        for status in (InventoryStatus.TIMEOUT, InventoryStatus.PERMISSION_DENIED,
                       InventoryStatus.UNSUPPORTED):
            with self.subTest(status=status):
                failed = InventoryResult(status, (), "Fixture unavailable")
                with mock.patch.dict(porkyhud.INVENTORY_COLLECTORS,
                                     {"devices": mock.Mock(return_value=failed)}):
                    screen, _config, _coordinator, _pollers, _captured, _body = self.run_dashboard(
                        [ord("8"), ord("q")], collect_inventory=True,
                    )
                self.assertIn(status.value, screen.row(4))
                self.assertNotIn("retained sample", screen.row(4))
                self.assertNotIn("Fixture keyboard", screen.text())
                self.assertIn("NORMAL", screen.row(0))

    def test_new_mode_headers_preserve_complete_evidence_copy_at_minimum_width(self):
        for mode in ("insights", "devices", "network", "connections"):
            for case in ("checking", "normal", "incomplete", "delayed", "memory_critical"):
                for unicode_enabled in (True, False):
                    with self.subTest(mode=mode, case=case, unicode=unicode_enabled):
                        frame = make_health_frame_for_test(case)
                        screen = FakeScreen(22, 76)
                        config = porkyhud.HudConfig(layout_index=porkyhud.LAYOUTS.index(mode))
                        with mock.patch.object(porkyhud, "unicode_ok", return_value=unicode_enabled), \
                                mock.patch.object(porkyhud, "color_pair", return_value=0):
                            y = porkyhud.draw_decision_header(screen, frame, config, 20.0)
                            porkyhud.draw_evidence_ribbon(screen, y, frame, 20.0)
                        self.assertIn(mode.upper(), screen.row(0))
                        self.assertTrue(all(porkyhud.display_cell_width(screen.row(row).rstrip()) <= 75
                                            for row in range(3)))
                        for source in ("CPU", "MEM", "THERM", "DISK"):
                            self.assertIn(source, screen.row(2))
                        if not unicode_enabled:
                            self.assertTrue(all(ord(character) < 128
                                                for character in screen.text()))

    def test_menu_opens_selected_view_with_arrows_and_enter(self):
        for keys, expected in (
            ([ord("d"), porkyhud.curses.KEY_DOWN, 10, ord("q")], "compute"),
            ([ord("d"), ord("i"), -1, ord("q")], "system"),
        ):
            with self.subTest(mode=expected):
                screen, config, coordinator, _pollers, captured, _body = self.run_dashboard(keys)
                self.assertEqual(config.layout_name, expected)
                self.assertFalse(config.show_menu)
                self.assertTrue(any("ALL TOOLS" in "\n".join(rows)
                                    for rows in captured))
                self.assertNotIn("ALL TOOLS", screen.text())
                self.assertGreaterEqual(coordinator.tick.call_count, 3)

    def test_direct_inventory_keys_work_from_dashboard_and_menu(self):
        for key, mode in (("8", "devices"), ("9", "network"), ("0", "connections")):
            for menu in (False, True):
                with self.subTest(key=key, menu=menu):
                    keys = ([ord("d")] if menu else []) + [ord(key), ord("q")]
                    screen, config, _coordinator, _pollers, _captured, _body = self.run_dashboard(keys)
                    self.assertEqual(config.layout_name, mode)
                    self.assertFalse(config.show_menu)
                    self.assertIn("CHECKING", screen.text())
                    self.assertIn("NORMAL", screen.row(0))
                    self.assertIn("LIVE", screen.row(0))

    def test_escape_closes_menu_and_keeps_the_dashboard_running(self):
        screen, config, coordinator, _pollers, _captured, _body = self.run_dashboard(
            [ord("d"), 27, ord("q")],
        )
        self.assertFalse(config.show_menu)
        self.assertEqual(config.layout_name, "balanced")
        self.assertEqual(coordinator.tick.call_count, 2)
        self.assertNotIn("ALL TOOLS", screen.text())

    def test_explore_is_lazy_and_reuses_collectors_on_revisit(self):
        _screen, _config, _coordinator, baseline, _captured, _body = self.run_dashboard(
            [-1, ord("q")],
        )
        _screen, _config, _coordinator, explored, _captured, _body = self.run_dashboard(
            [ord("8"), ord("1"), ord("8"), ord("r"), ord("q")],
        )
        self.assertEqual(len(explored), len(baseline) + 1)
        inventory_poller = explored[-1]
        self.assertEqual(len(inventory_poller.calls), 3)
        self.assertTrue(inventory_poller.calls[-1][1])

    def test_menu_projection_fits_and_keeps_header_and_footer(self):
        for height, width in ((22, 76), (42, 132)):
            for unicode_enabled in (True, False):
                with self.subTest(size=(height, width), unicode=unicode_enabled):
                    screen, _config, _coordinator, _pollers, _captured, _body = self.run_dashboard(
                        [ord("d"), ord("q")], height=height, width=width,
                        unicode_enabled=unicode_enabled,
                    )
                    self.assertIn("NORMAL", screen.row(0))
                    self.assertIn("CPU", screen.row(2))
                    self.assertIn("q quit", screen.row(height - 1))
                    self.assertIn("ALL TOOLS", screen.text())
                    self.assertTrue(all(0 <= y < height and 0 <= x < width - 1
                                        for y, x, *_rest in screen.writes))
                    if not unicode_enabled:
                        self.assertTrue(all(ord(character) < 128
                                            for character in screen.text()))


class AppearanceFlowTests(DashboardHarness):
    def test_brief_intro_keeps_collecting_and_settles_to_motion_off(self):
        _screen, config, coordinator, _pollers, _captured, body = self.run_dashboard(
            [-1, -1, -1, ord("q")], advance=0.5,
        )
        phases = [call.args[-1] for call in body]
        self.assertEqual(phases[0], 0)
        self.assertGreater(phases[1], 0)
        self.assertEqual(phases[-1], 0)
        self.assertEqual(config.animation_mode, 0)
        self.assertEqual(coordinator.tick.call_count, 3)

    def test_no_intro_and_any_input_skip_the_flourish(self):
        for keys, config in (
            ([-1, -1, ord("q")], porkyhud.HudConfig(intro_enabled=False)),
            ([ord("d"), -1, ord("q")], porkyhud.HudConfig()),
        ):
            with self.subTest(keys=keys):
                _screen, config, _coordinator, _pollers, _captured, body = self.run_dashboard(
                    keys, config=config, advance=0.2,
                )
                self.assertTrue(all(call.args[-1] == 0 for call in body))
                self.assertFalse(config.intro_enabled)

    def test_style_cycles_without_changing_motion_or_decision(self):
        screen, config, _coordinator, _pollers, _captured, _body = self.run_dashboard(
            [ord("s"), ord("s"), ord("q")],
        )
        self.assertEqual(config.visual_style, "ascii")
        self.assertEqual(config.animation_mode, 0)
        self.assertIn("NORMAL", screen.row(0))
        self.assertIn("visual style: ascii", screen.row(21))
        self.assertIn("(oo)", screen.text())

    def test_signature_preserves_all_mascot_rows_in_own_rectangle(self):
        screen = FakeScreen(22, 76)
        config = porkyhud.HudConfig(visual_style="ascii")
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            self.assertEqual(porkyhud.draw_signature(screen, 3, 76, {}, config, 0), 10)
        mascot = porkyhud.render_mascot(0, "ascii", False)
        for index, row in enumerate(mascot.plain_rows):
            self.assertEqual(screen.row(3 + index)[2:30], row)

    def test_no_color_retains_text_and_ascii_visual_fallback(self):
        screen = FakeScreen(22, 76)
        with mock.patch.dict(porkyhud.os.environ, {"NO_COLOR": "1"}), \
                mock.patch.object(porkyhud, "unicode_ok", return_value=False):
            porkyhud.draw_signature(screen, 3, 76, {}, porkyhud.HudConfig(), 0)
        self.assertIn("P O R K Y H U D", screen.text())
        self.assertIn("(oo)", screen.text())
        self.assertTrue(all(write[4] == 0 or write[4] == porkyhud.curses.A_BOLD
                            for write in screen.writes))
        self.assertTrue(all(ord(character) < 128 for character in screen.text()))


class InventoryProjectionTests(unittest.TestCase):
    def result_for(self, mode, *, truncated=False):
        if mode == "devices":
            items = tuple(DeviceRecord("Display", "Monitor %d " % index + "界" * 80)
                          for index in range(60))
            return InventoryResult(InventoryStatus.LIVE, items, "", truncated,
                                   not truncated)
        if mode == "connections":
            items = tuple(ConnectionRecord("tcp4", "127.0.0.1.%d" % index,
                                           "192.0.2.1.443", "ESTABLISHED")
                          for index in range(60))
            return InventoryResult(InventoryStatus.LIVE, items, "", truncated,
                                   not truncated)
        return NetworkInventory(
            InventoryStatus.LIVE,
            tuple(NetworkInterface("en%d" % index, "active", ("192.0.2.1",))
                  for index in range(30)), "", truncated, not truncated,
            "192.0.2.254", "en0", ("192.0.2.53",),
            InventoryStatus.LIVE, InventoryStatus.LIVE, InventoryStatus.LIVE,
        )

    def test_each_inventory_scrolls_and_preserves_rectangle_and_truncation(self):
        for height, width in ((22, 76), (42, 132)):
            for mode in ("devices", "network", "connections"):
                for unicode_enabled in (True, False):
                    with self.subTest(mode=mode, size=(height, width), unicode=unicode_enabled):
                        screen = FakeScreen(height, width, fill="~")
                        with mock.patch.object(porkyhud, "unicode_ok", return_value=unicode_enabled), \
                                mock.patch.object(porkyhud, "color_pair", return_value=0):
                            effective = porkyhud.draw_inventory_body(
                                screen, mode, 3, height - 4, width,
                                self.result_for(mode, truncated=True), 0.0, "", 10**9,
                            )
                        self.assertGreater(effective, 0)
                        self.assertIn("TRUNCATED", screen.row(height - 3))
                        self.assertIn("PARTIAL", screen.row(4))
                        self.assertIn("read-only", screen.row(4))
                        self.assertEqual(screen.row(0), "~" * width)
                        self.assertEqual(screen.row(height - 1), "~" * width)
                        self.assertTrue(all(3 <= y <= height - 2
                                            and 1 <= x < width - 1
                                            and porkyhud.display_cell_width(text) <= width - x - 1
                                            for y, x, text, _limit, _attr in screen.writes))
                        if not unicode_enabled:
                            self.assertTrue(all(ord(character) < 128
                                                for character in screen.text()))

    def test_retained_inventory_never_projects_as_live(self):
        for age, error, expected in ((31.0, "", "STALE"),
                                     (0.0, "private error", "ERROR")):
            with self.subTest(expected=expected):
                screen = FakeScreen(22, 76)
                with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                        mock.patch.object(porkyhud, "color_pair", return_value=0):
                    porkyhud.draw_inventory_body(screen, "devices", 3, 18, 76,
                                                 self.result_for("devices"), age, error, 0)
                self.assertIn(expected, screen.row(4))
                self.assertIn("retained sample", screen.row(4))
                self.assertNotIn("private error", screen.text())

    def test_missing_inventory_is_never_described_as_a_retained_sample(self):
        screen = FakeScreen(22, 76)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_inventory_body(screen, "devices", 3, 18, 76,
                                         None, 31.0, "", 0)
        self.assertIn("CHECKING", screen.row(4))
        self.assertNotIn("retained sample", screen.row(4))

    def test_network_projection_exposes_supplement_statuses_and_read_only_scope(self):
        result = self.result_for("network")
        lines = porkyhud.inventory_lines("network", result)
        self.assertIn("192.0.2.254", lines[0])
        self.assertIn("192.0.2.53", lines[1])
        self.assertIn("[LIVE]", lines[0])
        screen = FakeScreen(22, 76)
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                mock.patch.object(porkyhud, "color_pair", return_value=0):
            porkyhud.draw_inventory_body(screen, "network", 3, 18, 76,
                                         result, 0.0, "", 0)
        self.assertIn("No connectivity test or network changes", screen.text())


class CommandLineUpgradeTests(unittest.TestCase):
    def test_live_preferences_are_passed_to_hud_as_one_configuration(self):
        screen = FakeScreen(42, 132)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(porkyhud, "terminal_ui_problem", return_value=None))
            stack.enter_context(mock.patch.object(porkyhud, "enforce_dark_terminal"))
            stack.enter_context(mock.patch.object(porkyhud.sys.stdout, "isatty", return_value=False))
            stack.enter_context(mock.patch.object(porkyhud.signal, "signal"))
            stack.enter_context(mock.patch.object(porkyhud.signal, "getsignal", return_value=None))
            stack.enter_context(mock.patch.object(porkyhud.curses, "wrapper",
                                                  side_effect=lambda callback: callback(screen)))
            hud = stack.enter_context(mock.patch.object(porkyhud, "hud"))
            self.assertEqual(porkyhud.main([
                "--theme", "catppuccin", "--style", "outline", "--motion", "calm",
                "--mode", "network", "--no-intro",
            ]), 0)
        passed_screen, config = hud.call_args.args
        self.assertIs(passed_screen, screen)
        self.assertEqual(config.theme_name, "Catppuccin")
        self.assertEqual(config.visual_style, "outline")
        self.assertEqual(config.animation_mode, 1)
        self.assertEqual(config.layout_name, "network")
        self.assertFalse(config.intro_enabled)

    def test_invalid_style_is_rejected_before_live_effects(self):
        with redirect_stderr(io.StringIO()), \
                mock.patch.object(porkyhud, "terminal_ui_problem") as terminal, \
                self.assertRaises(SystemExit) as exit_error:
            porkyhud.main(["--style", "unsupported"])
        self.assertEqual(exit_error.exception.code, 2)
        terminal.assert_not_called()

    def test_inspect_json_has_precedence_and_uses_only_selected_read_only_collector(self):
        result = InventoryResult(InventoryStatus.LIVE,
                                 (DeviceRecord("USB", "Fixture keyboard"),),
                                 "", False, True)
        collector = mock.Mock(return_value=result)
        output = io.StringIO()
        with mock.patch.dict(porkyhud.INVENTORY_COLLECTORS, {"devices": collector}), \
                mock.patch.object(porkyhud, "collect_snapshot") as snapshot, \
                mock.patch.object(porkyhud, "terminal_ui_problem") as terminal, \
                mock.patch.object(porkyhud, "install_sensor_access") as install, \
                redirect_stdout(output):
            self.assertEqual(porkyhud.main(["--inspect", "devices", "--json", "--snapshot"]), 0)
        collector.assert_called_once_with(porkyhud._inventory_probe)
        snapshot.assert_not_called()
        terminal.assert_not_called()
        install.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "LIVE")
        self.assertEqual(payload["items"][0]["name"], "Fixture keyboard")
        self.assertNotIn("hostname", payload)
        self.assertNotIn("serial", output.getvalue().lower())

    def test_unavailable_inspection_has_nonzero_exit_and_typed_status(self):
        result = InventoryResult(InventoryStatus.PERMISSION_DENIED, (),
                                 "Access denied", False, False)
        output = io.StringIO()
        with mock.patch.dict(porkyhud.INVENTORY_COLLECTORS,
                             {"devices": mock.Mock(return_value=result)}), \
                redirect_stdout(output):
            self.assertEqual(porkyhud.main(["--inspect", "devices"]), 1)
        self.assertIn("PERMISSION DENIED", output.getvalue())

    def test_inventory_probe_keeps_raw_failure_output_out_of_public_records(self):
        command = SimpleNamespace(stdout="private stdout", stderr="private stderr",
                                  outcome=core.AttemptOutcome.TIMEOUT,
                                  failure=core.FailureCode.COMMAND_TIMEOUT)
        with mock.patch.object(porkyhud, "run_structured_command", return_value=command) as runner:
            result = porkyhud._inventory_probe(("/usr/sbin/fixture",),
                                               timeout_s=2.0, max_output_bytes=4096)
        runner.assert_called_once_with(("/usr/sbin/fixture",), "inventory", 2.0,
                                       4096, 16 * 1024)
        self.assertEqual(result.status, InventoryStatus.TIMEOUT)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("private", repr(result))


class AtomicSampleTests(unittest.TestCase):
    def test_sample_keeps_success_value_age_and_failure_revision_together(self):
        poller = porkyhud.AsyncPoller(1.0, lambda: "first")
        with mock.patch.object(porkyhud.time, "monotonic", side_effect=(10.0, 10.2)):
            poller._run()
        sample = poller.sample_snapshot(12.0)
        self.assertEqual((sample.value, sample.age_s, sample.error, sample.revision,
                          sample.completed_mono_s), ("first", 2.0, "", 1, 10.2))
        with self.assertRaises(FrozenInstanceError):
            sample.revision = 9
        poller.collector = mock.Mock(side_effect=RuntimeError("collector failed"))
        with mock.patch.object(porkyhud.time, "monotonic", return_value=13.0):
            poller._run()
        failed = poller.sample_snapshot(14.0)
        self.assertEqual((failed.value, failed.age_s, failed.error, failed.revision,
                          failed.completed_mono_s),
                         ("first", 4.0, "collector failed", 2, 13.0))
        self.assertEqual(poller.snapshot(14.0),
                         (failed.value, failed.age_s, failed.error))

    def test_distinct_idle_sample_is_zero_while_reset_and_gap_need_new_baseline(self):
        baseline, rates = porkyhud._network_rate_update(None, (100, 200), 10.0)
        self.assertIsNone(rates)
        same, rates = porkyhud._network_rate_update(baseline, (100, 200), 10.0)
        self.assertEqual(same, baseline)
        self.assertIsNone(rates)
        idle, rates = porkyhud._network_rate_update(baseline, (100, 200), 11.0)
        self.assertEqual(rates, (0.0, 0.0))
        self.assertEqual(idle[1], 11.0)
        for totals, clock in (((10, 200), 12.0), ((100, 200), 9.0), ((200, 400), 20.0)):
            with self.subTest(totals=totals, clock=clock):
                reset, rates = porkyhud._network_rate_update(idle, totals, clock)
                self.assertEqual(reset, (totals, clock))
                self.assertIsNone(rates)


class InventoryShutdownTests(unittest.TestCase):
    def test_close_before_start_prevents_collection_and_future_ticks(self):
        collector = mock.Mock(return_value="late value")
        poller = porkyhud.AsyncPoller(1.0, collector, initial="initial")
        before = poller.sample_snapshot(1.0)
        poller.close(0.0)
        poller.close(0.0)
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(RuntimeError):
                poller.tick(2.0, force)
        poller._run()
        collector.assert_not_called()
        self.assertEqual(poller.sample_snapshot(1.0), before)

    def test_close_after_preparation_prevents_a_delayed_worker_from_collecting(self):
        threads = []

        class PreparedThread:
            ident = None

            def __init__(self, target, daemon):
                self.target = target
                threads.append(self)

            def start(self):
                pass

        collector = mock.Mock(return_value="late value")
        poller = porkyhud.AsyncPoller(1.0, collector, initial="initial")
        with mock.patch.object(porkyhud.threading, "Thread", PreparedThread):
            poller.tick(1.0)
            self.assertEqual(len(threads), 1)
            poller.close(0.0)
            threads[0].target()
        collector.assert_not_called()
        self.assertEqual(poller.sample_snapshot(2.0).revision, 0)
        with self.assertRaises(RuntimeError):
            poller.tick(3.0, True)

    def test_close_active_worker_discards_completion_and_forced_followup(self):
        poller = porkyhud.AsyncPoller(1.0, lambda: "last good")
        with mock.patch.object(porkyhud.time, "monotonic", side_effect=(10.0, 10.2)):
            poller._run()
        poller.collector = mock.Mock(side_effect=RuntimeError("last known failure"))
        with mock.patch.object(porkyhud.time, "monotonic", return_value=11.0):
            poller._run()
        started, release = threading.Event(), threading.Event()
        calls = []

        def blocked_collector():
            calls.append(True)
            started.set()
            if not release.wait(5.0):
                raise RuntimeError("fixture release missing")
            return "completion after shutdown"

        poller.collector = blocked_collector
        try:
            poller.tick(12.0, True)
            self.assertTrue(started.wait(2.0), "collector did not start")
            poller.tick(12.1, True)
            self.assertTrue(poller._force_pending)
            before = poller.sample_snapshot(14.0)
            poller.close(0.0)
            self.assertFalse(poller._force_pending)
            release.set()
            poller.close(2.0)
            self.assertEqual(calls, [True])
            self.assertEqual(poller.sample_snapshot(14.0), before)
            self.assertFalse(poller._running)
            with self.assertRaises(RuntimeError):
                poller.tick(15.0, True)
        finally:
            release.set()
            poller.close(2.0)

    def test_invalid_close_budget_does_not_close_the_poller(self):
        poller = porkyhud.AsyncPoller(1.0, lambda: None)
        for invalid in (True, -0.1, float("nan"), float("inf"), None):
            with self.subTest(timeout=invalid), self.assertRaises(ValueError):
                poller.close(invalid)
            self.assertFalse(poller._closed)
        poller.close(0.0)

    def test_hud_cancels_every_inventory_before_joining_even_on_coordinator_error(self):
        cancellations = [threading.Event(), threading.Event(), threading.Event()]
        observations = []

        class Poller:
            def close(self, timeout_s):
                observations.append((all(event.is_set() for event in cancellations), timeout_s))

        coordinator = mock.Mock()
        coordinator.close.side_effect = RuntimeError("coordinator cleanup fixture")

        def populated_impl(_screen, coordinators, _config, inventory_jobs, tool_sessions):
            coordinators.append(coordinator)
            inventory_jobs.extend((Poller(), event) for event in cancellations)

        with mock.patch.object(porkyhud, "_hud_impl", side_effect=populated_impl), \
                self.assertRaises(RuntimeError):
            porkyhud.hud(FakeScreen(22, 76))
        coordinator.close.assert_called_once_with()
        self.assertEqual(len(observations), len(cancellations))
        self.assertTrue(all(cancelled and 0.0 <= budget <= 1.5
                            for cancelled, budget in observations))

    def test_closed_inventory_probe_never_starts_a_command(self):
        cancellation = threading.Event()
        cancellation.set()
        with mock.patch.object(porkyhud, "run_structured_command") as runner:
            result = porkyhud._inventory_probe(("/usr/sbin/fixture",), timeout_s=1.0,
                                               max_output_bytes=1024,
                                               cancel_event=cancellation)
        runner.assert_not_called()
        self.assertEqual(result.status, InventoryStatus.ERROR)
        self.assertEqual(result.stdout, "")


class ExternalCommandCancellationTests(unittest.TestCase):
    def test_precancelled_command_does_not_spawn_a_child(self):
        cancellation = threading.Event()
        cancellation.set()
        with mock.patch.object(porkyhud, "_command_worker_popen") as spawn:
            result = porkyhud.run_structured_command(
                (sys.executable, "-c", "raise AssertionError('must not launch')"),
                "fixture", 2.0, 1024, 1024, cancel_event=cancellation,
            )
        spawn.assert_not_called()
        self.assertEqual(result.outcome, core.AttemptOutcome.CANCELLED)
        self.assertEqual(result.failure, core.FailureCode.USER_CANCELLED)
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_external_event_terminates_and_reaps_a_live_descendant_group(self):
        cancellation = threading.Event()
        ready = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            code, pid_path, heartbeat_path = fixtures.StructuredCommandTests.descendant_fixture(
                self, directory, emit_invalid_utf8=False,
                safety_seconds=8.0, parent_lingers=True,
            )

            def cancel_after_descendant_is_live():
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if os.path.exists(heartbeat_path) and os.path.exists(pid_path):
                        with open(pid_path, encoding="utf-8") as recorded:
                            if len(recorded.read().splitlines()) >= 2:
                                ready.set()
                                break
                    time.sleep(0.01)
                cancellation.set()

            notifier = threading.Thread(target=cancel_after_descendant_is_live,
                                        name="PorkyHUD-fixture-canceller")
            notifier.start()
            try:
                result = porkyhud.run_structured_command(
                    (sys.executable, "-c", code), "fixture", 10.0,
                    4096, 4096, cancel_event=cancellation,
                )
            finally:
                cancellation.set()
                notifier.join(6.0)
            self.assertFalse(notifier.is_alive(), "cancellation notifier did not stop")
            self.assertTrue(ready.is_set(), "descendant fixture never became live")
            fixtures.StructuredCommandTests.assert_recorded_processes_are_gone(self, pid_path, 2.0)
            with open(pid_path, encoding="utf-8") as recorded:
                group = int(recorded.readline())
            with self.assertRaises(ProcessLookupError):
                os.killpg(group, 0)
            self.assertFalse(any(thread.name.startswith("PorkyHUD-command-")
                                 for thread in threading.enumerate()))
            self.assertEqual(result.outcome, core.AttemptOutcome.CANCELLED)
            self.assertEqual(result.failure, core.FailureCode.USER_CANCELLED)
            self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_invalid_external_cancellation_handle_is_rejected_before_spawn(self):
        with mock.patch.object(porkyhud, "_command_worker_popen") as spawn, \
                self.assertRaises(ValueError):
            porkyhud.run_structured_command((sys.executable, "-c", "pass"),
                                            "fixture", 1.0, 1024, 1024,
                                            cancel_event=object())
        spawn.assert_not_called()


class NavigationAndSearchTests(DashboardHarness):
    def test_tab_and_reverse_tab_cycle_every_supported_view(self):
        for height, width, keys, expected in (
            (22, 76, [9], "compute"),
            (22, 76, [porkyhud.curses.KEY_BTAB], "system"),
            (34, 118, [porkyhud.curses.KEY_BTAB], "cinema"),
            (22, 76, [9, porkyhud.curses.KEY_BTAB], "balanced"),
        ):
            with self.subTest(size=(height, width), keys=keys):
                _screen, config, *_rest = self.run_dashboard(keys + [ord("q")], height=height, width=width)
                self.assertEqual(config.layout_name, expected)

    def test_system_details_are_a_direct_view_with_no_inventory_probe(self):
        screen, config, _coordinator, pollers, _captured, _body = self.run_dashboard(
            [ord("i"), ord("q")],
            static={"chip": "Fixture Chip", "model": "Fixture Mac", "model_id": "Mac0,0",
                    "os": "Fixture macOS", "boot_seconds": 60},
        )
        self.assertEqual(config.layout_name, "system")
        self.assertIn("THIS MAC", screen.text())
        self.assertIn("Fixture Chip", screen.text())
        self.assertIn("Fixture macOS", screen.text())
        self.assertFalse(any(getattr(p.collector, "__name__", "") == "collect_observation" for p in pollers))

    def test_search_consumes_shortcuts_and_escape_clears_before_quit(self):
        screen, config, coordinator, _pollers, captured, _body = self.run_dashboard(
            [ord("/"), ord("q"), ord("1"), 10, 27, ord("q")],
        )
        self.assertEqual(config.layout_name, "balanced")
        self.assertEqual(config.search_query, "")
        self.assertFalse(config.search_editing)
        self.assertEqual(coordinator.tick.call_count, 5)
        self.assertTrue(any("Find: q1" in "\n".join(frame) for frame in captured))
        self.assertNotIn("Find:", screen.row(21))

    def test_process_search_is_filtered_before_render_and_retained_per_view(self):
        rows = (
            porkyhud.ProcessRow(10, 50, 1, None, "R", "00:01", "/usr/bin/worker --token secret-value"),
            porkyhud.ProcessRow(11, 5, 1, None, "S", "00:01", "/usr/bin/other"),
        )
        _screen, config, _coordinator, _pollers, _captured, calls = self.run_dashboard(
            [ord("/"), *map(ord, "worker"), 10, ord("5"), ord("1"), ord("q")], process_rows=rows,
        )
        self.assertEqual(config.searches, {"balanced": "worker"})
        final_rows = calls[-1].args[16]
        self.assertEqual([row.pid for row in final_rows], [10])
        process_call = next(call for call in calls if call.args[1] == "processes")
        self.assertEqual(len(process_call.args[16]), 2)

    def test_process_search_cannot_match_redacted_values(self):
        rows = (porkyhud.ProcessRow(42, 1, 1, None, "S", "00:01",
                                 "/usr/bin/worker --token private-secret"),)
        self.assertEqual(porkyhud.filter_process_rows(rows, "private-secret"), ())
        self.assertEqual(porkyhud.filter_process_rows(rows, "WORKER 42"), rows)
        self.assertEqual(porkyhud.filter_process_rows(rows, "[.*]"), ())

    def test_query_editing_is_bounded_and_can_erase(self):
        config = porkyhud.HudConfig(search_editing=True)
        for key in (ord("a"), ord("b"), 127, ord("c")):
            self.assertTrue(porkyhud.edit_search(config, key))
        self.assertEqual(config.search_query, "ac")
        porkyhud.edit_search(config, 21)
        self.assertEqual(config.search_query, "")
        for _ in range(porkyhud.SEARCH_QUERY_LIMIT + 5):
            porkyhud.edit_search(config, ord("x"))
        self.assertEqual(len(config.search_query), porkyhud.SEARCH_QUERY_LIMIT)
        porkyhud.edit_search(config, 27)
        self.assertFalse(config.search_editing)
        self.assertEqual(config.search_query, "")
        with self.assertRaises(ValueError):
            porkyhud.normalize_search_query("x" * (porkyhud.SEARCH_QUERY_LIMIT + 1))
        self.assertEqual(porkyhud.normalize_search_query("a\x1b b\n c"), "a b c")

    def test_connection_filter_and_search_work_together(self):
        result = InventoryResult(InventoryStatus.LIVE, (
            ConnectionRecord("tcp4", "127.0.0.1:8080", "*:*", "LISTEN"),
            ConnectionRecord("tcp4", "127.0.0.1:443", "*:*", "LISTEN"),
            ConnectionRecord("tcp4", "127.0.0.1:8080", "192.0.2.1:443", "ESTABLISHED"),
        ), "partial fixture", False, False)
        with mock.patch.dict(porkyhud.INVENTORY_COLLECTORS, {"connections": mock.Mock(return_value=result)}):
            screen, config, *_rest = self.run_dashboard(
                [ord("0"), ord("f"), ord("/"), *map(ord, "8080"), 10, ord("q")], collect_inventory=True,
            )
        self.assertEqual(config.connection_filter, "listeners")
        self.assertEqual(config.search_query, "8080")
        self.assertIn("127.0.0.1:8080", screen.text())
        self.assertNotIn("192.0.2.1", screen.text())
        self.assertIn("PARTIAL", screen.row(4))
        self.assertIn("2 listeners", screen.row(5))
        self.assertIn("1/3 matches", screen.row(5))

    def test_wide_character_search_accepts_unicode_without_treating_arrows_as_text(self):
        config = porkyhud.HudConfig(search_editing=True)
        for key in ("é", "界", porkyhud.curses.KEY_DOWN, "\n"):
            self.assertTrue(porkyhud.edit_search(config, key))
        self.assertEqual(config.search_query, "é界")
        self.assertFalse(config.search_editing)
        screen = mock.Mock()
        screen.get_wch.return_value = "界"
        self.assertEqual(porkyhud.read_hud_key(screen, True), "界")
        screen.get_wch.side_effect = porkyhud.curses.error()
        self.assertEqual(porkyhud.read_hud_key(screen, True), -1)
        screen.getch.return_value = ord("d")
        self.assertEqual(porkyhud.read_hud_key(screen, False), ord("d"))

    def test_no_matching_processes_is_distinct_from_loading(self):
        config = porkyhud.HudConfig(searches={"balanced": "no-match"})
        frame = make_health_frame_for_test()
        view = core.SourceView(core.SourceId.PROCESS_TABLE, core.SourceViewStatus.LIVE,
                               1.0, False, True, 1, None)
        frame = replace(frame, source_views={**frame.source_views, core.SourceId.PROCESS_TABLE: view})
        screen, *_rest = self.run_dashboard([-1, ord("q")], config=config, frame=frame)
        self.assertIn("No matching processes", screen.text())
        self.assertNotIn("collecting processes", screen.text())

    def test_process_search_waits_for_a_valid_list_before_claiming_no_matches(self):
        config = porkyhud.HudConfig(searches={"balanced": "no-match"})
        screen, *_rest = self.run_dashboard([-1, ord("q")], config=config)
        self.assertIn("Processes CHECKING", screen.text())
        self.assertNotIn("No matching processes", screen.text())
        self.assertNotIn("0 matches", screen.text())

    def test_escape_resets_connection_filter_before_quitting(self):
        _screen, config, coordinator, *_rest = self.run_dashboard(
            [ord("0"), ord("f"), 27, ord("q")],
        )
        self.assertEqual(config.layout_name, "connections")
        self.assertEqual(config.connection_filter, "all")
        self.assertEqual(coordinator.tick.call_count, 3)

    def test_long_query_keeps_apply_and_clear_hints_visible(self):
        config = porkyhud.HudConfig(search_editing=True, searches={"balanced": "x" * 256})
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False):
            text = porkyhud.shortcut_status(76, config)
        self.assertIn("Enter apply", text)
        self.assertIn("Esc clear", text)
        self.assertLessEqual(porkyhud.display_cell_width(text), 74)

    def test_all_tools_menu_contains_every_view_at_minimum_size(self):
        screen, *_rest = self.run_dashboard([ord("d"), ord("q")])
        for label in ("Overview", "CPU & memory", "Temperatures & fans", "Storage & traffic",
                      "Processes", "Cinema", "Memory activity", "Connected devices", "Network",
                      "TCP connections", "This Mac"):
            self.assertIn(label, screen.text())
        self.assertIn("ALL TOOLS", screen.text())

    def test_unsupported_cinema_stays_in_menu_with_clear_size_message(self):
        screen, config, *_rest = self.run_dashboard([ord("d"), ord("6"), ord("q")])
        self.assertTrue(config.show_menu)
        self.assertEqual(config.layout_name, "balanced")
        self.assertIn("ALL TOOLS", screen.text())
        self.assertIn("requires 118x34", screen.row(21))

    def test_live_search_filters_do_not_collect_inventory_on_overview(self):
        _screen, _config, _coordinator, pollers, _captured, _body = self.run_dashboard(
            [ord("/"), ord("a"), 10, ord("q")],
        )
        self.assertFalse(any(getattr(p.collector, "__name__", "") == "collect_observation" for p in pollers))


class SearchCliTests(unittest.TestCase):
    def test_failed_filtered_text_report_has_no_match_count_claim(self):
        result = InventoryResult(InventoryStatus.TIMEOUT, detail="Fixture timeout")
        output = io.StringIO()
        with mock.patch.dict(porkyhud.INVENTORY_COLLECTORS, {"connections": mock.Mock(return_value=result)}), \
                redirect_stdout(output):
            status = porkyhud.main(["--inspect", "connections", "--state", "listeners"])
        self.assertEqual(status, 1)
        self.assertIn("TIMEOUT", output.getvalue())
        self.assertIn("Counts unavailable", output.getvalue())
        self.assertNotIn("0 matches", output.getvalue())

    def test_filtered_text_report_labels_matching_and_returned_counts(self):
        result = InventoryResult(InventoryStatus.LIVE, (
            DeviceRecord("USB", "Fixture keyboard"), DeviceRecord("USB", "Fixture mouse"),
        ), complete=False)
        output = io.StringIO()
        with mock.patch.dict(porkyhud.INVENTORY_COLLECTORS, {"devices": mock.Mock(return_value=result)}), \
                redirect_stdout(output):
            status = porkyhud.main(["--inspect", "devices", "--search", "keyboard"])
        self.assertEqual(status, 0)
        self.assertIn("1 matches / 2 returned · PARTIAL", output.getvalue())
        self.assertIn("Fixture keyboard", output.getvalue())
        self.assertNotIn("Fixture mouse", output.getvalue())

    def test_filtered_inventory_json_preserves_partial_coverage_and_totals(self):
        result = InventoryResult(InventoryStatus.LIVE, (
            ConnectionRecord("tcp4", "127.0.0.1:8080", "*:*", "LISTEN"),
            ConnectionRecord("tcp4", "127.0.0.1:9000", "192.0.2.1:443", "ESTABLISHED"),
        ), "partial fixture", True, False)
        output = io.StringIO()
        with mock.patch.dict(porkyhud.INVENTORY_COLLECTORS, {"connections": mock.Mock(return_value=result)}), redirect_stdout(output):
            status = porkyhud.main(["--inspect", "connections", "--state", "listeners", "--search", "8080", "--json"])
        data = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(len(data["items"]), 1)
        self.assertTrue(data["truncated"])
        self.assertFalse(data["complete"])
        self.assertEqual(data["filter"]["returned_records"], 2)
        self.assertEqual(data["filter"]["matched_records"], 1)
        self.assertEqual(data["summary"]["listeners"], 1)

    def test_state_flag_selects_connections_for_live_use(self):
        captured = []
        with mock.patch.object(porkyhud, "terminal_ui_problem", return_value=None), \
                mock.patch.object(porkyhud, "enforce_dark_terminal"), \
                mock.patch.object(porkyhud.curses, "wrapper", side_effect=lambda fn: fn(mock.Mock())), \
                mock.patch.object(porkyhud, "hud", side_effect=lambda _screen, config: captured.append(config)):
            self.assertEqual(porkyhud.main(["--state", "listeners", "--search", "443"]), 0)
        self.assertEqual(captured[0].layout_name, "connections")
        self.assertEqual(captured[0].connection_filter, "listeners")
        self.assertEqual(captured[0].search_query, "443")

    def test_invalid_state_combinations_reject_before_collection(self):
        for argv in (["--inspect", "devices", "--state", "listeners"],
                     ["--mode", "thermals", "--state", "listeners"],
                     ["--snapshot", "--search", "worker"]):
            with self.subTest(argv=argv), mock.patch.object(porkyhud, "collect_snapshot") as collector, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                porkyhud.main(argv)
            self.assertEqual(error.exception.code, 2)
            collector.assert_not_called()


class FullerDashboardTests(unittest.TestCase):
    def test_empty_process_panels_preserve_failure_status_without_search(self):
        frame = make_health_frame_for_test()
        for status in (core.SourceViewStatus.ERROR, core.SourceViewStatus.STALE,
                       core.SourceViewStatus.UNSUPPORTED):
            view = core.SourceView(core.SourceId.PROCESS_TABLE, status, None,
                                   False, False, None, None)
            current = replace(frame, source_views={**frame.source_views, core.SourceId.PROCESS_TABLE: view})
            screen = FakeScreen(22, 76)
            with self.subTest(status=status), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                    mock.patch.object(porkyhud, "color_pair", return_value=0):
                message = porkyhud.process_empty_message(current, None)
                porkyhud.draw_process_panel(screen, 3, 1, 12, 74, (), 0, "CPU", empty_message=message)
            self.assertIn(status.value, screen.text())
            self.assertNotIn("collecting", screen.text())
            self.assertNotIn("0 matches", screen.text())

    def test_failed_first_inventory_has_no_socket_count_claim(self):
        filtered = porkyhud.HudConfig(layout_index=porkyhud.LAYOUTS.index("connections"),
                                      searches={"connections": "fixture"}, connection_filter="listeners")
        for config in (None, filtered):
            screen = FakeScreen(22, 76)
            with self.subTest(filtered=config is not None), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                    mock.patch.object(porkyhud, "color_pair", return_value=0):
                porkyhud.draw_inventory_body(
                    screen, "connections", 3, 18, 76,
                    InventoryResult(InventoryStatus.TIMEOUT, detail="Fixture timeout"),
                    0, "", 0, config=config,
                )
            self.assertIn("TIMEOUT", screen.row(4))
            self.assertIn("Counts unavailable", screen.row(5))
            self.assertNotIn("0 listeners", screen.text())
            self.assertNotIn("0 connected", screen.text())
            self.assertNotIn("No matches", screen.text())

    def test_percent_bars_keep_full_numbers_and_units_in_narrow_panels(self):
        with mock.patch.object(porkyhud, "unicode_ok", return_value=False):
            for label in ("total ", "active "):
                for number in (0, 3.5, 100):
                    text = porkyhud.fitted_percent_bar(number, 34, 0, label)
                    self.assertLessEqual(porkyhud.display_cell_width(text), 34)
                    self.assertTrue(text.endswith("%5.1f%%" % number))

    def test_dense_overview_contains_metrics_and_processes_at_every_supported_size(self):
        for height, width in ((22, 76), (24, 80), (34, 118), (42, 132), (42, 164)):
            screen = FakeScreen(height, width, fill="~")
            history = porkyhud.MetricHistory()
            mem = {"total": 128 * 1024**3, "used": 64 * 1024**3, "swap_used": 1024**3,
                   "swap_total": 2 * 1024**3, "compressed": 1024**3, "cached": 2 * 1024**3}
            rows = [porkyhud.ProcessRow(42, 55, 1, None, "R", "00:01", "/usr/bin/fixture --token secret")]
            with self.subTest(size=(height, width)), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                    mock.patch.object(porkyhud, "color_pair", return_value=0):
                porkyhud.draw_mode_body(
                    screen, "balanced", 3, height - 4, width, make_health_frame_for_test(),
                    [10, 20], {"chip": "Fixture Chip"}, mem, fixtures.make_battery(), settled_sensor(),
                    porkyhud.DiskInfo("Data", "/", 100, 50, 50), porkyhud.DiskActivity(source="iostat"),
                    1024, 2048, history, rows, 0, "CPU", 0,
                )
            for text in ("CPU", "RAM", "MEMORY", "Swap", "STORAGE + I/O", "Net", "TOP PROCESSES", "fixture"):
                self.assertIn(text, screen.text())
            self.assertNotIn("secret", screen.text())
            self.assertEqual(screen.row(0), "~" * width)
            self.assertEqual(screen.row(height - 1), "~" * width)
            self.assertTrue(all(3 <= y < height - 1 and 1 <= x < width - 1 for y, x, *_rest in screen.writes))

    def test_human_status_text_uses_plain_labels_in_every_health_state(self):
        for case in ("checking", "normal", "incomplete", "delayed", "memory_critical"):
            frame = make_health_frame_for_test(case)
            for width in (76, 132):
                text = " ".join(porkyhud.decision_header_lines(frame, "balanced", width, 20.0)).casefold()
                self.assertNotIn("evidence", text)
                self.assertNotIn("provenance", text)
            text = porkyhud.legacy_read_projection(frame.assessment)["message"].casefold()
            self.assertNotIn("evidence", text)
            self.assertNotIn("provenance", text)

    def test_plain_status_copy_preserves_volume_and_process_names(self):
        storage = make_health_frame_for_test("storage_watch")
        storage = replace(
            storage, disk_capacity=replace(storage.disk_capacity, volume_name="Evidence"),
            assessment=replace(storage.assessment,
                               reason="Storage headroom is low: 8.0 GiB remains on Evidence."),
        )
        for width in (76, 132):
            self.assertIn("Evidence", porkyhud.compact_reason(storage, width, 20.0))
        self.assertIn("Evidence", porkyhud.legacy_read_projection(storage.assessment)["message"])
        cpu = make_health_frame_for_test("cpu_watch")
        cpu = replace(cpu, assessment=replace(cpu.assessment, contributor_name="provenance"))
        self.assertIn("provenance", porkyhud.compact_reason(cpu, 200, 20.0))

    def test_power_panels_do_not_invent_battery_absence_or_zero_charge(self):
        for changes, expected in (({"present": False, "source": "Power", "state": "checking"}, "CHECKING"),
                                  ({"present": False, "source": "Power", "state": "unknown"}, "UNAVAILABLE"),
                                  ({"present": True, "percent": None}, "NOT EXPOSED")):
            battery = fixtures.make_battery(**changes)
            screen = FakeScreen(24, 80)
            with self.subTest(changes=changes), \
                    mock.patch.object(porkyhud, "unicode_ok", return_value=False), \
                    mock.patch.object(porkyhud, "color_pair", return_value=0):
                porkyhud.draw_battery_panel(screen, 3, 1, 10, 38, battery, 0)
                porkyhud.draw_thermal_panel(screen, 3, 40, 18, 38, battery, settled_sensor(), 0)
            self.assertIn(expected, screen.text())
            self.assertNotIn("not installed", screen.text())
            self.assertNotIn("not present", screen.text())
            self.assertNotIn("0.0%", screen.text())


if __name__ == "__main__":
    unittest.main()
