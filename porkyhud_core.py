"""Pure immutable source-state reducers for PorkyHUD telemetry evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum, IntEnum
import math
from typing import Generic, Optional, Tuple, TypeVar


T = TypeVar("T")


class SourceId(str, Enum):
    CPU_USAGE = "cpu.usage"
    MEMORY_PRESSURE = "memory.pressure"
    THERMAL_OS = "thermal.os"
    DISK_CAPACITY = "disk.capacity"
    PROCESS_TABLE = "process.table"
    PROCESS_GPU = "process.gpu"
    MEMORY_STATS = "memory.stats"
    NETWORK_TOTALS = "network.totals"
    DISK_ACTIVITY = "disk.activity"
    BATTERY = "battery"
    SENSOR_SMC = "sensor.smc"
    SENSOR_POWERMETRICS = "sensor.powermetrics"
    SYSTEM_STATIC = "system.static"


class AttemptOutcome(str, Enum):
    SUCCESS = "success"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission_denied"
    MISSING_DEPENDENCY = "missing_dependency"
    TIMEOUT = "timeout"
    COMMAND_ERROR = "command_error"
    PARSE_ERROR = "parse_error"
    CANCELLED = "cancelled"
    UNKNOWN_ERROR = "unknown_error"


class FailureCode(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    MISSING_EXECUTABLE = "missing_executable"
    COMMAND_TIMEOUT = "command_timeout"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    NONZERO_EXIT = "nonzero_exit"
    DECODE_ERROR = "decode_error"
    OUTPUT_LIMIT = "output_limit"
    EMPTY_OUTPUT = "empty_output"
    PARSER_REJECTED = "parser_rejected"
    USER_CANCELLED = "user_cancelled"
    UNEXPECTED_EXCEPTION = "unexpected_exception"


class SourceViewStatus(str, Enum):
    CHECKING = "CHECKING"
    LIVE = "LIVE"
    STALE = "STALE"
    LOCKED = "LOCKED"
    PERMISSION_DENIED = "PERMISSION DENIED"
    MISSING = "MISSING"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT APPLICABLE"
    NOT_EXPOSED = "NOT EXPOSED"
    ERROR = "ERROR"


class MemoryPressureLevel(IntEnum):
    NORMAL = 1
    WARN = 2
    CRITICAL = 4


@dataclass(frozen=True)
class CpuUsage:
    percent: float
    window_started_mono_s: float
    window_completed_mono_s: float

    def __post_init__(self) -> None:
        _require_percent(self.percent, "percent")
        _require_finite_mono(self.window_started_mono_s, "window_started_mono_s")
        _require_finite_mono(self.window_completed_mono_s, "window_completed_mono_s")
        if self.window_completed_mono_s <= self.window_started_mono_s:
            raise ValueError("CPU usage window must have positive duration")


@dataclass(frozen=True)
class MemoryPressure:
    level: MemoryPressureLevel

    def __post_init__(self) -> None:
        if not isinstance(self.level, MemoryPressureLevel):
            raise ValueError("level must be a MemoryPressureLevel")


@dataclass(frozen=True)
class OsThermalState:
    thermal_warning_level: int
    performance_warning_level: int

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.thermal_warning_level, "thermal_warning_level")
        _require_nonnegative_int(
            self.performance_warning_level, "performance_warning_level"
        )


@dataclass(frozen=True)
class DiskCapacity:
    volume_id: str
    volume_name: str
    mount_point: str
    total_bytes: int
    free_bytes: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.volume_id, "volume_id"),
            (self.volume_name, "volume_name"),
            (self.mount_point, "mount_point"),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError("%s must be a non-empty string" % name)
        _require_nonnegative_int(self.total_bytes, "total_bytes")
        if self.total_bytes == 0:
            raise ValueError("total_bytes must be positive")
        _require_nonnegative_int(self.free_bytes, "free_bytes")
        if self.free_bytes > self.total_bytes:
            raise ValueError("free_bytes cannot exceed total_bytes")


@dataclass(frozen=True)
class ProcessFact:
    pid: int
    name: str
    cpu_percent: float
    elapsed_seconds: Optional[int]

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("pid must be a positive int")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be a non-empty string")
        _require_percent(self.cpu_percent, "cpu_percent")
        if self.elapsed_seconds is not None:
            _require_nonnegative_int(self.elapsed_seconds, "elapsed_seconds")


@dataclass(frozen=True)
class Sample(Generic[T]):
    value: T
    revision: int
    started_mono_s: float
    completed_mono_s: float
    completed_at_utc: datetime


@dataclass(frozen=True)
class CollectionAttempt:
    revision: int
    started_mono_s: float
    completed_mono_s: float
    completed_at_utc: datetime
    outcome: AttemptOutcome
    failure: Optional[FailureCode] = None


@dataclass(frozen=True)
class SourceState(Generic[T]):
    source_id: SourceId
    expected_cadence_s: float
    stale_after_s: float
    revision: int = 0
    active_revision: Optional[int] = None
    active_started_mono_s: Optional[float] = None
    last_attempt: Optional[CollectionAttempt] = None
    last_valid: Optional[Sample[T]] = None


@dataclass(frozen=True)
class SourceView:
    source_id: SourceId
    status: SourceViewStatus
    age_s: Optional[float]
    refreshing: bool
    decision_ready: bool
    last_valid_revision: Optional[int]
    failure: Optional[FailureCode]


@dataclass(frozen=True)
class CpuWindow:
    samples: Tuple[Sample[CpuUsage], ...] = ()
    last_attempt_revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.samples, tuple):
            raise ValueError("samples must be a tuple")
        if type(self.last_attempt_revision) is not int or self.last_attempt_revision < 0:
            raise ValueError("last_attempt_revision must be a nonnegative int")
        previous = None
        for sample in self.samples:
            _validate_cpu_sample(sample)
            if previous is not None:
                previous_usage = previous.value
                usage = sample.value
                if sample.revision <= previous.revision:
                    raise ValueError("CPU window sample revisions must increase")
                if usage.window_started_mono_s < previous_usage.window_completed_mono_s:
                    raise ValueError("CPU window samples cannot overlap")
                if usage.window_completed_mono_s <= previous_usage.window_completed_mono_s:
                    raise ValueError("CPU window sample times must increase")
            previous = sample
        if self.samples and self.last_attempt_revision < self.samples[-1].revision:
            raise ValueError("last_attempt_revision cannot precede retained samples")


@dataclass(frozen=True)
class CpuWindowSummary:
    duration_s: float
    mean_percent: float
    started_mono_s: float
    completed_mono_s: float

    def __post_init__(self) -> None:
        _require_finite_positive(self.duration_s, "duration_s")
        _require_percent(self.mean_percent, "mean_percent")
        _require_finite_mono(self.started_mono_s, "started_mono_s")
        _require_finite_mono(self.completed_mono_s, "completed_mono_s")
        if self.completed_mono_s <= self.started_mono_s:
            raise ValueError("summary window must have positive duration")
        if not math.isclose(
            self.completed_mono_s - self.started_mono_s,
            self.duration_s,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("summary duration does not match its time window")


_LEGAL_FAILURES = {
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


def new_source(
    source_id: SourceId, expected_cadence_s: float, stale_after_s: float
) -> SourceState[T]:
    """Create an empty source state with a validated freshness budget."""
    if not isinstance(source_id, SourceId):
        raise ValueError("source_id must be a SourceId")
    _require_finite_positive(expected_cadence_s, "expected_cadence_s")
    _require_finite_positive(stale_after_s, "stale_after_s")
    if stale_after_s < expected_cadence_s:
        raise ValueError("stale_after_s must not be shorter than expected_cadence_s")
    return SourceState(source_id, expected_cadence_s, stale_after_s)


def start_attempt(
    state: SourceState[T], started_mono_s: float
) -> Tuple[SourceState[T], int]:
    """Allocate the next revision and mark it as the source's active attempt."""
    _require_finite_mono(started_mono_s, "started_mono_s")
    if state.active_revision is not None:
        raise ValueError("source already has an active attempt")
    if state.last_attempt is not None and started_mono_s < state.last_attempt.completed_mono_s:
        raise ValueError("started_mono_s cannot precede the previous completion")
    revision = state.revision + 1
    return (
        replace(
            state,
            revision=revision,
            active_revision=revision,
            active_started_mono_s=started_mono_s,
        ),
        revision,
    )


def complete_success(
    state: SourceState[T],
    revision: int,
    value: T,
    completed_mono_s: float,
    completed_at_utc: datetime,
) -> SourceState[T]:
    """Record the active attempt as a successfully parsed sample."""
    started_mono_s, completed_at_utc = _validate_completion(
        state, revision, completed_mono_s, completed_at_utc
    )
    attempt = CollectionAttempt(
        revision,
        started_mono_s,
        completed_mono_s,
        completed_at_utc,
        AttemptOutcome.SUCCESS,
    )
    sample = Sample(
        value, revision, started_mono_s, completed_mono_s, completed_at_utc
    )
    return replace(
        state,
        active_revision=None,
        active_started_mono_s=None,
        last_attempt=attempt,
        last_valid=sample,
    )


def complete_failure(
    state: SourceState[T],
    revision: int,
    outcome: AttemptOutcome,
    failure: Optional[FailureCode],
    completed_mono_s: float,
    completed_at_utc: datetime,
) -> SourceState[T]:
    """Record a proven failed attempt without replacing the last valid sample."""
    if not isinstance(outcome, AttemptOutcome):
        raise ValueError("outcome must be an AttemptOutcome")
    if failure is not None and not isinstance(failure, FailureCode):
        raise ValueError("failure must be a FailureCode or None")
    if outcome is AttemptOutcome.SUCCESS:
        raise ValueError("success must be completed with complete_success")
    if failure not in _LEGAL_FAILURES.get(outcome, set()):
        raise ValueError("failure code is not compatible with the attempt outcome")
    started_mono_s, completed_at_utc = _validate_completion(
        state, revision, completed_mono_s, completed_at_utc
    )
    attempt = CollectionAttempt(
        revision,
        started_mono_s,
        completed_mono_s,
        completed_at_utc,
        outcome,
        failure,
    )
    return replace(
        state,
        active_revision=None,
        active_started_mono_s=None,
        last_attempt=attempt,
    )


def sample_age_s(state: SourceState[T], now_mono_s: float) -> Optional[float]:
    """Return the monotonic age of the retained valid sample, if one exists."""
    _require_finite_mono(now_mono_s, "now_mono_s")
    if state.last_valid is None:
        return None
    age_s = now_mono_s - state.last_valid.completed_mono_s
    if age_s < 0:
        raise ValueError("now_mono_s cannot precede the sample completion")
    return age_s


def is_decision_ready(state: SourceState[T], now_mono_s: float) -> bool:
    """Whether the latest evidence is successful and within its freshness budget."""
    age_s = sample_age_s(state, now_mono_s)
    return bool(
        age_s is not None
        and age_s <= state.stale_after_s
        and state.last_attempt is not None
        and state.last_attempt.outcome is AttemptOutcome.SUCCESS
    )


def source_view(state: SourceState[T], now_mono_s: float) -> SourceView:
    """Derive a presentation view without mutating source evidence."""
    age_s = sample_age_s(state, now_mono_s)
    refreshing = state.active_revision is not None
    last_valid_revision = state.last_valid.revision if state.last_valid else None
    last_attempt = state.last_attempt

    if last_attempt is None and refreshing:
        status = SourceViewStatus.CHECKING
    elif last_attempt is not None and last_attempt.outcome is not AttemptOutcome.SUCCESS:
        status = _failure_status(state.source_id, last_attempt.outcome)
    elif age_s is not None and age_s > state.stale_after_s:
        status = SourceViewStatus.STALE
    elif age_s is not None:
        status = SourceViewStatus.LIVE
    else:
        status = SourceViewStatus.CHECKING

    return SourceView(
        source_id=state.source_id,
        status=status,
        age_s=age_s,
        refreshing=refreshing,
        decision_ready=is_decision_ready(state, now_mono_s),
        last_valid_revision=last_valid_revision,
        failure=last_attempt.failure if last_attempt else None,
    )


def reduce_cpu_window(
    window: CpuWindow, source: SourceState[CpuUsage]
) -> CpuWindow:
    """Incorporate one completed CPU attempt without inventing coverage."""
    if not isinstance(window, CpuWindow):
        raise ValueError("window must be a CpuWindow")
    if not isinstance(source, SourceState) or source.source_id is not SourceId.CPU_USAGE:
        raise ValueError("source must be a CPU usage SourceState")
    attempt = source.last_attempt
    if attempt is None:
        return window
    _validate_cpu_attempt(attempt)

    if attempt.revision == window.last_attempt_revision:
        return window

    if attempt.outcome is not AttemptOutcome.SUCCESS:
        return CpuWindow((), attempt.revision)

    sample = _validated_cpu_success(source, attempt)
    if attempt.revision < window.last_attempt_revision:
        return CpuWindow((sample,), attempt.revision)

    if not window.samples:
        return CpuWindow((sample,), attempt.revision)

    previous = window.samples[-1]
    previous_usage = previous.value
    usage = sample.value
    gap_s = usage.window_started_mono_s - previous_usage.window_completed_mono_s
    if (
        usage.window_started_mono_s < previous_usage.window_completed_mono_s
        or usage.window_completed_mono_s <= previous_usage.window_completed_mono_s
        or gap_s > 2.5
    ):
        return CpuWindow((sample,), attempt.revision)

    return CpuWindow(
        _retain_cpu_samples(window.samples + (sample,)), attempt.revision
    )


def cpu_window_summary(
    window: CpuWindow, duration_s: float
) -> Optional[CpuWindowSummary]:
    """Summarize a fully represented continuous CPU interval by elapsed time."""
    if not isinstance(window, CpuWindow):
        raise ValueError("window must be a CpuWindow")
    _require_finite_positive(duration_s, "duration_s")
    if not window.samples:
        return None

    completed_mono_s = window.samples[-1].value.window_completed_mono_s
    started_mono_s = completed_mono_s - duration_s
    represented_until = started_mono_s
    weighted_percent_seconds = 0.0

    for sample in window.samples:
        usage = sample.value
        if usage.window_completed_mono_s <= represented_until:
            continue
        if usage.window_started_mono_s > represented_until:
            return None
        segment_started_mono_s = max(
            usage.window_started_mono_s, represented_until
        )
        segment_duration_s = (
            usage.window_completed_mono_s - segment_started_mono_s
        )
        weighted_percent_seconds += usage.percent * segment_duration_s
        represented_until = usage.window_completed_mono_s

    if represented_until < completed_mono_s:
        return None
    return CpuWindowSummary(
        duration_s,
        weighted_percent_seconds / duration_s,
        started_mono_s,
        completed_mono_s,
    )


def _retain_cpu_samples(
    samples: Tuple[Sample[CpuUsage], ...]
) -> Tuple[Sample[CpuUsage], ...]:
    cutoff_mono_s = samples[-1].value.window_completed_mono_s - 15.0
    return tuple(
        sample
        for sample in samples
        if sample.value.window_completed_mono_s > cutoff_mono_s
    )


def _validated_cpu_success(
    source: SourceState[CpuUsage], attempt: CollectionAttempt
) -> Sample[CpuUsage]:
    sample = source.last_valid
    if sample is None or sample.revision != attempt.revision:
        raise ValueError("successful CPU attempt lacks its matching sample")
    _validate_cpu_sample(sample)
    if (
        sample.started_mono_s != attempt.started_mono_s
        or sample.completed_mono_s != attempt.completed_mono_s
        or sample.completed_at_utc != attempt.completed_at_utc
    ):
        raise ValueError("CPU sample does not match its completed attempt")
    return sample


def _validate_cpu_attempt(attempt: CollectionAttempt) -> None:
    if not isinstance(attempt, CollectionAttempt):
        raise ValueError("last_attempt must be a CollectionAttempt")
    if type(attempt.revision) is not int or attempt.revision <= 0:
        raise ValueError("CPU attempt revision must be a positive int")
    _require_finite_mono(attempt.started_mono_s, "attempt.started_mono_s")
    _require_finite_mono(attempt.completed_mono_s, "attempt.completed_mono_s")
    if attempt.completed_mono_s < attempt.started_mono_s:
        raise ValueError("CPU attempt completion cannot precede its start")
    if (
        attempt.completed_at_utc.tzinfo is None
        or attempt.completed_at_utc.utcoffset() is None
    ):
        raise ValueError("CPU attempt completion timestamp must be timezone-aware")
    if not isinstance(attempt.outcome, AttemptOutcome):
        raise ValueError("CPU attempt outcome must be an AttemptOutcome")
    if attempt.outcome is AttemptOutcome.SUCCESS:
        if attempt.failure is not None:
            raise ValueError("successful CPU attempt cannot have a failure code")
    elif attempt.failure not in _LEGAL_FAILURES.get(attempt.outcome, set()):
        raise ValueError("CPU attempt failure code is incompatible with its outcome")


def _validate_cpu_sample(sample: Sample[CpuUsage]) -> None:
    if not isinstance(sample, Sample):
        raise ValueError("CPU window samples must be Samples")
    if type(sample.revision) is not int or sample.revision <= 0:
        raise ValueError("CPU sample revision must be a positive int")
    if not isinstance(sample.value, CpuUsage):
        raise ValueError("CPU sample value must be CpuUsage")
    _require_finite_mono(sample.started_mono_s, "sample.started_mono_s")
    _require_finite_mono(sample.completed_mono_s, "sample.completed_mono_s")
    if sample.completed_mono_s < sample.started_mono_s:
        raise ValueError("CPU sample completion cannot precede its start")
    if sample.completed_at_utc.tzinfo is None or sample.completed_at_utc.utcoffset() is None:
        raise ValueError("CPU sample completion timestamp must be timezone-aware")


def _require_finite_positive(value: float, name: str) -> None:
    _require_finite_mono(value, name)
    if value <= 0:
        raise ValueError("%s must be finite and positive" % name)


def _require_finite_mono(value: float, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("%s must be a finite int or float" % name)


def _require_percent(value: float, name: str) -> None:
    _require_finite_mono(value, name)
    if value < 0 or value > 100:
        raise ValueError("%s must be within 0..100" % name)


def _require_nonnegative_int(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("%s must be a nonnegative int" % name)


def _validate_completion(
    state: SourceState[T],
    revision: int,
    completed_mono_s: float,
    completed_at_utc: datetime,
) -> Tuple[float, datetime]:
    if type(revision) is not int:
        raise ValueError("revision must be an int")
    if state.active_revision != revision or state.active_started_mono_s is None:
        raise ValueError("completion does not match an active revision")
    _require_finite_mono(completed_mono_s, "completed_mono_s")
    if completed_mono_s < state.active_started_mono_s:
        raise ValueError("completed_mono_s cannot precede started_mono_s")
    if completed_at_utc.tzinfo is None or completed_at_utc.utcoffset() is None:
        raise ValueError("completed_at_utc must be timezone-aware")
    return state.active_started_mono_s, completed_at_utc.astimezone(timezone.utc)


def _failure_status(source_id: SourceId, outcome: AttemptOutcome) -> SourceViewStatus:
    if outcome is AttemptOutcome.NOT_APPLICABLE:
        return SourceViewStatus.NOT_APPLICABLE
    if outcome is AttemptOutcome.UNSUPPORTED:
        return SourceViewStatus.UNSUPPORTED
    if outcome is AttemptOutcome.PERMISSION_DENIED:
        if source_id is SourceId.SENSOR_POWERMETRICS:
            return SourceViewStatus.LOCKED
        return SourceViewStatus.PERMISSION_DENIED
    if outcome is AttemptOutcome.MISSING_DEPENDENCY:
        return SourceViewStatus.MISSING
    return SourceViewStatus.ERROR
