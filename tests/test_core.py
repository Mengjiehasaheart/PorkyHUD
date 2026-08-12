"""Behavioral contract tests for the immutable telemetry source reducer."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import inf, nan
import unittest

from porkyhud_core import (
    AttemptOutcome,
    CpuUsage,
    CpuWindow,
    CpuWindowSummary,
    DiskCapacity,
    FailureCode,
    HealthSeverity,
    LatchPhase,
    MemoryPressure,
    MemoryPressureLevel,
    OsThermalState,
    ProcessFact,
    Sample,
    SourceId,
    SourceViewStatus,
    RuleId,
    RuleLatch,
    TriggerEvidence,
    complete_failure,
    complete_success,
    cpu_window_summary,
    is_decision_ready,
    new_source,
    reduce_cpu_window,
    reduce_cpu_latch,
    reduce_memory_latch,
    reduce_storage_latch,
    reduce_thermal_latch,
    empty_latch,
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
    def cpu_sample(self, revision, started, completed, percent=50.0):
        return Sample(
            CpuUsage(percent, started, completed),
            revision,
            started,
            completed,
            UTC_NOW,
        )

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

    def test_gap_of_exactly_2_5_seconds_preserves_10_represented_seconds(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 5.0, 100.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        source = self.complete_cpu(source, 7.5, 12.5, 0.0)
        retained = reduce_cpu_window(window, source)
        self.assertEqual(len(retained.samples), 2)
        summary = cpu_window_summary(retained, 10.0)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.duration_s, 10.0)
        self.assertEqual(summary.mean_percent, 50.0)
        self.assertEqual(summary.started_mono_s, 0.0)
        self.assertEqual(summary.completed_mono_s, 12.5)

    def test_allowed_gaps_preserve_15_represented_seconds_and_clip_oldest(self):
        source = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        window = CpuWindow()
        for start, end, percent in ((0.0, 6.0, 100.0),
                                    (8.0, 14.0, 50.0),
                                    (16.5, 22.5, 100.0)):
            source = self.complete_cpu(source, start, end, percent)
            window = reduce_cpu_window(window, source)
        summary = cpu_window_summary(window, 15.0)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.duration_s, 15.0)
        self.assertEqual(summary.mean_percent, 80.0)
        self.assertEqual(summary.started_mono_s, 3.0)
        self.assertEqual(summary.completed_mono_s, 22.5)

    def test_summary_span_may_include_unrepresented_observation_gaps(self):
        summary = CpuWindowSummary(10.0, 50.0, 0.0, 12.5)
        self.assertEqual(summary.completed_mono_s - summary.started_mono_s, 12.5)

    def test_summary_wall_span_cannot_be_shorter_than_represented_duration(self):
        self.assertEqual(CpuWindowSummary(10.0, 50.0, 0.0, 10.0).duration_s,
                         10.0)
        self.assertEqual(CpuWindowSummary(10.0, 50.0, 0.0, 12.5).duration_s,
                         10.0)
        with self.assertRaises(ValueError):
            CpuWindowSummary(10.0, 50.0, 0.0, 5.0)

    def test_summary_rejects_boolean_and_nonfinite_fields(self):
        cases = (
            (True, 50.0, 0.0, 10.0),
            (nan, 50.0, 0.0, 10.0),
            (inf, 50.0, 0.0, 10.0),
            (10.0, True, 0.0, 10.0),
            (10.0, nan, 0.0, 10.0),
            (10.0, inf, 0.0, 10.0),
            (10.0, 50.0, True, 10.0),
            (10.0, 50.0, nan, 10.0),
            (10.0, 50.0, -inf, 10.0),
            (10.0, 50.0, 0.0, True),
            (10.0, 50.0, 0.0, nan),
            (10.0, 50.0, 0.0, inf),
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    CpuWindowSummary(*values)

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
        window = CpuWindow((self.cpu_sample(5, 0.0, 5.0, 80.0),), 5)
        reset = reduce_cpu_window(window, source)
        self.assertEqual(reset.samples, (source.last_valid,))
        self.assertEqual(reset.last_attempt_revision, 1)

    def test_direct_cpu_window_accepts_consecutive_samples_and_exact_gap(self):
        first = self.cpu_sample(1, 0.0, 1.0)
        adjacent = self.cpu_sample(2, 1.0, 2.0)
        exact_gap = self.cpu_sample(2, 3.5, 4.5)

        self.assertEqual(CpuWindow((first, adjacent), 2).samples[-1], adjacent)
        self.assertEqual(CpuWindow((first, exact_gap), 2).samples[-1], exact_gap)

    def test_direct_cpu_window_rejects_forged_history(self):
        first = self.cpu_sample(1, 0.0, 1.0)
        cases = (
            ((first, self.cpu_sample(3, 1.0, 2.0)), 3),
            ((first, self.cpu_sample(2, 3.5001, 4.5001)), 2),
            ((first, self.cpu_sample(2, 1.0, 2.0)), 3),
        )
        for samples, last_attempt_revision in cases:
            with self.subTest(samples=samples,
                              last_attempt_revision=last_attempt_revision):
                with self.assertRaises(ValueError):
                    CpuWindow(samples, last_attempt_revision)

        self.assertEqual(CpuWindow((), 7).last_attempt_revision, 7)

    def test_skipped_failed_revision_cannot_be_bridged_by_later_success(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 1.0, 50.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        source = self.failed_cpu(source, 1.0, 2.0)
        source = self.complete_cpu(source, 2.0, 3.0, 70.0)

        reset = reduce_cpu_window(window, source)

        self.assertEqual(tuple(sample.revision for sample in reset.samples), (3,))
        self.assertEqual(reset.last_attempt_revision, 3)

    def test_skipped_successful_revision_cannot_be_assumed_contiguous(self):
        source = self.complete_cpu(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), 0.0, 1.0, 50.0
        )
        window = reduce_cpu_window(CpuWindow(), source)
        source = self.complete_cpu(source, 1.0, 2.0, 60.0)
        source = self.complete_cpu(source, 2.0, 3.0, 70.0)

        reset = reduce_cpu_window(window, source)

        self.assertEqual(tuple(sample.revision for sample in reset.samples), (3,))
        self.assertEqual(reset.last_attempt_revision, 3)

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
        tiny_summary = cpu_window_summary(window, 1e-10)
        self.assertIsNotNone(tiny_summary)
        self.assertEqual(tiny_summary.duration_s, 1e-10)
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
                                             (1, "App", 1.0, -1),
                                             (1, "App", 1.0, True)):
            with self.subTest(pid=pid, name=name, percent=percent, elapsed=elapsed):
                with self.assertRaises(ValueError):
                    ProcessFact(pid, name, percent, elapsed)

    def test_process_cpu_allows_multithreaded_ps_values_but_rejects_nonfinite(self):
        self.assertEqual(ProcessFact(1, "compiler", 250.0, 3).cpu_percent, 250.0)
        for percent in (-0.1, True, nan, inf, -inf):
            with self.subTest(percent=percent):
                with self.assertRaises(ValueError):
                    ProcessFact(1, "compiler", percent, 3)

    def test_segmented_exact_threshold_means_remain_on_the_safe_side(self):
        for percent in (90.0, 70.0):
            with self.subTest(percent=percent):
                source = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
                window = CpuWindow()
                for index in range(150):
                    source = self.complete_cpu(
                        source, index / 10.0, (index + 1) / 10.0, percent
                    )
                    window = reduce_cpu_window(window, source)
                summary = cpu_window_summary(window, 15.0)
                self.assertIsNotNone(summary)
                self.assertAlmostEqual(summary.mean_percent, percent)
                if percent == 90.0:
                    self.assertGreaterEqual(summary.mean_percent, 90.0)
                else:
                    self.assertLessEqual(summary.mean_percent, 70.0)


class RuleLatchTests(unittest.TestCase):
    """Exact entry, evidence, and conservative recovery contracts."""

    def source(self, source_id, value, completed, cadence=1.0, stale=2.5):
        state = new_source(source_id, cadence, stale)
        return self.next_success(state, value, completed)

    def next_success(self, state, value, completed):
        state, revision = start_attempt(state, completed - 0.1)
        return complete_success(state, revision, value, completed, UTC_NOW)

    def next_failure(self, state, completed):
        state, revision = start_attempt(state, completed - 0.1)
        return complete_failure(state, revision, AttemptOutcome.TIMEOUT,
                                FailureCode.COMMAND_TIMEOUT, completed, UTC_NOW)

    def memory_source(self, level, completed, state=None):
        if state is None:
            state = new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        return self.next_success(state, MemoryPressure(MemoryPressureLevel(level)), completed)

    def thermal_source(self, thermal, performance, completed, state=None):
        if state is None:
            state = new_source(SourceId.THERMAL_OS, 5.0, 12.0)
        return self.next_success(state, OsThermalState(thermal, performance), completed)

    def disk_source(self, total, free, completed, state=None, volume_id="disk1", mount_point="/"):
        if state is None:
            state = new_source(SourceId.DISK_CAPACITY, 30.0, 75.0)
        return self.next_success(
            state, DiskCapacity(volume_id, "Data", mount_point, total, free), completed
        )

    def cpu_source_and_window(self, samples):
        source = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        window = CpuWindow()
        for started, completed, percent in samples:
            source = self.next_success(source, CpuUsage(percent, started, completed), completed)
            window = reduce_cpu_window(window, source)
        return source, window

    def test_storage_requires_both_entry_thresholds(self):
        gib = 1024 ** 3
        cases = (
            (100 * gib, 9 * gib, True),
            (300 * gib, 25 * gib, False),
            (100 * gib, 10 * gib, False),
            (1000 * gib, 19 * gib, True),
        )
        for total, free, expected_active in cases:
            with self.subTest(total=total, free=free):
                source = self.disk_source(total, free, 30.0)
                latch = reduce_storage_latch(empty_latch(RuleId.STORAGE_WATCH), source, 30.0)
                self.assertEqual(latch.phase is not LatchPhase.CLEAR, expected_active)

    def test_only_memory_four_creates_critical_latch(self):
        for level, severity in ((1, None), (2, HealthSeverity.WATCH),
                                (4, HealthSeverity.CRITICAL)):
            with self.subTest(level=level):
                latch = reduce_memory_latch(
                    empty_latch(RuleId.MEMORY_WATCH), self.memory_source(level, 1.0), 1.0
                )
                self.assertEqual(latch.severity, severity)

    def test_memory_normal_recovery_needs_ten_samples_spanning_exactly_nine_seconds(self):
        state = self.memory_source(2, -1.0)
        latch = reduce_memory_latch(empty_latch(RuleId.MEMORY_WATCH), state, -1.0)
        state = self.memory_source(1, 0.0, state)
        latch = reduce_memory_latch(latch, state, 0.0)
        for completed in range(1, 9):
            state = self.memory_source(1, float(completed), state)
            latch = reduce_memory_latch(latch, state, float(completed))
        self.assertEqual(latch.phase, LatchPhase.RECOVERING)
        state = self.memory_source(1, 9.0, state)
        self.assertEqual(reduce_memory_latch(latch, state, 9.0).phase, LatchPhase.CLEAR)

    def test_memory_critical_needs_five_noncritical_samples_spanning_four_seconds_before_downgrade(self):
        state = self.memory_source(4, -1.0)
        latch = reduce_memory_latch(empty_latch(RuleId.MEMORY_WATCH), state, -1.0)
        state = self.memory_source(1, 0.0, state)
        for completed in range(4):
            if completed:
                state = self.memory_source(1, float(completed), state)
            latch = reduce_memory_latch(latch, state, float(completed))
        self.assertEqual(latch.severity, HealthSeverity.CRITICAL)
        state = self.memory_source(1, 4.0, state)
        latch = reduce_memory_latch(latch, state, 4.0)
        self.assertEqual((latch.severity, latch.rule_id, latch.phase),
                         (HealthSeverity.WATCH, RuleId.MEMORY_WATCH, LatchPhase.RECOVERING))

    def test_memory_critical_to_elevated_is_truthful_and_resets_normal_recovery(self):
        state = self.memory_source(4, -1.0)
        latch = reduce_memory_latch(empty_latch(RuleId.MEMORY_WATCH), state, -1.0)
        state = self.memory_source(2, 0.0, state)
        latch = reduce_memory_latch(latch, state, 0.0)
        self.assertEqual((latch.severity, latch.phase, latch.recovery_count),
                         (HealthSeverity.CRITICAL, LatchPhase.RECOVERING, 1))
        for completed in range(1, 5):
            state = self.memory_source(2, float(completed), state)
            latch = reduce_memory_latch(latch, state, float(completed))
        self.assertEqual((latch.severity, latch.rule_id),
                         (HealthSeverity.WATCH, RuleId.MEMORY_WATCH))

    def test_ten_normal_samples_clear_a_critical_memory_latch_on_the_tenth(self):
        state = self.memory_source(4, -1.0)
        latch = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), state, -1.0
        )
        for completed in range(10):
            state = self.memory_source(1, float(completed), state)
            latch = reduce_memory_latch(latch, state, float(completed))
            if completed < 9:
                self.assertIsNot(latch.phase, LatchPhase.CLEAR)
        self.assertEqual(latch.phase, LatchPhase.CLEAR)

    def test_critical_downgrade_carries_only_trailing_normal_samples(self):
        state = self.memory_source(4, -1.0)
        latch = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), state, -1.0
        )
        for completed, level in enumerate((1, 1, 2, 1, 1)):
            state = self.memory_source(level, float(completed), state)
            latch = reduce_memory_latch(latch, state, float(completed))
        self.assertEqual(
            (
                latch.rule_id,
                latch.severity,
                latch.phase,
                latch.recovery_count,
                latch.recovery_started_mono_s,
            ),
            (
                RuleId.MEMORY_WATCH,
                HealthSeverity.WATCH,
                LatchPhase.RECOVERING,
                2,
                3.0,
            ),
        )

    def test_thermal_requires_three_clear_samples_spanning_exactly_ten_seconds(self):
        state = self.thermal_source(1, 0, -1.0)
        latch = reduce_thermal_latch(empty_latch(RuleId.THERMAL_WATCH), state, -1.0)
        state = self.thermal_source(0, 0, 0.0, state)
        latch = reduce_thermal_latch(latch, state, 0.0)
        state = self.thermal_source(0, 0, 5.0, state)
        latch = reduce_thermal_latch(latch, state, 5.0)
        self.assertEqual(latch.phase, LatchPhase.RECOVERING)
        state = self.thermal_source(0, 0, 10.0, state)
        self.assertEqual(reduce_thermal_latch(latch, state, 10.0).phase, LatchPhase.CLEAR)

    def test_cpu_enters_at_fifteen_represented_seconds_and_recovers_at_ten(self):
        entry_source, entry_window = self.cpu_source_and_window(((0.0, 15.0, 90.0),))
        latch = reduce_cpu_latch(empty_latch(RuleId.CPU_WATCH), entry_source, entry_window, 15.0)
        self.assertEqual(latch.severity, HealthSeverity.WATCH)
        short_source, short_window = self.cpu_source_and_window(((0.0, 14.999, 90.0),))
        self.assertEqual(reduce_cpu_latch(empty_latch(RuleId.CPU_WATCH), short_source, short_window, 14.999).phase,
                         LatchPhase.CLEAR)
        entry_source = self.next_success(entry_source, CpuUsage(15.0, 25.0, 70.0), 25.0)
        entry_window = reduce_cpu_window(entry_window, entry_source)
        latch = reduce_cpu_latch(latch, entry_source, entry_window, 25.0)
        self.assertEqual(latch.phase, LatchPhase.CLEAR)

    def test_cpu_windows_never_round_short_duration_up_to_policy_thresholds(self):
        short_entry = 14.9999999995
        source, window = self.cpu_source_and_window(
            ((0.0, short_entry, 90.0),)
        )
        latch = reduce_cpu_latch(
            empty_latch(RuleId.CPU_WATCH), source, window, short_entry
        )
        self.assertEqual(latch.phase, LatchPhase.CLEAR)

        source, window = self.cpu_source_and_window(((0.0, 15.0, 90.0),))
        active = reduce_cpu_latch(
            empty_latch(RuleId.CPU_WATCH), source, window, 15.0
        )
        short_recovery_end = 24.9999999995
        source = self.next_success(
            source,
            CpuUsage(70.0, 15.0, short_recovery_end),
            short_recovery_end,
        )
        window = reduce_cpu_window(window, source)
        recovering = reduce_cpu_latch(
            active, source, window, short_recovery_end
        )
        self.assertEqual(recovering.phase, LatchPhase.RECOVERING)

        exact_source, exact_window = self.cpu_source_and_window(
            ((0.0, 15.0, 90.0),)
        )
        exact_active = reduce_cpu_latch(
            empty_latch(RuleId.CPU_WATCH),
            exact_source,
            exact_window,
            15.0,
        )
        exact_source = self.next_success(
            exact_source, CpuUsage(70.0, 15.0, 25.0), 25.0
        )
        exact_window = reduce_cpu_window(exact_window, exact_source)
        self.assertEqual(
            reduce_cpu_latch(
                exact_active, exact_source, exact_window, 25.0
            ).phase,
            LatchPhase.CLEAR,
        )

    def test_cpu_retention_keeps_an_extra_sample_when_recent_time_is_short(self):
        source, window = self.cpu_source_and_window(
            (
                (0.0, 1.0, 50.0),
                (1.0, 6.0, 50.0),
                (6.0, 11.0, 50.0),
                (11.0, 15.9999999995, 50.0),
            )
        )
        self.assertEqual(
            tuple(sample.revision for sample in window.samples),
            (1, 2, 3, 4),
        )
        self.assertEqual(window.last_attempt_revision, source.last_attempt.revision)

    def test_storage_recovery_requires_same_identity_two_samples_spanning_thirty_seconds(self):
        gib = 1024 ** 3
        state = self.disk_source(100 * gib, 9 * gib, -1.0)
        latch = reduce_storage_latch(empty_latch(RuleId.STORAGE_WATCH), state, -1.0)
        state = self.disk_source(100 * gib, 12 * gib, 0.0, state)
        latch = reduce_storage_latch(latch, state, 0.0)
        self.assertEqual(latch.phase, LatchPhase.RECOVERING)
        state = self.disk_source(100 * gib, 12 * gib, 30.0, state)
        self.assertEqual(reduce_storage_latch(latch, state, 30.0).phase, LatchPhase.CLEAR)

    def test_recovery_resets_on_failure_stale_and_excessive_gap(self):
        state = self.memory_source(2, -1.0)
        latch = reduce_memory_latch(empty_latch(RuleId.MEMORY_WATCH), state, -1.0)
        state = self.memory_source(1, 0.0, state)
        latch = reduce_memory_latch(latch, state, 0.0)
        self.assertEqual(latch.recovery_count, 1)
        failed = self.next_failure(state, 1.0)
        latch = reduce_memory_latch(latch, failed, 1.0)
        self.assertEqual(latch.recovery_count, 0)
        # A stale current timestamp resets even when the source still retains a valid sample.
        state = self.memory_source(1, 2.0, failed)
        latch = reduce_memory_latch(latch, state, 5.0)
        self.assertEqual(latch.recovery_count, 0)
        state = self.memory_source(1, 6.0, state)
        latch = reduce_memory_latch(latch, state, 6.0)
        state = self.memory_source(1, 8.6, state)
        self.assertEqual(reduce_memory_latch(latch, state, 8.6).recovery_count, 1)

    def test_storage_identity_change_resets_recovery_and_exact_recovery_boundaries(self):
        gib = 1024 ** 3
        state = self.disk_source(100 * gib, 9 * gib, -1.0)
        latch = reduce_storage_latch(empty_latch(RuleId.STORAGE_WATCH), state, -1.0)
        state = self.disk_source(100 * gib, 12 * gib, 0.0, state)
        latch = reduce_storage_latch(latch, state, 0.0)
        changed = self.disk_source(100 * gib, 12 * gib, 30.0, state, "disk2")
        latch = reduce_storage_latch(latch, changed, 30.0)
        self.assertEqual(latch.recovery_count, 1)
        # Recovery accepts either percentage or GiB at inclusive boundaries.
        total = 1000 * gib
        state = self.disk_source(total, 25 * gib, 60.0, changed, "disk2")
        self.assertEqual(reduce_storage_latch(latch, state, 60.0).phase, LatchPhase.CLEAR)

    def test_active_and_recovering_latches_keep_trigger_and_are_revision_idempotent(self):
        source = self.memory_source(2, 1.0)
        latch = reduce_memory_latch(empty_latch(RuleId.MEMORY_WATCH), source, 1.0)
        duplicate = reduce_memory_latch(latch, source, 1.0)
        self.assertIs(duplicate, latch)
        self.assertEqual(latch.phase, LatchPhase.ACTIVE)
        state = self.memory_source(1, 2.0, source)
        recovering = reduce_memory_latch(latch, state, 2.0)
        self.assertEqual(recovering.trigger, latch.trigger)
        self.assertEqual(recovering.phase, LatchPhase.RECOVERING)

    def test_duplicate_revisions_still_validate_time_and_reset_stale_recovery(self):
        source = self.memory_source(2, 0.0)
        active = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), source, 0.0
        )
        source = self.memory_source(1, 1.0, source)
        recovering = reduce_memory_latch(active, source, 1.0)

        self.assertIs(reduce_memory_latch(recovering, source, 3.5), recovering)
        stale = reduce_memory_latch(recovering, source, 3.500001)
        self.assertEqual(
            (stale.phase, stale.recovery_count, stale.last_processed_revision),
            (LatchPhase.ACTIVE, 0, recovering.last_processed_revision),
        )

        with self.assertRaises(ValueError):
            reduce_memory_latch(recovering, source, 0.5)

    def test_duplicate_revisions_reject_now_before_source_and_latch_times(self):
        source = self.memory_source(2, 3.0)
        latch = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), source, 3.0
        )
        with self.assertRaises(ValueError):
            reduce_memory_latch(latch, source, 2.0)

        active_source, _ = start_attempt(source, 5.0)
        with self.assertRaises(ValueError):
            reduce_memory_latch(latch, active_source, 4.0)

        future_trigger = replace(latch.trigger, triggered_mono_s=4.0)
        future_latch = replace(latch, trigger=future_trigger)
        with self.assertRaises(ValueError):
            reduce_memory_latch(future_latch, source, 3.5)

        normal = self.memory_source(1, 4.0, source)
        recovering = reduce_memory_latch(latch, normal, 4.0)
        rewound = replace(
            normal,
            last_attempt=replace(
                normal.last_attempt,
                started_mono_s=3.4,
                completed_mono_s=3.5,
            ),
            last_valid=replace(
                normal.last_valid,
                started_mono_s=3.4,
                completed_mono_s=3.5,
            ),
        )
        with self.assertRaises(ValueError):
            reduce_memory_latch(recovering, rewound, 3.75)

    def test_skipped_attempt_revisions_restart_recovery_from_current_sample(self):
        for skipped_failure in (False, True):
            with self.subTest(skipped_failure=skipped_failure):
                source = self.memory_source(2, 0.0)
                latch = reduce_memory_latch(
                    empty_latch(RuleId.MEMORY_WATCH), source, 0.0
                )
                source = self.memory_source(1, 1.0, source)
                latch = reduce_memory_latch(latch, source, 1.0)
                if skipped_failure:
                    source = self.next_failure(source, 2.0)
                else:
                    source = self.memory_source(1, 2.0, source)
                source = self.memory_source(1, 3.0, source)

                restarted = reduce_memory_latch(latch, source, 3.0)
                self.assertEqual(
                    (
                        restarted.phase,
                        restarted.recovery_count,
                        restarted.recovery_started_mono_s,
                        restarted.last_processed_revision,
                    ),
                    (LatchPhase.RECOVERING, 1, 3.0, source.last_attempt.revision),
                )

    def test_latch_construction_and_reducers_reject_corruption_and_wrong_families(self):
        with self.assertRaises(ValueError):
            empty_latch(RuleId.MEMORY_WATCH.value)
        with self.assertRaises(ValueError):
            RuleLatch(RuleId.MEMORY_WATCH, HealthSeverity.NORMAL, LatchPhase.CLEAR,
                      None, 0, None, 0, None)
        memory = self.memory_source(2, 1.0)
        with self.assertRaises(ValueError):
            reduce_memory_latch(empty_latch(RuleId.THERMAL_WATCH), memory, 1.0)
        with self.assertRaises(ValueError):
            reduce_thermal_latch(empty_latch(RuleId.THERMAL_WATCH), memory, 1.0)
        with self.assertRaises(ValueError):
            reduce_memory_latch(empty_latch(RuleId.MEMORY_WATCH), memory, True)

    def test_latch_and_source_state_validation_reject_corrupt_combinations(self):
        wrong_trigger = TriggerEvidence(SourceId.THERMAL_OS, 1, 1.0, "thermal=1")
        with self.assertRaises(ValueError):
            RuleLatch(RuleId.MEMORY_WATCH, HealthSeverity.WATCH,
                      LatchPhase.ACTIVE, wrong_trigger, 0, None, 1, None)
        trigger = TriggerEvidence(SourceId.MEMORY_PRESSURE, 1, 1.0, "memory pressure level 2")
        with self.assertRaises(ValueError):
            RuleLatch(RuleId.MEMORY_WATCH, HealthSeverity.WATCH,
                      LatchPhase.RECOVERING, trigger, 1, 1.0, 1, "not-a-marker")
        source = self.memory_source(2, 1.0)
        with self.assertRaises(ValueError):
            reduce_memory_latch(empty_latch(RuleId.MEMORY_WATCH),
                                replace(source, revision=2), 1.0)

    def test_rule_latch_rejects_incoherent_recovery_metadata(self):
        trigger = TriggerEvidence(
            SourceId.MEMORY_PRESSURE, 2, 1.0, "memory pressure level 2"
        )
        with self.assertRaises(ValueError):
            RuleLatch(
                RuleId.MEMORY_WATCH,
                HealthSeverity.WATCH,
                LatchPhase.ACTIVE,
                trigger,
                0,
                None,
                1,
                None,
            )

        memory = self.memory_source(2, 0.0)
        memory_latch = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), memory, 0.0
        )
        memory = self.memory_source(1, 1.0, memory)
        memory_recovery = reduce_memory_latch(memory_latch, memory, 1.0)

        thermal = self.thermal_source(1, 0, 0.0)
        thermal_latch = reduce_thermal_latch(
            empty_latch(RuleId.THERMAL_WATCH), thermal, 0.0
        )
        thermal = self.thermal_source(0, 0, 5.0, thermal)
        thermal_recovery = reduce_thermal_latch(thermal_latch, thermal, 5.0)

        gib = 1024 ** 3
        disk = self.disk_source(100 * gib, 9 * gib, 0.0)
        disk_latch = reduce_storage_latch(
            empty_latch(RuleId.STORAGE_WATCH), disk, 0.0
        )
        disk = self.disk_source(100 * gib, 12 * gib, 30.0, disk)
        disk_recovery = reduce_storage_latch(disk_latch, disk, 30.0)

        critical = self.memory_source(4, 0.0)
        critical_latch = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), critical, 0.0
        )
        critical = self.memory_source(1, 1.0, critical)
        critical_recovery = reduce_memory_latch(
            critical_latch, critical, 1.0
        )
        critical = self.memory_source(1, 2.0, critical)
        critical_recovery_with_two_normals = reduce_memory_latch(
            critical_recovery, critical, 2.0
        )

        for latch, count in (
            (memory_recovery, 10),
            (thermal_recovery, 3),
            (disk_recovery, 2),
            (critical_recovery, 5),
        ):
            with self.subTest(rule=latch.rule_id, count=count):
                with self.assertRaises(ValueError):
                    replace(latch, recovery_count=count)
                with self.assertRaises(ValueError):
                    replace(
                        latch,
                        recovery_count=0,
                        recovery_started_mono_s=None,
                        recovery_identity=None,
                    )

        with self.assertRaises(ValueError):
            replace(
                critical_recovery_with_two_normals,
                recovery_count=1,
            )

        with self.assertRaises(ValueError):
            replace(
                memory_recovery,
                trigger=replace(
                    memory_recovery.trigger, triggered_mono_s=2.0
                ),
            )

        cpu_source, cpu_window = self.cpu_source_and_window(
            ((0.0, 15.0, 90.0),)
        )
        cpu_active = reduce_cpu_latch(
            empty_latch(RuleId.CPU_WATCH),
            cpu_source,
            cpu_window,
            15.0,
        )
        cpu_source = self.next_success(
            cpu_source, CpuUsage(80.0, 15.0, 16.0), 16.0
        )
        cpu_window = reduce_cpu_window(cpu_window, cpu_source)
        cpu_recovery = reduce_cpu_latch(
            cpu_active, cpu_source, cpu_window, 16.0
        )
        self.assertEqual(
            (
                cpu_recovery.recovery_count,
                cpu_recovery.recovery_started_mono_s,
                cpu_recovery.recovery_identity,
            ),
            (0, None, None),
        )
        with self.assertRaises(ValueError):
            replace(cpu_recovery, recovery_started_mono_s=16.0)

    def test_recovery_markers_are_rule_typed_and_storage_identity_is_injective(self):
        memory = self.memory_source(2, 0.0)
        memory_latch = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), memory, 0.0
        )
        memory = self.memory_source(1, 1.0, memory)
        memory_recovery = reduce_memory_latch(memory_latch, memory, 1.0)

        gib = 1024 ** 3
        disk = self.disk_source(
            100 * gib,
            9 * gib,
            0.0,
            volume_id="disk\x1fgroup",
            mount_point="/data",
        )
        disk_latch = reduce_storage_latch(
            empty_latch(RuleId.STORAGE_WATCH), disk, 0.0
        )
        disk = self.disk_source(
            100 * gib,
            12 * gib,
            30.0,
            disk,
            volume_id="disk\x1fgroup",
            mount_point="/data",
        )
        disk_recovery = reduce_storage_latch(disk_latch, disk, 30.0)
        with self.assertRaises(ValueError):
            replace(
                disk_recovery,
                recovery_identity=memory_recovery.recovery_identity,
            )

        changed = self.disk_source(
            100 * gib,
            12 * gib,
            60.0,
            disk,
            volume_id="disk",
            mount_point="group\x1f/data",
        )
        restarted = reduce_storage_latch(disk_recovery, changed, 60.0)
        self.assertEqual(
            (
                restarted.phase,
                restarted.recovery_count,
                restarted.recovery_started_mono_s,
            ),
            (LatchPhase.RECOVERING, 1, 60.0),
        )

    def test_cpu_intermediate_or_incomplete_evidence_cannot_silently_clear(self):
        source, window = self.cpu_source_and_window(((0.0, 15.0, 90.0),))
        latch = reduce_cpu_latch(empty_latch(RuleId.CPU_WATCH), source, window, 15.0)
        source = self.next_success(source, CpuUsage(50.0, 15.0, 16.0), 16.0)
        window = reduce_cpu_window(window, source)
        intermediate = reduce_cpu_latch(latch, source, window, 16.0)
        self.assertEqual((intermediate.severity, intermediate.phase),
                         (HealthSeverity.WATCH, LatchPhase.RECOVERING))

    def test_recovery_gap_boundaries_are_inclusive_and_excesses_restart_streaks(self):
        state = self.memory_source(2, -1.0)
        latch = reduce_memory_latch(empty_latch(RuleId.MEMORY_WATCH), state, -1.0)
        state = self.memory_source(1, 0.0, state)
        latch = reduce_memory_latch(latch, state, 0.0)
        state = self.memory_source(1, 2.5, state)
        latch = reduce_memory_latch(latch, state, 2.5)
        self.assertEqual(latch.recovery_count, 2)
        state = self.memory_source(1, 5.0001, state)
        self.assertEqual(reduce_memory_latch(latch, state, 5.0001).recovery_count, 1)

    def test_in_progress_first_attempt_is_not_warning_evidence(self):
        source, _ = start_attempt(new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5), 1.0)
        latch = empty_latch(RuleId.MEMORY_WATCH)
        self.assertIs(reduce_memory_latch(latch, source, 1.0), latch)


if __name__ == "__main__":
    unittest.main()
