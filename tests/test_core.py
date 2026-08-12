"""Behavioral contract tests for the immutable telemetry source reducer."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import inf, nan
import unittest

from porkyhud_core import (
    AttemptOutcome,
    ContributorState,
    CpuContributor,
    CpuUsage,
    CpuWindow,
    CpuWindowSummary,
    DiskCapacity,
    FailureCode,
    HealthSeverity,
    HealthFacts,
    HealthAssessment,
    EvidenceRef,
    EvidenceRole,
    EvidenceState,
    LatchPhase,
    MemoryPressure,
    MemoryPressureLevel,
    OsThermalState,
    ProcessFact,
    ProcessHistory,
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
    reduce_process_history,
    reduce_memory_latch,
    reduce_storage_latch,
    reduce_thermal_latch,
    empty_latch,
    initial_policy_state,
    reduce_health,
    reset_policy_state,
    sample_age_s,
    source_view,
    start_attempt,
    select_cpu_contributor,
    validate_source_state,
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

    def test_generic_source_validator_accepts_reducer_state_and_rejects_forgery(self):
        state = new_source(SourceId.PROCESS_GPU, 5.0, 15.0)
        state, revision = start_attempt(state, 10.0)
        state = complete_success(state, revision, "sample", 10.2, UTC_NOW)
        self.assertIs(
            validate_source_state(state, 10.5, SourceId.PROCESS_GPU),
            state.last_attempt,
        )

        forged = {
            "cadence": replace(state, expected_cadence_s=0.0),
            "stale budget": replace(state, stale_after_s=1.0),
            "source revision": replace(state, revision=2),
            "active attempt": replace(
                state, revision=2, active_revision=2, active_started_mono_s=10.1
            ),
            "outcome/failure": replace(
                state,
                last_attempt=replace(
                    state.last_attempt,
                    outcome=AttemptOutcome.TIMEOUT,
                    failure=None,
                ),
            ),
            "sample ownership": replace(state, last_valid=None),
            "sample revision": replace(
                state, last_valid=replace(state.last_valid, revision=2)
            ),
            "sample start equality": replace(
                state, last_valid=replace(state.last_valid, started_mono_s=8.0)
            ),
            "sample completion equality": replace(
                state, last_valid=replace(state.last_valid, completed_mono_s=10.1)
            ),
            "sample UTC equality": replace(
                state,
                last_valid=replace(
                    state.last_valid,
                    completed_at_utc=UTC_NOW + timedelta(hours=1),
                ),
            ),
        }
        for case, invalid in forged.items():
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_source_state(invalid, 10.5, SourceId.PROCESS_GPU)
        with self.assertRaises(ValueError):
            validate_source_state(state, 10.1, SourceId.PROCESS_GPU)

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


class ContributorTests(unittest.TestCase):
    """Conservative two-sample CPU-attribution contracts."""

    def process_source(self, rows, completed, previous=None):
        state = previous or new_source(SourceId.PROCESS_TABLE, 1.0, 7.5)
        active, revision = start_attempt(state, completed - 0.1)
        return complete_success(active, revision, rows, completed, UTC_NOW)

    def failed_process_source(self, previous, completed):
        active, revision = start_attempt(previous, completed - 0.1)
        return complete_failure(
            active,
            revision,
            AttemptOutcome.TIMEOUT,
            FailureCode.COMMAND_TIMEOUT,
            completed,
            UTC_NOW,
        )

    def history_for(self, first, second):
        source = self.process_source(first, 1.0)
        history = reduce_process_history(ProcessHistory(), source)
        source = self.process_source(second, 3.0, source)
        return reduce_process_history(history, source), source

    def test_requires_stable_identity_and_clear_dominance(self):
        first = (
            ProcessFact(42, "Xcode", 30.0, 100),
            ProcessFact(7, "runner", 10.0, 50),
        )
        second = (
            ProcessFact(42, "Xcode", 36.0, 102),
            ProcessFact(7, "runner", 20.0, 52),
        )
        history, source = self.history_for(first, second)

        contributor = select_cpu_contributor(history, source, 3.0)

        self.assertEqual(contributor.state, ContributorState.LEADING)
        self.assertEqual(contributor.name, "Xcode")
        self.assertEqual(contributor.source_revision, 2)

    def test_threshold_and_ratio_boundaries_do_not_overstate_a_leader(self):
        cases = (
            (24.9999, 0.0, ContributorState.NO_CLEAR_LEADER),
            (25.0, 0.0, ContributorState.LEADING),
            (30.0, 20.0, ContributorState.LEADING),
            (29.9999, 20.0, ContributorState.NO_CLEAR_LEADER),
            (30.0, 30.0, ContributorState.NO_CLEAR_LEADER),
        )
        for top_cpu, runner_cpu, expected in cases:
            with self.subTest(top_cpu=top_cpu, runner_cpu=runner_cpu):
                first = (
                    ProcessFact(42, "Xcode", 10.0, 100),
                    ProcessFact(7, "runner", 10.0, 50),
                )
                second = (ProcessFact(42, "Xcode", top_cpu, 102),)
                if runner_cpu:
                    second += (ProcessFact(7, "runner", runner_cpu, 52),)
                history, source = self.history_for(first, second)
                self.assertEqual(
                    select_cpu_contributor(history, source, 3.0).state,
                    expected,
                )

    def test_empty_valid_table_has_no_clear_leader(self):
        history, source = self.history_for((), ())
        contributor = select_cpu_contributor(history, source, 3.0)
        self.assertEqual(contributor.state, ContributorState.NO_CLEAR_LEADER)
        self.assertIsNone(contributor.name)
        self.assertIsNone(contributor.source_revision)

    def test_pid_reuse_name_or_elapsed_change_disqualifies_the_top_process(self):
        cases = (
            ((ProcessFact(42, "Xcode", 10.0, 100),),
             (ProcessFact(42, "Other", 40.0, 102),)),
            ((ProcessFact(42, "Xcode", 10.0, 100),),
             (ProcessFact(42, "Xcode", 40.0, 99),)),
            ((ProcessFact(42, "Xcode", 10.0, 100),),
             (ProcessFact(42, "Xcode", 40.0, None),)),
            ((ProcessFact(42, "Xcode", 10.0, None),),
             (ProcessFact(42, "Xcode", 40.0, 102),)),
        )
        for first, second in cases:
            with self.subTest(first=first, second=second):
                history, source = self.history_for(first, second)
                self.assertEqual(
                    select_cpu_contributor(history, source, 3.0).state,
                    ContributorState.NO_CLEAR_LEADER,
                )

    def test_unstable_top_process_cannot_promote_a_lower_stable_process(self):
        first = (
            ProcessFact(42, "Xcode", 10.0, 100),
            ProcessFact(7, "runner", 10.0, 50),
        )
        second = (
            ProcessFact(42, "New Xcode", 60.0, 102),
            ProcessFact(7, "runner", 35.0, 52),
        )
        history, source = self.history_for(first, second)
        self.assertEqual(
            select_cpu_contributor(history, source, 3.0).state,
            ContributorState.NO_CLEAR_LEADER,
        )

    def test_history_clears_on_failure_and_skipped_attempts_never_bridge(self):
        first = (ProcessFact(42, "Xcode", 30.0, 100),)
        second = (ProcessFact(42, "Xcode", 36.0, 102),)
        source = self.process_source(first, 1.0)
        history = reduce_process_history(ProcessHistory(), source)
        failed = self.failed_process_source(source, 2.0)
        cleared = reduce_process_history(history, failed)
        self.assertEqual((cleared.previous, cleared.current), ((), ()))
        self.assertEqual(cleared.last_attempt_revision, 2)

        source = self.process_source(second, 3.0, failed)
        reset = reduce_process_history(history, source)
        self.assertEqual(reset.previous, ())
        self.assertEqual(reset.current, second)
        self.assertEqual(reset.current_revision, 3)

    def test_skipped_successful_source_revision_resets_to_current_only(self):
        first = (ProcessFact(42, "Xcode", 30.0, 100),)
        second = (ProcessFact(42, "Xcode", 36.0, 102),)
        third = (ProcessFact(42, "Xcode", 40.0, 104),)
        source = self.process_source(first, 1.0)
        history = reduce_process_history(ProcessHistory(), source)
        source = self.process_source(second, 2.0, source)
        source = self.process_source(third, 3.0, source)
        reset = reduce_process_history(history, source)
        self.assertEqual(reset.previous, ())
        self.assertEqual(reset.current, third)
        self.assertEqual(reset.current_revision, 3)

    def test_duplicate_attempt_is_identity_idempotent(self):
        source = self.process_source((ProcessFact(42, "Xcode", 30.0, 100),), 1.0)
        history = reduce_process_history(ProcessHistory(), source)
        self.assertIs(reduce_process_history(history, source), history)

    def test_same_revision_success_must_match_the_consumed_current_table(self):
        first = (ProcessFact(42, "Xcode", 30.0, 100),)
        second = (ProcessFact(42, "Xcode", 36.0, 102),)
        altered = (ProcessFact(42, "Other", 36.0, 102),)
        source = self.process_source(first, 1.0)
        one_sample_history = reduce_process_history(ProcessHistory(), source)
        two_sample_history, two_sample_source = self.history_for(first, second)
        for history, completed_source in (
            (one_sample_history, source),
            (two_sample_history, two_sample_source),
        ):
            with self.subTest(history=history):
                forged = replace(
                    completed_source,
                    last_valid=replace(completed_source.last_valid, value=altered),
                )
                with self.assertRaises(ValueError):
                    reduce_process_history(history, forged)

    def test_empty_source_allows_only_revision_zero_or_initial_active_revision_one(self):
        empty = new_source(SourceId.PROCESS_TABLE, 1.0, 7.5)
        active, _ = start_attempt(empty, 1.0)
        history = ProcessHistory()
        self.assertIs(reduce_process_history(history, empty), history)
        self.assertIs(reduce_process_history(history, active), history)
        self.assertEqual(
            select_cpu_contributor(history, active, 1.0).state,
            ContributorState.UNAVAILABLE,
        )
        impossible = replace(active, revision=2, active_revision=2)
        with self.assertRaises(ValueError):
            reduce_process_history(history, impossible)
        with self.assertRaises(ValueError):
            select_cpu_contributor(history, impossible, 1.0)

    def test_process_source_rejects_raw_enum_strings(self):
        rows = (ProcessFact(42, "Xcode", 30.0, 100),)
        source = self.process_source(rows, 1.0)
        cases = (
            replace(
                source,
                last_attempt=replace(
                    source.last_attempt,
                    outcome=AttemptOutcome.TIMEOUT,
                    failure="command_timeout",
                ),
                last_valid=None,
            ),
            replace(
                source,
                last_attempt=replace(
                    source.last_attempt,
                    outcome="timeout",
                    failure=FailureCode.COMMAND_TIMEOUT,
                ),
                last_valid=None,
            ),
        )
        for forged in cases:
            with self.subTest(forged=forged):
                with self.assertRaises(ValueError):
                    reduce_process_history(ProcessHistory(), forged)
                with self.assertRaises(ValueError):
                    select_cpu_contributor(ProcessHistory(), forged, 1.0)

    def test_history_attempt_revision_cannot_recede(self):
        rows = (ProcessFact(42, "Xcode", 30.0, 100),)
        source = self.process_source(rows, 1.0)
        newer_history = ProcessHistory(
            current=rows,
            current_revision=2,
            last_attempt_revision=2,
        )
        with self.assertRaises(ValueError):
            reduce_process_history(newer_history, source)

    def test_attribution_requires_latest_success_two_samples_and_freshness(self):
        rows = (ProcessFact(42, "Xcode", 30.0, 100),)
        source = self.process_source(rows, 1.0)
        history = reduce_process_history(ProcessHistory(), source)
        self.assertEqual(
            select_cpu_contributor(history, source, 1.0).state,
            ContributorState.UNAVAILABLE,
        )
        source = self.process_source(rows, 3.0, source)
        history = reduce_process_history(history, source)
        self.assertEqual(
            select_cpu_contributor(history, source, 9.0).state,
            ContributorState.LEADING,
        )
        self.assertEqual(
            select_cpu_contributor(history, source, 9.0000001).state,
            ContributorState.UNAVAILABLE,
        )
        failed = self.failed_process_source(source, 4.0)
        self.assertEqual(
            select_cpu_contributor(history, failed, 4.0).state,
            ContributorState.UNAVAILABLE,
        )

    def test_history_current_table_must_match_the_successful_source(self):
        rows = (ProcessFact(42, "Xcode", 30.0, 100),)
        history, source = self.history_for(rows, rows)
        forged = ProcessHistory(
            previous=history.previous,
            current=(ProcessFact(7, "other", 90.0, 102),),
            previous_revision=history.previous_revision,
            current_revision=history.current_revision,
            last_attempt_revision=history.last_attempt_revision,
        )
        self.assertEqual(
            select_cpu_contributor(forged, source, 3.0).state,
            ContributorState.UNAVAILABLE,
        )

    def test_source_validation_rejects_revision_and_active_attempt_forgery(self):
        rows = (ProcessFact(42, "Xcode", 30.0, 100),)
        history, source = self.history_for(rows, rows)
        sources = (
            replace(source, revision=99),
            replace(source, active_revision=3, active_started_mono_s=3.1),
        )
        for forged in sources:
            with self.subTest(forged=forged):
                with self.assertRaises(ValueError):
                    reduce_process_history(history, forged)
                with self.assertRaises(ValueError):
                    select_cpu_contributor(history, forged, 3.2)

    def test_same_consumed_revision_that_becomes_failure_clears_history(self):
        first = (ProcessFact(42, "Xcode", 30.0, 100),)
        second = (ProcessFact(42, "Xcode", 36.0, 102),)
        first_source = self.process_source(first, 1.0)
        history = reduce_process_history(ProcessHistory(), first_source)
        source = self.process_source(second, 2.0, first_source)
        history = reduce_process_history(history, source)
        replaced_failure = replace(
            source,
            last_attempt=replace(
                source.last_attempt,
                outcome=AttemptOutcome.TIMEOUT,
                failure=FailureCode.COMMAND_TIMEOUT,
            ),
            last_valid=first_source.last_valid,
        )
        cleared = reduce_process_history(history, replaced_failure)
        self.assertEqual((cleared.previous, cleared.current), ((), ()))
        self.assertEqual(cleared.last_attempt_revision, 2)
        self.assertEqual(
            select_cpu_contributor(history, replaced_failure, 2.0).state,
            ContributorState.UNAVAILABLE,
        )

    def test_source_validation_rejects_failure_retaining_future_sample(self):
        rows = (ProcessFact(42, "Xcode", 30.0, 100),)
        first = self.process_source(rows, 1.0)
        second = self.process_source(rows, 2.0, first)
        failed = self.failed_process_source(second, 3.0)
        forged = replace(
            failed,
            last_valid=replace(second.last_valid, revision=4),
        )
        history = reduce_process_history(ProcessHistory(), second)
        with self.assertRaises(ValueError):
            reduce_process_history(history, forged)
        with self.assertRaises(ValueError):
            select_cpu_contributor(history, forged, 3.0)

    def test_legitimate_active_refresh_retains_usable_contributor_evidence(self):
        rows = (ProcessFact(42, "Xcode", 30.0, 100),)
        history, source = self.history_for(rows, rows)
        active, _ = start_attempt(source, 3.1)
        self.assertIs(reduce_process_history(history, active), history)
        self.assertEqual(
            select_cpu_contributor(history, active, 3.2).state,
            ContributorState.LEADING,
        )

    def test_ratio_uses_exact_float_values_at_the_boundary(self):
        first = (
            ProcessFact(42, "Xcode", 30.0, 100),
            ProcessFact(7, "runner", 20.0, 50),
        )
        rounded_down = (
            ProcessFact(42, "Xcode", 48.00000000000003, 102),
            ProcessFact(7, "runner", 32.00000000000002, 52),
        )
        history, source = self.history_for(first, rounded_down)
        self.assertEqual(
            select_cpu_contributor(history, source, 3.0).state,
            ContributorState.NO_CLEAR_LEADER,
        )
        exact = (
            ProcessFact(42, "Xcode", 48.0, 102),
            ProcessFact(7, "runner", 32.0, 52),
        )
        history, source = self.history_for(first, exact)
        self.assertEqual(
            select_cpu_contributor(history, source, 3.0).state,
            ContributorState.LEADING,
        )

    def test_attribution_rejects_invalid_clock_and_age_budgets(self):
        rows = (ProcessFact(42, "Xcode", 30.0, 100),)
        history, source = self.history_for(rows, rows)
        for now in (True, float("nan"), float("inf"), 2.9999):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    select_cpu_contributor(history, source, now)
        for max_age in (True, 0.0, -1.0, float("nan"), float("inf"), 6.1):
            with self.subTest(max_age=max_age):
                with self.assertRaises(ValueError):
                    select_cpu_contributor(history, source, 3.0, max_age)

    def test_duplicate_pid_or_corrupt_source_table_is_rejected(self):
        rows = (
            ProcessFact(42, "Xcode", 30.0, 100),
            ProcessFact(42, "Xcode", 20.0, 101),
        )
        source = self.process_source(rows, 1.0)
        with self.assertRaises(ValueError):
            reduce_process_history(ProcessHistory(), source)
        corrupt = self.process_source((ProcessFact(42, "Xcode", 30.0, 100),), 1.0)
        corrupt = replace(corrupt, last_valid=replace(corrupt.last_valid, value=("bad",)))
        with self.assertRaises(ValueError):
            reduce_process_history(ProcessHistory(), corrupt)

    def test_contributor_and_history_reject_forged_structural_combinations(self):
        with self.assertRaises(ValueError):
            CpuContributor(ContributorState.LEADING, None, 1)
        with self.assertRaises(ValueError):
            CpuContributor(ContributorState.LEADING, "Xcode", 0)
        with self.assertRaises(ValueError):
            CpuContributor(ContributorState.NO_CLEAR_LEADER, "Xcode", None)
        with self.assertRaises(ValueError):
            CpuContributor("leading", "Xcode", 1)

        fact = ProcessFact(42, "Xcode", 30.0, 100)
        cases = (
            ((fact,), (), 1, 0, 1),
            ((fact,), (fact,), 1, 3, 3),
            ((fact,), (fact,), 1, 2, 3),
            ((fact, fact), (), 1, 0, 1),
            (("bad",), (), 1, 0, 1),
            ((), (fact,), 0, 1, 2),
        )
        for previous, current, previous_revision, current_revision, last_attempt in cases:
            with self.subTest(previous=previous, current=current):
                with self.assertRaises(ValueError):
                    ProcessHistory(
                        previous,
                        current,
                        previous_revision,
                        current_revision,
                        last_attempt,
                    )


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

    def test_post_transition_stale_duplicates_preserve_latch_identity(self):
        warning_source = self.memory_source(2, 0.0)
        active = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), warning_source, 0.0
        )
        normal_source = self.memory_source(1, 1.0, warning_source)
        recovering = reduce_memory_latch(active, normal_source, 1.0)

        stale_active = reduce_memory_latch(recovering, normal_source, 3.500001)
        self.assertIsNot(stale_active, recovering)
        self.assertEqual(stale_active.phase, LatchPhase.ACTIVE)
        self.assertIs(
            reduce_memory_latch(stale_active, normal_source, 3.500001),
            stale_active,
        )

        clear_source = self.memory_source(1, 1.0)
        clear = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), clear_source, 1.0
        )
        self.assertIs(
            reduce_memory_latch(clear, clear_source, 3.500001),
            clear,
        )

        duplicate_active_source = self.memory_source(2, 1.0)
        duplicate_active = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH),
            duplicate_active_source,
            1.0,
        )
        self.assertIs(
            reduce_memory_latch(
                duplicate_active, duplicate_active_source, 3.500001
            ),
            duplicate_active,
        )

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

    def test_failed_attempt_rejects_impossible_retained_sample_chronology(self):
        source = self.memory_source(1, 3.0)
        base_attempt = source.last_attempt

        impossible_mono = replace(
            source,
            revision=2,
            last_attempt=replace(
                base_attempt,
                revision=2,
                started_mono_s=2.0,
                completed_mono_s=2.1,
                outcome=AttemptOutcome.TIMEOUT,
                failure=FailureCode.COMMAND_TIMEOUT,
            ),
        )
        with self.assertRaises(ValueError):
            reduce_memory_latch(
                empty_latch(RuleId.MEMORY_WATCH), impossible_mono, 3.0
            )

        impossible_wall = replace(
            source,
            revision=2,
            last_attempt=replace(
                base_attempt,
                revision=2,
                started_mono_s=3.0,
                completed_mono_s=3.1,
                completed_at_utc=UTC_NOW - timedelta(seconds=1),
                outcome=AttemptOutcome.TIMEOUT,
                failure=FailureCode.COMMAND_TIMEOUT,
            ),
        )
        with self.assertRaises(ValueError):
            reduce_memory_latch(
                empty_latch(RuleId.MEMORY_WATCH), impossible_wall, 3.1
            )

        failed_own_sample = replace(
            source,
            last_attempt=replace(
                base_attempt,
                outcome=AttemptOutcome.TIMEOUT,
                failure=FailureCode.COMMAND_TIMEOUT,
            ),
        )
        with self.assertRaises(ValueError):
            reduce_memory_latch(
                empty_latch(RuleId.MEMORY_WATCH), failed_own_sample, 3.0
            )

        boundary = replace(
            source,
            revision=2,
            last_attempt=replace(
                base_attempt,
                revision=2,
                started_mono_s=3.0,
                completed_mono_s=3.1,
                completed_at_utc=UTC_NOW + timedelta(seconds=1),
                outcome=AttemptOutcome.TIMEOUT,
                failure=FailureCode.COMMAND_TIMEOUT,
            ),
        )
        boundary_latch = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), boundary, 3.1
        )
        self.assertEqual(boundary_latch.last_processed_revision, 2)

        later_failure = replace(
            boundary,
            revision=4,
            last_attempt=replace(
                boundary.last_attempt,
                revision=4,
                started_mono_s=4.0,
                completed_mono_s=4.1,
                completed_at_utc=UTC_NOW + timedelta(seconds=2),
            ),
        )
        later_latch = reduce_memory_latch(
            empty_latch(RuleId.MEMORY_WATCH), later_failure, 4.1
        )
        self.assertEqual(later_latch.last_processed_revision, 4)

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


def make_ready_policy_case() -> Tuple[HealthFacts, float]:
    """A deterministic all-fresh, 15-second healthy policy case."""
    return HealthPolicyTests().ready_facts(), 20.0


class HealthPolicyTests(unittest.TestCase):
    """Whole-policy precedence, provenance, and incomplete-evidence tests."""

    def successful(self, source_id, value, completed=20.0, previous=None):
        state = previous or new_source(source_id, 1.0, 2.5)
        active, revision = start_attempt(state, completed - 0.1)
        return complete_success(active, revision, value, completed, UTC_NOW)

    def failed(self, source, completed=20.0, outcome=AttemptOutcome.TIMEOUT):
        active, revision = start_attempt(source, completed - 0.1)
        failure = {
            AttemptOutcome.TIMEOUT: FailureCode.COMMAND_TIMEOUT,
            AttemptOutcome.UNSUPPORTED: FailureCode.UNSUPPORTED_CAPABILITY,
            AttemptOutcome.NOT_APPLICABLE: FailureCode.NOT_APPLICABLE,
        }[outcome]
        return complete_failure(active, revision, outcome, failure, completed, UTC_NOW)

    def cpu_source(self, percent=10.0, duration=15.0, completed=20.0):
        source = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        return self.successful(SourceId.CPU_USAGE,
                               CpuUsage(percent, completed - duration, completed),
                               completed, source)

    def ready_facts(self, memory=1, thermal=0, cpu_percent=10.0,
                    cpu_duration=15.0, storage_low=False, processes=()):
        free = 1 * 1024 ** 3 if storage_low else 50 * 1024 ** 3
        return HealthFacts(
            self.cpu_source(cpu_percent, cpu_duration),
            self.successful(SourceId.MEMORY_PRESSURE,
                            MemoryPressure(MemoryPressureLevel(memory))),
            self.successful(SourceId.THERMAL_OS,
                            OsThermalState(thermal, 0)),
            self.successful(SourceId.DISK_CAPACITY,
                            DiskCapacity("disk", "Data", "/", 100 * 1024 ** 3, free)),
            self.successful(SourceId.PROCESS_TABLE, tuple(processes)),
        )

    def test_critical_memory_owns_headline_and_keeps_other_rules(self):
        policy, assessment = reduce_health(
            initial_policy_state(),
            self.ready_facts(memory=4, thermal=1, cpu_percent=95.0,
                             cpu_duration=15.0, storage_low=True),
            20.0,
        )
        self.assertIsInstance(policy, type(initial_policy_state()))
        self.assertEqual(assessment.severity, HealthSeverity.CRITICAL)
        self.assertEqual(assessment.reason,
                         "macOS reports critical memory pressure.")
        self.assertEqual(assessment.active_rules,
                         (RuleId.MEMORY_CRITICAL, RuleId.THERMAL_WATCH,
                          RuleId.STORAGE_WATCH, RuleId.CPU_WATCH))

    def test_normal_requires_all_four_fresh_and_complete_cpu_window(self):
        _, incomplete = reduce_health(initial_policy_state(),
                                      self.ready_facts(cpu_duration=14.999), 20.0)
        self.assertEqual(incomplete.severity, HealthSeverity.UNKNOWN)
        self.assertEqual(incomplete.evidence_state, EvidenceState.INCOMPLETE)
        self.assertEqual(incomplete.reason, "CPU trend requires live monitoring.")
        _, normal = reduce_health(initial_policy_state(),
                                  self.ready_facts(cpu_duration=15.0), 20.0)
        self.assertEqual(normal.severity, HealthSeverity.NORMAL)
        self.assertEqual(normal.evidence_state, EvidenceState.LIVE)
        self.assertEqual(normal.reason,
                         "Fresh core evidence shows no active CPU, memory, thermal, or storage pressure.")

    def test_headline_reasons_and_precedence_are_exact(self):
        cases = (
            (dict(thermal=1), HealthSeverity.WATCH,
             "macOS reports an active thermal or performance warning."),
            (dict(memory=2), HealthSeverity.WATCH,
             "macOS reports elevated memory pressure."),
            (dict(storage_low=True), HealthSeverity.WATCH,
             "Storage headroom is low: 1.0 GiB remains on Data."),
            (dict(cpu_percent=90.0), HealthSeverity.WATCH,
             "CPU capacity has averaged at least 90% for 15 seconds."),
        )
        for kwargs, severity, reason in cases:
            with self.subTest(kwargs=kwargs):
                _, assessment = reduce_health(initial_policy_state(), self.ready_facts(**kwargs), 20.0)
                self.assertEqual((assessment.severity, assessment.reason), (severity, reason))

    def test_active_rules_use_canonical_precedence_order(self):
        _, assessment = reduce_health(
            initial_policy_state(),
            self.ready_facts(memory=2, thermal=1, storage_low=True, cpu_percent=95.0),
            20.0,
        )
        self.assertEqual(assessment.active_rules, (
            RuleId.THERMAL_WATCH, RuleId.MEMORY_WATCH,
            RuleId.STORAGE_WATCH, RuleId.CPU_WATCH,
        ))

    def test_warning_with_missing_peer_is_degraded_but_failed_decisive_source_is_not(self):
        facts = self.ready_facts(memory=2)
        missing_cpu = HealthFacts(
            new_source(SourceId.CPU_USAGE, 1.0, 2.5), facts.memory,
            facts.thermal, facts.disk, facts.processes)
        _, warning = reduce_health(initial_policy_state(), missing_cpu, 20.0)
        self.assertEqual((warning.severity, warning.evidence_state),
                         (HealthSeverity.WATCH, EvidenceState.DEGRADED))
        self.assertEqual(warning.blockers, (SourceId.CPU_USAGE,))
        failed_memory = self.failed(facts.memory, 21.0)
        _, unknown = reduce_health(initial_policy_state(), HealthFacts(
            facts.cpu, failed_memory, facts.thermal, facts.disk, facts.processes), 21.0)
        self.assertEqual((unknown.severity, unknown.evidence_state),
                         (HealthSeverity.UNKNOWN, EvidenceState.DELAYED))

    def test_incomplete_precedes_delayed_and_overlap_has_one_state(self):
        facts = self.ready_facts()
        variants = (
            HealthFacts(new_source(SourceId.CPU_USAGE, 1.0, 2.5), facts.memory,
                        facts.thermal, facts.disk, facts.processes),
            HealthFacts(facts.cpu, self.failed(facts.memory, 21.0, AttemptOutcome.UNSUPPORTED),
                        facts.thermal, facts.disk, facts.processes),
            HealthFacts(facts.cpu, self.failed(facts.memory, 21.0), facts.thermal,
                        facts.disk, facts.processes),
            HealthFacts(facts.cpu, facts.memory, facts.thermal,
                        self.successful(SourceId.DISK_CAPACITY, facts.disk.last_valid.value, 0.0),
                        facts.processes),
        )
        expected = (EvidenceState.INCOMPLETE, EvidenceState.INCOMPLETE,
                    EvidenceState.DELAYED, EvidenceState.DELAYED)
        for case, evidence_state in zip(variants, expected):
            with self.subTest(case=evidence_state):
                _, assessment = reduce_health(initial_policy_state(), case, 21.0)
                self.assertIn(assessment.evidence_state, tuple(EvidenceState))
                self.assertEqual(assessment.evidence_state, evidence_state)

    def test_evidence_overlap_table_is_complete_and_disjoint(self):
        """Enumerate outcome × prior sample × freshness × decisive warning."""
        facts = self.ready_facts()
        failures = tuple(outcome for outcome in AttemptOutcome
                         if outcome is not AttemptOutcome.SUCCESS)
        coverage = 0
        for outcome in failures:
            for had_prior in (False, True):
                for fresh in (False, True):
                    for decisive_warning in (False, True):
                        with self.subTest(outcome=outcome, had_prior=had_prior,
                                          fresh=fresh, decisive_warning=decisive_warning):
                            memory = new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5)
                            if had_prior:
                                prior_completed = 19.0 if fresh else 0.0
                                memory = self.successful(
                                    SourceId.MEMORY_PRESSURE,
                                    MemoryPressure(MemoryPressureLevel.WARN
                                                   if decisive_warning else MemoryPressureLevel.NORMAL),
                                    prior_completed,
                                    memory,
                                )
                            failure_completed = 20.0
                            active, revision = start_attempt(
                                memory, failure_completed - 0.1)
                            memory = complete_failure(
                                active, revision, outcome,
                                next(iter(LEGAL_FAILURES[outcome])), failure_completed,
                                UTC_NOW,
                            )
                            if had_prior:
                                self.assertEqual(
                                    sample_age_s(memory, 20.0),
                                    1.0 if fresh else 20.0,
                                )
                            else:
                                self.assertIsNone(memory.last_valid)
                            _, assessment = reduce_health(initial_policy_state(), HealthFacts(
                                facts.cpu, memory, facts.thermal, facts.disk,
                                facts.processes), 20.0)
                            expected_severity = HealthSeverity.UNKNOWN
                            if (outcome in (AttemptOutcome.NOT_APPLICABLE,
                                            AttemptOutcome.UNSUPPORTED) or not had_prior):
                                expected_state = EvidenceState.INCOMPLETE
                            else:
                                expected_state = EvidenceState.DELAYED
                            self.assertEqual((assessment.severity, assessment.evidence_state),
                                             (expected_severity, expected_state))
                            coverage += 1
        self.assertEqual(coverage, len(failures) * 2 * 2 * 2)

        for had_prior in (False, True):
            for fresh in (False, True):
                for decisive_warning in (False, True):
                    with self.subTest(outcome=AttemptOutcome.SUCCESS,
                                      had_prior=had_prior, fresh=fresh,
                                      decisive_warning=decisive_warning):
                        memory = new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5)
                        if had_prior:
                            memory = self.successful(
                                SourceId.MEMORY_PRESSURE,
                                MemoryPressure(MemoryPressureLevel.NORMAL),
                                -1.0,
                                memory,
                            )
                        completed = 20.0 if fresh else 0.0
                        memory = self.successful(
                            SourceId.MEMORY_PRESSURE,
                            MemoryPressure(MemoryPressureLevel.WARN
                                           if decisive_warning else MemoryPressureLevel.NORMAL),
                            completed,
                            memory,
                        )
                        self.assertEqual(memory.revision, 2 if had_prior else 1)
                        _, assessment = reduce_health(initial_policy_state(), HealthFacts(
                            facts.cpu, memory, facts.thermal, facts.disk,
                            facts.processes), 20.0)
                        expected = (HealthSeverity.WATCH, EvidenceState.LIVE) if (
                            fresh and decisive_warning) else (
                            HealthSeverity.NORMAL, EvidenceState.LIVE) if fresh else (
                            HealthSeverity.UNKNOWN, EvidenceState.DELAYED)
                        self.assertEqual((assessment.severity, assessment.evidence_state),
                                         expected)
                        coverage += 1
        self.assertEqual(coverage, len(failures) * 2 * 2 * 2 + 2 * 2 * 2)

    def test_health_facts_source_ids_and_assessment_values_are_nominal_and_structural(self):
        facts = self.ready_facts()
        with self.assertRaises(ValueError):
            HealthFacts(facts.memory, facts.memory, facts.thermal, facts.disk, facts.processes)
        with self.assertRaises(ValueError):
            EvidenceRef(SourceId.CPU_USAGE, 0, EvidenceRole.CURRENT)
        with self.assertRaises(ValueError):
            EvidenceRef(SourceId.CPU_USAGE, 1, "current")
        with self.assertRaises(ValueError):
            HealthAssessment("normal", EvidenceState.LIVE, "x", (), (), None, None, (), 0.0)

    def test_recovery_cites_current_and_historical_trigger_and_excludes_old_trigger_age(self):
        initial = self.ready_facts(memory=2)
        policy, _ = reduce_health(initial_policy_state(), initial, 20.0)
        recovering_memory = self.successful(
            SourceId.MEMORY_PRESSURE, MemoryPressure(MemoryPressureLevel.NORMAL), 21.0,
            initial.memory)
        facts = HealthFacts(initial.cpu, recovering_memory, initial.thermal,
                            initial.disk, initial.processes)
        _, assessment = reduce_health(policy, facts, 21.0)
        self.assertEqual(assessment.reason,
                         "macOS memory pressure is now normal; recovery confirmation is in progress.")
        self.assertEqual(assessment.evidence[0].role, EvidenceRole.CURRENT)
        self.assertIn(EvidenceRole.TRIGGER, tuple(ref.role for ref in assessment.evidence))
        self.assertLessEqual(assessment.evidence_age_s, 1.0)

    def test_each_recovery_reason_is_truthful_and_exact(self):
        memory_critical = self.ready_facts(memory=4)
        policy, _ = reduce_health(initial_policy_state(), memory_critical, 20.0)
        memory_warn = self.successful(SourceId.MEMORY_PRESSURE,
                                      MemoryPressure(MemoryPressureLevel.WARN), 21.0,
                                      memory_critical.memory)
        _, assessment = reduce_health(policy, HealthFacts(
            memory_critical.cpu, memory_warn, memory_critical.thermal,
            memory_critical.disk, memory_critical.processes), 21.0)
        self.assertEqual(assessment.reason,
                         "macOS memory pressure has eased from critical to elevated; recovery confirmation is in progress.")

        thermal = self.ready_facts(thermal=1)
        policy, _ = reduce_health(initial_policy_state(), thermal, 20.0)
        cleared_thermal = self.successful(SourceId.THERMAL_OS, OsThermalState(0, 0),
                                           21.0, thermal.thermal)
        _, assessment = reduce_health(policy, HealthFacts(
            thermal.cpu, thermal.memory, cleared_thermal, thermal.disk,
            thermal.processes), 21.0)
        self.assertEqual(assessment.reason,
                         "macOS warnings are now clear; thermal recovery confirmation is in progress.")

        storage = self.ready_facts(storage_low=True)
        policy, _ = reduce_health(initial_policy_state(), storage, 20.0)
        healed_disk = self.successful(SourceId.DISK_CAPACITY,
                                      DiskCapacity("disk", "Data", "/", 100 * 1024 ** 3,
                                                   50 * 1024 ** 3), 50.0, storage.disk)
        _, assessment = reduce_health(policy, HealthFacts(
            storage.cpu, storage.memory, storage.thermal, healed_disk,
            storage.processes), 50.0)
        self.assertEqual(assessment.reason,
                         "Storage headroom has improved; recovery confirmation is in progress.")

        cpu = self.ready_facts(cpu_percent=95.0)
        policy, _ = reduce_health(initial_policy_state(), cpu, 20.0)
        eased_cpu = self.successful(SourceId.CPU_USAGE,
                                    CpuUsage(10.0, 20.0, 21.0), 21.0, cpu.cpu)
        refreshed_memory = self.successful(SourceId.MEMORY_PRESSURE,
                                           MemoryPressure(MemoryPressureLevel.NORMAL),
                                           21.0, cpu.memory)
        refreshed_thermal = self.successful(SourceId.THERMAL_OS, OsThermalState(0, 0),
                                            21.0, cpu.thermal)
        refreshed_disk = self.successful(SourceId.DISK_CAPACITY, cpu.disk.last_valid.value,
                                         21.0, cpu.disk)
        _, assessment = reduce_health(policy, HealthFacts(
            eased_cpu, refreshed_memory, refreshed_thermal, refreshed_disk,
            cpu.processes), 21.0)
        self.assertEqual(assessment.reason,
                         "CPU use has eased; recovery confirmation is in progress.")

    def test_cpu_contributor_is_named_only_for_cpu_pressure(self):
        rows = (ProcessFact(7, "Xcode", 30.0, 100),)
        facts = self.ready_facts(cpu_percent=95.0, processes=rows)
        policy, _ = reduce_health(initial_policy_state(), facts, 20.0)
        # Build two process samples for conservative attribution.
        process2 = self.successful(SourceId.PROCESS_TABLE,
                                   (ProcessFact(7, "Xcode", 35.0, 101),), 20.1,
                                   facts.processes)
        facts = HealthFacts(facts.cpu, facts.memory, facts.thermal, facts.disk, process2)
        _, cpu_assessment = reduce_health(policy, facts, 20.1)
        self.assertEqual(cpu_assessment.contributor_name, "Xcode")
        _, memory_assessment = reduce_health(initial_policy_state(),
                                             self.ready_facts(memory=2, processes=rows), 20.0)
        self.assertIsNone(memory_assessment.contributor_name)

    def test_policy_reducers_are_identity_idempotent_and_reset_is_fresh(self):
        facts = self.ready_facts()
        policy, first = reduce_health(initial_policy_state(), facts, 20.0)
        same, second = reduce_health(policy, facts, 20.0)
        self.assertIs(same, policy)
        self.assertEqual(second, first)
        self.assertEqual(reset_policy_state(), initial_policy_state())

    def test_cpu_failure_or_gap_cannot_turn_fresh_high_cpu_into_eased_recovery(self):
        facts = self.ready_facts(cpu_percent=95.0)
        policy, assessment = reduce_health(initial_policy_state(), facts, 20.0)
        self.assertEqual(assessment.reason,
                         "CPU capacity has averaged at least 90% for 15 seconds.")
        failed_cpu = self.failed(facts.cpu, 21.0)
        policy, failed = reduce_health(policy, HealthFacts(
            failed_cpu, facts.memory, facts.thermal, facts.disk, facts.processes), 21.0)
        self.assertEqual(failed.severity, HealthSeverity.UNKNOWN)
        retry_high = self.successful(SourceId.CPU_USAGE,
                                     CpuUsage(95.0, 21.0, 22.0), 22.0, failed_cpu)
        policy, result = reduce_health(policy, HealthFacts(
            retry_high, facts.memory, facts.thermal, facts.disk, facts.processes), 22.0)
        self.assertEqual((result.severity, result.reason, result.evidence_state), (
            HealthSeverity.WATCH,
            "CPU capacity has averaged at least 90% for 15 seconds.",
            EvidenceState.DEGRADED,
        ))
        self.assertEqual(result.evidence[0],
                         EvidenceRef(SourceId.CPU_USAGE, retry_high.last_valid.revision,
                                     EvidenceRole.CURRENT))
        self.assertEqual(result.cpu_coverage_s, 1.0)
        duplicate, repeated = reduce_health(policy, HealthFacts(
            retry_high, facts.memory, facts.thermal, facts.disk, facts.processes), 22.0)
        self.assertIs(duplicate, policy)
        self.assertEqual(repeated, result)

        gap_policy, _ = reduce_health(initial_policy_state(), facts, 20.0)
        gapped_high = self.successful(SourceId.CPU_USAGE,
                                      CpuUsage(95.0, 23.0, 24.0), 24.0, facts.cpu)
        refreshed_memory = self.successful(SourceId.MEMORY_PRESSURE,
                                           MemoryPressure(MemoryPressureLevel.NORMAL),
                                           24.0, facts.memory)
        refreshed_thermal = self.successful(SourceId.THERMAL_OS, OsThermalState(0, 0),
                                            24.0, facts.thermal)
        refreshed_disk = self.successful(SourceId.DISK_CAPACITY, facts.disk.last_valid.value,
                                         24.0, facts.disk)
        _, gap_result = reduce_health(gap_policy, HealthFacts(
            gapped_high, refreshed_memory, refreshed_thermal, refreshed_disk,
            facts.processes), 24.0)
        self.assertEqual(gap_result.reason,
                         "CPU capacity has averaged at least 90% for 15 seconds.")

    def test_short_low_cpu_after_failure_never_claims_eased_before_exact_recovery_window(self):
        facts = self.ready_facts(cpu_percent=95.0)
        policy, _ = reduce_health(initial_policy_state(), facts, 20.0)
        failed_cpu = self.failed(facts.cpu, 21.0)
        policy, _ = reduce_health(policy, HealthFacts(
            failed_cpu, facts.memory, facts.thermal, facts.disk, facts.processes), 21.0)
        low = failed_cpu
        for end in range(22, 32):
            low = self.successful(SourceId.CPU_USAGE,
                                  CpuUsage(10.0, end - 1.0, float(end)), float(end), low)
            memory = self.successful(SourceId.MEMORY_PRESSURE,
                                     MemoryPressure(MemoryPressureLevel.NORMAL), float(end), facts.memory)
            thermal = self.successful(SourceId.THERMAL_OS, OsThermalState(0, 0),
                                      float(end), facts.thermal)
            disk = self.successful(SourceId.DISK_CAPACITY, facts.disk.last_valid.value,
                                   float(end), facts.disk)
            policy, assessment = reduce_health(policy, HealthFacts(
                low, memory, thermal, disk, facts.processes), float(end))
            if end < 31:
                self.assertEqual(assessment.reason,
                                 "CPU use has eased; recovery confirmation is in progress.")
            else:
                self.assertEqual((assessment.severity, assessment.reason), (
                    HealthSeverity.UNKNOWN,
                    "CPU trend requires live monitoring.",
                ))

    def test_policy_rejects_corrupt_initial_active_and_non_utc_health_sources(self):
        facts = self.ready_facts()
        active, _ = start_attempt(new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5), 20.0)
        accepted = HealthFacts(facts.cpu, active, facts.thermal, facts.disk, facts.processes)
        self.assertEqual(reduce_health(initial_policy_state(), accepted, 20.0)[1].severity,
                         HealthSeverity.UNKNOWN)
        corrupt_active = replace(active, revision=2, active_revision=2)
        with self.assertRaises(ValueError):
            reduce_health(initial_policy_state(), HealthFacts(
                facts.cpu, corrupt_active, facts.thermal, facts.disk, facts.processes), 20.0)
        fixed_offset = UTC_NOW.astimezone(timezone(timedelta(hours=1)))
        for field in ("cpu", "memory", "thermal", "disk", "processes"):
            source = getattr(facts, field)
            corrupt = replace(source,
                              last_attempt=replace(source.last_attempt,
                                                   completed_at_utc=fixed_offset),
                              last_valid=replace(source.last_valid,
                                                 completed_at_utc=fixed_offset))
            with self.subTest(field=field), self.assertRaises(ValueError):
                reduce_health(initial_policy_state(), replace(facts, **{field: corrupt}), 20.0)

    def test_policy_rejects_corrupt_source_combinations_for_every_core_domain(self):
        facts = self.ready_facts()
        for field in ("cpu", "memory", "thermal", "disk"):
            source = getattr(facts, field)
            corrupt = replace(source, revision=source.revision + 1)
            with self.subTest(field=field), self.assertRaises(ValueError):
                reduce_health(initial_policy_state(), replace(facts, **{field: corrupt}), 20.0)

    def test_duplicate_policy_state_must_match_same_revision_source_evidence(self):
        healthy = self.ready_facts()
        high_cpu = self.ready_facts(cpu_percent=95.0)
        cpu_policy, _ = reduce_health(initial_policy_state(), high_cpu, 20.0)
        with self.assertRaises(ValueError):
            reduce_health(cpu_policy, healthy, 20.0)

        watch_memory = self.ready_facts(memory=2)
        memory_policy, _ = reduce_health(initial_policy_state(), watch_memory, 20.0)
        with self.assertRaises(ValueError):
            reduce_health(memory_policy, healthy, 20.0)

        watch_thermal = self.ready_facts(thermal=1)
        thermal_policy, _ = reduce_health(initial_policy_state(), watch_thermal, 20.0)
        with self.assertRaises(ValueError):
            reduce_health(thermal_policy, healthy, 20.0)

        watch_storage = self.ready_facts(storage_low=True)
        storage_policy, _ = reduce_health(initial_policy_state(), watch_storage, 20.0)
        with self.assertRaises(ValueError):
            reduce_health(storage_policy, healthy, 20.0)

    def test_duplicate_policy_process_history_must_match_current_process_source(self):
        facts = self.ready_facts()
        forged_history = ProcessHistory(
            current=(ProcessFact(77, "forged", 50.0, 2),),
            current_revision=1,
            last_attempt_revision=1,
        )
        forged_state = replace(initial_policy_state(), process_history=forged_history)
        with self.assertRaises(ValueError):
            reduce_health(forged_state, facts, 20.0)

    def test_complete_below_entry_cpu_window_with_final_high_point_is_recovering_and_idempotent(self):
        facts = self.ready_facts(cpu_percent=95.0)
        policy, _ = reduce_health(initial_policy_state(), facts, 20.0)
        cpu, memory, thermal, disk = facts.cpu, facts.memory, facts.thermal, facts.disk
        for end in range(21, 36):
            percent = 95.0 if end == 35 else 80.0
            cpu = self.successful(SourceId.CPU_USAGE,
                                  CpuUsage(percent, end - 1.0, float(end)), float(end), cpu)
            memory = self.successful(SourceId.MEMORY_PRESSURE,
                                     MemoryPressure(MemoryPressureLevel.NORMAL), float(end), memory)
            thermal = self.successful(SourceId.THERMAL_OS, OsThermalState(0, 0),
                                      float(end), thermal)
            disk = self.successful(SourceId.DISK_CAPACITY, disk.last_valid.value,
                                   float(end), disk)
            current = HealthFacts(cpu, memory, thermal, disk, facts.processes)
            policy, assessment = reduce_health(policy, current, float(end))
        self.assertEqual(assessment.reason,
                         "CPU use has eased; recovery confirmation is in progress.")
        duplicate, repeated = reduce_health(policy, current, 35.0)
        self.assertIs(duplicate, policy)
        self.assertEqual(repeated, assessment)

    def test_complete_exact_90_cpu_stays_active_and_incomplete_high_rebuild_stays_active(self):
        cpu = new_source(SourceId.CPU_USAGE, 1.0, 2.5)
        facts = self.ready_facts()
        policy = initial_policy_state()
        memory = new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5)
        thermal = new_source(SourceId.THERMAL_OS, 1.0, 2.5)
        disk = new_source(SourceId.DISK_CAPACITY, 1.0, 2.5)
        for end in range(21, 36):
            cpu = self.successful(SourceId.CPU_USAGE,
                                  CpuUsage(90.0, end - 1.0, float(end)), float(end), cpu)
            memory = self.successful(SourceId.MEMORY_PRESSURE,
                                     MemoryPressure(MemoryPressureLevel.NORMAL), float(end), memory)
            thermal = self.successful(SourceId.THERMAL_OS, OsThermalState(0, 0),
                                      float(end), thermal)
            disk = self.successful(SourceId.DISK_CAPACITY, disk.last_valid.value,
                                   float(end), disk) if disk.last_valid else self.successful(
                                       SourceId.DISK_CAPACITY,
                                       facts.disk.last_valid.value, float(end), disk)
            current = HealthFacts(cpu, memory, thermal, disk, facts.processes)
            policy, assessment = reduce_health(policy, current, float(end))
        self.assertEqual(assessment.reason,
                         "CPU capacity has averaged at least 90% for 15 seconds.")
        duplicate, repeated = reduce_health(policy, current, 35.0)
        self.assertIs(duplicate, policy)
        self.assertEqual(repeated, assessment)

    def test_incomplete_cpu_replay_uses_current_point_threshold_and_rejects_inverse_latches(self):
        facts = self.ready_facts(cpu_percent=95.0)
        entered, _ = reduce_health(initial_policy_state(), facts, 20.0)
        failed_cpu = self.failed(facts.cpu, 21.0)
        after_failure, _ = reduce_health(entered, HealthFacts(
            failed_cpu, facts.memory, facts.thermal, facts.disk, facts.processes), 21.0)
        for percent in (0.0, 70.0, 70.001, 80.0, 89.999):
            with self.subTest(percent=percent):
                retry = self.successful(SourceId.CPU_USAGE,
                                        CpuUsage(percent, 21.0, 22.0), 22.0, failed_cpu)
                current = HealthFacts(retry, facts.memory, facts.thermal,
                                      facts.disk, facts.processes)
                state, assessment = reduce_health(after_failure, current, 22.0)
                self.assertEqual(assessment.reason,
                                 "CPU use has eased; recovery confirmation is in progress.")
                duplicate, repeated = reduce_health(state, current, 22.0)
                self.assertIs(duplicate, state)
                self.assertEqual(repeated, assessment)
                inverse = replace(
                    state,
                    cpu_latch=replace(state.cpu_latch, phase=LatchPhase.ACTIVE),
                )
                with self.assertRaises(ValueError):
                    reduce_health(inverse, current, 22.0)

        retry_90 = self.successful(SourceId.CPU_USAGE,
                                   CpuUsage(90.0, 21.0, 22.0), 22.0, failed_cpu)
        current_90 = HealthFacts(retry_90, facts.memory, facts.thermal,
                                 facts.disk, facts.processes)
        active_state, active_assessment = reduce_health(after_failure, current_90, 22.0)
        self.assertEqual(active_assessment.reason,
                         "CPU capacity has averaged at least 90% for 15 seconds.")
        duplicate, repeated = reduce_health(active_state, current_90, 22.0)
        self.assertIs(duplicate, active_state)
        self.assertEqual(repeated, active_assessment)
        inverse = replace(
            active_state,
            cpu_latch=replace(active_state.cpu_latch, phase=LatchPhase.RECOVERING),
        )
        with self.assertRaises(ValueError):
            reduce_health(inverse, current_90, 22.0)

        retry_95 = self.successful(SourceId.CPU_USAGE,
                                   CpuUsage(95.0, 21.0, 22.0), 22.0, failed_cpu)
        current_95 = HealthFacts(retry_95, facts.memory, facts.thermal,
                                 facts.disk, facts.processes)
        active_state, _ = reduce_health(after_failure, current_95, 22.0)
        inverse = replace(
            active_state,
            cpu_latch=replace(active_state.cpu_latch, phase=LatchPhase.RECOVERING),
        )
        with self.assertRaises(ValueError):
            reduce_health(inverse, current_95, 22.0)

    def test_same_revision_clear_latches_reject_triggering_current_evidence(self):
        facts = self.ready_facts()
        clear_state, _ = reduce_health(initial_policy_state(), facts, 20.0)
        changed_memory = replace(
            facts.memory,
            last_valid=replace(facts.memory.last_valid,
                               value=MemoryPressure(MemoryPressureLevel.WARN)),
        )
        changed_thermal = replace(
            facts.thermal,
            last_valid=replace(facts.thermal.last_valid, value=OsThermalState(1, 0)),
        )
        changed_disk = replace(
            facts.disk,
            last_valid=replace(facts.disk.last_valid,
                               value=DiskCapacity("disk", "Data", "/", 100 * 1024 ** 3,
                                                  1 * 1024 ** 3)),
        )
        high_usage = CpuUsage(95.0, 5.0, 20.0)
        changed_cpu = replace(
            facts.cpu,
            last_valid=replace(facts.cpu.last_valid, value=high_usage),
        )
        high_window = CpuWindow((replace(facts.cpu.last_valid, value=high_usage),), 1)
        cases = (
            replace(facts, memory=changed_memory),
            replace(facts, memory=replace(
                changed_memory,
                last_valid=replace(changed_memory.last_valid,
                                   value=MemoryPressure(MemoryPressureLevel.CRITICAL)),
            )),
            replace(facts, thermal=changed_thermal),
            replace(facts, disk=changed_disk),
            replace(facts, cpu=changed_cpu),
        )
        states = (clear_state, clear_state, clear_state, clear_state,
                  replace(clear_state, cpu_window=high_window))
        for state, current in zip(states, cases):
            with self.subTest(current=current), self.assertRaises(ValueError):
                reduce_health(state, current, 20.0)
        with self.assertRaises(ValueError):
            reduce_health(
                replace(clear_state, process_history=ProcessHistory(last_attempt_revision=1)),
                facts,
                20.0,
            )

    def test_stale_recovery_reset_and_failed_process_duplicate_are_idempotent(self):
        facts = self.ready_facts(memory=2)
        policy, _ = reduce_health(initial_policy_state(), facts, 20.0)
        recovered_memory = self.successful(
            SourceId.MEMORY_PRESSURE, MemoryPressure(MemoryPressureLevel.NORMAL),
            21.0, facts.memory)
        recovering_facts = HealthFacts(facts.cpu, recovered_memory, facts.thermal,
                                       facts.disk, facts.processes)
        policy, _ = reduce_health(policy, recovering_facts, 21.0)
        stale_state, stale_assessment = reduce_health(policy, recovering_facts, 24.0)
        duplicate, repeated = reduce_health(stale_state, recovering_facts, 24.0)
        self.assertIs(duplicate, stale_state)
        self.assertEqual(repeated, stale_assessment)

        for field, initial, recovered in (
            ("thermal", self.ready_facts(thermal=1), OsThermalState(0, 0)),
            ("disk", self.ready_facts(storage_low=True),
             DiskCapacity("disk", "Data", "/", 100 * 1024 ** 3, 50 * 1024 ** 3)),
        ):
            with self.subTest(field=field):
                policy, _ = reduce_health(initial_policy_state(), initial, 20.0)
                source = getattr(initial, field)
                source_id = SourceId.THERMAL_OS if field == "thermal" else SourceId.DISK_CAPACITY
                changed = self.successful(source_id, recovered, 21.0, source)
                current = replace(initial, **{field: changed})
                policy, _ = reduce_health(policy, current, 21.0)
                stale_state, stale_assessment = reduce_health(policy, current, 24.0)
                duplicate, repeated = reduce_health(stale_state, current, 24.0)
                self.assertIs(duplicate, stale_state)
                self.assertEqual(repeated, stale_assessment)

        cpu_initial = self.ready_facts(cpu_percent=95.0)
        policy, _ = reduce_health(initial_policy_state(), cpu_initial, 20.0)
        low_cpu = self.successful(SourceId.CPU_USAGE, CpuUsage(10.0, 20.0, 21.0),
                                  21.0, cpu_initial.cpu)
        cpu_current = replace(cpu_initial, cpu=low_cpu)
        policy, _ = reduce_health(policy, cpu_current, 21.0)
        stale_state, stale_assessment = reduce_health(policy, cpu_current, 24.0)
        duplicate, repeated = reduce_health(stale_state, cpu_current, 24.0)
        self.assertIs(duplicate, stale_state)
        self.assertEqual(repeated, stale_assessment)

        process_rows = (ProcessFact(7, "Xcode", 30.0, 10),)
        process_facts = self.ready_facts(processes=process_rows)
        process_state, _ = reduce_health(initial_policy_state(), process_facts, 20.0)
        failed_process = self.failed(process_facts.processes, 21.0)
        failed_facts = replace(process_facts, processes=failed_process)
        process_state, process_assessment = reduce_health(process_state, failed_facts, 21.0)
        duplicate, repeated = reduce_health(process_state, failed_facts, 21.0)
        self.assertIs(duplicate, process_state)
        self.assertEqual(repeated, process_assessment)
        stale_process_state, stale_process_assessment = reduce_health(
            initial_policy_state(), process_facts, 28.0)
        duplicate, repeated = reduce_health(stale_process_state, process_facts, 28.0)
        self.assertIs(duplicate, stale_process_state)
        self.assertEqual(repeated, stale_process_assessment)


if __name__ == "__main__":
    unittest.main()
