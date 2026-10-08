"""Pure, descriptive telemetry calculations; these never establish health severity.

Collectors supply complete counter samples and their actual completion times.
The reducers preserve the distinction between an initial baseline, measured
zero activity, and unavailable activity.  Presentation and native APIs remain
outside this module.
"""

from dataclasses import dataclass
from enum import Enum
import math
from typing import Mapping, Optional, Sequence, Tuple


class InsightStatus(str, Enum):
    CHECKING = "CHECKING"
    LIVE = "LIVE"
    STALE = "STALE"
    NOT_EXPOSED = "NOT EXPOSED"
    ERROR = "ERROR"


def _finite_number(value: object, *, nonnegative: bool = True) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and (not nonnegative or value >= 0)
    except OverflowError:
        return False


def _counter(value: object, label: str) -> None:
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("%s must be an unsigned 64-bit integer" % label)


@dataclass(frozen=True)
class VmCounters:
    """Cumulative page counters from one complete vm_stat response."""

    page_size_bytes: int
    compressions: int
    decompressions: int
    swapins: int
    swapouts: int

    def __post_init__(self) -> None:
        if (type(self.page_size_bytes) is not int or self.page_size_bytes <= 0
                or self.page_size_bytes > 2**32
                or self.page_size_bytes & (self.page_size_bytes - 1)):
            raise ValueError("page_size_bytes must be a positive power of two")
        for name in ("compressions", "decompressions", "swapins", "swapouts"):
            _counter(getattr(self, name), name)


@dataclass(frozen=True)
class VmRateState:
    previous: Optional[VmCounters] = None
    completed_mono_s: Optional[float] = None

    def __post_init__(self) -> None:
        if self.previous is None:
            if self.completed_mono_s is not None:
                raise ValueError("a completion time requires matching counters")
        else:
            if not isinstance(self.previous, VmCounters):
                raise ValueError("previous must be VmCounters or None")
            self.previous.__post_init__()
            if not _finite_number(self.completed_mono_s):
                raise ValueError("counter completion time must be finite and nonnegative")


@dataclass(frozen=True)
class VmRates:
    """Observed page events per second, not inferred physical disk byte rates."""

    compressions_per_s: float
    decompressions_per_s: float
    swapins_per_s: float
    swapouts_per_s: float
    window_s: float

    def __post_init__(self) -> None:
        for name in ("compressions_per_s", "decompressions_per_s",
                     "swapins_per_s", "swapouts_per_s", "window_s"):
            if not _finite_number(getattr(self, name)):
                raise ValueError("%s must be finite and nonnegative" % name)
        if self.window_s <= 0:
            raise ValueError("rate window must be positive")


@dataclass(frozen=True)
class VmRateView:
    status: InsightStatus = InsightStatus.CHECKING
    rates: Optional[VmRates] = None
    detail: str = "awaiting memory activity baseline"

    def __post_init__(self) -> None:
        if not isinstance(self.status, InsightStatus):
            raise ValueError("status must be InsightStatus")
        if (self.status is InsightStatus.LIVE) != (self.rates is not None):
            raise ValueError("only LIVE activity may contain measured rates")
        if self.rates is not None:
            if not isinstance(self.rates, VmRates):
                raise ValueError("rates must be VmRates or None")
            self.rates.__post_init__()
        if type(self.detail) is not str or not self.detail:
            raise ValueError("detail must be nonempty text")


VM_COUNTER_FIELDS = (
    "vm_page_size", "vm_compressions", "vm_decompressions", "vm_swapins", "vm_swapouts",
)


def vm_counters_from_mapping(detail: Mapping[str, object]) -> Optional[VmCounters]:
    """Read a complete collector projection without manufacturing missing zeros."""

    if not isinstance(detail, Mapping):
        raise ValueError("memory detail must be a mapping")
    present = {key: detail[key] for key in VM_COUNTER_FIELDS if key in detail}
    for key, value in present.items():
        if key == "vm_page_size":
            if (type(value) is not int or value <= 0 or value > 2**32
                    or value & (value - 1)):
                raise ValueError("vm_page_size must be a positive power of two")
        else:
            _counter(value, key)
    if len(present) != len(VM_COUNTER_FIELDS):
        return None
    return VmCounters(*(present[key] for key in VM_COUNTER_FIELDS))


def update_vm_rates(
    state: VmRateState,
    counters: Optional[VmCounters],
    completed_mono_s: float,
    *,
    source_error: bool = False,
    max_gap_s: float = 6.0,
) -> Tuple[VmRateState, VmRateView]:
    """Reduce actual collector completions into independent descriptive rates.

    A failed latest attempt invalidates retained rates immediately.  Missing
    counters have no rate; resets prime a new baseline.  Invalid or reversed
    time clears all history rather than assigning activity to a fabricated
    interval.  The maximum accepted gap is inclusive.
    """

    if not isinstance(state, VmRateState):
        raise ValueError("state must be VmRateState")
    state.__post_init__()
    if type(source_error) is not bool:
        raise ValueError("source_error must be bool")
    if not _finite_number(max_gap_s) or max_gap_s <= 0:
        raise ValueError("max_gap_s must be finite and positive")
    if counters is not None:
        if not isinstance(counters, VmCounters):
            raise ValueError("counters must be VmCounters or None")
        counters.__post_init__()
    if source_error:
        return VmRateState(), VmRateView(InsightStatus.ERROR, None, "memory activity collector failed")
    if (not _finite_number(completed_mono_s)
            or (state.completed_mono_s is not None
                and completed_mono_s <= state.completed_mono_s)):
        return VmRateState(), VmRateView(InsightStatus.ERROR, None, "memory activity clock reset")
    if counters is None:
        return VmRateState(), VmRateView(InsightStatus.NOT_EXPOSED, None,
                                        "memory activity counters not exposed")
    next_state = VmRateState(counters, float(completed_mono_s))
    if state.previous is None:
        return next_state, VmRateView()
    elapsed = completed_mono_s - state.completed_mono_s
    if counters.page_size_bytes != state.previous.page_size_bytes:
        return next_state, VmRateView(detail="memory page size changed; awaiting baseline")
    names = ("compressions", "decompressions", "swapins", "swapouts")
    deltas = tuple(getattr(counters, name) - getattr(state.previous, name) for name in names)
    if any(delta < 0 for delta in deltas):
        return next_state, VmRateView(detail="memory counters reset; awaiting baseline")
    if elapsed > max_gap_s:
        return next_state, VmRateView(detail="memory collection gap; awaiting baseline")
    values = tuple(delta / elapsed for delta in deltas)
    if any(not math.isfinite(value) for value in values):
        return next_state, VmRateView(InsightStatus.ERROR, None, "memory activity interval is invalid")
    return next_state, VmRateView(InsightStatus.LIVE, VmRates(*values, float(elapsed)), "measured page events per second")


@dataclass(frozen=True)
class CpuSampleSummary:
    """Arithmetic sample mean and peak over a fresh, contiguous observation span."""

    mean_percent: float
    peak_percent: float
    sample_count: int
    coverage_s: float

    def __post_init__(self) -> None:
        for name in ("mean_percent", "peak_percent"):
            if not _finite_number(getattr(self, name)) or getattr(self, name) > 100:
                raise ValueError("%s must be finite in 0..100" % name)
        if self.peak_percent < self.mean_percent:
            raise ValueError("peak cannot be below mean")
        if type(self.sample_count) is not int or self.sample_count < 1:
            raise ValueError("sample_count must be a positive integer")
        if not _finite_number(self.coverage_s):
            raise ValueError("coverage_s must be finite and nonnegative")
        if self.sample_count == 1 and self.coverage_s != 0:
            raise ValueError("one sample has no measured span")


def summarize_cpu_samples(
    samples: Sequence[Tuple[float, float]],
    now_mono_s: float,
    *,
    window_s: float = 60.0,
    max_gap_s: float = 2.5,
) -> Optional[CpuSampleSummary]:
    """Summarize real (completion time, percent) points without inferred timing.

    The newest contiguous suffix within the requested window is used.  A stale
    latest point yields no summary; a single point reports zero coverage.
    This is descriptive and does not substitute for the health CPU window.
    """

    for value, name in ((now_mono_s, "now_mono_s"), (window_s, "window_s"),
                        (max_gap_s, "max_gap_s")):
        if not _finite_number(value) or (name != "now_mono_s" and value <= 0):
            raise ValueError("%s must be finite and within its valid range" % name)
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise ValueError("samples must be a sequence of time/value pairs")
    previous_time = None
    points = []
    for point in samples:
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            raise ValueError("CPU samples must be time/value pairs")
        timestamp, percent = point
        if (not _finite_number(timestamp) or not _finite_number(percent)
                or percent > 100 or timestamp > now_mono_s):
            raise ValueError("CPU sample must have valid observed time and percent")
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("CPU sample times must be strictly increasing")
        previous_time = timestamp
        if now_mono_s - timestamp <= window_s:
            points.append((timestamp, percent))
    if not points or now_mono_s - points[-1][0] > max_gap_s:
        return None
    start = len(points) - 1
    while start > 0 and points[start][0] - points[start - 1][0] <= max_gap_s:
        start -= 1
    observed = points[start:]
    values = [point[1] for point in observed]
    peak = max(values)
    # Correct only roundoff at the mathematical upper bound: fsum([0.1] * 3)
    # divided by 3 can round just above the identical observed peak.
    mean = min(peak, math.fsum(values) / len(values))
    return CpuSampleSummary(
        mean, peak, len(values),
        float(observed[-1][0] - observed[0][0]),
    )
