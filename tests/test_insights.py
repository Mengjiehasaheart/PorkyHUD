"""Measured descriptive insights must never manufacture freshness or health."""

from dataclasses import FrozenInstanceError, replace
from math import inf, nan
import unittest
from unittest import mock

import porkyhud
from porkyhud_insights import (
    CpuSampleSummary,
    InsightStatus,
    VmCounters,
    VmRateState,
    VmRateView,
    summarize_cpu_samples,
    update_vm_rates,
    vm_counters_from_mapping,
)


def counters(value=100, page_size=16384):
    return VmCounters(page_size, value, value + 10, value + 20, value + 30)


class VmRateTests(unittest.TestCase):
    def test_first_sample_primes_then_exact_elapsed_time_produces_rates(self):
        state, view = update_vm_rates(VmRateState(), counters(), 10.0)
        self.assertEqual(view.status, InsightStatus.CHECKING)
        self.assertIsNone(view.rates)
        next_state, view = update_vm_rates(state, counters(120), 12.5)
        self.assertEqual(view.status, InsightStatus.LIVE)
        self.assertEqual(view.rates.compressions_per_s, 8.0)
        self.assertEqual(view.rates.decompressions_per_s, 8.0)
        self.assertEqual(view.rates.swapins_per_s, 8.0)
        self.assertEqual(view.rates.swapouts_per_s, 8.0)
        self.assertEqual(view.rates.window_s, 2.5)
        self.assertEqual(next_state.completed_mono_s, 12.5)
        self.assertEqual(state.completed_mono_s, 10.0)

    def test_measured_unchanged_counters_are_live_zero(self):
        state, _ = update_vm_rates(VmRateState(), counters(), 1.0)
        _, view = update_vm_rates(state, counters(), 3.0)
        self.assertEqual(view.status, InsightStatus.LIVE)
        self.assertEqual(view.rates.swapouts_per_s, 0.0)

    def test_stale_view_cannot_retain_fresh_rates(self):
        state, _ = update_vm_rates(VmRateState(), counters(), 1.0)
        _, view = update_vm_rates(state, counters(110), 3.0)
        with self.assertRaises(ValueError):
            replace(view, status=InsightStatus.STALE)
        stale = VmRateView(InsightStatus.STALE, None, "memory activity source is stale")
        self.assertIsNone(stale.rates)

    def test_counter_rates_remain_pages_not_guessed_disk_bytes(self):
        state, _ = update_vm_rates(VmRateState(), counters(page_size=4096), 1.0)
        _, view = update_vm_rates(state, counters(110, page_size=4096), 3.0)
        self.assertEqual(view.rates.swapouts_per_s, 5.0)
        self.assertFalse(hasattr(view.rates, "swapout_bytes_per_s"))

    def test_error_discards_retained_counters_and_rates(self):
        state, _ = update_vm_rates(VmRateState(), counters(), 1.0)
        state, view = update_vm_rates(state, counters(120), 3.0, source_error=True)
        self.assertEqual(state, VmRateState())
        self.assertEqual(view.status, InsightStatus.ERROR)
        self.assertIsNone(view.rates)
        _, view = update_vm_rates(state, counters(130), 4.0)
        self.assertEqual(view.status, InsightStatus.CHECKING)

    def test_missing_counters_reset_baseline_without_reporting_zero(self):
        state, _ = update_vm_rates(VmRateState(), counters(), 1.0)
        state, view = update_vm_rates(state, None, 3.0)
        self.assertEqual(state, VmRateState())
        self.assertEqual(view.status, InsightStatus.NOT_EXPOSED)
        self.assertIsNone(view.rates)

    def test_recognized_missing_counters_are_distinct_from_initial_baseline(self):
        self.assertEqual(VmRateView().status, InsightStatus.CHECKING)
        state, view = update_vm_rates(VmRateState(), None, 1.0)
        self.assertEqual(view.status, InsightStatus.NOT_EXPOSED)
        self.assertIsNone(view.rates)
        self.assertIsNone(state.previous)
        state, view = update_vm_rates(state, counters(), 2.0)
        self.assertEqual(view.status, InsightStatus.CHECKING)
        self.assertIsNone(view.rates)
        _, view = update_vm_rates(state, counters(120), 4.0)
        self.assertEqual(view.status, InsightStatus.LIVE)

    def test_reversal_size_change_and_gap_prime_new_baseline(self):
        for latest, timestamp in (
            (replace(counters(120), swapins=0), 3.0),
            (counters(120, page_size=4096), 3.0),
            (counters(120), 7.01),
        ):
            with self.subTest(latest=latest, timestamp=timestamp):
                state, _ = update_vm_rates(VmRateState(), counters(), 1.0)
                state, view = update_vm_rates(state, latest, timestamp)
                self.assertEqual(view.status, InsightStatus.CHECKING)
                self.assertIsNone(view.rates)
                self.assertEqual(state.previous, latest)
                _, view = update_vm_rates(state, replace(latest, swapouts=latest.swapouts + 2), timestamp + 2)
                self.assertEqual(view.status, InsightStatus.LIVE)
                self.assertEqual(view.rates.swapouts_per_s, 1.0)

    def test_freshness_gap_boundary_is_inclusive(self):
        state, _ = update_vm_rates(VmRateState(), counters(), 1.0)
        _, view = update_vm_rates(state, counters(112), 7.0)
        self.assertEqual(view.status, InsightStatus.LIVE)
        self.assertEqual(view.rates.compressions_per_s, 2.0)

    def test_nonfinite_negative_and_reversed_clock_cannot_report_fresh_rates(self):
        for timestamp in (nan, inf, -1.0, 0.5, 1.0, True):
            with self.subTest(timestamp=timestamp):
                state, _ = update_vm_rates(VmRateState(), counters(), 1.0)
                state, view = update_vm_rates(state, counters(120), timestamp)
                self.assertEqual(state, VmRateState())
                self.assertEqual(view.status, InsightStatus.ERROR)
                self.assertIsNone(view.rates)

    def test_models_and_mapping_reject_invalid_values(self):
        for field, value in (("page_size_bytes", 0), ("page_size_bytes", 5000),
                             ("compressions", -1), ("decompressions", nan),
                             ("swapins", True), ("swapouts", 2**64)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                replace(counters(), **{field: value})
        with self.assertRaises(FrozenInstanceError):
            counters().swapouts = 1
        with self.assertRaises(ValueError):
            VmRateState(counters(), None)
        with self.assertRaises(ValueError):
            update_vm_rates(VmRateState(), counters(), 1.0, max_gap_s=0)
        with self.assertRaises(ValueError):
            update_vm_rates(VmRateState(), {}, 1.0)
        mapping = {"vm_page_size": 16384, "vm_compressions": 0,
                   "vm_decompressions": 0, "vm_swapins": 0, "vm_swapouts": 0}
        self.assertEqual(vm_counters_from_mapping(mapping), VmCounters(16384, 0, 0, 0, 0))
        self.assertIsNone(vm_counters_from_mapping({"vm_page_size": 16384}))
        with self.assertRaises(ValueError):
            vm_counters_from_mapping({**mapping, "vm_swapouts": 0.5})


class CpuSummaryTests(unittest.TestCase):
    def test_summary_uses_real_sample_times_and_names_mean_as_sample_mean(self):
        summary = summarize_cpu_samples(((10.0, 10.0), (11.0, 30.0), (12.5, 50.0)), 13.0)
        self.assertEqual(summary, CpuSampleSummary(30.0, 50.0, 3, 2.5))

    def test_identical_fractional_samples_keep_mean_within_observed_peak(self):
        summary = summarize_cpu_samples(((1.0, 0.1), (2.0, 0.1), (3.0, 0.1)), 3.0)
        self.assertEqual(summary.mean_percent, 0.1)
        self.assertEqual(summary.peak_percent, 0.1)

    def test_old_points_and_disconnected_suffix_are_excluded(self):
        summary = summarize_cpu_samples(((0.0, 99.0), (50.0, 98.0), (59.0, 20.0), (60.0, 40.0)), 60.0)
        self.assertEqual(summary, CpuSampleSummary(30.0, 40.0, 2, 1.0))
        summary = summarize_cpu_samples(((58.0, 90.0), (59.0, 20.0), (60.0, 40.0)), 60.0, window_s=1.0)
        self.assertEqual(summary.sample_count, 2)

    def test_missing_or_stale_samples_are_unknown_and_one_sample_has_zero_coverage(self):
        self.assertIsNone(summarize_cpu_samples((), 1.0))
        self.assertIsNone(summarize_cpu_samples(((1.0, 40.0),), 4.0))
        summary = summarize_cpu_samples(((1.0, 40.0),), 3.5)
        self.assertEqual(summary.coverage_s, 0.0)
        self.assertEqual(summary.sample_count, 1)

    def test_invalid_order_future_and_nonfinite_points_are_rejected(self):
        for points, now in ((((1.0, nan),), 2.0), (((1.0, 101.0),), 2.0),
                            (((2.0, 50.0),), 1.0), (((1.0, 40.0), (1.0, 30.0)), 2.0),
                            (((2.0, 40.0), (1.0, 30.0)), 3.0), (((1.0, 40.0),), inf)):
            with self.subTest(points=points, now=now), self.assertRaises(ValueError):
                summarize_cpu_samples(points, now)


class VmParserTests(unittest.TestCase):
    RAW = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free: 3.
Anonymous pages: 100.
Pages wired down: 50.
Pages occupied by compressor: 25.
Compressions: 1000.
Decompressions: 900.
Swapins: 800.
Swapouts: 700.
"""

    def test_existing_probe_publishes_complete_counters_without_another_command(self):
        with mock.patch.object(porkyhud, "_physical_memory_bytes", return_value=8 * 1024**3), \
                mock.patch.object(porkyhud, "run_command", return_value=self.RAW) as command:
            detail = porkyhud.memory_detail()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0], ["vm_stat"])
        self.assertEqual(vm_counters_from_mapping(detail), VmCounters(16384, 1000, 900, 800, 700))
        self.assertEqual(detail["compressed"], 25 * 16384)

    def test_absent_optional_counters_do_not_become_zero(self):
        raw = self.RAW.replace("Swapouts: 700.\n", "")
        with mock.patch.object(porkyhud, "_physical_memory_bytes", return_value=8 * 1024**3), \
                mock.patch.object(porkyhud, "run_command", return_value=raw):
            detail = porkyhud.memory_detail()
        self.assertIsNone(vm_counters_from_mapping(detail))
        self.assertFalse(any(key.startswith("vm_") for key in detail))

    def test_duplicate_or_malformed_counters_reject_the_attempt(self):
        for raw in (self.RAW + "Swapouts: 701.\n", self.RAW.replace("Swapouts: 700.", "Swapouts: 7.5."),
                    self.RAW.replace("Swapouts: 700.", "Swapouts: -700."),
                    self.RAW.replace("16384 bytes", "0 bytes"),
                    self.RAW.replace("Swapouts: 700.", "Swapouts: 18446744073709551616.")):
            with self.subTest(raw=raw), mock.patch.object(porkyhud, "_physical_memory_bytes", return_value=8 * 1024**3), \
                    mock.patch.object(porkyhud, "run_command", return_value=raw), self.assertRaises(ValueError):
                porkyhud.memory_detail()


if __name__ == "__main__":
    unittest.main()
