"""Recording bounds, independent sample timing, replay, and export privacy."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from porkyhud_tools import sessions


def sample(elapsed=0.0, *, value=10.0, revision=None, start_id="start-a"):
    reading = {"value": value, "unit": "%", "status": "LIVE"}
    if revision is not None:
        reading.update(revision=revision, age_s=0.0)
    return {"schema_version": 1,
            "timestamp": (datetime(2026, 10, 8, tzinfo=timezone.utc)
                          + timedelta(seconds=elapsed)).isoformat(),
            "elapsed_s": elapsed, "metrics": {"cpu_pct": reading},
            "processes": [{"pid": 42, "start_id": start_id, "ppid": 1,
                           "name": "worker", "cpu_pct": value, "rss_bytes": 1024,
                           "state": "S", "command": "PRIVATE_TOKEN"}],
            "processes_complete": False, "processes_truncated": True,
            "events": [], "health": {"severity": "UNKNOWN", "readiness": "CHECKING"},
            "host": "PRIVATE_HOST", "addresses": ["PRIVATE_ADDRESS"]}


class SessionValidationTests(unittest.TestCase):
    def test_minimizes_unknown_fields_and_keeps_metric_source_details(self):
        current = sample(revision=2)
        current["metrics"]["cpu_pct"]["max_gap_s"] = 2.5
        result = sessions.validate_sample(current)
        self.assertNotIn("host", result)
        self.assertNotIn("command", result["processes"][0])
        self.assertEqual(result["metrics"]["cpu_pct"]["revision"], 2)
        self.assertEqual(result["metrics"]["cpu_pct"]["age_s"], 0)
        self.assertEqual(result["metrics"]["cpu_pct"]["max_gap_s"], 2.5)
        self.assertFalse(result["processes_complete"])

    def test_private_change_labels_are_replaced_and_extra_fields_dropped(self):
        current = sample()
        current["events"] = [{"kind": "devices", "label": "PRIVATE_DEVICE appeared",
                              "address": "PRIVATE_ADDRESS"}]
        minimized = sessions.validate_sample(current, include_private=False)
        self.assertEqual(minimized["events"], [{"kind": "devices", "label": "Devices changed"}])
        self.assertNotIn("PRIVATE", repr(minimized))

    def test_system_configuration_is_allowlisted_in_private_default_exports(self):
        current = sample()
        current["system"] = {"model": "Mac17,6", "chip": "Apple M5 Max", "os": "macOS 26.6",
                             "build": "25G100", "ram_bytes": 64 * 1024**3,
                             "host": "PRIVATE_HOST", "serial_number": "PRIVATE_SERIAL",
                             "kernel_path": "/PRIVATE_PATH"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system.json"
            sessions.export_report(current, path)
            loaded = sessions.load_session(path)["samples"][0]
            self.assertEqual(set(loaded["system"]), {"model", "chip", "os", "build", "ram_bytes"})
            self.assertEqual(loaded["system"]["ram_bytes"], 64 * 1024**3)
            self.assertNotIn("PRIVATE", path.read_text())

    def test_malformed_system_fields_are_rejected(self):
        for system in (None, [], {"model": ""}, {"chip": "x" * 129}, {"os": "macOS\x1b"},
                       {"build": 123}, {"ram_bytes": 0}, {"ram_bytes": -1},
                       {"ram_bytes": True}, {"ram_bytes": 1.0}, {"ram_bytes": float("inf")}):
            current = sample()
            current["system"] = system
            with self.subTest(system=system), self.assertRaises(ValueError):
                sessions.validate_sample(current)

    def test_process_source_state_survives_private_export_without_private_fields(self):
        current = sample()
        current["process_source"] = {"status": "STALE", "revision": 4, "age_s": 8.5,
                                     "command": "PRIVATE_COMMAND"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "process-state.json"
            sessions.export_report(current, path)
            restored = sessions.load_session(path)["samples"][0]
            self.assertEqual(restored["process_source"], {"status": "STALE", "revision": 4, "age_s": 8.5})
            self.assertEqual(restored["processes"], [])
            self.assertNotIn("PRIVATE_COMMAND", path.read_text())
        for source in (None, {"status": "LIVE", "revision": True, "age_s": 0},
                       {"status": "LIVE", "revision": 1, "age_s": -1},
                       {"status": "LIVE", "revision": 1, "age_s": float("inf")},
                       {"status": "LIVE", "age_s": 0}):
            current["process_source"] = source
            with self.subTest(source=source), self.assertRaises(ValueError):
                sessions.validate_sample(current)

    def test_rejects_nonfinite_boolean_and_wrong_version_samples(self):
        for bad in (float("nan"), float("inf"), True, 10**1000):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(ValueError):
                sessions.validate_sample(sample(value=bad))
        for version in (True, 2, "1"):
            invalid = sample()
            invalid["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                sessions.validate_sample(invalid)

    def test_rejects_invalid_time_status_controls_and_process_identity(self):
        for key, value in (("timestamp", "2026-10-08T00:00:00-05:00"),
                           ("elapsed_s", -1), ("health", {"severity": [], "readiness": "LIVE"})):
            invalid = sample()
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                sessions.validate_sample(invalid)
        invalid = sample()
        invalid["processes"][0]["name"] = "worker\x1b[31m"
        with self.assertRaises(ValueError):
            sessions.validate_sample(invalid)


class RecorderTests(unittest.TestCase):
    def test_memory_ring_is_bounded_and_copies_samples(self):
        recorder = sessions.SessionRecorder(max_samples=2)
        first = sample()
        recorder.append(first)
        first["metrics"]["cpu_pct"]["value"] = 999
        recorder.append(sample(1))
        recorder.append(sample(2))
        self.assertEqual([item["elapsed_s"] for item in recorder.samples], [1, 2])
        returned = recorder.samples
        returned[0]["metrics"]["cpu_pct"]["value"] = 999
        self.assertEqual(recorder.samples[0]["metrics"]["cpu_pct"]["value"], 10)
        self.assertTrue(recorder.to_report().truncated)
        self.assertFalse(recorder.append(sample(2)))
        with self.assertRaises(ValueError):
            recorder.append(sample(1))
        self.assertTrue(recorder.stop()["writer_finished"])
        self.assertFalse(recorder.append(sample(3)))

    def test_private_default_disk_recording_and_bookmark_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            recorder = sessions.SessionRecorder(path)
            first = sample()
            first["system"] = {"model": "Mac17,6", "ram_bytes": 64 * 1024**3,
                               "serial": "PRIVATE_SERIAL"}
            recorder.append(first)
            recorder.append(sample(1, revision=2))
            recorder.mark("build finished")
            self.assertTrue(recorder.stop()["complete"])
            loaded = sessions.load_session(path)
            self.assertTrue(loaded["complete"])
            self.assertEqual(len(loaded["samples"]), 2)
            self.assertEqual(loaded["samples"][0]["processes"], [])
            self.assertEqual(loaded["samples"][0]["system"],
                             {"model": "Mac17,6", "ram_bytes": 64 * 1024**3})
            self.assertEqual(loaded["events"][0]["label"], "build finished")
            wire = path.read_text()
            for private in ("PRIVATE_TOKEN", "PRIVATE_HOST", "PRIVATE_ADDRESS", "PRIVATE_SERIAL", "worker"):
                self.assertNotIn(private, wire)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_mid_session_bookmarks_use_sample_time_and_constructor_has_no_clock_read(self):
        with mock.patch.object(sessions.time, "monotonic", side_effect=(900.0, 902.5)) as clock:
            recorder = sessions.SessionRecorder()
            self.assertEqual(clock.call_count, 0)
            recorder.append(sample(600))
            event = recorder.mark("build started")
            self.assertEqual(event["elapsed_s"], 602.5)
            self.assertEqual(clock.call_count, 2)
            recorder.stop()

    def test_supplied_observation_clock_avoids_extra_reads_and_anchors_bookmarks(self):
        with mock.patch.object(sessions.time, "monotonic", return_value=902.5) as clock:
            recorder = sessions.SessionRecorder()
            recorder.append(sample(600), observed_mono_s=900)
            self.assertEqual(clock.call_count, 0)
            self.assertEqual(recorder.mark("build started")["elapsed_s"], 602.5)
            self.assertEqual(clock.call_count, 1)
            recorder.stop()
        recorder = sessions.SessionRecorder()
        for invalid in (float("nan"), float("inf"), True, "900", 10**1000):
            with self.subTest(invalid=type(invalid).__name__), self.assertRaises(ValueError):
                recorder.append(sample(), observed_mono_s=invalid)
        recorder.stop()

    def test_timeline_rows_deduplicate_source_readings_and_include_bookmarks(self):
        recorder = sessions.SessionRecorder()
        recorder.append(sample(0, value=10, revision=1))
        recorder.append(sample(1, value=10, revision=1))
        recorder.append(sample(2, value=20, revision=2))
        recorder.mark("build complete")
        report = recorder.to_report()
        self.assertEqual(report.tool, "Session timeline")
        self.assertEqual(report.status, "LIVE")
        self.assertEqual(report.rows[0], ("cpu_pct", "20", "15", "20", "%", "2", "LIVE"))
        self.assertIn("2.0 s · build complete", report.details)
        recorder.stop()

    def test_disk_cap_is_explicit_and_replay_preserves_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small.jsonl"
            recorder = sessions.SessionRecorder(path, max_bytes=1024, queue_size=32)
            for elapsed in range(20):
                recorder.append(sample(elapsed))
            state = recorder.stop()
            self.assertFalse(state["complete"])
            self.assertTrue(state["truncated"])
            self.assertLessEqual(path.stat().st_size, 1024)
            loaded = sessions.load_session(path)
            self.assertFalse(loaded["complete"])
            self.assertGreater(loaded["dropped_samples"], 0)

    def test_writer_sample_cap_stays_loadable(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(sessions, "MAX_LOAD_SAMPLES", 2):
            path = Path(directory) / "limited.jsonl"
            recorder = sessions.SessionRecorder(path, max_samples=2)
            for elapsed in range(3):
                recorder.append(sample(elapsed))
            self.assertTrue(recorder.stop()["truncated"])
            loaded = sessions.load_session(path)
            self.assertEqual(len(loaded["samples"]), 2)
            self.assertFalse(loaded["complete"])

    def test_existing_file_is_never_replaced_and_writer_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.jsonl"
            path.write_text("keep")
            recorder = sessions.SessionRecorder(path)
            recorder.append(sample())
            state = recorder.stop()
            self.assertEqual(path.read_text(), "keep")
            self.assertFalse(state["complete"])
            self.assertTrue(state["error"])
            self.assertNotIn(directory, state["error"])

    def test_blocked_writer_does_not_block_append_and_stop_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        original = sessions.os.fdopen

        class BlockedStream:
            def __init__(self, descriptor, mode):
                self.stream = original(descriptor, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, value):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("Fixture writer did not release")
                return self.stream.write(value)

            def flush(self):
                self.stream.flush()

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(sessions.os, "fdopen", side_effect=BlockedStream):
            recorder = sessions.SessionRecorder(Path(directory) / "blocked.jsonl", queue_size=1)
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(recorder.append(sample()))
                self.assertTrue(recorder.append(sample(1)))
                state = recorder.stop(timeout_s=0)
                self.assertFalse(state["writer_finished"])
                self.assertGreaterEqual(state["dropped_samples"], 1)
            finally:
                release.set()
                self.assertTrue(recorder.stop()["writer_finished"])


class ReplayAndExportTests(unittest.TestCase):
    def test_export_defaults_private_and_detailed_export_stays_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            result = sessions.export_report(sample(), path)
            self.assertFalse(result["include_private"])
            self.assertEqual(sessions.load_session(path)["samples"][0]["processes"], [])
            sessions.export_report(sample(), path, include_private=True)
            loaded = sessions.load_session(path)
            self.assertEqual(loaded["samples"][0]["processes"][0]["pid"], 42)
            self.assertNotIn("PRIVATE_TOKEN", path.read_text())
            self.assertNotIn("PRIVATE_HOST", path.read_text())

    def test_incomplete_final_line_retains_valid_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.jsonl"
            path.write_bytes(sessions._encoded({"type": "header", "schema_version": 1,
                                               "format": "porkyhud-session"})
                             + sessions._encoded({"type": "sample", "schema_version": 1,
                                                  "sample": sample()})
                             + b'{"type":"sample"')
            loaded = sessions.load_session(path)
            self.assertEqual(len(loaded["samples"]), 1)
            self.assertFalse(loaded["complete"])
            self.assertTrue(loaded["truncated"])

    def test_nonempty_data_after_completion_is_rejected_even_for_a_partial_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "finished.jsonl"
            finished = (sessions._encoded({"type": "header", "schema_version": 1,
                                          "format": "porkyhud-session"})
                        + sessions._encoded({"type": "sample", "schema_version": 1, "sample": sample()})
                        + sessions._encoded({"type": "stop", "schema_version": 1,
                                             "complete": True, "truncated": False, "dropped_samples": 0}))
            for tail in (b'{', b'{"type":"sample"', b'not-json\n',
                         sessions._encoded({"type": "mark", "schema_version": 1,
                                            "event": {"kind": "mark", "label": "later"}})):
                path.write_bytes(finished + tail)
                with self.subTest(tail=tail), self.assertRaisesRegex(ValueError, "Data follows session completion"):
                    sessions.load_session(path)
            path.write_bytes(finished + b'\n \t\n')
            restored = sessions.load_session(path)
            self.assertTrue(restored["complete"])
            self.assertFalse(restored["truncated"])

    def test_malformed_finished_line_unknown_version_and_duplicate_fields_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            for content in (b'{"type":"header","schema_version":2,"format":"porkyhud-session"}\n',
                            b'{"schema_version":1,"schema_version":1}\n',
                            b'not json\n'):
                path.write_bytes(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    sessions.load_session(path)

    def test_outer_export_version_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"format": "porkyhud-export", "schema_version": 2,
                                        "kind": "sample", "sample": sample()}))
            with self.assertRaises(ValueError):
                sessions.load_session(path)

    def test_failed_atomic_replace_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("keep")
            with mock.patch.object(sessions.os, "replace", side_effect=OSError("fixture")), \
                    self.assertRaises(OSError):
                sessions.export_report(sample(), path)
            self.assertEqual(path.read_text(), "keep")
            self.assertEqual(tuple(Path(directory).iterdir()), (path,))

    def test_compare_counts_source_revisions_once_and_matches_units(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "a.json", Path(directory) / "b.json"
            a = {"schema_version": 1, "samples": [sample(0, value=10, revision=1),
                                                     sample(1, value=10, revision=1),
                                                     sample(2, value=20, revision=2)], "complete": True}
            b = {"schema_version": 1, "samples": [sample(0, value=30, revision=1)], "complete": True}
            sessions.export_report(a, first)
            sessions.export_report(b, second)
            report = sessions.compare_sessions(first, second)
            comparison = report.data["comparisons"][0]
            self.assertEqual(comparison["first"]["count"], 2)
            self.assertEqual(comparison["first"]["mean"], 15)
            self.assertEqual(comparison["mean_change"], 15)
            b["samples"][0]["metrics"]["cpu_pct"]["unit"] = "W"
            sessions.export_report(b, second)
            self.assertEqual(sessions.compare_sessions(first, second).rows, ())

    def test_process_history_never_joins_reused_pid_and_retains_missing_intervals(self):
        first, reused, returned = sample(), sample(1, start_id="start-b"), sample(8)
        report = sessions.process_trends((first, reused, returned), 42, "start-a")
        self.assertEqual(len(report.rows), 2)
        self.assertEqual([point["status"] for point in report.data["points"]],
                         ["LIVE", "MISSING", "GAP"])
        self.assertFalse(report.complete)
        self.assertEqual(sessions.process_trends((first,), 42, None).status, "INCOMPLETE")

    def test_process_history_deduplicates_source_revisions_and_preserves_unavailable_intervals(self):
        readings = [sample(elapsed) for elapsed in range(6)]
        sources = (("LIVE", 1, 0), ("LIVE", 1, 1), ("LIVE", 2, 0),
                   ("ERROR", 2, 1), ("STALE", 2, 2), ("LIVE", 3, 0))
        for current, (status, revision, age) in zip(readings, sources):
            current["process_source"] = {"status": status, "revision": revision, "age_s": age}
            if status != "LIVE":
                current["processes"] = []
        report = sessions.process_trends(readings, 42, "start-a")
        self.assertEqual([point["status"] for point in report.data["points"]],
                         ["LIVE", "LIVE", "ERROR", "STALE", "GAP"])
        self.assertEqual(len(report.rows), 5)
        self.assertEqual(report.summary, "3 readings for process 42")
        self.assertEqual(report.status, "PARTIAL")
        self.assertFalse(report.complete)

    def test_large_finite_values_have_finite_means_and_safe_change(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "a.json", Path(directory) / "b.json"
            a = {"schema_version": 1, "samples": [sample(0, value=1e308),
                                                     sample(1, value=1e308)], "complete": True}
            b = {"schema_version": 1, "samples": [sample(0, value=-1e308)], "complete": True}
            # Negative aggregate readings are valid; negative process CPU is not.
            b["samples"][0]["processes"] = []
            sessions.export_report(a, first)
            sessions.export_report(b, second)
            report = sessions.compare_sessions(first, second)
            self.assertEqual(report.data["comparisons"][0]["first"]["mean"], 1e308)
            self.assertIsNone(report.data["comparisons"][0]["mean_change"])
            json.dumps(report.data, allow_nan=False)

    def test_incompatible_host_configuration_has_explicit_comparison_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "a.json", Path(directory) / "b.json"
            left, right = sample(), sample()
            left["system"] = {"model": "Mac16,7", "chip": "Apple M4 Pro", "os": "macOS 26.5",
                              "build": "25F100", "ram_bytes": 24 * 1024**3}
            right["system"] = {"model": "Mac17,6", "chip": "Apple M5 Max", "os": "macOS 26.6",
                               "build": "25G100", "ram_bytes": 64 * 1024**3}
            sessions.export_report(left, first)
            sessions.export_report(right, second)
            report = sessions.compare_sessions(first, second)
            self.assertIn("Mac model differs: Mac16,7 / Mac17,6.", report.details)
            self.assertIn("Chip differs: Apple M4 Pro / Apple M5 Max.", report.details)
            self.assertEqual(set(report.data["configurations"]["differences"]),
                             {"model", "chip", "os", "build", "ram_bytes"})
            self.assertTrue(any("workload" in line for line in report.details))

    def test_missing_or_changing_configuration_is_reported_without_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "a.json", Path(directory) / "b.json"
            a, b = sample(), sample(1)
            a["system"], b["system"] = {"model": "Mac16,7"}, {"model": "Mac17,6"}
            sessions.export_report({"schema_version": 1, "samples": [a, b]}, first)
            sessions.export_report(sample(), second)
            report = sessions.compare_sessions(first, second)
            self.assertIn("Configuration changed within session 1: model.", report.details)
            self.assertTrue(any("incomplete" in line for line in report.details))
            self.assertEqual(report.data["configurations"]["differences"], [])


if __name__ == "__main__":
    unittest.main()
