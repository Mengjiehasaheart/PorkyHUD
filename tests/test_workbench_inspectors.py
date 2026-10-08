"""Selected-process identity, read-only storage, and power tool contracts."""

from dataclasses import replace
import json
import os
from pathlib import Path
import plistlib
import tempfile
import threading
import unittest
from unittest import mock

from porkyhud_inventory import InventoryStatus, ProbeResult
from porkyhud_tools.processes import inspect_process, workload_report, watch_job_report
from porkyhud_tools.storage import inspect_storage, scan_folder, compare_folder_snapshots
from porkyhud_tools.power import inspect_sleep, power_timeline_report


START = "Thu Oct 8 10:00:00 2026"
IDENTITY = "42 1 S 2048 Thu Oct 8 10:00:00 2026 /usr/bin/python3\n"
FILES = "p42\0\nf3r\0tREG\0n/Users/private/run/data.txt\0\nf4u\0tIPv4\0PTCP\0n127.0.0.1:8080\0TST=LISTEN\0"
ASSERTIONS = '''2026-10-08 10:00:00 -0500
Assertion status system-wide:
   BackgroundTask                 0
   UserIsActive                   1
   PreventUserIdleSystemSleep      1
Listed by owning process:
   pid 42(python3): [0x000000010000abcd] 01:02:03 PreventUserIdleSystemSleep named: "training token=PRIVATE_TOKEN"
   pid 88(WindowServer): [0x000000020000abcd] 00:00:03 UserIsActive named: "display event"
No kernel assertions.
'''


def row(pid=42, ppid=1, **updates):
    value = {"pid": pid, "ppid": ppid, "start_id": START, "name": "python3", "cpu_pct": 25.0,
             "rss_bytes": 2097152, "state": "S"}
    value.update(updates)
    return value


def probe(raw="", status=InventoryStatus.LIVE):
    return ProbeResult(raw, status, "PRIVATE_COMMAND_ERROR")


def plist(payload):
    return probe(plistlib.dumps(payload).decode())


class SequenceRunner:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        return next(self.replies)


class ProcessInspectorTests(unittest.TestCase):
    def test_selected_details_check_identity_twice_and_shorten_paths(self):
        runner = SequenceRunner((probe(IDENTITY), probe(FILES), probe(IDENTITY)))
        result = inspect_process(42, runner, (row(),))
        self.assertEqual(result.status, "LIVE")
        self.assertEqual(result.data["identity"]["rss_bytes"], 2097152)
        self.assertEqual(result.rows[0][2], "data.txt")
        self.assertEqual(result.rows[1][2], "127.0.0.1:8080")
        self.assertFalse(result.complete)
        self.assertEqual(len(runner.calls), 3)
        rendered = json.dumps(result.payload())
        self.assertNotIn("/Users/private", rendered)
        self.assertNotIn("PRIVATE_COMMAND_ERROR", rendered)

    def test_cached_pid_reuse_stops_before_open_file_probe(self):
        runner = SequenceRunner((probe(IDENTITY),))
        result = inspect_process(42, runner, (row(start_id="Thu Oct 8 09:00:00 2026"),))
        self.assertEqual(result.status, "PID CHANGED")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result.data, {})

    def test_reuse_during_collection_discards_files(self):
        runner = SequenceRunner((probe(IDENTITY), probe(FILES), probe(IDENTITY.replace("10:00:00", "10:01:00"))))
        result = inspect_process(42, runner)
        self.assertEqual(result.status, "PID CHANGED")
        self.assertEqual(result.rows, ())
        self.assertEqual(result.data, {})

    def test_exited_and_permission_denied_are_distinct(self):
        self.assertEqual(inspect_process(42, SequenceRunner((probe(""),))).status, "EXITED")
        denied = inspect_process(42, SequenceRunner((probe("", InventoryStatus.PERMISSION_DENIED),)))
        self.assertEqual(denied.status, "PERMISSION DENIED")
        self.assertNotIn("PRIVATE_COMMAND_ERROR", str(denied))

    def test_malformed_identity_and_foreign_lsof_pid_are_not_joined(self):
        self.assertEqual(inspect_process(42, SequenceRunner((probe("43 malformed"),))).status, "ERROR")
        runner = SequenceRunner((probe(IDENTITY), probe(FILES.replace("p42", "p43")), probe(IDENTITY)))
        result = inspect_process(42, runner)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.rows, ())

    def test_inaccessible_files_retain_identity_without_error_details(self):
        runner = SequenceRunner((probe(IDENTITY), probe("PRIVATE_RAW", InventoryStatus.PERMISSION_DENIED), probe(IDENTITY)))
        result = inspect_process(42, runner)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.data["identity"]["pid"], 42)
        self.assertNotIn("PRIVATE_RAW", json.dumps(result.payload()))

    def test_open_file_row_limit_remains_explicit(self):
        files = "p42\n" + "".join("f%d\ntREG\nn/private/file%d\n" % (number, number) for number in range(140))
        runner = SequenceRunner((probe(IDENTITY), probe(files), probe(IDENTITY)))
        result = inspect_process(42, runner)
        self.assertEqual(len(result.rows), 128)
        self.assertTrue(result.truncated)

    def test_invalid_pid_never_runs_a_command(self):
        runner = mock.Mock()
        for pid in (True, 0, -1, "42"):
            self.assertEqual(inspect_process(pid, runner).status, "INVALID")
        runner.assert_not_called()

    def test_workload_groups_descendants_and_keeps_units_explicit(self):
        result = workload_report(42, (row(), row(43, 42, cpu_pct=50), row(44, 43), row(99, 1)))
        self.assertEqual(result.status, "LIVE")
        self.assertEqual(result.data["process_count"], 3)
        self.assertEqual(result.data["cpu_pct_sum"], 100)
        self.assertEqual(result.data["rss_bytes_sum"], 6291456)
        self.assertIn("shared pages", " ".join(result.details))

    def test_conflicting_identities_and_missing_metrics_are_partial(self):
        result = workload_report(42, (row(), row(43, 42), row(43, 42, start_id="Different"), row(44, 42, rss_bytes=None)))
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.data["process_count"], 2)
        self.assertIsNone(result.data["rss_bytes_sum"])
        self.assertNotIn(43, [item["pid"] for item in result.data["processes"]])

    def test_parent_cycles_terminate_and_row_limit_is_explicit(self):
        self.assertEqual(workload_report(42, (row(ppid=43), row(43, 42))).data["process_count"], 2)
        result = workload_report(42, [row()] + [row(pid, 42) for pid in range(100, 250)])
        self.assertTrue(result.truncated)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.rows), 128)

    def test_progress_is_explicit_bounded_json_and_never_completion_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            path.write_text(json.dumps({"current": 4, "total": 10, "status": "running", "command": "PRIVATE_COMMAND", "eta": 12}))
            result = watch_job_report(42, (row(),), path)
            self.assertEqual(result.data["progress"]["current"], 4)
            self.assertEqual(result.data["progress_status"], "LIVE")
            self.assertTrue(result.complete)
            self.assertNotIn("eta", result.data["progress"])
            self.assertNotIn("PRIVATE_COMMAND", str(result))
            path.write_text("x" * 65537)
            oversized = watch_job_report(42, (row(),), path)
            self.assertEqual(oversized.data["progress"], {})
            self.assertEqual(oversized.data["progress_status"], "ERROR")
            self.assertEqual(oversized.status, "PARTIAL")
            self.assertFalse(oversized.complete)

    def test_progress_symlink_and_fifo_are_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            path.write_text('{"percent": 5}')
            link = Path(directory) / "link"
            link.symlink_to(path)
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            for target in (link, fifo):
                result = watch_job_report(42, (row(),), target)
                self.assertEqual(result.data["progress"], {})
                self.assertEqual(result.status, "PARTIAL")
                self.assertFalse(result.complete)

    def test_requested_progress_missing_and_denied_have_distinct_incomplete_states(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = watch_job_report(42, (row(),), Path(directory) / "missing.json")
            self.assertEqual(missing.status, "PARTIAL")
            self.assertFalse(missing.complete)
            self.assertEqual(missing.data["progress_status"], "UNAVAILABLE")
        with mock.patch("porkyhud_tools.processes.os.open", side_effect=PermissionError("PRIVATE_PATH")):
            denied = watch_job_report(42, (row(),), "private.json")
        self.assertEqual(denied.status, "PARTIAL")
        self.assertFalse(denied.complete)
        self.assertEqual(denied.data["progress_status"], "PERMISSION DENIED")
        self.assertNotIn("PRIVATE_PATH", str(denied))
        standalone = watch_job_report(42, (row(),))
        self.assertEqual(standalone.status, "LIVE")
        self.assertTrue(standalone.complete)
        self.assertNotIn("progress_status", standalone.data)

    def test_malformed_and_deep_progress_json_cannot_claim_complete_job_readings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            for content, status in (("{invalid", "ERROR"), ("[]", "ERROR"), ("{}", "NOT EXPOSED"),
                                    ("[" * 2000 + "0" + "]" * 2000, "ERROR")):
                path.write_text(content)
                result = watch_job_report(42, (row(),), path)
                self.assertEqual(result.status, "PARTIAL")
                self.assertFalse(result.complete)
                self.assertEqual(result.data["progress_status"], status)
            path.write_text('{"percent": 5}')
            unavailable = watch_job_report(999, (row(),), path)
            self.assertEqual(unavailable.status, "UNAVAILABLE")
            self.assertFalse(unavailable.complete)


class StorageInspectorTests(unittest.TestCase):
    def apfs(self):
        return {"Containers": [{"ContainerReference": "disk3", "CapacityCeiling": 1000, "CapacityFree": 250,
                "Volumes": [{"DeviceIdentifier": "disk3s1", "Name": "Data", "CapacityInUse": 600, "CapacityQuota": 0},
                            {"DeviceIdentifier": "disk3s2", "Name": "System", "CapacityInUse": 150}]}]}

    def disks(self):
        return {"AllDisksAndPartitions": [{"DeviceIdentifier": "disk0", "Size": 1000, "Content": "GUID_partition_scheme"}]}

    def test_apfs_capacity_and_snapshot_count_do_not_invent_reclaimable_bytes(self):
        runner = SequenceRunner((plist(self.disks()), plist(self.apfs()), plist({"Snapshots": [{"Name": "com.apple.test", "Purgeable": True}]}), plist({"Snapshots": []})))
        result = inspect_storage(runner)
        self.assertTrue(result.complete)
        self.assertEqual(result.data["containers"][0]["free_bytes"], 250)
        self.assertEqual(result.data["containers"][0]["volumes"][0]["snapshot_count"], 1)
        self.assertNotIn("reclaimable_bytes", json.dumps(result.payload()))
        self.assertTrue(all(call[0][1] in ("list", "apfs") for call in runner.calls))

    def test_failed_snapshot_probe_is_partial_with_safe_details(self):
        runner = SequenceRunner((plist(self.disks()), plist(self.apfs()), probe("PRIVATE_RAW", InventoryStatus.PERMISSION_DENIED), plist({"Snapshots": []})))
        result = inspect_storage(runner)
        self.assertEqual(result.status, "PARTIAL")
        self.assertFalse(result.complete)
        self.assertNotIn("PRIVATE_RAW", str(result))
        self.assertEqual(result.data["containers"][0]["volumes"][0]["snapshot_status"], "PERMISSION DENIED")

    def test_xml_errors_and_unrecognized_schema_return_error(self):
        for payload in (probe("<plist><broken"), plist({"NotContainers": []})):
            result = inspect_storage(SequenceRunner((plist(self.disks()), payload)))
            self.assertEqual(result.status, "ERROR")
            self.assertFalse(result.complete)

    def test_unvalidated_device_never_enters_a_command(self):
        data = self.apfs()
        data["Containers"][0]["Volumes"][0]["DeviceIdentifier"] = "disk3s1; touch BAD"
        runner = SequenceRunner((plist(self.disks()), plist(data)))
        self.assertEqual(inspect_storage(runner).status, "ERROR")
        self.assertEqual(len(runner.calls), 2)

    def test_snapshot_probe_budget_is_bounded(self):
        data = self.apfs()
        data["Containers"][0]["Volumes"] = [{"DeviceIdentifier": "disk3s%d" % n, "Name": "V"} for n in range(1, 11)]
        runner = SequenceRunner([plist(self.disks()), plist(data)] + [plist({"Snapshots": []})] * 8)
        result = inspect_storage(runner)
        self.assertTrue(result.truncated)
        self.assertEqual(len(runner.calls), 10)

    def test_folder_reports_logical_bytes_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first").write_bytes(b"hello")
            (root / "nested").mkdir()
            (root / "nested" / "second").write_bytes(b"123")
            (root / "link").symlink_to(root / "nested", target_is_directory=True)
            result = scan_folder(root)
            self.assertTrue(result.complete)
            self.assertEqual(result.data["logical_bytes"], 8)
            self.assertEqual(result.data["file_count"], 2)
            self.assertEqual(result.data["skipped_symlinks"], 1)
            json.dumps(result.payload())
            self.assertEqual(scan_folder(root / "link").status, "INVALID")
            self.assertEqual(scan_folder(root / "first").status, "INVALID")

    def test_hard_links_count_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first").write_bytes(b"hello")
            os.link(root / "first", root / "second")
            result = scan_folder(root)
            self.assertEqual(result.data["logical_bytes"], 5)
            self.assertEqual(result.data["file_count"], 1)

    def test_entry_limit_cancel_and_time_limit_preserve_partial_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number in range(4):
                (root / str(number)).write_bytes(b"123")
            result = scan_folder(root, max_entries=2)
            self.assertEqual(result.status, "PARTIAL")
            self.assertTrue(result.truncated)
            self.assertEqual(result.data["visited_entries"], 2)
            self.assertEqual(result.data["logical_bytes"], 6)
            cancel = threading.Event()
            cancel.set()
            self.assertEqual(scan_folder(root, cancel=cancel).status, "CANCELLED")
            with mock.patch("porkyhud_tools.storage.time.monotonic", side_effect=(0, 11, 12)):
                self.assertEqual(scan_folder(root).data["stopped_reason"], "time limit")

    def test_folder_child_row_limit_preserves_collection_total(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number in range(140):
                (root / str(number)).write_bytes(b"x")
            result = scan_folder(root)
            self.assertEqual(result.data["logical_bytes"], 140)
            self.assertEqual(len(result.rows), 128)
            self.assertTrue(result.truncated)
            self.assertFalse(result.complete)

    def test_folder_invalid_limits_do_not_start_a_walk(self):
        for entries, timeout in ((0, 1), (True, 1), (1000001, 1), (1, float("nan")), (1, 0)):
            self.assertEqual(scan_folder("/not/a/real/path", max_entries=entries, timeout_s=timeout).status, "INVALID")

    def test_depth_limit_and_unreadable_children_remain_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one").mkdir()
            (root / "one" / "two").mkdir()
            (root / "one" / "two" / "file").write_bytes(b"secret")
            with mock.patch("porkyhud_tools.storage.MAX_DEPTH", 1):
                result = scan_folder(root)
            self.assertEqual(result.data["skipped_depth"], 1)
            self.assertFalse(result.complete)
            original = os.open
            def deny(path, flags, *args, **kwargs):
                if path == "one":
                    raise PermissionError("PRIVATE_PATH")
                return original(path, flags, *args, **kwargs)
            with mock.patch("porkyhud_tools.storage.os.open", side_effect=deny):
                result = scan_folder(root)
            self.assertEqual(result.status, "PARTIAL")
            self.assertEqual(result.data["denied_entries"], 1)
            self.assertNotIn("PRIVATE_PATH", str(result))

    def test_other_volume_directory_is_not_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "child").mkdir()
            actual = os.fstat
            def different_volume(descriptor):
                value = actual(descriptor)
                if value.st_ino == (root / "child").stat().st_ino:
                    values = list(value)
                    values[2] += 1
                    return os.stat_result(values)
                return value
            with mock.patch("porkyhud_tools.storage.os.fstat", side_effect=different_volume):
                result = scan_folder(root)
            self.assertEqual(result.data["skipped_mounts"], 1)
            self.assertEqual(result.data["file_count"], 0)

    def test_folder_comparison_validates_scope_and_partial_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file").write_bytes(b"123")
            first = scan_folder(root)
            (root / "file").write_bytes(b"12345")
            second = scan_folder(root)
            result = compare_folder_snapshots(first, second)
            self.assertEqual(result.data["logical_bytes_delta"], 2)
            self.assertTrue(result.complete)
            self.assertEqual(compare_folder_snapshots(replace(first, complete=False), second).status, "PARTIAL")
            self.assertEqual(compare_folder_snapshots(first.data, {**second.data, "complete": False}).status, "PARTIAL")
            self.assertEqual(compare_folder_snapshots(first, {}).status, "INVALID")


class PowerInspectorTests(unittest.TestCase):
    def test_sleep_owners_duration_and_known_secret_labels(self):
        runner = SequenceRunner((probe(ASSERTIONS),))
        result = inspect_sleep(runner)
        self.assertEqual(result.status, "LIVE")
        self.assertEqual(result.data["owners"][0]["duration_s"], 3723)
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result.payload()))
        self.assertIn("[redacted]", result.rows[0][-1])
        self.assertEqual(runner.calls[0][0], ("/usr/bin/pmset", "-g", "assertions"))

    def test_unrecognized_owner_rows_are_partial_and_missing_header_is_error(self):
        malformed = ASSERTIONS.replace("01:02:03", "01:99:03")
        self.assertEqual(inspect_sleep(SequenceRunner((probe(malformed),))).status, "PARTIAL")
        self.assertEqual(inspect_sleep(SequenceRunner((probe("unknown"),))).status, "ERROR")

    def test_permission_error_does_not_disclose_command_output(self):
        result = inspect_sleep(SequenceRunner((probe("PRIVATE_RAW", InventoryStatus.PERMISSION_DENIED),)))
        self.assertEqual(result.status, "PERMISSION DENIED")
        self.assertNotIn("PRIVATE", str(result))

    def battery(self, percent=80, charging=False, external=False, present=True):
        return {"present": present, "percent": percent, "is_charging": charging,
                "external_connected": external, "state": "Charging" if charging else "Discharging"}

    def test_timeline_charging_transitions_and_percent_change(self):
        samples = ({"time": "one", "battery": self.battery()}, {"time": "two", "battery": self.battery(75)},
                   {"time": "three", "battery": self.battery(76, True, True)})
        result = power_timeline_report(samples)
        self.assertEqual(result.status, "LIVE")
        self.assertEqual(result.data["percent_change"], -4)
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.rows[-1][3:5], ("External", "Charging"))

    def test_gaps_break_continuity_and_do_not_infer_a_delta(self):
        result = power_timeline_report((self.battery(), {}, self.battery(70)))
        self.assertEqual(result.status, "PARTIAL")
        self.assertIsNone(result.data["percent_change"])
        self.assertEqual(result.data["missing_count"], 1)

    def test_confirmed_no_battery_does_not_become_zero_charge(self):
        result = power_timeline_report((self.battery(None, False, True, False),))
        self.assertEqual(result.rows[0][1], "No battery")
        self.assertEqual(result.rows[0][2], "—")

    def test_standard_metric_frames_preserve_status_and_available_power(self):
        def frame(status="LIVE", power=12):
            return {"timestamp": "now", "metrics": {
                "battery_pct": {"value": 80, "status": status},
                "battery_charging": {"value": 0, "status": status},
                "battery_external": {"value": 0, "status": status},
                "package_power_w": {"value": power, "status": "LIVE"},
                "cpu_temp_c": {"value": 55, "status": "LIVE"}}}
        result = power_timeline_report((frame(), frame("STALE", 15)))
        self.assertEqual(result.rows[-1][1], "Battery unavailable")
        self.assertEqual(result.rows[-1][2], "—")
        self.assertEqual(result.rows[-1][5], "15.0 W")
        self.assertEqual(result.data["observed_ranges"]["package_power_w"], {"minimum": 12, "maximum": 15})
        self.assertEqual(result.data["events"][-1]["source_status"]["battery_pct"], "STALE")
        self.assertNotIn("energy", result.data)

    def test_metric_frames_without_current_readings_stay_unavailable(self):
        result = power_timeline_report(({"metrics": {"battery_pct": {"value": 80, "status": "STALE"}}},))
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertFalse(result.complete)
        self.assertIn("STALE", " ".join(result.details))

    def test_standard_batteryless_reading_is_affirmed_by_present_flag(self):
        result = power_timeline_report(({"metrics": {"battery_present": {"value": 0, "status": "LIVE"}}},))
        self.assertEqual(result.rows[0][1], "No battery")
        self.assertEqual(result.rows[0][2], "—")


if __name__ == "__main__":
    unittest.main()
