"""Pure immutable source-state reducers for PorkyHUD telemetry evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum, IntEnum
import math
from typing import Generic, Optional, Tuple, TypeVar


T = TypeVar("T")
_CPU_TIME_ABS_TOLERANCE_S = 1e-9


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
        _require_finite_nonnegative(self.cpu_percent, "cpu_percent")
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
                if sample.revision != previous.revision + 1:
                    raise ValueError("CPU window sample revisions must be consecutive")
                if usage.window_started_mono_s < previous_usage.window_completed_mono_s:
                    raise ValueError("CPU window samples cannot overlap")
                if (
                    usage.window_started_mono_s
                    - previous_usage.window_completed_mono_s
                    > 2.5
                ):
                    raise ValueError("CPU window sample gap cannot exceed 2.5 seconds")
                if usage.window_completed_mono_s <= previous_usage.window_completed_mono_s:
                    raise ValueError("CPU window sample times must increase")
            previous = sample
        if (
            self.samples
            and self.last_attempt_revision != self.samples[-1].revision
        ):
            raise ValueError(
                "last_attempt_revision must match the newest retained sample"
            )


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
        wall_span_s = self.completed_mono_s - self.started_mono_s
        if wall_span_s < self.duration_s and not math.isclose(
            wall_span_s,
            self.duration_s,
            rel_tol=0.0,
            abs_tol=_CPU_TIME_ABS_TOLERANCE_S,
        ):
            raise ValueError(
                "summary wall span cannot be shorter than represented duration"
            )


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
    """Incorporate one completed CPU attempt without inventing coverage.

    Observation gaps up to 2.5 seconds preserve continuity eligibility, but
    the gap itself never becomes represented CPU time.
    """
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
    if not window.samples:
        return CpuWindow((sample,), attempt.revision)

    if attempt.revision != window.last_attempt_revision + 1:
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
    """Summarize represented CPU time, excluding allowed observation gaps."""
    if not isinstance(window, CpuWindow):
        raise ValueError("window must be a CpuWindow")
    _require_finite_positive(duration_s, "duration_s")
    if not window.samples:
        return None

    sample_durations = tuple(
        sample.value.window_completed_mono_s
        - sample.value.window_started_mono_s
        for sample in window.samples
    )
    available_duration_s = math.fsum(sample_durations)
    if available_duration_s < duration_s and not math.isclose(
        available_duration_s,
        duration_s,
        rel_tol=0.0,
        abs_tol=_CPU_TIME_ABS_TOLERANCE_S,
    ):
        return None

    completed_mono_s = window.samples[-1].value.window_completed_mono_s
    used_segments = []
    used_durations = []
    started_mono_s = None

    for sample in reversed(window.samples):
        usage = sample.value
        sample_duration_s = (
            usage.window_completed_mono_s - usage.window_started_mono_s
        )
        represented_duration_s = math.fsum(used_durations)
        remaining_duration_s = duration_s - represented_duration_s
        if remaining_duration_s <= 0.0:
            break
        if sample_duration_s < remaining_duration_s and not math.isclose(
            sample_duration_s,
            remaining_duration_s,
            rel_tol=0.0,
            abs_tol=_CPU_TIME_ABS_TOLERANCE_S,
        ):
            used_duration_s = sample_duration_s
        else:
            used_duration_s = min(sample_duration_s, remaining_duration_s)
        used_segments.append((usage.percent, used_duration_s))
        used_durations.append(used_duration_s)
        started_mono_s = usage.window_completed_mono_s - used_duration_s

    represented_duration_s = math.fsum(used_durations)
    if (
        represented_duration_s < duration_s
        and not math.isclose(
            represented_duration_s,
            duration_s,
            rel_tol=0.0,
            abs_tol=_CPU_TIME_ABS_TOLERANCE_S,
        )
    ) or started_mono_s is None:
        return None
    reference_percent = used_segments[0][0]
    mean_percent = reference_percent + math.fsum(
        (percent - reference_percent) * represented_s
        for percent, represented_s in used_segments
    ) / represented_duration_s
    return CpuWindowSummary(
        duration_s,
        mean_percent,
        started_mono_s,
        completed_mono_s,
    )


def _retain_cpu_samples(
    samples: Tuple[Sample[CpuUsage], ...]
) -> Tuple[Sample[CpuUsage], ...]:
    """Keep 15 represented seconds plus the intact clipped-boundary sample."""
    represented_durations = []
    first_retained_index = len(samples)
    for index in range(len(samples) - 1, -1, -1):
        usage = samples[index].value
        represented_durations.append(
            usage.window_completed_mono_s - usage.window_started_mono_s
        )
        first_retained_index = index
        represented_duration_s = math.fsum(represented_durations)
        if represented_duration_s > 15.0 or math.isclose(
            represented_duration_s,
            15.0,
            rel_tol=0.0,
            abs_tol=_CPU_TIME_ABS_TOLERANCE_S,
        ):
            break
    return samples[first_retained_index:]


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


def _require_finite_nonnegative(value: float, name: str) -> None:
    _require_finite_mono(value, name)
    if value < 0:
        raise ValueError("%s must be finite and nonnegative" % name)


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


class HealthSeverity(str, Enum):
    NORMAL = "normal"
    WATCH = "watch"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class LatchPhase(str, Enum):
    CLEAR = "clear"
    ACTIVE = "active"
    RECOVERING = "recovering"


class RuleId(str, Enum):
    MEMORY_CRITICAL = "memory.critical"
    THERMAL_WATCH = "thermal.watch"
    MEMORY_WATCH = "memory.watch"
    STORAGE_WATCH = "storage.watch"
    CPU_WATCH = "cpu.watch"


@dataclass(frozen=True)
class TriggerEvidence:
    source_id: SourceId
    revision: int
    triggered_mono_s: float
    value_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, SourceId):
            raise ValueError("trigger source_id must be a SourceId")
        if type(self.revision) is not int or self.revision <= 0:
            raise ValueError("trigger revision must be a positive int")
        _require_finite_mono(self.triggered_mono_s, "triggered_mono_s")
        if not isinstance(self.value_text, str) or not self.value_text:
            raise ValueError("trigger value_text must be a non-empty string")


@dataclass(frozen=True)
class RuleLatch:
    rule_id: RuleId
    severity: Optional[HealthSeverity]
    phase: LatchPhase
    trigger: Optional[TriggerEvidence]
    recovery_count: int
    recovery_started_mono_s: Optional[float]
    last_processed_revision: int
    recovery_identity: Optional[str]

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, RuleId):
            raise ValueError("rule_id must be a RuleId")
        if self.severity is not None and not isinstance(self.severity, HealthSeverity):
            raise ValueError("severity must be a HealthSeverity or None")
        if not isinstance(self.phase, LatchPhase):
            raise ValueError("phase must be a LatchPhase")
        if self.trigger is not None and not isinstance(self.trigger, TriggerEvidence):
            raise ValueError("trigger must be TriggerEvidence or None")
        if type(self.recovery_count) is not int or self.recovery_count < 0:
            raise ValueError("recovery_count must be a nonnegative int")
        if self.recovery_started_mono_s is not None:
            _require_finite_mono(self.recovery_started_mono_s,
                                 "recovery_started_mono_s")
        if type(self.last_processed_revision) is not int or self.last_processed_revision < 0:
            raise ValueError("last_processed_revision must be a nonnegative int")
        if self.recovery_identity is not None and not isinstance(self.recovery_identity, str):
            raise ValueError("recovery_identity must be a string or None")
        if self.phase is LatchPhase.CLEAR:
            if any((self.severity is not None, self.trigger is not None,
                    self.recovery_count != 0,
                    self.recovery_started_mono_s is not None,
                    self.recovery_identity is not None)):
                raise ValueError("clear latch cannot retain warning or recovery metadata")
            return
        if self.severity not in (HealthSeverity.WATCH, HealthSeverity.CRITICAL):
            raise ValueError("active latch severity must be watch or critical")
        if self.trigger is None:
            raise ValueError("active latch requires trigger evidence")
        if not _rule_matches(self.rule_id, self.severity):
            raise ValueError("rule_id is incompatible with latch severity")
        if self.trigger.source_id is not _rule_source_for(self.rule_id):
            raise ValueError("trigger source_id is incompatible with rule_id")
        if self.phase is LatchPhase.ACTIVE:
            if any((self.recovery_count != 0,
                    self.recovery_started_mono_s is not None,
                    self.recovery_identity is not None)):
                raise ValueError("active latch cannot carry a recovery streak")
        elif self.recovery_count > 0 and self.recovery_started_mono_s is None:
            raise ValueError("recovery streak requires its start time")
        if self.recovery_identity is not None:
            last_recovery_mono_s = _recovery_last_time(self.recovery_identity)
            if (self.recovery_started_mono_s is not None
                    and last_recovery_mono_s < self.recovery_started_mono_s):
                raise ValueError("recovery marker cannot precede recovery start")


def empty_latch(rule_id: RuleId) -> RuleLatch:
    """Create the only structurally clear state for a rule family."""
    if not isinstance(rule_id, RuleId):
        raise ValueError("rule_id must be a RuleId")
    return RuleLatch(rule_id, None, LatchPhase.CLEAR, None, 0, None, 0, None)


def reduce_memory_latch(
    latch: RuleLatch, source: SourceState[MemoryPressure], now_mono_s: float
) -> RuleLatch:
    """Apply the macOS pressure rule without treating retained evidence as live."""
    latch = _validate_latch_for(latch, (RuleId.MEMORY_WATCH, RuleId.MEMORY_CRITICAL))
    attempt = _rule_attempt(source, SourceId.MEMORY_PRESSURE, MemoryPressure, now_mono_s)
    if attempt is None:
        return latch
    duplicate = _check_attempt_revision(latch, attempt)
    if duplicate:
        return latch
    if not is_decision_ready(source, now_mono_s):
        return _reset_recovery(latch, attempt.revision)
    sample = _rule_success(source, attempt, MemoryPressure)
    level = sample.value.level
    if level is MemoryPressureLevel.CRITICAL:
        return _active_latch(
            RuleId.MEMORY_CRITICAL, HealthSeverity.CRITICAL, attempt.revision,
            _trigger(source.source_id, sample, "memory pressure level 4"),
        )
    if level is MemoryPressureLevel.WARN:
        if latch.severity is HealthSeverity.CRITICAL:
            return _memory_noncritical(latch, sample, attempt.revision, level)
        return _active_latch(
            RuleId.MEMORY_WATCH, HealthSeverity.WATCH, attempt.revision,
            _trigger(source.source_id, sample, "memory pressure level 2"),
        )
    if latch.phase is LatchPhase.CLEAR:
        return _clear_latch(latch.rule_id, attempt.revision)
    if latch.severity is HealthSeverity.CRITICAL:
        return _memory_noncritical(latch, sample, attempt.revision, level)
    return _advance_streak(latch, attempt.revision, sample.completed_mono_s,
                           2.5, 10, 9.0, None)


def reduce_thermal_latch(
    latch: RuleLatch, source: SourceState[OsThermalState], now_mono_s: float
) -> RuleLatch:
    """Apply the explicit OS thermal-warning rule."""
    latch = _validate_latch_for(latch, (RuleId.THERMAL_WATCH,))
    attempt = _rule_attempt(source, SourceId.THERMAL_OS, OsThermalState, now_mono_s)
    if attempt is None:
        return latch
    duplicate = _check_attempt_revision(latch, attempt)
    if duplicate:
        return latch
    if not is_decision_ready(source, now_mono_s):
        return _reset_recovery(latch, attempt.revision)
    sample = _rule_success(source, attempt, OsThermalState)
    value = sample.value
    if value.thermal_warning_level > 0 or value.performance_warning_level > 0:
        return _active_latch(
            RuleId.THERMAL_WATCH, HealthSeverity.WATCH, attempt.revision,
            _trigger(source.source_id, sample,
                     "thermal=%d performance=%d" % (
                         value.thermal_warning_level, value.performance_warning_level)),
        )
    if latch.phase is LatchPhase.CLEAR:
        return _clear_latch(latch.rule_id, attempt.revision)
    return _advance_streak(latch, attempt.revision, sample.completed_mono_s,
                           12.0, 3, 10.0, None)


def reduce_storage_latch(
    latch: RuleLatch, source: SourceState[DiskCapacity], now_mono_s: float
) -> RuleLatch:
    """Apply low-headroom entry and same-volume recovery confirmation."""
    latch = _validate_latch_for(latch, (RuleId.STORAGE_WATCH,))
    attempt = _rule_attempt(source, SourceId.DISK_CAPACITY, DiskCapacity, now_mono_s)
    if attempt is None:
        return latch
    duplicate = _check_attempt_revision(latch, attempt)
    if duplicate:
        return latch
    if not is_decision_ready(source, now_mono_s):
        return _reset_recovery(latch, attempt.revision)
    sample = _rule_success(source, attempt, DiskCapacity)
    disk = sample.value
    if _storage_low(disk):
        return _active_latch(
            RuleId.STORAGE_WATCH, HealthSeverity.WATCH, attempt.revision,
            _trigger(source.source_id, sample,
                     "%d bytes free of %d bytes on %s" % (
                         disk.free_bytes, disk.total_bytes, disk.volume_id)),
        )
    if latch.phase is LatchPhase.CLEAR:
        return _clear_latch(latch.rule_id, attempt.revision)
    if not _storage_recovered(disk):
        return _reset_recovery(latch, attempt.revision)
    return _advance_streak(latch, attempt.revision, sample.completed_mono_s,
                           75.0, 2, 30.0,
                           _volume_identity(disk))


def reduce_cpu_latch(
    latch: RuleLatch,
    source: SourceState[CpuUsage],
    window: CpuWindow,
    now_mono_s: float,
) -> RuleLatch:
    """Latch only sustained, continuously represented CPU pressure."""
    latch = _validate_latch_for(latch, (RuleId.CPU_WATCH,))
    attempt = _rule_attempt(source, SourceId.CPU_USAGE, CpuUsage, now_mono_s)
    if not isinstance(window, CpuWindow):
        raise ValueError("window must be a CpuWindow")
    if attempt is None:
        return latch
    duplicate = _check_attempt_revision(latch, attempt)
    if duplicate:
        return latch
    if window.last_attempt_revision != attempt.revision:
        raise ValueError("CPU window must include the latest source attempt")
    if not is_decision_ready(source, now_mono_s):
        return _reset_recovery(latch, attempt.revision)
    sample = _rule_success(source, attempt, CpuUsage)
    entry = cpu_window_summary(window, 15.0)
    if entry is not None and entry.mean_percent >= 90.0:
        return _active_latch(
            RuleId.CPU_WATCH, HealthSeverity.WATCH, attempt.revision,
            _trigger(source.source_id, sample,
                     "%.1f%% mean over 15s" % entry.mean_percent),
        )
    if latch.phase is LatchPhase.CLEAR:
        return _clear_latch(latch.rule_id, attempt.revision)
    recovery = cpu_window_summary(window, 10.0)
    if recovery is not None and recovery.mean_percent <= 70.0:
        return _clear_latch(latch.rule_id, attempt.revision)
    return RuleLatch(latch.rule_id, latch.severity, LatchPhase.RECOVERING,
                     latch.trigger, 0, None, attempt.revision, None)


def _rule_matches(rule_id: RuleId, severity: HealthSeverity) -> bool:
    return (
        (rule_id is RuleId.MEMORY_CRITICAL and severity is HealthSeverity.CRITICAL)
        or (rule_id in (RuleId.MEMORY_WATCH, RuleId.THERMAL_WATCH,
                        RuleId.STORAGE_WATCH, RuleId.CPU_WATCH)
            and severity is HealthSeverity.WATCH)
    )


def _rule_source_for(rule_id: RuleId) -> SourceId:
    if rule_id in (RuleId.MEMORY_WATCH, RuleId.MEMORY_CRITICAL):
        return SourceId.MEMORY_PRESSURE
    if rule_id is RuleId.THERMAL_WATCH:
        return SourceId.THERMAL_OS
    if rule_id is RuleId.STORAGE_WATCH:
        return SourceId.DISK_CAPACITY
    return SourceId.CPU_USAGE


def _validate_latch_for(latch: RuleLatch, permitted: Tuple[RuleId, ...]) -> RuleLatch:
    if not isinstance(latch, RuleLatch) or latch.rule_id not in permitted:
        raise ValueError("latch has the wrong rule family")
    if latch.trigger is not None and latch.trigger.source_id not in _rule_sources(permitted):
        raise ValueError("latch trigger belongs to the wrong source family")
    return latch


def _rule_sources(rule_ids: Tuple[RuleId, ...]) -> Tuple[SourceId, ...]:
    sources = []
    for rule_id in rule_ids:
        if rule_id in (RuleId.MEMORY_WATCH, RuleId.MEMORY_CRITICAL):
            sources.append(SourceId.MEMORY_PRESSURE)
        elif rule_id is RuleId.THERMAL_WATCH:
            sources.append(SourceId.THERMAL_OS)
        elif rule_id is RuleId.STORAGE_WATCH:
            sources.append(SourceId.DISK_CAPACITY)
        else:
            sources.append(SourceId.CPU_USAGE)
    return tuple(sources)


def _rule_attempt(
    source: SourceState[T], source_id: SourceId, value_type: type,
    now_mono_s: float,
) -> Optional[CollectionAttempt]:
    _require_finite_mono(now_mono_s, "now_mono_s")
    if not isinstance(source, SourceState) or source.source_id is not source_id:
        raise ValueError("source has the wrong SourceId")
    _require_finite_positive(source.expected_cadence_s, "source.expected_cadence_s")
    _require_finite_positive(source.stale_after_s, "source.stale_after_s")
    if source.stale_after_s < source.expected_cadence_s:
        raise ValueError("source stale_after_s is shorter than cadence")
    if type(source.revision) is not int or source.revision < 0:
        raise ValueError("source revision must be a nonnegative int")
    if source.active_revision is None:
        if source.active_started_mono_s is not None:
            raise ValueError("inactive source cannot retain an active start time")
    else:
        if (type(source.active_revision) is not int or source.active_revision <= 0
                or source.active_revision != source.revision):
            raise ValueError("active source revision is inconsistent")
        if source.active_started_mono_s is None:
            raise ValueError("active source requires an active start time")
        _require_finite_mono(source.active_started_mono_s,
                             "source.active_started_mono_s")
    attempt = source.last_attempt
    if attempt is None:
        if source.last_valid is not None:
            raise ValueError("source cannot retain a sample without an attempt")
        if source.active_revision is None and source.revision != 0:
            raise ValueError("inactive source cannot have an uncompleted revision")
        return None
    _validate_cpu_attempt(attempt)
    if source.active_revision is None:
        if attempt.revision != source.revision:
            raise ValueError("completed attempt must match source revision")
    elif (attempt.revision != source.active_revision - 1
          or source.active_started_mono_s < attempt.completed_mono_s):
        raise ValueError("active source cannot skip or precede an attempt")
    if attempt.outcome is AttemptOutcome.SUCCESS:
        _rule_success(source, attempt, value_type)
    elif source.last_valid is not None:
        _validate_rule_sample(source.last_valid, value_type)
    return attempt


def _rule_success(
    source: SourceState[T], attempt: CollectionAttempt, value_type: type
) -> Sample[T]:
    if attempt.outcome is not AttemptOutcome.SUCCESS:
        raise ValueError("successful source sample requires a successful attempt")
    sample = source.last_valid
    if sample is None or sample.revision != attempt.revision:
        raise ValueError("successful attempt lacks matching sample revision")
    _validate_rule_sample(sample, value_type)
    if (sample.started_mono_s != attempt.started_mono_s
            or sample.completed_mono_s != attempt.completed_mono_s
            or sample.completed_at_utc != attempt.completed_at_utc):
        raise ValueError("successful sample does not match completed attempt")
    return sample


def _validate_rule_sample(sample: Sample[T], value_type: type) -> None:
    if not isinstance(sample, Sample):
        raise ValueError("source last_valid must be a Sample")
    if not isinstance(sample.value, value_type):
        raise ValueError("sample value has the wrong normalized fact type")
    if type(sample.revision) is not int or sample.revision <= 0:
        raise ValueError("sample revision must be a positive int")
    _require_finite_mono(sample.started_mono_s, "sample.started_mono_s")
    _require_finite_mono(sample.completed_mono_s, "sample.completed_mono_s")
    if sample.completed_mono_s < sample.started_mono_s:
        raise ValueError("sample completion cannot precede its start")
    if sample.completed_at_utc.tzinfo is None or sample.completed_at_utc.utcoffset() is None:
        raise ValueError("sample completion timestamp must be timezone-aware")


def _check_attempt_revision(latch: RuleLatch, attempt: CollectionAttempt) -> bool:
    if attempt.revision == latch.last_processed_revision:
        return True
    if attempt.revision < latch.last_processed_revision:
        raise ValueError("source attempt revision cannot go backwards")
    if latch.trigger is not None and attempt.completed_mono_s < latch.trigger.triggered_mono_s:
        raise ValueError("source completion cannot precede latch trigger")
    return False


def _trigger(source_id: SourceId, sample: Sample[T], value_text: str) -> TriggerEvidence:
    return TriggerEvidence(source_id, sample.revision, sample.completed_mono_s, value_text)


def _active_latch(
    rule_id: RuleId, severity: HealthSeverity, revision: int,
    trigger: TriggerEvidence,
) -> RuleLatch:
    return RuleLatch(rule_id, severity, LatchPhase.ACTIVE, trigger,
                     0, None, revision, None)


def _clear_latch(rule_id: RuleId, revision: int) -> RuleLatch:
    return RuleLatch(rule_id, None, LatchPhase.CLEAR, None,
                     0, None, revision, None)


def _reset_recovery(latch: RuleLatch, revision: int) -> RuleLatch:
    if latch.phase is LatchPhase.CLEAR:
        return _clear_latch(latch.rule_id, revision)
    return RuleLatch(latch.rule_id, latch.severity, LatchPhase.ACTIVE,
                     latch.trigger, 0, None, revision, None)


def _memory_noncritical(
    latch: RuleLatch, sample: Sample[MemoryPressure], revision: int,
    level: MemoryPressureLevel,
) -> RuleLatch:
    progress = _advance_streak(latch, revision, sample.completed_mono_s,
                               2.5, 5, 4.0, None)
    if progress.severity is HealthSeverity.CRITICAL:
        return progress
    if level is MemoryPressureLevel.WARN:
        return _active_latch(RuleId.MEMORY_WATCH, HealthSeverity.WATCH,
                             revision, latch.trigger)
    # The fifth non-critical normal observation becomes the first independent
    # normal proof for the subsequent watch recovery.
    return RuleLatch(RuleId.MEMORY_WATCH, HealthSeverity.WATCH,
                     LatchPhase.RECOVERING, latch.trigger,
                     1, sample.completed_mono_s, revision,
                     _recovery_marker(None, sample.completed_mono_s))


def _advance_streak(
    latch: RuleLatch, revision: int, completed_mono_s: float, max_gap_s: float,
    required_count: int, required_span_s: float, identity: Optional[str],
) -> RuleLatch:
    previous_time = _recovery_last_time(latch.recovery_identity)
    same_identity = _recovery_base_identity(latch.recovery_identity) == identity
    if (latch.phase is not LatchPhase.RECOVERING or not same_identity
            or previous_time is None):
        count = 1
        started = completed_mono_s
    else:
        if completed_mono_s < previous_time:
            raise ValueError("recovery sample time cannot go backwards")
        if completed_mono_s - previous_time > max_gap_s:
            count = 1
            started = completed_mono_s
        else:
            count = latch.recovery_count + 1
            started = latch.recovery_started_mono_s
    marker = _recovery_marker(identity, completed_mono_s)
    if count >= required_count and completed_mono_s - started >= required_span_s:
        return _clear_latch(latch.rule_id, revision)
    return RuleLatch(latch.rule_id, latch.severity, LatchPhase.RECOVERING,
                     latch.trigger, count, started, revision, marker)


def _recovery_marker(identity: Optional[str], last_mono_s: float) -> str:
    base = "" if identity is None else identity
    return "%s\x1f%.17g" % (base, last_mono_s)


def _recovery_base_identity(marker: Optional[str]) -> Optional[str]:
    if marker is None:
        return None
    if "\x1f" not in marker:
        raise ValueError("recovery_identity is malformed")
    base, _ = marker.rsplit("\x1f", 1)
    return base or None


def _recovery_last_time(marker: Optional[str]) -> Optional[float]:
    if marker is None:
        return None
    if "\x1f" not in marker:
        raise ValueError("recovery_identity is malformed")
    _, encoded = marker.rsplit("\x1f", 1)
    try:
        value = float(encoded)
    except ValueError:
        raise ValueError("recovery_identity has an invalid timestamp")
    _require_finite_mono(value, "recovery_identity timestamp")
    return value


def _storage_low(disk: DiskCapacity) -> bool:
    return disk.free_bytes * 100 < disk.total_bytes * 10 and disk.free_bytes < 20 * 1024 ** 3


def _storage_recovered(disk: DiskCapacity) -> bool:
    return disk.free_bytes * 100 >= disk.total_bytes * 12 or disk.free_bytes >= 25 * 1024 ** 3


def _volume_identity(disk: DiskCapacity) -> str:
    return "%s\x1f%s" % (disk.volume_id, disk.mount_point)
