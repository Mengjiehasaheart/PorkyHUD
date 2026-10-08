"""Local profile validation and custom watches independent from health policy."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from porkyhud_tools.profiles import (
    WatchEngine, WatchRule, list_profiles, load_profile, save_profile,
)


def sample(elapsed=0, value=90, status="LIVE", **reading):
    return {"elapsed_s": elapsed, "metrics": {
        "cpu_pct": {"value": value, "unit": "%", "status": status, **reading}},
            "health": {"severity": "NORMAL", "readiness": "LIVE"}}


class ProfileTests(unittest.TestCase):
    def test_round_trip_settings_watches_pin_and_owner_only_permissions(self):
        settings = {"mode": "overview", "theme": "neon-grid", "style": "ascii",
                    "motion": "off", "no_intro": True, "sort": "CPU", "state": "listeners",
                    "searches": {"processes": "worker"}, "pinned_pid": 42,
                    "pinned_start_id": "start-a", "watches": [WatchRule("cpu_pct", ">=", 90, 5)]}
        with tempfile.TemporaryDirectory() as directory:
            saved = save_profile("work", settings, directory)
            self.assertEqual(saved["mode"], "balanced")
            self.assertEqual(load_profile("work", directory), saved)
            self.assertEqual(list_profiles(directory), ("work",))
            self.assertEqual(os.stat(Path(directory) / "work.json").st_mode & 0o777, 0o600)

    def test_rejects_path_names_unknown_settings_poll_cadence_and_unsafe_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("../escape", "/tmp/escape", "a/b", "", ".hidden"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    save_profile(name, {}, directory)
            for settings in ({"refresh_s": 0.01}, {"command": "delete"},
                             {"theme": "cyan"}, {"pinned_pid": 42},
                             {"pinned_pid": True, "pinned_start_id": "start-a"},
                             {"search": "private\x1b[31m"}):
                with self.subTest(settings=settings), self.assertRaises(ValueError):
                    save_profile("bad", settings, directory)

    def test_load_revalidates_version_settings_size_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for value in ({"schema_version": 2, "name": "bad", "settings": {}},
                          {"schema_version": 1, "name": "bad", "settings": {"mode": "invented"}}):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    load_profile("bad", directory)
            path.write_text(" " * (64 * 1024 + 1))
            with self.assertRaises(ValueError):
                load_profile("bad", directory)
            path.unlink()
            target = Path(directory) / "target.txt"
            target.write_text("keep")
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                load_profile("bad", directory)
            with self.assertRaises(ValueError):
                save_profile("bad", {}, directory)
            self.assertEqual(list_profiles(directory), ())


class WatchTests(unittest.TestCase):
    def test_exact_threshold_duration_and_recovery_do_not_change_health(self):
        engine = WatchEngine([WatchRule("cpu_pct", ">=", 90, 2)])
        first = sample(0)
        self.assertEqual(engine.update(first), ())
        self.assertEqual(first["health"]["severity"], "NORMAL")
        self.assertEqual(engine.update(sample(1)), ())
        active = engine.update(sample(2))
        self.assertEqual(active[0]["duration_s"], 2)
        self.assertEqual(engine.update(sample(3, value=89.999)), ())
        self.assertEqual(engine.update(sample(4)), ())

    def test_missing_stale_nonfinite_and_long_gaps_reset_sustained_timer(self):
        for broken in (sample(1, value=None), sample(1, status="STALE"),
                       sample(1, value=float("nan")), {"elapsed_s": 1, "metrics": {}}):
            engine = WatchEngine([WatchRule("cpu_pct", ">", 80, 2)])
            engine.update(sample(0))
            self.assertEqual(engine.update(broken), ())
            self.assertEqual(engine.update(sample(2)), ())
            self.assertEqual(engine.update(sample(3)), ())
            self.assertTrue(engine.update(sample(4)))
        engine = WatchEngine([WatchRule("cpu_pct", ">", 80, 2)])
        engine.update(sample(0))
        self.assertEqual(engine.update(sample(10)), ())
        self.assertTrue(engine.update(sample(12)))

    def test_repeated_source_revisions_do_not_manufacture_watch_duration(self):
        engine = WatchEngine([WatchRule("cpu_pct", ">", 80, 2)])
        self.assertEqual(engine.update(sample(0, revision=1)), ())
        self.assertEqual(engine.update(sample(1, revision=1, age_s=1)), ())
        self.assertEqual(engine.update(sample(2, revision=1, age_s=2)), ())
        active = engine.update(sample(3, revision=2))
        self.assertEqual(active[0]["duration_s"], 3)
        self.assertEqual(engine.update(sample(4, revision=2, age_s=1))[0]["duration_s"], 3)
        self.assertEqual(engine.update(sample(5, status="STALE", revision=2)), ())

    def test_missing_metrics_and_revision_reset_clear_active_notices(self):
        engine = WatchEngine([WatchRule("cpu_pct", ">", 80, 2)])
        engine.update(sample(0, revision=1))
        self.assertTrue(engine.update(sample(2, revision=2)))
        self.assertEqual(engine.update({"elapsed_s": 3, "metrics": None}), ())
        self.assertEqual(engine.update(sample(4, revision=2)), ())
        self.assertTrue(engine.update(sample(6, revision=3)))
        self.assertEqual(engine.update(sample(7, revision=1)), ())

    def test_app_controlled_source_gap_supports_slower_readings_without_repeat_duration(self):
        engine = WatchEngine([WatchRule("cpu_pct", ">", 80, 15)])
        engine.update(sample(0, revision=1, max_gap_s=45))
        for elapsed in range(1, 15):
            self.assertEqual(engine.update(sample(elapsed, revision=1,
                                                 age_s=elapsed, max_gap_s=45)), ())
        self.assertTrue(engine.update(sample(15, revision=2, max_gap_s=45)))
        self.assertEqual(engine.update(sample(16, revision=2, age_s=46, max_gap_s=45)), ())
        for bad in (0, -1, float("inf"), 301, True):
            self.assertEqual(engine.update(sample(17, revision=3, max_gap_s=bad)), ())

    def test_duplicate_time_backwards_time_and_source_gaps_are_not_sustained(self):
        engine = WatchEngine([WatchRule("cpu_pct", ">", 80, 2)])
        engine.update(sample(0))
        for _ in range(5):
            self.assertEqual(engine.update(sample(0)), ())
        self.assertTrue(engine.update(sample(2)))
        self.assertEqual(engine.update(sample(1)), ())
        self.assertEqual(engine.update(sample(2)), ())
        engine.update(sample(3, revision=1, age_s=3))
        self.assertEqual(engine.update(sample(8, revision=2)), ())

    def test_operators_and_invalid_watch_rules(self):
        for operator in (">", ">=", "<", "<=", "==", "!="):
            engine = WatchEngine([WatchRule("cpu_pct", operator, 80)])
            result = engine.update(sample(0, value=90))
            self.assertEqual(bool(result), operator in (">", ">=", "!="))
        for values in (("../bad", ">", 90, 0), ("cpu_pct", "exec", 90, 0),
                       ("cpu_pct", ">", float("inf"), 0), ("cpu_pct", ">", True, 0),
                       ("cpu_pct", ">", 90, -1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                WatchRule(*values)
        with self.assertRaises(ValueError):
            WatchEngine([WatchRule("cpu_pct", ">", 90)] * 17)


if __name__ == "__main__":
    unittest.main()
