"""Deterministic contracts for bounded live process projection reuse."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

import porkyhud
import porkyhud_core as core


class ProcessProjectionCacheTests(unittest.TestCase):
    UTC = datetime(2026, 10, 8, tzinfo=timezone.utc)

    def process_state(self, *, rows=None, started=1.0, completed=1.1):
        if rows is None:
            rows = (porkyhud.ProcessRow(
                1, 30.0, 1.0, None, "S", "01:00",
                "/usr/bin/python --token fixture-secret --mode worker",
            ),)
        state = core.new_source(core.SourceId.PROCESS_TABLE, 2.5, 7.5)
        state, revision = core.start_attempt(state, started)
        return core.complete_success(
            state, revision, rows, completed, self.UTC,
        )

    def states(self, process):
        states = dict(porkyhud._registry_states())
        states[core.SourceId.PROCESS_TABLE] = process
        return states

    def test_large_unchanged_process_sample_is_parsed_once(self):
        rows = tuple(porkyhud.ProcessRow(
            pid, float(pid % 80), 0.1, None, "S", "01:00",
            "/usr/bin/python --token fixture-secret --mode worker",
        ) for pid in range(1, 1001))
        state = self.process_state(rows=rows)
        states = self.states(state)
        cached_policy = core.initial_policy_state()
        cache = porkyhud._ProcessFactCache()
        with mock.patch.object(
                porkyhud, "process_facts", wraps=porkyhud.process_facts) as parser:
            for index in range(40):
                cached_policy, cached_frame = porkyhud.build_health_frame(
                    cached_policy, states, 2.0 + index * 0.001,
                    process_cache=cache,
                )
            self.assertEqual(parser.call_count, 1)
        policy = core.initial_policy_state()
        for index in range(40):
            policy, frame = porkyhud.build_health_frame(
                policy, states, 2.0 + index * 0.001,
            )
        self.assertEqual((cached_policy, cached_frame), (policy, frame))
        self.assertNotIn("fixture-secret", repr(cache._facts))

    def test_cache_preserves_active_failure_and_freshness_metadata(self):
        state = self.process_state()
        cache = porkyhud._ProcessFactCache()
        with mock.patch.object(
                porkyhud, "process_facts", wraps=porkyhud.process_facts) as parser:
            initial = porkyhud._sanitized_process_state(state, 2.0, cache)
            active, revision = core.start_attempt(state, 2.1)
            checking = porkyhud._sanitized_process_state(active, 2.2, cache)
            failure = core.complete_failure(
                active, revision, core.AttemptOutcome.TIMEOUT,
                core.FailureCode.COMMAND_TIMEOUT, 2.3,
                self.UTC + timedelta(seconds=1),
            )
            retained = porkyhud._sanitized_process_state(failure, 2.4, cache)
            self.assertEqual(parser.call_count, 1)
        self.assertIs(checking.last_valid.value, initial.last_valid.value)
        self.assertIs(retained.last_valid.value, initial.last_valid.value)
        self.assertEqual(checking.active_revision, revision)
        self.assertEqual(retained.last_attempt, failure.last_attempt)
        self.assertFalse(core.is_decision_ready(retained, 2.4))
        self.assertEqual(core.source_view(retained, 2.4).status,
                         core.SourceViewStatus.ERROR)
        fresh = porkyhud._sanitized_process_state(state, 8.6, cache)
        stale = porkyhud._sanitized_process_state(state, 8.6001, cache)
        self.assertTrue(core.is_decision_ready(fresh, 8.6))
        self.assertFalse(core.is_decision_ready(stale, 8.6001))

    def test_replaced_same_revision_sample_is_reparsed_and_rejected(self):
        state = self.process_state()
        cache = porkyhud._ProcessFactCache()
        policy, _frame = porkyhud.build_health_frame(
            core.initial_policy_state(), self.states(state), 2.0,
            process_cache=cache,
        )
        corrupted_row = replace(state.last_valid.value[0], cpu=99.0)
        corrupted = replace(
            state, last_valid=replace(state.last_valid, value=(corrupted_row,)),
        )
        with mock.patch.object(
                porkyhud, "process_facts", wraps=porkyhud.process_facts) as parser:
            with self.assertRaisesRegex(ValueError, "process history must match"):
                porkyhud.build_health_frame(
                    policy, self.states(corrupted), 2.1, process_cache=cache,
                )
            self.assertEqual(parser.call_count, 1)

    def test_mutable_same_sample_rows_cannot_reuse_valid_projection(self):
        for field in ("pid", "cpu"):
            with self.subTest(field=field):
                state = self.process_state()
                cache = porkyhud._ProcessFactCache()
                porkyhud._sanitized_process_state(state, 2.0, cache)
                setattr(state.last_valid.value[0], field, True)
                with self.assertRaises(ValueError):
                    porkyhud._sanitized_process_state(state, 2.1, cache)

    def test_invalid_same_revision_chronology_is_checked_before_cache_reuse(self):
        state = self.process_state()
        cache = porkyhud._ProcessFactCache()
        porkyhud._sanitized_process_state(state, 2.0, cache)
        invalid = replace(state, revision=state.revision + 1)
        with self.assertRaisesRegex(ValueError, "completed attempt must match"):
            porkyhud._sanitized_process_state(invalid, 2.1, cache)

    def test_cache_retains_only_latest_sample_and_clear_releases_it(self):
        first, second = self.process_state(), self.process_state()
        cache = porkyhud._ProcessFactCache()
        with mock.patch.object(
                porkyhud, "process_facts", wraps=porkyhud.process_facts) as parser:
            porkyhud._sanitized_process_state(first, 2.0, cache)
            porkyhud._sanitized_process_state(second, 2.0, cache)
            porkyhud._sanitized_process_state(first, 2.0, cache)
            self.assertEqual(parser.call_count, 3)
        cache.clear()
        self.assertIsNone(cache._sample)
        self.assertEqual((cache._row_key, cache._facts), ((), ()))

    def test_live_generation_reset_and_close_clear_projection(self):
        class FixedPoller:
            def __init__(self, state, *_args, **_kwargs):
                self.state = state

            def tick(self, _now, _force=False):
                return self.state

            def snapshot(self):
                return self.state

            def close(self, _timeout):
                pass

        with mock.patch.object(porkyhud, "SourcePoller", FixedPoller), \
                mock.patch.object(porkyhud, "MacCpuSampler"), \
                mock.patch.object(porkyhud, "GPUProcessSampler"):
            coordinator = porkyhud.LiveHealthCoordinator()
            try:
                state = self.process_state()
                coordinator._pollers[core.SourceId.PROCESS_TABLE].state = state
                coordinator.tick(2.0)
                self.assertIs(coordinator._process_facts_cache._sample,
                              state.last_valid)
                coordinator.observe_time(2.0, self.UTC)
                coordinator.observe_time(33.0, self.UTC + timedelta(seconds=31))
                self.assertIsNone(coordinator._process_facts_cache._sample)
                later = self.process_state(started=33.0, completed=33.1)
                coordinator._pollers[core.SourceId.PROCESS_TABLE].state = later
                coordinator.tick(34.0)
                self.assertIs(coordinator._process_facts_cache._sample,
                              later.last_valid)
            finally:
                coordinator.close()
        self.assertIsNone(coordinator._process_facts_cache._sample)


if __name__ == "__main__":
    unittest.main()
