"""Behavioral contract tests for the immutable telemetry source reducer."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import inf, nan
import unittest

from porkyhud_core import (
    AttemptOutcome,
    CpuUsage,
    CpuWindow,
    DiskCapacity,
    FailureCode,
    MemoryPressure,
    MemoryPressureLevel,
    OsThermalState,
    ProcessFact,
    SourceId,
    SourceViewStatus,
    complete_failure,
    complete_success,
    cpu_window_summary,
    is_decision_ready,
    new_source,
    reduce_cpu_window,
    sample_age_s,
    source_view,
    start_attempt,
)


UTC_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
LEGAL_FAILURES = {
    AttemptOutcome.NOT_APPLICABLE: {FailureCode.NOT_APPLICABLE},
    AttemptOutcome.UNSUPPORTED: {FailureCode.UNSUPPORTED_CAPABILITY},
    AttemptOutcome.PERMISSION_DENIED: {FailureCode.PERMISSION_DENIED},
    AttemptOutcome.MISSING_DEPENDENCY: {FailureCode.MISSING_EXECUTABLE},
    AttemptOutcome.TIMEOUT: {
        FailureCode.COMMAND_TIMEOUT,
        FailureCode.DEADLINE_EXCEEDED,
    },
    AttemptOutcome.COMMAND_ERROR: {
        FailureCode.NONZERO_EXIT,
        FailureCode.DECODE_ERROR,
        FailureCode.OUTPUT_LIMIT,
    },
    AttemptOutcome.PARSE_ERROR: {
        FailureCode.EMPTY_OUTPUT,
        FailureCode.PARSER_REJECTED,
    },
    AttemptOutcome.CANCELLED: {FailureCode.USER_CANCELLED},
    AttemptOutcome.UNKNOWN_ERROR: {FailureCode.UNEXPECTED_EXCEPTION},
}


class SourceStateTests(unittest.TestCase):
    def successful_source(self, source_id, cadence, stale_after, completed):
        state = new_source(source_id, cadence, stale_after)
        state, revision = start_attempt(state, completed - 0.2)
        return complete_success(state, revision, "normal", completed, UTC_NOW)

    def failed_source(self, source_id, outcome, failure):
        state = new_source(source_id, 1.0, 2.5)
        state, revision = start_attempt(state, 10.0)
        return complete_failure(state, revision, outcome, failure, 10.2, UTC_NOW)

    def test_failure_retains_last_valid_but_blocks_decisions(self):
        state = new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        state, revision = start_attempt(state, 10.0)
        state = complete_success(state, revision, "normal", 10.2, UTC_NOW)
        state, revision = start_attempt(state, 11.0)
        failed = complete_failure(
            state,
            revision,
            AttemptOutcome.TIMEOUT,
            FailureCode.COMMAND_TIMEOUT,
            12.0,
            UTC_NOW,
        )
        self.assertEqual(failed.last_valid.revision, 1)
        self.assertFalse(is_decision_ready(failed, 12.0))
        self.assertEqual(source_view(failed, 12.0).status, SourceViewStatus.ERROR)

    def test_active_refresh_keeps_fresh_success_live(self):
        state = self.successful_source(SourceId.CPU_USAGE, 1.0, 2.5, 10.0)
        refreshing, _ = start_attempt(state, 11.0)
        view = source_view(refreshing, 11.1)
        self.assertEqual(view.status, SourceViewStatus.LIVE)
        self.assertTrue(view.refreshing)
        self.assertTrue(view.decision_ready)

    def test_source_configuration_requires_finite_ordered_positive_values(self):
        for cadence, stale_after in ((0.0, 1.0), (-1.0, 1.0), (nan, 1.0),
                                     (inf, 1.0), (1.0, 0.0), (1.0, nan),
                                     (1.0, inf), (2.0, 1.0)):
            with self.subTest(cadence=cadence, stale_after=stale_after):
                with self.assertRaises(ValueError):
                    new_source(SourceId.CPU_USAGE, cadence, stale_after)

    def test_source_id_must_be_an_enum_member(self):
        with self.assertRaises(ValueError):
            new_source(SourceId.SENSOR_POWERMETRICS.value, 1.0, 2.5)

    def test_source_configuration_rejects_boolean_values(self):
        for cadence, stale_after in ((True, 2.5), (1.0, True)):
            with self.subTest(cadence=cadence, stale_after=stale_after):
                with self.assertRaises(ValueError):
                    new_source(SourceId.CPU_USAGE, cadence, stale_after)

    def test_reducers_return_new_immutable_state_and_increase_revisions(self):
        initial = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, first_revision = start_attempt(initial, 10.0)
        complete = complete_success(active, first_revision, "normal", 10.2, UTC_NOW)
        next_active, second_revision = start_attempt(complete, 11.0)
        self.assertIsNot(initial, active)
        self.assertEqual(initial.revision, 0)
        self.assertEqual(active.revision, 1)
        self.assertEqual(complete.active_revision, None)
        self.assertEqual((first_revision, second_revision), (1, 2))
        with self.assertRaises(Exception):
            complete.revision = 99
        self.assertEqual(next_active.active_revision, 2)

    def test_attempt_times_must_be_finite_and_completion_cannot_precede_start(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        for started in (nan, inf, -inf):
            with self.subTest(started=started):
                with self.assertRaises(ValueError):
                    start_attempt(state, started)
        active, revision = start_attempt(state, 10.0)
        for completed in (nan, inf, -inf, 9.9):
            with self.subTest(completed=completed):
                with self.assertRaises(ValueError):
                    complete_success(active, revision, "normal", completed, UTC_NOW)

    def test_attempt_start_rejects_boolean_monotonic_time(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        with self.assertRaises(ValueError):
            start_attempt(state, True)

    def test_completion_rejects_boolean_monotonic_time(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, revision = start_attempt(state, 0.0)
        with self.assertRaises(ValueError):
            complete_success(active, revision, "normal", True, UTC_NOW)

    def test_sample_age_rejects_boolean_current_time(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, revision = start_attempt(state, 0.0)
        complete = complete_success(active, revision, "normal", 0.5, UTC_NOW)
        with self.assertRaises(ValueError):
            sample_age_s(complete, True)

    def test_completions_require_an_active_matching_revision_once(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, revision = start_attempt(state, 10.0)
        with self.assertRaises(ValueError):
            complete_success(active, revision + 1, "normal", 10.1, UTC_NOW)
        complete = complete_success(active, revision, "normal", 10.1, UTC_NOW)
        with self.assertRaises(ValueError):
            complete_success(complete, revision, "normal", 10.2, UTC_NOW)

    def test_completions_require_plain_integer_revisions(self):
        for completion in ("success", "failure"):
            for invalid_revision in (1.0, True):
                with self.subTest(
                    completion=completion,
                    invalid_revision=invalid_revision,
                ):
                    state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
                    active, _ = start_attempt(state, 10.0)
                    with self.assertRaises(ValueError):
                        if completion == "success":
                            complete_success(
                                active,
                                invalid_revision,
                                "normal",
                                10.1,
                                UTC_NOW,
                            )
                        else:
                            complete_failure(
                                active,
                                invalid_revision,
                                AttemptOutcome.TIMEOUT,
                                FailureCode.COMMAND_TIMEOUT,
                                10.1,
                                UTC_NOW,
                            )

    def test_completed_timestamp_requires_timezone_and_normalizes_to_utc(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, revision = start_attempt(state, 10.0)
        with self.assertRaises(ValueError):
            complete_success(active, revision, "normal", 10.1, UTC_NOW.replace(tzinfo=None))
        local_time = UTC_NOW.astimezone(timezone(timedelta(hours=-5)))
        complete = complete_success(active, revision, "normal", 10.1, local_time)
        self.assertEqual(complete.last_valid.completed_at_utc, UTC_NOW)
        self.assertEqual(complete.last_valid.completed_at_utc.tzinfo, timezone.utc)

    def test_outcome_failure_matrix_is_exhaustive(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, revision = start_attempt(state, 10.0)
        successful = complete_success(active, revision, "normal", 10.1, UTC_NOW)
        self.assertEqual(successful.last_attempt.outcome, AttemptOutcome.SUCCESS)
        self.assertIsNone(successful.last_attempt.failure)

        candidates = (None,) + tuple(FailureCode)
        for outcome in AttemptOutcome:
            for failure in candidates:
                state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
                active, revision = start_attempt(state, 10.0)
                legal_failures = LEGAL_FAILURES.get(outcome, set())
                if outcome is not AttemptOutcome.SUCCESS and failure in legal_failures:
                    complete = complete_failure(active, revision, outcome, failure, 10.1, UTC_NOW)
                    self.assertEqual(complete.last_attempt.failure, failure)
                else:
                    with self.assertRaises(ValueError):
                        complete_failure(active, revision, outcome, failure, 10.1, UTC_NOW)

    def test_failure_outcome_and_code_must_be_enum_members(self):
        cases = (
            (
                AttemptOutcome.PERMISSION_DENIED.value,
                FailureCode.PERMISSION_DENIED,
            ),
            (
                AttemptOutcome.PERMISSION_DENIED,
                FailureCode.PERMISSION_DENIED.value,
            ),
        )
        for outcome, failure in cases:
            with self.subTest(outcome=outcome, failure=failure):
                state = new_source(SourceId.SENSOR_POWERMETRICS, 1.0, 2.5)
                active, revision = start_attempt(state, 10.0)
                with self.assertRaises(ValueError):
                    complete_failure(
                        active,
                        revision,
                        outcome,
                        failure,
                        10.1,
                        UTC_NOW,
                    )

    def test_freshness_boundary_is_inclusive_and_stale_after_is_strict(self):
        state = self.successful_source(SourceId.CPU_USAGE, 1.0, 2.5, 10.0)
        self.assertEqual(sample_age_s(state, 12.5), 2.5)
        self.assertTrue(is_decision_ready(state, 12.5))
        self.assertEqual(source_view(state, 12.5).status, SourceViewStatus.LIVE)
        self.assertFalse(is_decision_ready(state, 12.5001))
        self.assertEqual(source_view(state, 12.5001).status, SourceViewStatus.STALE)

    def test_view_maps_proven_failures_without_inferring_not_exposed(self):
        cases = (
            (SourceId.BATTERY, AttemptOutcome.NOT_APPLICABLE, FailureCode.NOT_APPLICABLE,
             SourceViewStatus.NOT_APPLICABLE),
            (SourceId.CPU_USAGE, AttemptOutcome.UNSUPPORTED, FailureCode.UNSUPPORTED_CAPABILITY,
             SourceViewStatus.UNSUPPORTED),
            (SourceId.CPU_USAGE, AttemptOutcome.MISSING_DEPENDENCY, FailureCode.MISSING_EXECUTABLE,
             SourceViewStatus.MISSING),
            (SourceId.CPU_USAGE, AttemptOutcome.TIMEOUT, FailureCode.COMMAND_TIMEOUT,
             SourceViewStatus.ERROR),
        )
        for source_id, outcome, failure, expected_status in cases:
            with self.subTest(outcome=outcome):
                self.assertEqual(source_view(self.failed_source(source_id, outcome, failure), 10.2).status,
                                 expected_status)

    def test_permission_mapping_locks_only_powermetrics(self):
        locked = self.failed_source(
            SourceId.SENSOR_POWERMETRICS,
            AttemptOutcome.PERMISSION_DENIED,
            FailureCode.PERMISSION_DENIED,
        )
        denied = self.failed_source(
            SourceId.SENSOR_SMC,
            AttemptOutcome.PERMISSION_DENIED,
            FailureCode.PERMISSION_DENIED,
        )
        self.assertIs(locked.source_id, SourceId.SENSOR_POWERMETRICS)
        self.assertIs(locked.last_attempt.outcome, AttemptOutcome.PERMISSION_DENIED)
        self.assertIs(locked.last_attempt.failure, FailureCode.PERMISSION_DENIED)
        self.assertEqual(source_view(locked, 10.2).status, SourceViewStatus.LOCKED)
        self.assertEqual(source_view(denied, 10.2).status, SourceViewStatus.PERMISSION_DENIED)

    def test_initial_and_expired_refresh_views_are_checking_and_stale(self):
        initial = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        self.assertEqual(source_view(initial, 0.0).status, SourceViewStatus.CHECKING)
        active, _ = start_attempt(initial, 10.0)
        self.assertEqual(source_view(active, 10.1).status, SourceViewStatus.CHECKING)
        expired = self.successful_source(SourceId.CPU_USAGE, 1.0, 2.5, 10.0)
        refreshing, _ = start_attempt(expired, 13.0)
        view = source_view(refreshing, 13.0)
        self.assertEqual(view.status, SourceViewStatus.STALE)
        self.assertTrue(view.refreshing)
        self.assertFalse(view.decision_ready)


class CpuWindowTests(unittest.TestCase):
    def complete_cpu(self, source, started, completed, percent):
        active, revision = start_attempt(source, started)
        return complete_success(
            active,
            revision,
            CpuUsage(percent, started, completed),
            completed,
            UTC_NOW,
        )

    def failed_cpu(self, source, started, completed):
        active, revision = start_attempt(source, started)
        return complete_failure(
            active,
            revision,
            AttemptOutcome.TIMEOUT,
            FailureCode.COMMAND_TIMEOUT,
            completed,
            UTC_NOW,
        )

    def test_time_weighted_window_clips_oldest_segment(self):
        window = CpuWindow()
        source = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        for start, end, percent in ((0.0, 6.0, 100.0),
                                    (6.0, 12.0, 50.0),
                                    (12.0, 18.0, 100.0)):
            source = self.complete_cpu(source, start, end, percent)
            window = reduce_cpu_window(window, source)
        summary = cpu_window_summary(window, 15.0)
        self.assertAlmostEqual(summary.duration_s, 15.0)
        self.assertAlmostEqual(summary.mean_percent, 80.0)
        self.assertEqual(summary.started_mono_s, 3.0)
        self.assertEqual(summary.completed_mono_s, 18.0)

    def test_complete_windows_preserve_exact_90_and_70_percent_means(self):
        for percent in (90.0, 70.0):
            with self.subTest(percent=percent):
                source = self.complete_cpu(
                    new_source(SourceId.CPU_USAGE, 1.0, 2.5),
                    0.0,
                    15.0,
                    percent,
                )
                summary = cpu_window_summary(
                    reduce_cpu_window(CpuWindow(), source), 15.0
                )
                self.assertEqual(summary.mean_percent, percent)

    def test_gap_of_exactly_2_5_seconds_is_retained_but_not_represented(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 5.0, 60.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        source = self.complete_cpu(source, 7.5, 12.5, 60.0)
        retained = reduce_cpu_window(window, source)
        self.assertEqual(len(retained.samples), 2)
        self.assertIsNone(cpu_window_summary(retained, 12.5))

    def test_gap_strictly_greater_than_2_5_seconds_resets_to_current_sample(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 5.0, 60.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        source = self.complete_cpu(source, 7.5001, 12.5, 60.0)
        reset = reduce_cpu_window(window, source)
        self.assertEqual(len(reset.samples), 1)
        self.assertEqual(reset.samples[0].revision, source.last_valid.revision)

    def test_overlapping_and_reversed_windows_reset_to_current_sample(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 5.0, 60.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        active, revision = start_attempt(source, 5.0)
        source = complete_success(
            active, revision, CpuUsage(70.0, 4.0, 9.0), 9.0, UTC_NOW
        )
        overlapped = reduce_cpu_window(window, source)
        self.assertEqual(len(overlapped.samples), 1)

        reversed_source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 8.0, 60.0
        )
        reversed_window = reduce_cpu_window(CpuWindow(), reversed_source)
        active, revision = start_attempt(reversed_source, 8.0)
        reversed_source = complete_success(
            active, revision, CpuUsage(70.0, 5.0, 7.0), 9.0, UTC_NOW
        )
        reversed_result = reduce_cpu_window(reversed_window, reversed_source)
        self.assertEqual(len(reversed_result.samples), 1)

    def test_failed_latest_attempt_clears_window(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 5.0, 60.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        failed = self.failed_cpu(source, 5.0, 6.0)
        cleared = reduce_cpu_window(window, failed)
        self.assertEqual(cleared.samples, ())
        self.assertEqual(cleared.last_attempt_revision, failed.last_attempt.revision)

    def test_duplicate_revision_is_idempotent(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 5.0, 60.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        self.assertIs(reduce_cpu_window(window, source), window)

    def test_counter_reset_revision_replaces_existing_history(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 5.0, 60.0
        )
        window = CpuWindow(reduce_cpu_window(CpuWindow(), source).samples, 5)
        reset = reduce_cpu_window(window, source)
        self.assertEqual(reset.samples, (source.last_valid,))
        self.assertEqual(reset.last_attempt_revision, 1)

    def test_cpu_window_rejects_source_mismatches_and_corrupt_successes(self):
        with self.assertRaises(ValueError):
            reduce_cpu_window(
                CpuWindow(), new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5)
            )
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 5.0, 60.0
        )
        corrupt = replace(source, last_valid=replace(source.last_valid, revision=2))
        with self.assertRaises(ValueError):
            reduce_cpu_window(CpuWindow(), corrupt)
        corrupt_attempt = replace(
            source,
            last_attempt=replace(
                source.last_attempt, failure=FailureCode.COMMAND_TIMEOUT
            ),
        )
        with self.assertRaises(ValueError):
            reduce_cpu_window(CpuWindow(), corrupt_attempt)

    def test_window_retains_the_15_second_history_and_boundary_sample(self):
        source = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        window = CpuWindow()
        for started, completed in ((0.0, 6.0), (6.0, 12.0), (12.0, 18.0),
                                   (18.0, 24.0)):
            source = self.complete_cpu(source, started, completed, 50.0)
            window = reduce_cpu_window(window, source)
        self.assertEqual(
            tuple(sample.value.window_started_mono_s for sample in window.samples),
            (6.0, 12.0, 18.0),
        )

    def test_summary_requires_complete_continuous_representation(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 10.0, 80.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        self.assertIsNone(cpu_window_summary(window, 15.0))
        for duration in (0.0, -1.0, True, nan, inf):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    cpu_window_summary(window, duration)

    def test_cpu_usage_rejects_invalid_percentages_and_windows(self):
        for percent in (-0.1, 100.1, True, nan, inf, -inf):
            with self.subTest(percent=percent):
                with self.assertRaises(ValueError):
                    CpuUsage(percent, 0.0, 1.0)
        for started, completed in ((True, 1.0), (0.0, True), (nan, 1.0),
                                   (0.0, inf), (1.0, 1.0), (1.0, 0.0)):
            with self.subTest(started=started, completed=completed):
                with self.assertRaises(ValueError):
                    CpuUsage(50.0, started, completed)

    def test_normalized_facts_enforce_identity_and_numeric_invariants(self):
        self.assertEqual(MemoryPressure(MemoryPressureLevel.NORMAL).level,
                         MemoryPressureLevel.NORMAL)
        with self.assertRaises(ValueError):
            MemoryPressure(1)
        for thermal, performance in ((-1, 0), (0, -1), (True, 0), (0, True)):
            with self.subTest(thermal=thermal, performance=performance):
                with self.assertRaises(ValueError):
                    OsThermalState(thermal, performance)
        for total, free in ((-1, 0), (0, 0), (1, -1), (1, 2),
                            (True, 0), (1, True)):
            with self.subTest(total=total, free=free):
                with self.assertRaises(ValueError):
                    DiskCapacity("disk1", "Data", "/", total, free)
        for values in (("", "Data", "/"), ("disk1", "", "/"),
                       ("disk1", "Data", "")):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    DiskCapacity(*values, 1, 0)
        for pid, name, percent, elapsed in ((0, "App", 1.0, None),
                                             (True, "App", 1.0, None),
                                             (1, "", 1.0, None),
                                             (1, "App", True, None),
                                             (1, "App", 100.1, None),
                                             (1, "App", 1.0, -1),
                                             (1, "App", 1.0, True)):
            with self.subTest(pid=pid, name=name, percent=percent, elapsed=elapsed):
                with self.assertRaises(ValueError):
                    ProcessFact(pid, name, percent, elapsed)


if __name__ == "__main__":
    unittest.main()
