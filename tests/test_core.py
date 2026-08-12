"""Behavioral contract tests for the immutable telemetry source reducer."""

from datetime import datetime, timedelta, timezone
from math import inf, nan
import unittest

from porkyhud_core import (
    AttemptOutcome,
    FailureCode,
    SourceId,
    SourceViewStatus,
    complete_failure,
    complete_success,
    is_decision_ready,
    new_source,
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

    def test_completions_require_an_active_matching_revision_once(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, revision = start_attempt(state, 10.0)
        with self.assertRaises(ValueError):
            complete_success(active, revision + 1, "normal", 10.1, UTC_NOW)
        complete = complete_success(active, revision, "normal", 10.1, UTC_NOW)
        with self.assertRaises(ValueError):
            complete_success(complete, revision, "normal", 10.2, UTC_NOW)

    def test_completed_timestamp_requires_timezone_and_normalizes_to_utc(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, revision = start_attempt(state, 10.0)
        with self.assertRaises(ValueError):
            complete_success(active, revision, "normal", 10.1, UTC_NOW.replace(tzinfo=None))
        local_time = UTC_NOW.astimezone(timezone(timedelta(hours=-5)))
        complete = complete_success(active, revision, "normal", 10.1, local_time)
        self.assertEqual(complete.last_valid.completed_at_utc, UTC_NOW)
        self.assertEqual(complete.last_valid.completed_at_utc.tzinfo, timezone.utc)

    def test_success_requires_no_failure_and_non_success_requires_legal_pair(self):
        state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        active, revision = start_attempt(state, 10.0)
        with self.assertRaises(ValueError):
            complete_failure(
                active,
                revision,
                AttemptOutcome.SUCCESS,
                FailureCode.COMMAND_TIMEOUT,
                10.1,
                UTC_NOW,
            )
        with self.assertRaises(ValueError):
            complete_failure(
                active,
                revision,
                AttemptOutcome.TIMEOUT,
                None,
                10.1,
                UTC_NOW,
            )

    def test_every_legal_failure_pair_completes_and_every_other_pair_rejects(self):
        all_failures = set(FailureCode)
        for outcome, legal_failures in LEGAL_FAILURES.items():
            for failure in all_failures:
                state = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
                active, revision = start_attempt(state, 10.0)
                if failure in legal_failures:
                    complete = complete_failure(active, revision, outcome, failure, 10.1, UTC_NOW)
                    self.assertEqual(complete.last_attempt.failure, failure)
                else:
                    with self.assertRaises(ValueError):
                        complete_failure(active, revision, outcome, failure, 10.1, UTC_NOW)

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


if __name__ == "__main__":
    unittest.main()
