"""Behavioral contracts for tracked, source-tree repository tools."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def wait_for_identity(path: Path, timeout_s: float = 2.0) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            identity = int(path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            identity = 0
        if identity > 0:
            return identity
        time.sleep(0.01)
    raise AssertionError("child identity was not published")


def identity_is_absent(identity: int, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(identity, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


def terminate_identity(identity: int) -> None:
    try:
        os.kill(identity, signal.SIGKILL)
    except ProcessLookupError:
        pass


def snapshot_fixture(*commands: str):
    payload = {
        "generated_at": "fixture", "read": {}, "system": {}, "cpu": {},
        "memory": {}, "battery": {}, "sensors": {}, "disk": {},
        "processes": [{"command": command} for command in commands],
    }
    return payload


def load_tool(name: str):
    path = TOOLS / (name + ".py")
    if not path.is_file():
        raise AssertionError("missing repository tool: tools/%s.py" % name)
    spec = importlib.util.spec_from_file_location("test_%s" % name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load repository tool: %s" % name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TemporaryRepository:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="PorkyHUD repository tools with spaces ",
        )
        self.root = Path(self._temporary.name)
        subprocess.run(
            ["git", "init", "-q", str(self.root)], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def close(self) -> None:
        self._temporary.cleanup()

    def write(self, relative: str, text: str = "fixture\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


def agent_map(*paths: str, extra: str = "") -> str:
    rows = "\n".join(
        "| `%s` | fixture responsibility | fixture changes |" % path
        for path in paths
    )
    return (
        "# Agent fixture\n\n"
        "## Complete tracked-file map\n\n"
        "| Path | Responsibility | Change with |\n"
        "| --- | --- | --- |\n%s\n%s\n\n"
        "## Next section\n" % (rows, extra)
    )


class RepositoryValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TemporaryRepository()

    def tearDown(self) -> None:
        self.repo.close()

    def validator(self):
        return load_tool("validate_docs")

    def test_inventory_and_map_report_both_mismatch_directions(self) -> None:
        validator = self.validator()
        self.repo.write("AGENTS.md", agent_map("AGENTS.md", "gone.py"))
        self.repo.write("new source.py")
        self.assertEqual(
            validator.tracked_files(self.repo.root),
            ("AGENTS.md", "new source.py"),
        )
        self.assertEqual(
            validator.validate_tracked_map(self.repo.root),
            [
                "AGENTS map path is not tracked: gone.py",
                "tracked file missing from AGENTS map: new source.py",
            ],
        )

    def test_inventory_rejects_unsafe_and_case_conflicting_git_paths(self) -> None:
        validator = self.validator()
        for payload in (
            b"/absolute.py\0", b"a/../b.py\0", b"a\\b.py\0",
            b"a\x01b.py\0", b"./relative.py\0",
            b"README.md\0Readme.md\0",
        ):
            result = subprocess.CompletedProcess([], 0, payload, b"")
            with self.subTest(payload=payload), mock.patch.object(
                validator.subprocess, "run", return_value=result,
            ), self.assertRaises(ValueError):
                validator.tracked_files(self.repo.root)

    def test_map_parser_is_bounded_strict_and_case_preserving(self) -> None:
        validator = self.validator()
        valid = agent_map("A.py", "nested/B.md")
        self.assertEqual(
            validator.mapped_files(valid), ("A.py", "nested/B.md"),
        )
        duplicate = agent_map("A.py", "A.py")
        malformed = agent_map("A.py", extra="| no inline code | x | y |")
        with self.assertRaisesRegex(ValueError, "duplicate mapped path: A.py"):
            validator.mapped_files(duplicate)
        with self.assertRaisesRegex(ValueError, "malformed tracked-file map row"):
            validator.mapped_files(malformed)
        with self.assertRaisesRegex(ValueError, "case-conflicting mapped paths"):
            validator.mapped_files(agent_map("README.md", "Readme.md"))
        with self.assertRaisesRegex(ValueError, "one exact"):
            validator.mapped_files(valid + "\n" + valid)

    def test_python_anchors_support_constants_and_qualified_members(self) -> None:
        validator = self.validator()
        source = (
            "VALUE = 1\n"
            "class Outer:\n"
            "    field: int = 1\n"
            "    def method(self):\n"
            "        return self.field\n"
        )
        self.repo.write("module.py", source)
        agents = agent_map("AGENTS.md", "module.py")
        agents = agents.replace(
            "## Next section",
            "Anchors: `module.py::VALUE`, `module.py::Outer.method`, "
            "and `module.py::Outer.field`.\n\n## Next section",
        )
        self.repo.write("AGENTS.md", agents)
        self.assertEqual(
            validator.symbol_anchors(agents),
            (
                ("module.py", "Outer.field"),
                ("module.py", "Outer.method"),
                ("module.py", "VALUE"),
            ),
        )
        self.assertEqual(validator.validate_symbol_anchors(self.repo.root), [])
        self.repo.write(
            "AGENTS.md",
            agents.replace("Outer.method", "Outer.missing"),
        )
        self.assertEqual(
            validator.validate_symbol_anchors(self.repo.root),
            ["missing Python symbol anchor: module.py::Outer.missing"],
        )

    def test_links_cover_inline_reference_html_and_ignore_fenced_code(self) -> None:
        validator = self.validator()
        self.repo.write("assets/demo.gif", "GIF89a")
        markdown = (
            "[inline](assets/demo.gif) [reference][demo]\n"
            "<img src=\"assets/demo.gif\">\n"
            "[demo]: assets/demo.gif\n\n"
            "```markdown\n[ignored](missing.txt)\n```\n"
            "````markdown\n[also ignored](missing-two.txt)\n"
            "```\n[still ignored](missing-three.txt)\n````\n"
        )
        self.repo.write("README.md", markdown)
        self.assertEqual(validator.validate_local_links(self.repo.root), [])

    def test_links_preserve_balanced_parentheses_in_inline_destinations(self) -> None:
        validator = self.validator()
        self.repo.write("tracked(link).md")
        self.repo.write("README.md", "[nested](tracked(link).md)\n")
        self.assertEqual(validator.validate_local_links(self.repo.root), [])

    def test_links_reject_traversal_case_untracked_and_symlink_escape(self) -> None:
        validator = self.validator()
        self.repo.write(".gitignore", "loose.txt\nescape.txt\n")
        outside = Path(self.repo._temporary.name).parent / "outside-link.txt"
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        self.repo.write("Tracked.md")
        self.repo.write("loose.txt")
        os.symlink(outside, self.repo.root / "escape.txt")
        self.repo.write(
            "README.md",
            "[case](tracked.md) [untracked](loose.txt) [encoded](%2e%2e/escape) "
            "[escape](escape.txt) [outside](../../outside-link.txt)\n",
        )
        subprocess.run(
            ["git", "-C", str(self.repo.root), "add", "README.md", "Tracked.md"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        errors = validator.validate_local_links(self.repo.root)
        self.assertEqual(errors, sorted(errors))
        self.assertTrue(any("case mismatch" in error for error in errors))
        self.assertTrue(any("untracked link target" in error for error in errors))
        self.assertTrue(any("symlink escape" in error for error in errors))
        self.assertTrue(any("outside repository" in error for error in errors))

    def test_links_reject_undefined_reference_uses(self) -> None:
        validator = self.validator()
        self.repo.write("README.md", "[broken][undefined]\n")
        self.assertEqual(
            validator.validate_local_links(self.repo.root),
            ["README.md: undefined reference link: undefined"],
        )

    def test_control_validator_detects_semantic_direct_mode_drift(self) -> None:
        validator = self.validator()
        source = (ROOT / "porkyhud.py").read_text(encoding="utf-8")
        self.repo.write(
            "porkyhud.py",
            source.replace('"6": "cinema"', '"6": "processes"', 1),
        )
        self.repo.write(
            "README.md", (ROOT / "README.md").read_text(encoding="utf-8"),
        )
        self.repo.write(
            "PorkyHUD.command",
            (ROOT / "PorkyHUD.command").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "DIRECT_MODE_KEYS does not match the canonical direct-mode mapping",
            validator.validate_control_contract(self.repo.root),
        )

    def test_control_validator_reads_the_hud_handler_not_stale_source_text(self) -> None:
        validator = self.validator()
        source = (ROOT / "porkyhud.py").read_text(encoding="utf-8")
        source = source.replace(
            'elif key in (ord("r"), ord("R")):',
            'elif key in (ord("x"), ord("X")):',
            1,
        ) + '\n# stale token: ord("r")\n'
        self.repo.write("porkyhud.py", source)
        self.repo.write("README.md", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.repo.write(
            "PorkyHUD.command",
            (ROOT / "PorkyHUD.command").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "input handler missing semantic control: r",
            validator.validate_control_contract(self.repo.root),
        )

    def test_cli_has_deterministic_private_streams_and_usage_exit(self) -> None:
        self.repo.write("AGENTS.md", agent_map("AGENTS.md", "missing.py"))
        validator_path = TOOLS / "validate_docs.py"
        result = subprocess.run(
            [sys.executable, str(validator_path), "--root", str(self.repo.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.splitlines(), sorted(result.stderr.splitlines()))
        self.assertTrue(all(line.startswith("ERROR ") for line in result.stderr.splitlines()))
        self.assertNotIn(str(self.repo.root), result.stderr)
        usage = subprocess.run(
            [sys.executable, str(validator_path), "--root"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=10,
        )
        self.assertEqual(usage.returncode, 2)

    def test_cli_diagnostics_never_echo_absolute_or_control_link_text(self) -> None:
        self.repo.write("AGENTS.md", agent_map("AGENTS.md", "README.md"))
        self.repo.write(
            "README.md",
            "[absolute](/Users/alice/private) [control](bad\x1btarget)\n",
        )
        result = subprocess.run(
            [
                sys.executable, str(TOOLS / "validate_docs.py"),
                "--root", str(self.repo.root),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"/Users/alice", result.stderr)
        self.assertNotIn(b"\x1b", result.stderr)
        for line in result.stderr.splitlines():
            self.assertTrue(line.startswith(b"ERROR "))

    def test_real_control_contract_is_current(self) -> None:
        validator = self.validator()
        self.assertEqual(validator.validate_control_contract(ROOT), [])
        launcher = (ROOT / "PorkyHUD.command").read_text(encoding="utf-8")
        self.assertEqual(launcher.count("Press h for current shortcuts."), 1)
        self.assertNotIn("press u", launcher.lower())


class MeasurementHarnessTests(unittest.TestCase):
    def measure(self):
        return load_tool("measure_release")

    def test_nearest_rank_and_summary_statistics_are_exact(self) -> None:
        measure = self.measure()
        values = [float(value) for value in range(1, 21)]
        self.assertEqual(measure.nearest_rank(values, 0.95), 19.0)
        self.assertEqual(
            measure.summarize(values),
            {
                "count": 20,
                "maximum": 20.0,
                "mean": 10.5,
                "median": 10.5,
                "minimum": 1.0,
                "p95": 19.0,
            },
        )
        for invalid in ([], [1.0, float("nan")], [True]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                measure.summarize(invalid)

    def test_public_payload_redacts_private_process_and_host_material(self) -> None:
        measure = self.measure()
        payload = {
            "environment": {
                "architecture": "arm64", "chip": "Apple Silicon",
                "hostname": "private-host", "home": "/Users/private",
                "macos": "26.0", "model": "Mac fixture", "python": "3.14",
                "ram_bytes": 1024, "username": "private-user",
            },
            "argv": ["python", "secret.py"],
            "raw_argv": ["python", "raw-secret.py"],
            "pid": 99,
            "process_text": "private process text",
            "processes": [{"command": "secret.py --token private"}],
            "pty_content": "private terminal content",
            "samples_ms": [1.0, 2.0],
        }
        sanitized = measure.sanitize_public(payload)
        encoded = json.dumps(sanitized, sort_keys=True)
        for private in (
            "private-host", "/Users/private", "private-user", "secret.py",
            "raw-secret.py", "private process text", "private terminal content",
            "--token", '"pid"', '"argv"', '"processes"',
        ):
            self.assertNotIn(private, encoded)
        self.assertEqual(
            sanitized["environment"],
            {
                "architecture": "arm64", "chip": "Apple Silicon",
                "macos": "26.0", "model": "Mac fixture", "python": "3.14",
                "ram_bytes": 1024,
            },
        )

    def test_bounded_runner_terminates_a_timed_out_process_group(self) -> None:
        measure = self.measure()
        started = time.monotonic()
        with self.assertRaises(measure.MeasurementError):
            measure.run_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_s=0.1,
            )
        self.assertLess(time.monotonic() - started, 3.0)
        with self.assertRaises(ValueError):
            measure.run_bounded(
                [sys.executable, str(ROOT / "porkyhud.py"), "--setup-sensors"],
                timeout_s=1.0,
            )

    def test_bounded_runner_stops_streaming_output_at_the_capture_limit(self) -> None:
        measure = self.measure()
        with tempfile.TemporaryDirectory() as temporary:
            identity_path = Path(temporary) / "streamer.identity"
            streamer = (
                "import os,sys,time;chunk=b'x'*65536\n"
                "while True:\n os.write(sys.stdout.fileno(),chunk);time.sleep(0.005)\n"
            )
            parent = (
                "import pathlib,subprocess,sys,time;"
                "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]);"
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid),encoding='ascii');"
                "time.sleep(30)"
            )
            started = time.monotonic()
            with self.assertRaisesRegex(measure.MeasurementError, "capture limit"):
                measure.run_bounded(
                    [sys.executable, "-c", parent, str(identity_path), streamer],
                    timeout_s=2.0,
                )
            identity = wait_for_identity(identity_path)
            self.addCleanup(terminate_identity, identity)
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertTrue(identity_is_absent(identity))

    def test_bounded_runner_cleans_residual_group_after_direct_nonzero_exit(self) -> None:
        measure = self.measure()
        with tempfile.TemporaryDirectory() as temporary:
            identity_path = Path(temporary) / "grandchild.identity"
            parent = (
                "import pathlib,subprocess,sys;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid),encoding='ascii');"
                "raise SystemExit(1)"
            )
            result = measure.run_bounded(
                [sys.executable, "-c", parent, str(identity_path)], timeout_s=3.0,
            )
            identity = wait_for_identity(identity_path)
            self.addCleanup(terminate_identity, identity)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(identity_is_absent(identity))

    def test_snapshot_redaction_is_checked_for_each_process_command(self) -> None:
        measure = self.measure()
        accepted = snapshot_fixture(
            "python --password <redacted>", "python --token <redacted>",
        )
        measure._validate_snapshot_payload(accepted)
        mixed = snapshot_fixture(
            "python --password <redacted>", "python --token plain-secret",
        )
        with self.assertRaisesRegex(measure.MeasurementError, "redaction"):
            measure._validate_snapshot_payload(mixed)

    def test_snapshot_batch_pairs_one_timed_launch_with_its_own_json_and_rss(self) -> None:
        measure = self.measure()
        result = measure.BoundedResult(
            0, json.dumps(snapshot_fixture("python --token <redacted>")).encode(),
            b"123 maximum resident set size\n", 12.5,
        )
        with mock.patch.object(measure, "run_bounded", return_value=result) as run, \
                mock.patch.object(measure, "collect_environment", return_value={}):
            payload = measure.measure_snapshot(2, 15.0)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[:2], ("/usr/bin/time", "-l"))
            self.assertEqual(argv[-2:], (str(measure.APP), "--json"))
        self.assertEqual(payload["elapsed_ms"]["count"], 2)
        self.assertEqual(payload["maximum_rss_bytes"]["minimum"], 123.0)

    def test_process_descendant_aggregation_is_transitive_and_bounded(self) -> None:
        measure = self.measure()
        rows = ((10, 1), (11, 10), (12, 11), (20, 2), (10, 12))
        self.assertEqual(set(measure._descendants(1, rows)), {10, 11, 12})

    def test_launch_bins_are_half_open_and_survivors_are_identity_only(self) -> None:
        measure = self.measure()
        events = (
            (100.0, 10), (159.999, 11), (160.0, 12),
            (219.999, 13), (220.0, 14), (399.999, 15), (400.0, 16),
        )
        summary = measure.summarize_launch_events(
            events, window_start_s=100.0, window_seconds=300.0,
            is_alive=lambda pid: pid in {12, 15},
        )
        self.assertEqual(
            summary,
            {
                "launch_bins": [2, 2, 1, 0, 1],
                "maximum_per_minute": 2,
                "recorded_launches": 6,
                "survivor_count": 2,
            },
        )
        self.assertNotIn("10", json.dumps(summary, sort_keys=True))

    def test_soak_windows_compare_five_second_thread_prefix_samples(self) -> None:
        measure = self.measure()
        samples = []
        for index in range(72):
            final = index >= 12
            samples.append((
                index * 5.0,
                3.0 + (1.0 if final and index == 71 else 0.0),
                2.0,
                {"SourcePoller": 2, "AsyncPoller": 3 if final else 2},
            ))
        comparison = measure.compare_soak_windows(
            samples, interval_s=5, window_s=60,
        )
        self.assertEqual(set(comparison), {"first", "final", "stable"})
        self.assertEqual(
            set(comparison["first"]),
            {"os_threads", "python_threads", "prefixes"},
        )
        for window in (comparison["first"], comparison["final"]):
            self.assertEqual(set(window["os_threads"]), {"maximum", "p95"})
            self.assertEqual(set(window["python_threads"]), {"maximum", "p95"})
        self.assertFalse(comparison["stable"])
        self.assertEqual(comparison["first"]["prefixes"]["AsyncPoller"], 2)
        self.assertEqual(comparison["final"]["prefixes"]["AsyncPoller"], 3)

    def test_soak_detects_known_thread_replaced_by_unclassified_thread(self) -> None:
        measure = self.measure()
        events = (
            (-1.0, "start", "SourcePoller", 1),
            (10.0, "stop", "SourcePoller", 1),
            (10.0, "start", "Other", 2),
        )
        samples = tuple(
            (
                timestamp, 2.0, 2.0,
                measure._thread_prefix_counts_at(events, timestamp),
            )
            for timestamp in (0.0, 5.0, 10.0, 15.0)
        )
        comparison = measure.compare_soak_windows(
            samples, interval_s=5, window_s=10,
        )
        self.assertEqual(comparison["first"]["prefixes"]["Other"], 0)
        self.assertEqual(comparison["final"]["prefixes"]["Other"], 1)
        self.assertFalse(comparison["stable"])

    def test_timeout_cleanup_proves_direct_reap_and_grandchild_absence(self) -> None:
        measure = self.measure()
        result = measure._timeout_cleanup_probe()
        self.assertEqual(result["outcome"], "timeout")
        self.assertIs(result["direct_reaped"], True)
        self.assertIs(result["grandchild_absent"], True)
        self.assertEqual(result["cleanup"], "process_group_terminated")
        encoded = json.dumps(measure.sanitize_public(result), sort_keys=True)
        self.assertNotIn('"pid"', encoded)
        self.assertNotIn('"argv"', encoded)

    def test_timeout_probe_waits_for_a_complete_identity_record(self) -> None:
        measure = self.measure()
        identity_path = mock.Mock()
        identity_path.read_text.side_effect = ["", "123"]
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(measure.time, "sleep"):
            self.assertEqual(
                measure._await_process_identity(identity_path, process, timeout_s=1.0),
                123,
            )
        self.assertEqual(identity_path.read_text.call_count, 2)

    def test_shared_event_clock_ignores_child_monotonic_epoch_offsets(self) -> None:
        measure = self.measure()
        origin_wall_ns = 1_000_000_000_000
        origin_mono_s = 100.0
        events = (
            (measure._aligned_event_time(
                origin_wall_ns + 59_999_000_000,
                origin_wall_ns, origin_mono_s,
            ), 1),
            (measure._aligned_event_time(
                origin_wall_ns + 60_000_000_000,
                origin_wall_ns, origin_mono_s,
            ), 2),
        )
        with mock.patch.object(measure.time, "monotonic", return_value=-5000.0):
            self.assertEqual(events, ((159.999, 1), (160.0, 2)))
        summary = measure.summarize_launch_events(
            events, window_start_s=100.0, window_seconds=120.0,
            is_alive=lambda _identity: False,
        )
        self.assertEqual(summary["launch_bins"], [1, 1])

    def test_batch_guard_rejects_power_and_clock_changes_and_reports_loads(self) -> None:
        measure = self.measure()
        initial = measure.BatchAuthority("AC", 100.0, 50.0, 1.25)
        valid = measure.BatchAuthority("AC", 110.0, 60.0, 1.5)
        self.assertEqual(
            measure._finish_batch_guard(initial, valid),
            {
                "ac_state": "AC", "final_load": 1.5,
                "initial_load": 1.25, "wall_mono_drift_s": 0.0,
            },
        )
        with self.assertRaisesRegex(measure.MeasurementError, "power"):
            measure._finish_batch_guard(
                initial, measure.BatchAuthority("BATTERY", 110.0, 60.0, 1.5),
            )
        with self.assertRaisesRegex(measure.MeasurementError, "sleep|clock"):
            measure._finish_batch_guard(
                initial, measure.BatchAuthority("AC", 115.0, 60.0, 1.5),
            )

    def test_startup_probe_fails_closed_and_cleans_a_surviving_descendant(self) -> None:
        measure = self.measure()
        with tempfile.TemporaryDirectory() as temporary:
            fake_root = Path(temporary)
            identity_path = fake_root / "grandchild.identity"
            fake_app = fake_root / "porkyhud.py"
            fake_app.write_text(
                "import pathlib,subprocess,sys,time\n"
                "def main():\n"
                " p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                " pathlib.Path(%r).write_text(str(p.pid),encoding='ascii')\n"
                " sys.stdout.write('CHECKING 0/4 core sources\\r\\nWaiting for core evidence.\\r\\n'"
                "+'CPU CHECKING | MEM CHECKING | THERM CHECKING | DISK CHECKING\\r\\n'"
                "+'CPU 1%% MEM NORMAL THERM CLEAR\\r\\n');sys.stdout.flush()\n"
                " time.sleep(0.2)\n"
                " return 0\n"
                "if __name__=='__main__': raise SystemExit(main())\n"
                % str(identity_path),
                encoding="utf-8",
            )
            identity = 0
            absent = False
            try:
                with mock.patch.object(measure, "ROOT", fake_root), \
                        mock.patch.object(measure, "APP", fake_app), \
                        self.assertRaises(measure.MeasurementError):
                    measure._startup_once(2.0)
                identity = wait_for_identity(identity_path)
                absent = identity_is_absent(identity)
            finally:
                if not identity:
                    identity = wait_for_identity(identity_path)
                if not absent:
                    terminate_identity(identity)
            self.assertTrue(absent)

    def test_deterministic_cli_batches_run_from_the_documented_path(self) -> None:
        measure_path = TOOLS / "measure_release.py"
        for arguments in (
            ("environment",),
            ("pure", "--iterations", "3", "--warmup-iterations", "1"),
            ("timeout-cleanup",),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(measure_path), *arguments],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                payload = json.loads(result.stdout)
                self.assertEqual(payload["mode"], arguments[0])
                encoded = json.dumps(payload, sort_keys=True)
                self.assertNotIn(str(Path.home()), encoded)
                self.assertNotIn('"pid"', encoded)

    def test_measurement_cli_closes_unexpected_failures_without_private_traceback(self) -> None:
        measure = self.measure()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            measure, "_dispatch",
            side_effect=RuntimeError("/Users/private/raw process text"),
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = measure.main(["environment"])
        self.assertEqual(status, 1)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload,
            {"category": "host_dependent", "mode": "environment", "status": "failed"},
        )
        self.assertNotIn("private", stdout.getvalue())

    def test_cli_exposes_every_task15_batch_as_json(self) -> None:
        measure = self.measure()
        parser = measure.build_parser()
        for mode in (
            "environment", "startup", "snapshot", "pure", "live", "soak",
            "timeout-cleanup",
        ):
            with self.subTest(mode=mode):
                namespace = parser.parse_args([mode])
                self.assertEqual(namespace.mode, mode)


class DemoRendererTests(unittest.TestCase):
    def renderer(self):
        return load_tool("render_demo_gif")

    def test_demo_is_fixed_synthetic_and_marks_every_frame(self) -> None:
        renderer = self.renderer()
        self.assertEqual(renderer.WIDTH, 960)
        source = (TOOLS / "render_demo_gif.py").read_text(encoding="utf-8")
        self.assertNotIn("--no-sandbox", source)
        for frame in (0, 18, 35):
            html_text = renderer.render_html(frame, 36)
            self.assertIn("ILLUSTRATIVE DEMO · SYNTHETIC DATA", html_text)
            self.assertIn("CHECKING", html_text)
            self.assertIn("NORMAL", html_text)
            self.assertNotIn("NOMINAL", html_text)
            self.assertIn("CPU", html_text)
            self.assertIn("m sort", html_text)
            self.assertNotIn("/Users/", html_text)
            self.assertNotIn("Mac Studio", html_text)

    def test_demo_rejects_unsafe_paths_and_unbounded_frame_rates(self) -> None:
        renderer = self.renderer()
        with tempfile.TemporaryDirectory(prefix="demo root with spaces ") as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            self.assertEqual(
                renderer.safe_output_path(root, "assets/demo.gif"),
                root / "assets" / "demo.gif",
            )
            for output in ("../escape.gif", "/tmp/escape.gif", "assets/demo.png"):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    renderer.safe_output_path(root, output)
            outside = root.parent / "demo-outside"
            outside.mkdir(exist_ok=True)
            self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)
            os.symlink(outside, root / "assets" / "linked")
            with self.assertRaises(ValueError):
                renderer.safe_output_path(root, "assets/linked/demo.gif")
        for frames, fps in ((0, 12), (121, 12), (36, 0), (36, 31)):
            with self.subTest(frames=frames, fps=fps), self.assertRaises(ValueError):
                renderer.validate_render_limits(frames, fps)

    def test_renderer_children_are_no_shell_bounded_process_groups(self) -> None:
        renderer = self.renderer()

        class TimedOutProcess:
            pid = 123
            returncode = None

            def communicate(self, timeout=None):
                del timeout
                raise subprocess.TimeoutExpired(["fixture"], 0.1)

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                del timeout
                self.returncode = -15
                return self.returncode

        process = TimedOutProcess()
        def signal_group(_identity, sig):
            if sig == 0:
                raise ProcessLookupError

        with mock.patch.object(renderer.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(renderer.os, "killpg", side_effect=signal_group) as killpg, \
                self.assertRaises(renderer.RenderError):
            renderer._run([sys.executable, "fixture.py"], 0.1, "fixture")
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)
        self.assertIs(popen.call_args.kwargs["shell"], False)
        self.assertEqual(
            [call for call in killpg.call_args_list if call.args[1] != 0],
            [
                mock.call(123, renderer.signal.SIGTERM),
                mock.call(123, renderer.signal.SIGKILL),
            ],
        )

    def test_renderer_cleans_residual_group_after_direct_nonzero_exit(self) -> None:
        renderer = self.renderer()
        with tempfile.TemporaryDirectory() as temporary:
            identity_path = Path(temporary) / "grandchild.identity"
            parent = (
                "import pathlib,subprocess,sys;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid),encoding='ascii');"
                "raise SystemExit(1)"
            )
            with self.assertRaises(renderer.RenderError):
                renderer._run(
                    [sys.executable, "-c", parent, str(identity_path)],
                    3.0, "fixture",
                )
            identity = wait_for_identity(identity_path)
            self.addCleanup(terminate_identity, identity)
            self.assertTrue(identity_is_absent(identity))


if __name__ == "__main__":
    unittest.main()
