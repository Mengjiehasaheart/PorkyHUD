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
        _require_finite_nonnegative(self.cpu_percent, "cpu_percent")
        if self.elapsed_seconds is not None:
            _require_nonnegative_int(self.elapsed_seconds, "elapsed_seconds")


class ContributorState(str, Enum):
    LEADING = "leading"
    NO_CLEAR_LEADER = "no_clear_leader"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CpuContributor:
    state: ContributorState
    name: Optional[str] = None
    source_revision: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ContributorState):
            raise ValueError("state must be a ContributorState")
        if self.state is ContributorState.LEADING:
            if type(self.name) is not str or not self.name:
                raise ValueError("leading contributor requires a non-empty name")
            if type(self.source_revision) is not int or self.source_revision <= 0:
                raise ValueError("leading contributor requires a positive source revision")
            return
        if self.name is not None or self.source_revision is not None:
            raise ValueError("non-leading contributor cannot retain identity evidence")


@dataclass(frozen=True)
class ProcessHistory:
    previous: Tuple[ProcessFact, ...] = ()
    current: Tuple[ProcessFact, ...] = ()
    previous_revision: int = 0
    current_revision: int = 0
    last_attempt_revision: int = 0

    def __post_init__(self) -> None:
        if type(self.previous) is not tuple or type(self.current) is not tuple:
            raise ValueError("process history rows must be tuples")
        _validate_process_table(self.previous, "previous")
        _validate_process_table(self.current, "current")
        for value, name in (
            (self.previous_revision, "previous_revision"),
            (self.current_revision, "current_revision"),
            (self.last_attempt_revision, "last_attempt_revision"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError("%s must be a nonnegative int" % name)
        if self.current_revision == 0:
            if self.previous or self.current or self.previous_revision:
                raise ValueError("absent process history must have zero sample revisions")
            return
        if self.current_revision != self.last_attempt_revision:
            raise ValueError("current revision must match the latest attempt")
        if self.previous_revision == 0:
            if self.previous:
                raise ValueError("missing previous sample cannot contain rows")
            return
        if self.previous_revision <= 0:
            raise ValueError("previous process sample requires a positive revision")
        if self.current_revision != self.previous_revision + 1:
            raise ValueError("process samples must have consecutive revisions")


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
        if wall_span_s < self.duration_s:
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


def reduce_process_history(
    history: ProcessHistory, source: SourceState[Tuple[ProcessFact, ...]]
) -> ProcessHistory:
    """Retain only two consecutive successful process-table samples.

    A process attempt is consumed once.  Any observed failure or unknown gap
    discards prior identity evidence instead of allowing it to bridge an
    unobserved process-table result.
    """
    if not isinstance(history, ProcessHistory):
        raise ValueError("history must be a ProcessHistory")
    attempt = _process_attempt(source)
    if attempt is None:
        return history
    if attempt.revision == history.last_attempt_revision:
        if attempt.outcome is not AttemptOutcome.SUCCESS and history.current_revision:
            return ProcessHistory(last_attempt_revision=attempt.revision)
        if attempt.outcome is AttemptOutcome.SUCCESS:
            sample = _validated_process_success(source, attempt)
            if (
                history.current_revision != attempt.revision
                or history.current != sample.value
            ):
                raise ValueError("duplicate success does not match consumed process history")
        return history
    if attempt.revision < history.last_attempt_revision:
        raise ValueError("process attempt revision cannot recede")
    if attempt.outcome is not AttemptOutcome.SUCCESS:
        return ProcessHistory(last_attempt_revision=attempt.revision)

    sample = _validated_process_success(source, attempt)
    if (
        history.current_revision == 0
        or attempt.revision != history.last_attempt_revision + 1
    ):
        return ProcessHistory(
            current=sample.value,
            current_revision=attempt.revision,
            last_attempt_revision=attempt.revision,
        )
    return ProcessHistory(
        previous=history.current,
        current=sample.value,
        previous_revision=history.current_revision,
        current_revision=attempt.revision,
        last_attempt_revision=attempt.revision,
    )


def select_cpu_contributor(
    history: ProcessHistory,
    source: SourceState[Tuple[ProcessFact, ...]],
    now_mono_s: float,
    max_age_s: float = 6.0,
) -> CpuContributor:
    """Name a CPU contributor only when recent identity evidence is decisive."""
    if not isinstance(history, ProcessHistory):
        raise ValueError("history must be a ProcessHistory")
    _require_finite_mono(now_mono_s, "now_mono_s")
    _require_finite_positive(max_age_s, "max_age_s")
    if max_age_s > 6.0:
        raise ValueError("max_age_s cannot exceed the attribution limit")

    attempt = _process_attempt(source, now_mono_s)
    if attempt is None:
        return CpuContributor(ContributorState.UNAVAILABLE)
    if attempt.outcome is not AttemptOutcome.SUCCESS:
        return CpuContributor(ContributorState.UNAVAILABLE)
    sample = _validated_process_success(source, attempt)
    age_s = sample_age_s(source, now_mono_s)
    if (
        not is_decision_ready(source, now_mono_s)
        or age_s is None
        or age_s > max_age_s
        or history.current_revision != sample.revision
        or history.last_attempt_revision != attempt.revision
        or history.previous_revision == 0
        or history.current != sample.value
    ):
        return CpuContributor(ContributorState.UNAVAILABLE)

    current = history.current
    if not current:
        return CpuContributor(ContributorState.NO_CLEAR_LEADER)
    top = max(current, key=lambda fact: fact.cpu_percent)
    runner_up_percent = max(
        (fact.cpu_percent for fact in current if fact is not top), default=0.0
    )
    if (
        top.cpu_percent < 25.0
        or _less_than_one_point_five_times(top.cpu_percent, runner_up_percent)
    ):
        return CpuContributor(ContributorState.NO_CLEAR_LEADER)
    previous = next((fact for fact in history.previous if fact.pid == top.pid), None)
    if (
        previous is None
        or previous.name != top.name
        or previous.elapsed_seconds is None
        or top.elapsed_seconds is None
        or top.elapsed_seconds < previous.elapsed_seconds
    ):
        return CpuContributor(ContributorState.NO_CLEAR_LEADER)
    return CpuContributor(ContributorState.LEADING, top.name, sample.revision)


def _validate_process_table(rows: Tuple[ProcessFact, ...], name: str) -> None:
    seen_pids = set()
    for fact in rows:
        _validate_process_fact(fact)
        if fact.pid in seen_pids:
            raise ValueError("%s process rows must have unique PIDs" % name)
        seen_pids.add(fact.pid)


def _process_attempt(
    source: SourceState[Tuple[ProcessFact, ...]], now_mono_s: Optional[float] = None
) -> Optional[CollectionAttempt]:
    """Validate a complete process source before any attribution early return."""
    if not isinstance(source, SourceState) or source.source_id is not SourceId.PROCESS_TABLE:
        raise ValueError("source must be a process table SourceState")
    _require_finite_positive(source.expected_cadence_s, "source.expected_cadence_s")
    _require_finite_positive(source.stale_after_s, "source.stale_after_s")
    if source.stale_after_s < source.expected_cadence_s:
        raise ValueError("source stale_after_s is shorter than cadence")
    if type(source.revision) is not int or source.revision < 0:
        raise ValueError("source revision must be a nonnegative int")
    if now_mono_s is not None:
        _require_finite_mono(now_mono_s, "now_mono_s")
    if source.active_revision is None:
        if source.active_started_mono_s is not None:
            raise ValueError("inactive source cannot retain an active start time")
    else:
        if (
            type(source.active_revision) is not int
            or source.active_revision <= 0
            or source.active_revision != source.revision
        ):
            raise ValueError("active source revision is inconsistent")
        if source.active_started_mono_s is None:
            raise ValueError("active source requires an active start time")
        _require_finite_mono(source.active_started_mono_s, "source.active_started_mono_s")
        if now_mono_s is not None and now_mono_s < source.active_started_mono_s:
            raise ValueError("now_mono_s cannot precede the active attempt")

    attempt = source.last_attempt
    if attempt is None:
        if source.last_valid is not None:
            raise ValueError("source cannot retain a sample without an attempt")
        if source.active_revision is None and source.revision != 0:
            raise ValueError("inactive source cannot have an uncompleted revision")
        if source.active_revision is not None and source.revision != 1:
            raise ValueError("initial active source must have revision one")
        return None
    _validate_process_attempt(attempt)
    if now_mono_s is not None and now_mono_s < attempt.completed_mono_s:
        raise ValueError("now_mono_s cannot precede the completed attempt")
    if source.active_revision is None:
        if attempt.revision != source.revision:
            raise ValueError("completed attempt must match source revision")
    elif (
        attempt.revision != source.active_revision - 1
        or source.active_started_mono_s < attempt.completed_mono_s
    ):
        raise ValueError("active source cannot skip or precede an attempt")

    sample = source.last_valid
    if sample is not None:
        _validate_process_sample(sample)
        if sample.revision > attempt.revision:
            raise ValueError("retained sample cannot follow the latest attempt")
        if now_mono_s is not None and now_mono_s < sample.completed_mono_s:
            raise ValueError("now_mono_s cannot precede the retained sample")
        if attempt.outcome is not AttemptOutcome.SUCCESS:
            if sample.revision == attempt.revision:
                raise ValueError("failed attempt cannot own a valid sample")
            if sample.completed_mono_s > attempt.started_mono_s:
                raise ValueError("retained sample cannot complete after a later attempt starts")
            if sample.completed_at_utc > attempt.completed_at_utc:
                raise ValueError("retained sample wall time cannot follow a later attempt")
    if attempt.outcome is AttemptOutcome.SUCCESS:
        _validated_process_success(source, attempt)
    return attempt


def _less_than_one_point_five_times(top: float, runner_up: float) -> bool:
    """Compare finite CPU values exactly without rounded float multiplication."""
    top_numerator, top_denominator = top.as_integer_ratio()
    runner_numerator, runner_denominator = runner_up.as_integer_ratio()
    return (
        2 * top_numerator * runner_denominator
        < 3 * runner_numerator * top_denominator
    )


def _validate_process_fact(fact: ProcessFact) -> None:
    if type(fact) is not ProcessFact:
        raise ValueError("process rows must be ProcessFact values")
    if type(fact.pid) is not int or fact.pid <= 0:
        raise ValueError("process pid must be a positive int")
    if type(fact.name) is not str or not fact.name:
        raise ValueError("process name must be a non-empty string")
    _require_finite_nonnegative(fact.cpu_percent, "process cpu_percent")
    if fact.elapsed_seconds is not None:
        _require_nonnegative_int(fact.elapsed_seconds, "process elapsed_seconds")


def _validate_process_attempt(attempt: CollectionAttempt) -> None:
    if not isinstance(attempt, CollectionAttempt):
        raise ValueError("last_attempt must be a CollectionAttempt")
    if type(attempt.revision) is not int or attempt.revision <= 0:
        raise ValueError("process attempt revision must be a positive int")
    _require_finite_mono(attempt.started_mono_s, "attempt.started_mono_s")
    _require_finite_mono(attempt.completed_mono_s, "attempt.completed_mono_s")
    if attempt.completed_mono_s < attempt.started_mono_s:
        raise ValueError("process attempt completion cannot precede its start")
    if (
        type(attempt.completed_at_utc) is not datetime
        or attempt.completed_at_utc.tzinfo is not timezone.utc
    ):
        raise ValueError("process attempt completion timestamp must be timezone-aware")
    if type(attempt.outcome) is not AttemptOutcome:
        raise ValueError("process attempt outcome must be an AttemptOutcome")
    if attempt.failure is not None and type(attempt.failure) is not FailureCode:
        raise ValueError("process attempt failure must be a FailureCode or None")
    if attempt.outcome is AttemptOutcome.SUCCESS:
        if attempt.failure is not None:
            raise ValueError("successful process attempt cannot have a failure code")
    elif attempt.failure not in _LEGAL_FAILURES.get(attempt.outcome, set()):
        raise ValueError("process attempt failure code is incompatible with its outcome")


def _validated_process_success(
    source: SourceState[Tuple[ProcessFact, ...]], attempt: CollectionAttempt
) -> Sample[Tuple[ProcessFact, ...]]:
    sample = source.last_valid
    if type(sample) is not Sample or sample.revision != attempt.revision:
        raise ValueError("successful process attempt lacks its matching sample")
    _validate_process_sample(sample)
    if (
        sample.started_mono_s != attempt.started_mono_s
        or sample.completed_mono_s != attempt.completed_mono_s
        or sample.completed_at_utc != attempt.completed_at_utc
    ):
        raise ValueError("process sample does not match its completed attempt")
    return sample


def _validate_process_sample(sample: Sample[Tuple[ProcessFact, ...]]) -> None:
    if type(sample) is not Sample:
        raise ValueError("source last_valid must be a Sample")
    if type(sample.value) is not tuple:
        raise ValueError("process sample rows must be a tuple")
    _validate_process_table(sample.value, "process sample")
    if type(sample.revision) is not int or sample.revision <= 0:
        raise ValueError("process sample revision must be a positive int")
    _require_finite_mono(sample.started_mono_s, "sample.started_mono_s")
    _require_finite_mono(sample.completed_mono_s, "sample.completed_mono_s")
    if sample.completed_mono_s < sample.started_mono_s:
        raise ValueError("process sample completion cannot precede its start")
    if type(sample.completed_at_utc) is not datetime or sample.completed_at_utc.tzinfo is not timezone.utc:
        raise ValueError("process sample completion timestamp must be normalized UTC")


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
    if available_duration_s < duration_s:
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
        if sample_duration_s < remaining_duration_s:
            used_duration_s = sample_duration_s
        else:
            used_duration_s = min(sample_duration_s, remaining_duration_s)
        used_segments.append((usage.percent, used_duration_s))
        used_durations.append(used_duration_s)
        started_mono_s = usage.window_completed_mono_s - used_duration_s

    represented_duration_s = math.fsum(used_durations)
    if represented_duration_s < duration_s or started_mono_s is None:
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
        if represented_duration_s >= 15.0:
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
    if (type(attempt.completed_at_utc) is not datetime
            or attempt.completed_at_utc.tzinfo is not timezone.utc):
        raise ValueError("CPU attempt completion timestamp must be normalized UTC")
    if type(attempt.outcome) is not AttemptOutcome:
        raise ValueError("CPU attempt outcome must be an AttemptOutcome")
    if attempt.failure is not None and type(attempt.failure) is not FailureCode:
        raise ValueError("CPU attempt failure must be a FailureCode or None")
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
    if type(sample.completed_at_utc) is not datetime or sample.completed_at_utc.tzinfo is not timezone.utc:
        raise ValueError("CPU sample completion timestamp must be normalized UTC")


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
        if self.trigger.revision > self.last_processed_revision:
            raise ValueError("trigger revision cannot exceed the processed revision")
        if not _rule_matches(self.rule_id, self.severity):
            raise ValueError("rule_id is incompatible with latch severity")
        if self.trigger.source_id is not _rule_source_for(self.rule_id):
            raise ValueError("trigger source_id is incompatible with rule_id")
        if self.phase is LatchPhase.ACTIVE:
            if any((self.recovery_count != 0,
                    self.recovery_started_mono_s is not None,
                    self.recovery_identity is not None)):
                raise ValueError("active latch cannot carry a recovery streak")
            return
        if self.rule_id is RuleId.CPU_WATCH:
            if any((self.recovery_count != 0,
                    self.recovery_started_mono_s is not None,
                    self.recovery_identity is not None)):
                raise ValueError("CPU recovery carries no sample-streak metadata")
            return
        count_limit = _recovery_count_limit(self.rule_id)
        if not 1 <= self.recovery_count <= count_limit:
            raise ValueError("recovery_count is outside the rule's public bound")
        if self.recovery_started_mono_s is None or self.recovery_identity is None:
            raise ValueError("non-CPU recovery requires complete streak metadata")
        if self.recovery_started_mono_s < self.trigger.triggered_mono_s:
            raise ValueError("recovery cannot begin before its trigger")
        (last_recovery_mono_s, _identity, trailing_normal_count,
         trailing_normal_started_mono_s) = _decode_recovery_marker(
             self.rule_id, self.recovery_identity
         )
        if last_recovery_mono_s < self.recovery_started_mono_s:
            raise ValueError("recovery marker cannot precede recovery start")
        if last_recovery_mono_s < self.trigger.triggered_mono_s:
            raise ValueError("recovery marker cannot precede its trigger")
        if self.rule_id is RuleId.MEMORY_CRITICAL:
            if (self.recovery_count < 4
                    and trailing_normal_count > self.recovery_count):
                raise ValueError(
                    "trailing normal count exceeds observed noncritical evidence"
                )
            if trailing_normal_count > 0:
                if trailing_normal_started_mono_s is None:
                    raise ValueError("trailing normal evidence requires a start time")
                if (trailing_normal_started_mono_s
                        < self.recovery_started_mono_s
                        or trailing_normal_started_mono_s
                        > last_recovery_mono_s):
                    raise ValueError("trailing normal evidence has incoherent time")


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
    _validate_latch_now(latch, now_mono_s)
    if attempt is None:
        return latch
    duplicate, skipped = _check_attempt_revision(latch, attempt)
    if duplicate:
        if not is_decision_ready(source, now_mono_s):
            return _reset_recovery(latch, attempt.revision)
        return latch
    if skipped:
        latch = _discard_recovery(latch)
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
    _validate_latch_now(latch, now_mono_s)
    if attempt is None:
        return latch
    duplicate, skipped = _check_attempt_revision(latch, attempt)
    if duplicate:
        if not is_decision_ready(source, now_mono_s):
            return _reset_recovery(latch, attempt.revision)
        return latch
    if skipped:
        latch = _discard_recovery(latch)
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
    _validate_latch_now(latch, now_mono_s)
    if attempt is None:
        return latch
    duplicate, skipped = _check_attempt_revision(latch, attempt)
    if duplicate:
        if not is_decision_ready(source, now_mono_s):
            return _reset_recovery(latch, attempt.revision)
        return latch
    if skipped:
        latch = _discard_recovery(latch)
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
    _validate_latch_now(latch, now_mono_s)
    if not isinstance(window, CpuWindow):
        raise ValueError("window must be a CpuWindow")
    if attempt is None:
        return latch
    if window.last_attempt_revision != attempt.revision:
        raise ValueError("CPU window must include the latest source attempt")
    duplicate, skipped = _check_attempt_revision(latch, attempt)
    if duplicate:
        if not is_decision_ready(source, now_mono_s):
            return _reset_recovery(latch, attempt.revision)
        return latch
    if skipped:
        latch = _discard_recovery(latch)
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
    if sample.value.percent >= 90.0:
        # A reset or failed read may leave only a short high-CPU sample.  It
        # cannot establish a new sustained entry, but it also is not evidence
        # that use has eased.  Keep the existing watch without recovery copy.
        return RuleLatch(latch.rule_id, latch.severity, LatchPhase.ACTIVE,
                         latch.trigger, 0, None, attempt.revision, None)
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
        if now_mono_s < source.active_started_mono_s:
            raise ValueError("now_mono_s cannot precede the active attempt")
    attempt = source.last_attempt
    if attempt is None:
        if source.last_valid is not None:
            raise ValueError("source cannot retain a sample without an attempt")
        if source.active_revision is None and source.revision != 0:
            raise ValueError("inactive source cannot have an uncompleted revision")
        if source.active_revision is not None and source.revision != 1:
            raise ValueError("initial active source must have revision one")
        return None
    _validate_cpu_attempt(attempt)
    if now_mono_s < attempt.completed_mono_s:
        raise ValueError("now_mono_s cannot precede the completed attempt")
    if source.active_revision is None:
        if attempt.revision != source.revision:
            raise ValueError("completed attempt must match source revision")
    elif (attempt.revision != source.active_revision - 1
          or source.active_started_mono_s < attempt.completed_mono_s):
        raise ValueError("active source cannot skip or precede an attempt")
    if source.last_valid is not None:
        sample = source.last_valid
        _validate_rule_sample(sample, value_type)
        if sample.revision > attempt.revision:
            raise ValueError("retained sample cannot follow the latest attempt")
        if now_mono_s < sample.completed_mono_s:
            raise ValueError("now_mono_s cannot precede the retained sample")
        if attempt.outcome is not AttemptOutcome.SUCCESS:
            if sample.revision == attempt.revision:
                raise ValueError("failed attempt cannot own a valid sample")
            if sample.completed_mono_s > attempt.started_mono_s:
                raise ValueError("retained sample cannot complete after a later attempt starts")
            if sample.completed_at_utc > attempt.completed_at_utc:
                raise ValueError("retained sample wall time cannot follow a later attempt")
    if attempt.outcome is AttemptOutcome.SUCCESS:
        _rule_success(source, attempt, value_type)
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
    if type(sample.completed_at_utc) is not datetime or sample.completed_at_utc.tzinfo is not timezone.utc:
        raise ValueError("sample completion timestamp must be normalized UTC")


def _validate_latch_now(latch: RuleLatch, now_mono_s: float) -> None:
    if latch.trigger is not None and now_mono_s < latch.trigger.triggered_mono_s:
        raise ValueError("now_mono_s cannot precede the latch trigger")
    if (latch.recovery_started_mono_s is not None
            and now_mono_s < latch.recovery_started_mono_s):
        raise ValueError("now_mono_s cannot precede recovery start")
    if latch.recovery_identity is not None:
        last_recovery_mono_s, _, _, _ = _decode_recovery_marker(
            latch.rule_id, latch.recovery_identity
        )
        if now_mono_s < last_recovery_mono_s:
            raise ValueError("now_mono_s cannot precede latest recovery evidence")


def _check_attempt_revision(
    latch: RuleLatch, attempt: CollectionAttempt
) -> Tuple[bool, bool]:
    if latch.trigger is not None and attempt.completed_mono_s < latch.trigger.triggered_mono_s:
        raise ValueError("source completion cannot precede latch trigger")
    if attempt.revision == latch.last_processed_revision:
        return True, False
    if attempt.revision < latch.last_processed_revision:
        raise ValueError("source attempt revision cannot go backwards")
    return False, attempt.revision > latch.last_processed_revision + 1


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
    if (latch.phase is not LatchPhase.RECOVERING
            and latch.last_processed_revision == revision):
        return latch
    if latch.phase is LatchPhase.CLEAR:
        return _clear_latch(latch.rule_id, revision)
    return RuleLatch(latch.rule_id, latch.severity, LatchPhase.ACTIVE,
                     latch.trigger, 0, None, revision, None)


def _discard_recovery(latch: RuleLatch) -> RuleLatch:
    if latch.phase is not LatchPhase.RECOVERING:
        return latch
    return RuleLatch(latch.rule_id, latch.severity, LatchPhase.ACTIVE,
                     latch.trigger, 0, None,
                     latch.last_processed_revision, None)


def _memory_noncritical(
    latch: RuleLatch, sample: Sample[MemoryPressure], revision: int,
    level: MemoryPressureLevel,
) -> RuleLatch:
    completed_mono_s = sample.completed_mono_s
    continuing = latch.phase is LatchPhase.RECOVERING
    trailing_normal_count = 0
    trailing_normal_started_mono_s = None
    previous_mono_s = None
    if continuing:
        (previous_mono_s, _identity, trailing_normal_count,
         trailing_normal_started_mono_s) = _decode_recovery_marker(
             RuleId.MEMORY_CRITICAL, latch.recovery_identity
         )
        if completed_mono_s < previous_mono_s:
            raise ValueError("recovery sample time cannot go backwards")
        if completed_mono_s - previous_mono_s > 2.5:
            continuing = False

    if continuing:
        noncritical_count = latch.recovery_count + 1
        noncritical_started_mono_s = latch.recovery_started_mono_s
    else:
        noncritical_count = 1
        noncritical_started_mono_s = completed_mono_s
        trailing_normal_count = 0
        trailing_normal_started_mono_s = None

    if level is MemoryPressureLevel.NORMAL:
        if continuing and trailing_normal_count > 0:
            trailing_normal_count += 1
        else:
            trailing_normal_count = 1
            trailing_normal_started_mono_s = completed_mono_s
    else:
        trailing_normal_count = 0
        trailing_normal_started_mono_s = None

    if (noncritical_count >= 5
            and completed_mono_s - noncritical_started_mono_s >= 4.0):
        if level is MemoryPressureLevel.WARN:
            return _active_latch(
                RuleId.MEMORY_WATCH, HealthSeverity.WATCH, revision,
                latch.trigger,
            )
        if (trailing_normal_count >= 10
                and completed_mono_s - trailing_normal_started_mono_s >= 9.0):
            return _clear_latch(RuleId.MEMORY_WATCH, revision)
        watch_count = min(trailing_normal_count, 9)
        return RuleLatch(
            RuleId.MEMORY_WATCH,
            HealthSeverity.WATCH,
            LatchPhase.RECOVERING,
            latch.trigger,
            watch_count,
            trailing_normal_started_mono_s,
            revision,
            _recovery_marker(RuleId.MEMORY_WATCH, completed_mono_s),
        )

    critical_count = min(noncritical_count, 4)
    return RuleLatch(
        RuleId.MEMORY_CRITICAL,
        HealthSeverity.CRITICAL,
        LatchPhase.RECOVERING,
        latch.trigger,
        critical_count,
        noncritical_started_mono_s,
        revision,
        _recovery_marker(
            RuleId.MEMORY_CRITICAL,
            completed_mono_s,
            trailing_normal_count=min(trailing_normal_count, 9),
            trailing_normal_started_mono_s=trailing_normal_started_mono_s,
        ),
    )


def _advance_streak(
    latch: RuleLatch, revision: int, completed_mono_s: float, max_gap_s: float,
    required_count: int, required_span_s: float,
    identity: Optional[Tuple[str, str]],
) -> RuleLatch:
    previous_time = None
    previous_identity = None
    if latch.phase is LatchPhase.RECOVERING:
        previous_time, previous_identity, _, _ = _decode_recovery_marker(
            latch.rule_id, latch.recovery_identity
        )
    same_identity = previous_identity == identity
    if latch.phase is not LatchPhase.RECOVERING or not same_identity:
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
    marker = _recovery_marker(latch.rule_id, completed_mono_s, identity)
    if count >= required_count and completed_mono_s - started >= required_span_s:
        return _clear_latch(latch.rule_id, revision)
    count = min(count, required_count - 1)
    return RuleLatch(latch.rule_id, latch.severity, LatchPhase.RECOVERING,
                     latch.trigger, count, started, revision, marker)


def _recovery_count_limit(rule_id: RuleId) -> int:
    limits = {
        RuleId.MEMORY_CRITICAL: 4,
        RuleId.MEMORY_WATCH: 9,
        RuleId.THERMAL_WATCH: 2,
        RuleId.STORAGE_WATCH: 1,
    }
    try:
        return limits[rule_id]
    except KeyError:
        raise ValueError("rule has no sample-streak recovery metadata")


def _recovery_marker(
    rule_id: RuleId,
    last_mono_s: float,
    identity: Optional[Tuple[str, str]] = None,
    trailing_normal_count: int = 0,
    trailing_normal_started_mono_s: Optional[float] = None,
) -> str:
    _require_finite_mono(last_mono_s, "recovery marker timestamp")
    prefix = "v1|%s|" % rule_id.value
    encoded_last = float(last_mono_s).hex()
    if rule_id is RuleId.STORAGE_WATCH:
        if (not isinstance(identity, tuple) or len(identity) != 2
                or not all(isinstance(part, str) and part for part in identity)):
            raise ValueError("storage recovery requires a two-part identity")
        if trailing_normal_count != 0 or trailing_normal_started_mono_s is not None:
            raise ValueError("storage recovery cannot carry memory metadata")
        volume_id, mount_point = identity
        return "%s%d:%s%d:%s%s" % (
            prefix,
            len(volume_id),
            volume_id,
            len(mount_point),
            mount_point,
            encoded_last,
        )
    if identity is not None:
        raise ValueError("only storage recovery accepts an identity")
    if rule_id is RuleId.MEMORY_CRITICAL:
        if type(trailing_normal_count) is not int or not 0 <= trailing_normal_count <= 9:
            raise ValueError("trailing normal count is outside its bound")
        if trailing_normal_count == 0:
            if trailing_normal_started_mono_s is not None:
                raise ValueError("empty trailing normal state cannot have a start")
            encoded_start = "-"
        else:
            if trailing_normal_started_mono_s is None:
                raise ValueError("trailing normal state requires a start")
            _require_finite_mono(
                trailing_normal_started_mono_s,
                "trailing normal recovery start",
            )
            encoded_start = float(trailing_normal_started_mono_s).hex()
        return "%s%s|%d|%s" % (
            prefix, encoded_last, trailing_normal_count, encoded_start
        )
    if trailing_normal_count != 0 or trailing_normal_started_mono_s is not None:
        raise ValueError("rule cannot carry trailing normal metadata")
    if rule_id not in (RuleId.MEMORY_WATCH, RuleId.THERMAL_WATCH):
        raise ValueError("rule has no encoded recovery marker")
    return prefix + encoded_last


def _decode_recovery_marker(
    rule_id: RuleId, marker: Optional[str]
) -> Tuple[float, Optional[Tuple[str, str]], int, Optional[float]]:
    if not isinstance(marker, str):
        raise ValueError("recovery_identity is malformed")
    prefix = "v1|%s|" % rule_id.value
    if not marker.startswith(prefix):
        raise ValueError("recovery_identity has the wrong rule type")
    payload = marker[len(prefix):]
    if rule_id is RuleId.STORAGE_WATCH:
        volume_id, offset = _read_length_prefixed(payload, 0)
        mount_point, offset = _read_length_prefixed(payload, offset)
        if not volume_id or not mount_point:
            raise ValueError("storage recovery identity cannot be empty")
        last_mono_s = _decode_mono_hex(payload[offset:])
        return last_mono_s, (volume_id, mount_point), 0, None
    if rule_id is RuleId.MEMORY_CRITICAL:
        parts = payload.split("|")
        if len(parts) != 3:
            raise ValueError("critical memory recovery marker is malformed")
        last_mono_s = _decode_mono_hex(parts[0])
        if not parts[1].isdigit() or str(int(parts[1])) != parts[1]:
            raise ValueError("critical memory trailing count is malformed")
        trailing_normal_count = int(parts[1])
        if not 0 <= trailing_normal_count <= 9:
            raise ValueError("critical memory trailing count is outside its bound")
        if trailing_normal_count == 0:
            if parts[2] != "-":
                raise ValueError("empty trailing normal state has a start")
            trailing_normal_started_mono_s = None
        else:
            if parts[2] == "-":
                raise ValueError("trailing normal state lacks a start")
            trailing_normal_started_mono_s = _decode_mono_hex(parts[2])
            if trailing_normal_started_mono_s > last_mono_s:
                raise ValueError("trailing normal start follows latest evidence")
        return (
            last_mono_s,
            None,
            trailing_normal_count,
            trailing_normal_started_mono_s,
        )
    if rule_id not in (RuleId.MEMORY_WATCH, RuleId.THERMAL_WATCH):
        raise ValueError("rule has no encoded recovery marker")
    return _decode_mono_hex(payload), None, 0, None


def _read_length_prefixed(payload: str, offset: int) -> Tuple[str, int]:
    colon = payload.find(":", offset)
    if colon < 0:
        raise ValueError("storage recovery identity is malformed")
    encoded_length = payload[offset:colon]
    if (not encoded_length.isdigit()
            or str(int(encoded_length)) != encoded_length):
        raise ValueError("storage recovery identity length is malformed")
    length = int(encoded_length)
    start = colon + 1
    end = start + length
    if end > len(payload):
        raise ValueError("storage recovery identity is truncated")
    return payload[start:end], end


def _decode_mono_hex(encoded: str) -> float:
    try:
        value = float.fromhex(encoded)
    except ValueError:
        raise ValueError("recovery_identity has an invalid timestamp")
    _require_finite_mono(value, "recovery_identity timestamp")
    if float(value).hex() != encoded:
        raise ValueError("recovery_identity timestamp is not canonical")
    return value


def _storage_low(disk: DiskCapacity) -> bool:
    return disk.free_bytes * 100 < disk.total_bytes * 10 and disk.free_bytes < 20 * 1024 ** 3


def _storage_recovered(disk: DiskCapacity) -> bool:
    return disk.free_bytes * 100 >= disk.total_bytes * 12 or disk.free_bytes >= 25 * 1024 ** 3


def _volume_identity(disk: DiskCapacity) -> Tuple[str, str]:
    return disk.volume_id, disk.mount_point


class EvidenceRole(str, Enum):
    CURRENT = "current"
    TRIGGER = "trigger"


class EvidenceState(str, Enum):
    LIVE = "live"
    DEGRADED = "degraded"
    INCOMPLETE = "incomplete"
    DELAYED = "delayed"


@dataclass(frozen=True)
class EvidenceRef:
    source_id: SourceId
    revision: int
    role: EvidenceRole

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, SourceId):
            raise ValueError("evidence source_id must be a SourceId")
        if type(self.revision) is not int or self.revision <= 0:
            raise ValueError("evidence revision must be a positive int")
        if not isinstance(self.role, EvidenceRole):
            raise ValueError("evidence role must be an EvidenceRole")


@dataclass(frozen=True)
class HealthFacts:
    cpu: SourceState[CpuUsage]
    memory: SourceState[MemoryPressure]
    thermal: SourceState[OsThermalState]
    disk: SourceState[DiskCapacity]
    processes: SourceState[Tuple[ProcessFact, ...]]

    def __post_init__(self) -> None:
        expected = (
            (self.cpu, SourceId.CPU_USAGE),
            (self.memory, SourceId.MEMORY_PRESSURE),
            (self.thermal, SourceId.THERMAL_OS),
            (self.disk, SourceId.DISK_CAPACITY),
            (self.processes, SourceId.PROCESS_TABLE),
        )
        for source, source_id in expected:
            if not isinstance(source, SourceState) or source.source_id is not source_id:
                raise ValueError("HealthFacts field has the wrong SourceId")


@dataclass(frozen=True)
class PolicyState:
    cpu_window: CpuWindow
    cpu_latch: RuleLatch
    memory_latch: RuleLatch
    thermal_latch: RuleLatch
    storage_latch: RuleLatch
    process_history: ProcessHistory

    def __post_init__(self) -> None:
        if not isinstance(self.cpu_window, CpuWindow):
            raise ValueError("cpu_window must be a CpuWindow")
        _validate_latch_for(self.cpu_latch, (RuleId.CPU_WATCH,))
        _validate_latch_for(
            self.memory_latch,
            (RuleId.MEMORY_WATCH, RuleId.MEMORY_CRITICAL),
        )
        _validate_latch_for(self.thermal_latch, (RuleId.THERMAL_WATCH,))
        _validate_latch_for(self.storage_latch, (RuleId.STORAGE_WATCH,))
        if not isinstance(self.process_history, ProcessHistory):
            raise ValueError("process_history must be a ProcessHistory")


@dataclass(frozen=True)
class HealthAssessment:
    severity: HealthSeverity
    evidence_state: EvidenceState
    reason: str
    evidence: Tuple[EvidenceRef, ...]
    active_rules: Tuple[RuleId, ...]
    contributor_name: Optional[str]
    evidence_age_s: Optional[float]
    blockers: Tuple[SourceId, ...]
    cpu_coverage_s: float

    def __post_init__(self) -> None:
        if not isinstance(self.severity, HealthSeverity):
            raise ValueError("severity must be a HealthSeverity")
        if not isinstance(self.evidence_state, EvidenceState):
            raise ValueError("evidence_state must be an EvidenceState")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason must be a non-empty string")
        if type(self.evidence) is not tuple or any(
                not isinstance(ref, EvidenceRef) for ref in self.evidence):
            raise ValueError("evidence must be a tuple of EvidenceRef values")
        if type(self.active_rules) is not tuple or any(
                not isinstance(rule, RuleId) for rule in self.active_rules):
            raise ValueError("active_rules must be a tuple of RuleId values")
        if len(set(self.active_rules)) != len(self.active_rules):
            raise ValueError("active_rules cannot contain duplicates")
        if self.contributor_name is not None and (
                type(self.contributor_name) is not str or not self.contributor_name):
            raise ValueError("contributor_name must be a non-empty string or None")
        if self.contributor_name is not None and RuleId.CPU_WATCH not in self.active_rules:
            raise ValueError("a contributor is only valid during CPU pressure")
        if self.evidence_age_s is not None:
            _require_finite_nonnegative(self.evidence_age_s, "evidence_age_s")
        if type(self.blockers) is not tuple or any(
                not isinstance(source_id, SourceId) for source_id in self.blockers):
            raise ValueError("blockers must be a tuple of SourceId values")
        if len(set(self.blockers)) != len(self.blockers):
            raise ValueError("blockers cannot contain duplicates")
        _require_finite_nonnegative(self.cpu_coverage_s, "cpu_coverage_s")


def initial_policy_state() -> PolicyState:
    """Return the immutable policy baseline with no inferred health history."""
    return PolicyState(
        CpuWindow(),
        empty_latch(RuleId.CPU_WATCH),
        empty_latch(RuleId.MEMORY_WATCH),
        empty_latch(RuleId.THERMAL_WATCH),
        empty_latch(RuleId.STORAGE_WATCH),
        ProcessHistory(),
    )


def reset_policy_state() -> PolicyState:
    """Discard policy history after a lifecycle discontinuity."""
    return initial_policy_state()


def reduce_health(
    state: PolicyState, facts: HealthFacts, now_mono_s: float
) -> Tuple[PolicyState, HealthAssessment]:
    """Reduce independently collected facts into one conservative assessment."""
    if not isinstance(state, PolicyState):
        raise ValueError("state must be a PolicyState")
    if not isinstance(facts, HealthFacts):
        raise ValueError("facts must be HealthFacts")
    _require_finite_mono(now_mono_s, "now_mono_s")
    _validate_policy_facts(facts, now_mono_s)
    _validate_policy_state_consistency(state, facts, now_mono_s)

    cpu_window = reduce_cpu_window(state.cpu_window, facts.cpu)
    memory_latch = reduce_memory_latch(state.memory_latch, facts.memory, now_mono_s)
    thermal_latch = reduce_thermal_latch(state.thermal_latch, facts.thermal, now_mono_s)
    storage_latch = reduce_storage_latch(state.storage_latch, facts.disk, now_mono_s)
    cpu_latch = reduce_cpu_latch(state.cpu_latch, facts.cpu, cpu_window, now_mono_s)
    process_history = reduce_process_history(state.process_history, facts.processes)
    next_state = PolicyState(
        cpu_window, cpu_latch, memory_latch, thermal_latch, storage_latch,
        process_history,
    )
    if next_state == state:
        next_state = state
    return next_state, _health_assessment(next_state, facts, now_mono_s)


def _validate_policy_facts(facts: HealthFacts, now_mono_s: float) -> None:
    """Validate every source before policy uses any early-return path."""
    for source, source_id, value_type in (
        (facts.cpu, SourceId.CPU_USAGE, CpuUsage),
        (facts.memory, SourceId.MEMORY_PRESSURE, MemoryPressure),
        (facts.thermal, SourceId.THERMAL_OS, OsThermalState),
        (facts.disk, SourceId.DISK_CAPACITY, DiskCapacity),
    ):
        attempt = _rule_attempt(source, source_id, value_type, now_mono_s)
        if attempt is not None and source.last_valid is not None:
            # Dataclasses can be forged with replace; re-check their public facts.
            source.last_valid.value.__post_init__()
    _process_attempt(facts.processes, now_mono_s)


def _validate_policy_state_consistency(
    state: PolicyState, facts: HealthFacts, now_mono_s: float
) -> None:
    """Reject forged state that contradicts source evidence it consumed.

    A caller may retain policy state while an active source refresh advances,
    but a component which says it already processed a revision must agree with
    that revision's successful evidence.  This closes same-revision
    replacement attacks without rejecting legitimate newer source revisions.
    """
    _validate_cpu_state_consistency(state, facts.cpu, now_mono_s)
    _validate_latch_state_consistency(state.memory_latch, facts.memory,
                                      MemoryPressure, now_mono_s)
    _validate_latch_state_consistency(state.thermal_latch, facts.thermal,
                                      OsThermalState, now_mono_s)
    _validate_latch_state_consistency(state.storage_latch, facts.disk,
                                      DiskCapacity, now_mono_s)
    _validate_process_state_consistency(state.process_history, facts.processes,
                                        now_mono_s)


def _validate_cpu_state_consistency(
    state: PolicyState, source: SourceState[CpuUsage], now_mono_s: float
) -> None:
    attempt = _rule_attempt(source, SourceId.CPU_USAGE, CpuUsage, now_mono_s)
    consumed_revision = state.cpu_window.last_attempt_revision
    if attempt is None:
        if consumed_revision or state.cpu_latch.last_processed_revision:
            raise ValueError("CPU policy state cannot precede an absent attempt")
        return
    if consumed_revision > attempt.revision:
        raise ValueError("CPU window revision cannot exceed source evidence")
    if state.cpu_latch.last_processed_revision > attempt.revision:
        raise ValueError("CPU latch revision cannot exceed source evidence")
    if consumed_revision != attempt.revision:
        return
    if attempt.outcome is not AttemptOutcome.SUCCESS:
        if state.cpu_window.samples:
            raise ValueError("failed CPU attempt cannot retain a policy window")
        return
    sample = _rule_success(source, attempt, CpuUsage)
    if not state.cpu_window.samples or state.cpu_window.samples[-1] != sample:
        raise ValueError("CPU window must retain the matching latest sample")
    _validate_current_latch_value(state.cpu_latch, sample.value, state.cpu_window)


def _validate_latch_state_consistency(
    latch: RuleLatch, source: SourceState[T], value_type: type,
    now_mono_s: float,
) -> None:
    attempt = _rule_attempt(source, _rule_source_for(latch.rule_id), value_type,
                            now_mono_s)
    if attempt is None:
        if latch.last_processed_revision:
            raise ValueError("latch revision cannot precede an absent attempt")
        return
    if latch.last_processed_revision > attempt.revision:
        raise ValueError("latch revision cannot exceed source evidence")
    if latch.last_processed_revision != attempt.revision:
        return
    if attempt.outcome is not AttemptOutcome.SUCCESS:
        if latch.phase is LatchPhase.RECOVERING:
            raise ValueError("failed source cannot leave a recovery latch current")
        return
    _validate_current_latch_value(latch, _rule_success(source, attempt, value_type).value,
                                  None)


def _validate_current_latch_value(
    latch: RuleLatch, value: object, window: Optional[CpuWindow]
) -> None:
    """Check only conclusions that the current successful value excludes."""
    if latch.phase is LatchPhase.CLEAR:
        return
    if latch.rule_id is RuleId.MEMORY_CRITICAL:
        if latch.phase is LatchPhase.ACTIVE and value.level is not MemoryPressureLevel.CRITICAL:
            raise ValueError("critical memory latch contradicts current evidence")
        if latch.phase is LatchPhase.RECOVERING and value.level is MemoryPressureLevel.CRITICAL:
            raise ValueError("critical memory recovery contradicts current evidence")
    elif latch.rule_id is RuleId.MEMORY_WATCH:
        if latch.phase is LatchPhase.ACTIVE and value.level is not MemoryPressureLevel.WARN:
            raise ValueError("memory watch latch contradicts current evidence")
        if latch.phase is LatchPhase.RECOVERING and value.level is not MemoryPressureLevel.NORMAL:
            raise ValueError("memory recovery contradicts current evidence")
    elif latch.rule_id is RuleId.THERMAL_WATCH:
        active = value.thermal_warning_level > 0 or value.performance_warning_level > 0
        if latch.phase is LatchPhase.ACTIVE and not active:
            raise ValueError("thermal watch latch contradicts current evidence")
        if latch.phase is LatchPhase.RECOVERING and active:
            raise ValueError("thermal recovery contradicts current evidence")
    elif latch.rule_id is RuleId.STORAGE_WATCH:
        if latch.phase is LatchPhase.ACTIVE and _storage_recovered(value):
            raise ValueError("storage watch latch contradicts current evidence")
        if latch.phase is LatchPhase.RECOVERING and not _storage_recovered(value):
            raise ValueError("storage recovery contradicts current evidence")
    elif latch.rule_id is RuleId.CPU_WATCH and window is not None:
        summary = cpu_window_summary(window, 15.0)
        if latch.phase is LatchPhase.ACTIVE and summary is not None and summary.mean_percent < 90.0:
            raise ValueError("CPU watch latch contradicts current window")
        if latch.phase is LatchPhase.RECOVERING and value.percent >= 90.0:
            raise ValueError("CPU recovery contradicts current high usage")


def _validate_process_state_consistency(
    history: ProcessHistory, source: SourceState[Tuple[ProcessFact, ...]],
    now_mono_s: float,
) -> None:
    attempt = _process_attempt(source, now_mono_s)
    if attempt is None:
        if history.last_attempt_revision:
            raise ValueError("process history cannot precede an absent attempt")
        return
    if history.last_attempt_revision > attempt.revision:
        raise ValueError("process history revision cannot exceed source evidence")
    if history.last_attempt_revision == attempt.revision:
        if attempt.outcome is not AttemptOutcome.SUCCESS:
            if history.current_revision:
                raise ValueError("failed process attempt cannot retain current history")
        else:
            sample = _validated_process_success(source, attempt)
            if history.current_revision != attempt.revision or history.current != sample.value:
                raise ValueError("process history must match consumed source evidence")


def _health_assessment(
    state: PolicyState, facts: HealthFacts, now_mono_s: float
) -> HealthAssessment:
    coverage_s = _cpu_coverage_s(state.cpu_window)
    cpu_complete = cpu_window_summary(state.cpu_window, 15.0) is not None
    domains = (
        (SourceId.CPU_USAGE, facts.cpu, cpu_complete),
        (SourceId.MEMORY_PRESSURE, facts.memory, True),
        (SourceId.THERMAL_OS, facts.thermal, True),
        (SourceId.DISK_CAPACITY, facts.disk, True),
    )
    blockers = tuple(
        source_id for source_id, source, policy_ready in domains
        if not is_decision_ready(source, now_mono_s) or not policy_ready
    )
    latches = (
        state.memory_latch,
        state.thermal_latch,
        state.storage_latch,
        state.cpu_latch,
    )
    active = tuple(sorted((
        latch for latch in latches
        if latch.phase is not LatchPhase.CLEAR
        and _rule_source_ready(latch.rule_id, facts, now_mono_s)
    ), key=_rule_precedence))
    active_rules = tuple(latch.rule_id for latch in active)
    if active:
        evidence_state = EvidenceState.LIVE if not blockers else EvidenceState.DEGRADED
        reason = _latch_reason(active[0], facts)
        evidence = _latch_evidence(active, facts)
        severity = active[0].severity
        contributor_name = _contributor_name(
            state, facts, now_mono_s, RuleId.CPU_WATCH in active_rules
        )
    elif not blockers:
        evidence_state = EvidenceState.LIVE
        severity = HealthSeverity.NORMAL
        reason = "Fresh core evidence shows no active CPU, memory, thermal, or storage pressure."
        evidence = _normal_evidence(facts)
        contributor_name = None
    else:
        if (SourceId.CPU_USAGE in blockers
                and is_decision_ready(facts.cpu, now_mono_s)
                and not cpu_complete):
            evidence_state = EvidenceState.INCOMPLETE
        else:
            evidence_state = _blocked_evidence_state(facts, blockers)
        severity = HealthSeverity.UNKNOWN
        reason = _incomplete_reason(facts, blockers, cpu_complete, now_mono_s)
        evidence = ()
        contributor_name = None
    return HealthAssessment(
        severity, evidence_state, reason, evidence, active_rules, contributor_name,
        _current_evidence_age_s(evidence, facts, now_mono_s), blockers, coverage_s,
    )


def _rule_source_ready(rule_id: RuleId, facts: HealthFacts, now_mono_s: float) -> bool:
    source = _fact_source(facts, _rule_source_for(rule_id))
    return is_decision_ready(source, now_mono_s)


def _rule_precedence(latch: RuleLatch) -> int:
    return (
        RuleId.MEMORY_CRITICAL,
        RuleId.THERMAL_WATCH,
        RuleId.MEMORY_WATCH,
        RuleId.STORAGE_WATCH,
        RuleId.CPU_WATCH,
    ).index(latch.rule_id)


def _fact_source(facts: HealthFacts, source_id: SourceId) -> SourceState:
    return {
        SourceId.CPU_USAGE: facts.cpu,
        SourceId.MEMORY_PRESSURE: facts.memory,
        SourceId.THERMAL_OS: facts.thermal,
        SourceId.DISK_CAPACITY: facts.disk,
        SourceId.PROCESS_TABLE: facts.processes,
    }[source_id]


def _latch_reason(latch: RuleLatch, facts: HealthFacts) -> str:
    if latch.phase is LatchPhase.RECOVERING:
        if latch.rule_id is RuleId.MEMORY_CRITICAL:
            memory = facts.memory.last_valid.value
            if memory.level is MemoryPressureLevel.WARN:
                return "macOS memory pressure has eased from critical to elevated; recovery confirmation is in progress."
            return "macOS memory pressure is now normal; recovery confirmation is in progress."
        if latch.rule_id is RuleId.MEMORY_WATCH:
            return "macOS memory pressure is now normal; recovery confirmation is in progress."
        if latch.rule_id is RuleId.THERMAL_WATCH:
            return "macOS warnings are now clear; thermal recovery confirmation is in progress."
        if latch.rule_id is RuleId.CPU_WATCH:
            return "CPU use has eased; recovery confirmation is in progress."
        return "Storage headroom has improved; recovery confirmation is in progress."
    if latch.rule_id is RuleId.MEMORY_CRITICAL:
        return "macOS reports critical memory pressure."
    if latch.rule_id is RuleId.THERMAL_WATCH:
        return "macOS reports an active thermal or performance warning."
    if latch.rule_id is RuleId.MEMORY_WATCH:
        return "macOS reports elevated memory pressure."
    if latch.rule_id is RuleId.STORAGE_WATCH:
        disk = facts.disk.last_valid.value
        return "Storage headroom is low: %s remains on %s." % (
            _format_bytes(disk.free_bytes), disk.volume_name)
    return "CPU capacity has averaged at least 90% for 15 seconds."


def _format_bytes(value: int) -> str:
    return "%.1f GiB" % (value / float(1024 ** 3))


def _latch_evidence(
    latches: Tuple[RuleLatch, ...], facts: HealthFacts
) -> Tuple[EvidenceRef, ...]:
    refs = []
    for latch in latches:
        source = _fact_source(facts, _rule_source_for(latch.rule_id))
        refs.append(EvidenceRef(source.source_id, source.last_valid.revision,
                                EvidenceRole.CURRENT))
        if latch.phase is LatchPhase.RECOVERING:
            refs.append(EvidenceRef(latch.trigger.source_id, latch.trigger.revision,
                                    EvidenceRole.TRIGGER))
    return tuple(refs)


def _normal_evidence(facts: HealthFacts) -> Tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef(source.source_id, source.last_valid.revision, EvidenceRole.CURRENT)
        for source in (facts.cpu, facts.memory, facts.thermal, facts.disk)
    )


def _current_evidence_age_s(
    evidence: Tuple[EvidenceRef, ...], facts: HealthFacts, now_mono_s: float
) -> Optional[float]:
    ages = []
    for ref in evidence:
        if ref.role is EvidenceRole.CURRENT:
            source = _fact_source(facts, ref.source_id)
            ages.append(now_mono_s - source.last_valid.completed_mono_s)
    return max(ages) if ages else None


def _cpu_coverage_s(window: CpuWindow) -> float:
    return math.fsum(
        sample.value.window_completed_mono_s - sample.value.window_started_mono_s
        for sample in window.samples
    )


def _blocked_evidence_state(
    facts: HealthFacts, blockers: Tuple[SourceId, ...]
) -> EvidenceState:
    for source_id in blockers:
        source = _fact_source(facts, source_id)
        attempt = source.last_attempt
        if (source.last_valid is None or attempt is None
                or attempt.outcome in (AttemptOutcome.NOT_APPLICABLE,
                                       AttemptOutcome.UNSUPPORTED)):
            return EvidenceState.INCOMPLETE
    return EvidenceState.DELAYED


def _incomplete_reason(
    facts: HealthFacts, blockers: Tuple[SourceId, ...], cpu_complete: bool,
    now_mono_s: float,
) -> str:
    if (not cpu_complete and facts.cpu.last_valid is not None
            and is_decision_ready(facts.cpu, now_mono_s)
            and all(source_id is SourceId.CPU_USAGE for source_id in blockers)):
        return "CPU trend requires live monitoring."
    return "No recommendation is available from incomplete evidence."


def _contributor_name(
    state: PolicyState, facts: HealthFacts, now_mono_s: float, cpu_active: bool
) -> Optional[str]:
    if not cpu_active:
        return None
    contributor = select_cpu_contributor(state.process_history, facts.processes, now_mono_s)
    if contributor.state is ContributorState.LEADING:
        return contributor.name
    return None
