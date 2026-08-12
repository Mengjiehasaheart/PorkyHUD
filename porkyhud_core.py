"""Pure immutable source-state reducers for PorkyHUD telemetry evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
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


def _require_finite_positive(value: float, name: str) -> None:
    _require_finite_mono(value, name)
    if value <= 0:
        raise ValueError("%s must be finite and positive" % name)


def _require_finite_mono(value: float, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("%s must be a finite int or float" % name)


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
