# PorkyHUD Trusted Decision Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Scope note (2026-10-08):** This is the completed Phase 1 execution contract and retains its fixed interfaces below. The subsequent user-authorized 0.2.0 artwork, descriptive insight, process-cache and read-only inventory extension is described in the companion design's subsequent-upgrade section and current `AGENTS.md` routes; it does not silently revise these historical tasks or rerun Task 15 measurement batches.

**Goal:** Build Phase 1 of PorkyHUD's trusted decision cockpit: typed evidence provenance, conservative health policy, independent collectors, a responsive six-mode terminal experience, truthful one-shot output, reproducible validation, and accurate maintainer documentation.

**Architecture:** Keep macOS effects, scheduling, curses, CLI, redaction, and the legacy snapshot projection in `porkyhud.py`. Add one dependency-free pure module, `porkyhud_core.py`, containing immutable source state, normalized policy facts, rolling latches, contributor selection, and deterministic assessment; effectful adapters translate native/command results into that boundary. Renderers consume an immutable `HealthFrame`, never run collection, and preserve the dependency direction documented in `AGENTS.md`.

**Tech Stack:** Python 3.9-3.14 standard library (`dataclasses`, `datetime`, `enum`, `typing`, `selectors`, `subprocess`, `threading`, `unittest`, curses/ctypes on macOS), zsh launcher, setuptools, GitHub Actions, Chrome/Chromium plus ffmpeg only for the optional illustrative demo generator.

## Global Constraints

- Runtime remains dependency-free beyond the Python standard library.
- Target architecture is Apple Silicon; Intel remains outside verified support.
- Keep `requires-python = ">=3.9"`; CI must run every minor from Python 3.9 through 3.14, and user docs must disclose Python 3.9's upstream end-of-life status.
- `porkyhud_core.py` may import only `dataclasses`, `datetime`, `enum`, `math`, and `typing`; it must not import `porkyhud`, curses, `ctypes`, `subprocess`, platform APIs, clocks, or perform I/O.
- The first six complete `SourceState` migrations are exactly `cpu.usage`, `memory.pressure`, `thermal.os`, `disk.capacity`, `process.table`, and `process.gpu`.
- The health policy is driven only by CPU usage, macOS memory pressure, macOS thermal/performance warnings, and selected-volume capacity. Optional sensors and descriptive metrics never affect severity.
- Only memory pressure value `4` can produce Phase 1 `CRITICAL`.
- A failed refresh retains the last valid value for display but immediately makes that source decision-ineligible.
- No failed, missing, stale, partial, or unrecognized result may appear as `0`, `nominal`, `locked`, or current evidence without proof.
- `LOCKED` and the `u` action belong only to `sensor.powermetrics`; other permission failures say `PERMISSION DENIED` and offer no unrelated action.
- A valid response that omits an optional field says `NOT EXPOSED`; it is distinct from source failure, unsupported hardware, and permission denial.
- Raw stderr, full command arguments, raw process commands, privileged details, and monotonic timestamps must not enter health facts, UI messages, JSON, logs, or future adapters.
- Known secret-bearing process arguments remain redacted at every human- and machine-readable output boundary; snapshots remain sanitized but not guaranteed private.
- Structured commands use no shell, force `LC_ALL=C`, bound stdout/stderr, and terminate and reap the whole child process group on timeout or output overflow.
- Pollers remain single-flight, retain last-good data, queue at most one forced follow-up, and retry failures after 1, 2, 4, then at most 10 seconds.
- Motion defaults off. Cinema never changes motion. Freshness and severity are always explicit text, never hue or animation alone.
- Minimum interactive size remains `76x22`; Cinema requires at least `118x34`.
- `compact` remains responsive policy only and is not selectable. Direct keys map `1` Overview, `2` Compute, `3` Thermals, `4` I/O, `5` Processes, and `6` Cinema.
- Messages say `selected`, never `saved`; `Home` and `End` remain supported and documented.
- Phase 1 does not version or promise stability for the existing snapshot schema.
- Automated validation must never install or remove sudoers rules.
- Documentation, the complete tracked-file map, stable `path::symbol` anchors, controls, package contents, and validation commands change in the same commit as the behavior they describe.

---

## File map

| Path | Action | Responsibility after Phase 1 |
| --- | --- | --- |
| `porkyhud_core.py` | Create | Pure source-state reducers, normalized facts, rolling rule state, contributor selection, evidence classification, and health assessment |
| `porkyhud.py` | Modify | Effects, structured command runner, adapters, source scheduling, live orchestration, snapshot projection, curses UI, controls, and CLI |
| `tests/test_core.py` | Create | Platform-independent source, policy, recovery, contributor, precedence, and evidence-state contracts |
| `tests/test_porkyhud.py` | Modify | Structured runner, parser, native adapter, poller, process/GPU, rendering, input, snapshot, terminal, and regression tests |
| `tests/test_repository_tools.py` | Create | Validator, renderer, measurement, path-safety, privacy, timeout-cleanup, and CLI contracts for source-tree tools |
| `tools/validate_docs.py` | Create | Mechanical tracked-map, stable-anchor, local-link, and control-contract validation using only the standard library |
| `tools/render_demo_gif.py` | Track and modify | Reproducible, sanitized, explicitly illustrative Overview demo; development-only Chrome/ffmpeg dependencies |
| `tools/measure_release.py` | Create | Bounded standard-library Task 15 measurement harness with private artifacts and redacted JSON results |
| `assets/porkyhud-demo.gif` | Modify | Updated illustrative product media, never runtime or hardware evidence |
| `.gitignore` | Modify | Keep local tools ignored except the three reviewed reproducible repository tools |
| `README.md` | Modify | User positioning, states, modes, support tiers, privacy limits, metric semantics, and current workflows |
| `AGENTS.md` | Modify | New boundary, routes, invariants, flows, tracked files, validation, and future adapter guidance |
| `PorkyHUD.command` | Modify | Short current launch/help guidance without duplicating the full key map |
| `pyproject.toml` | Modify | Ship both Python modules while preserving package metadata and Python floor |
| `.github/workflows/ci.yml` | Modify | Python 3.9-3.14 compile/test/launcher/wheel/import/docs validation matrix |
| `docs/superpowers/specs/2026-08-11-porkyhud-decision-cockpit-design.md` | Modify | Mark approved/implemented status and append measured validation evidence |
| `docs/superpowers/plans/2026-08-11-porkyhud-trusted-decision-cockpit.md` | Create | Test-first execution contract for this milestone |

## Interface ledger

The following names are fixed across tasks. Change one only by updating every later task and the approved design in the same reviewed edit.

```text
# porkyhud_core.py
new_source(source_id: SourceId, expected_cadence_s: float,
           stale_after_s: float) -> SourceState[T]
start_attempt(state: SourceState[T], started_mono_s: float)
    -> Tuple[SourceState[T], int]
complete_success(state: SourceState[T], revision: int, value: T,
                 completed_mono_s: float, completed_at_utc: datetime)
    -> SourceState[T]
complete_failure(state: SourceState[T], revision: int,
                 outcome: AttemptOutcome, failure: FailureCode,
                 completed_mono_s: float, completed_at_utc: datetime)
    -> SourceState[T]
source_view(state: SourceState[T], now_mono_s: float) -> SourceView
is_decision_ready(state: SourceState[T], now_mono_s: float) -> bool
initial_policy_state() -> PolicyState
reset_policy_state() -> PolicyState
reduce_health(policy_state: PolicyState, health_facts: HealthFacts,
              now_mono_s: float) -> Tuple[PolicyState, HealthAssessment]

# porkyhud.py
run_structured_command(args: Sequence[str], category: str, timeout_s: float,
                       stdout_limit: int, stderr_limit: int) -> CommandResult
collect_memory_pressure() -> AdapterResult[MemoryPressure]
collect_os_thermal_state() -> AdapterResult[OsThermalState]
collect_disk_capacity() -> AdapterResult[DiskCapacity]
collect_process_rows() -> AdapterResult[Tuple[ProcessRow, ...]]
collect_gpu_process_sample(sampler: GPUProcessSampler,
                           system_gpu_percent: Optional[float])
    -> AdapterResult[GpuProcessSample]
merge_gpu_process_rows(rows: Sequence[ProcessRow],
                       gpu_state: SourceState[GpuProcessSample],
                       now_mono_s: float) -> Tuple[ProcessRow, ...]
build_health_frame(policy_state: PolicyState,
                   states: Mapping[SourceId, SourceState[Any]],
                   now_mono_s: float) -> Tuple[PolicyState, HealthFrame]
```

---

### Task 1: Immutable source-state contract

**Files:**
- Create: `porkyhud_core.py`
- Create: `tests/test_core.py`
- Modify: `AGENTS.md`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: no project code
- Produces: `SourceId`, `AttemptOutcome`, `FailureCode`, `SourceViewStatus`, `Sample[T]`, `CollectionAttempt`, `SourceState[T]`, `SourceView`, `new_source`, `start_attempt`, `complete_success`, `complete_failure`, `sample_age_s`, `is_decision_ready`, and `source_view`

- [ ] **Step 1: Write the failing source-contract tests**

Create `SourceStateTests(unittest.TestCase)` with a UTC fixture and table-driven legal-pair coverage:

```python
UTC_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
LEGAL_FAILURES = {
    AttemptOutcome.NOT_APPLICABLE: {FailureCode.NOT_APPLICABLE},
    AttemptOutcome.UNSUPPORTED: {FailureCode.UNSUPPORTED_CAPABILITY},
    AttemptOutcome.PERMISSION_DENIED: {FailureCode.PERMISSION_DENIED},
    AttemptOutcome.MISSING_DEPENDENCY: {FailureCode.MISSING_EXECUTABLE},
    AttemptOutcome.TIMEOUT: {
        FailureCode.COMMAND_TIMEOUT, FailureCode.DEADLINE_EXCEEDED,
    },
    AttemptOutcome.COMMAND_ERROR: {
        FailureCode.NONZERO_EXIT, FailureCode.DECODE_ERROR,
        FailureCode.OUTPUT_LIMIT,
    },
    AttemptOutcome.PARSE_ERROR: {
        FailureCode.EMPTY_OUTPUT, FailureCode.PARSER_REJECTED,
    },
    AttemptOutcome.CANCELLED: {FailureCode.USER_CANCELLED},
    AttemptOutcome.UNKNOWN_ERROR: {FailureCode.UNEXPECTED_EXCEPTION},
}

def test_failure_retains_last_valid_but_blocks_decisions(self):
    state = new_source(SourceId.MEMORY_PRESSURE, 1.0, 2.5)
    state, revision = start_attempt(state, 10.0)
    state = complete_success(
        state, revision, "normal", 10.2, UTC_NOW,
    )
    state, revision = start_attempt(state, 11.0)
    failed = complete_failure(
        state, revision, AttemptOutcome.TIMEOUT,
        FailureCode.COMMAND_TIMEOUT, 12.0, UTC_NOW,
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
```

Also assert: finite positive cadence/stale values, `stale_after_s >= expected_cadence_s`, finite and ordered monotonic times, timezone-aware UTC normalization, immutable inputs, strictly increasing revisions, mismatched/duplicate completion rejection, `SUCCESS` with no failure only, every illegal outcome/failure pair rejection, exact fresh/stale boundary, `sensor.powermetrics` permission mapping to `LOCKED`, and other permission mapping to `PERMISSION_DENIED`.

Define the exact freshness boundary as `age <= stale_after_s` fresh and `age > stale_after_s` stale. `NOT_EXPOSED` is available to valid optional-field projection but is never inferred from a failed source attempt.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest tests.test_core.SourceStateTests -v`

Expected: import failure for missing `porkyhud_core`.

- [ ] **Step 3: Implement the source types and reducers**

Define every reserved source and outcome exactly:

```python
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
```

Use these frozen state shapes:

```python
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
```

Use these exact view statuses:

```python
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
class SourceView:
    source_id: SourceId
    status: SourceViewStatus
    age_s: Optional[float]
    refreshing: bool
    decision_ready: bool
    last_valid_revision: Optional[int]
    failure: Optional[FailureCode]
```

`start_attempt` returns `replace(state, revision=revision, active_revision=revision, active_started_mono_s=started_mono_s), revision`. Completion must validate the active revision before allocating `CollectionAttempt`; success replaces both `last_attempt` and `last_valid`, while failure replaces only `last_attempt`. Derive source views in the five-step precedence specified by the design; never store `CHECKING` or `STALE` as attempt outcomes.

In the same edit, add `porkyhud_core.py` and `tests/test_core.py` to the complete tracked-file map, change the AGENTS architecture from one production module to an effects module plus a pure policy module, set `py-modules = ["porkyhud", "porkyhud_core"]`, and compile both modules in CI. This keeps the new boundary mapped and shipped from its first commit.

- [ ] **Step 4: Run the source tests and complete core import audit**

Run:

```bash
python3 -m unittest tests.test_core.SourceStateTests -v
python3 -m pip wheel --no-deps --wheel-dir dist .
python3 - <<'PY'
import ast
from pathlib import Path
allowed = {"dataclasses", "datetime", "enum", "math", "typing", "__future__"}
tree = ast.parse(Path("porkyhud_core.py").read_text())
imports = {node.module.split(".")[0] for node in ast.walk(tree)
           if isinstance(node, ast.ImportFrom) and node.module}
imports |= {alias.name.split(".")[0] for node in ast.walk(tree)
            if isinstance(node, ast.Import) for alias in node.names}
assert imports <= allowed, imports - allowed
PY
```

Expected: all tests pass and the import audit emits no output.

- [ ] **Step 5: Commit the source contract**

```bash
git add porkyhud_core.py tests/test_core.py AGENTS.md pyproject.toml \
  .github/workflows/ci.yml
git commit -m "feat: add immutable telemetry source state"
```

---

### Task 2: Normalized facts and exact CPU windows

**Files:**
- Modify: `porkyhud_core.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- Consumes: `Sample`, `SourceState`, `AttemptOutcome`
- Produces: `MemoryPressureLevel`, `CpuUsage`, `MemoryPressure`, `OsThermalState`, `DiskCapacity`, `ProcessFact`, `CpuWindow`, `CpuWindowSummary`, `reduce_cpu_window`, and `cpu_window_summary`

- [ ] **Step 1: Add failing normalized-fact and CPU-window tests**

Create `CpuWindowTests` with helpers that complete exact CPU samples. Include this irregular-window assertion:

```python
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
```

Also test exact 90% and 70% means, a 2.5-second allowed boundary, a gap over 2.5 seconds, overlap, reversal, failed latest attempt, duplicate revision idempotence, counter-reset replacement, percentage bounds, finite times, disk byte invariants, and nonnegative thermal warning levels.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest tests.test_core.CpuWindowTests -v`

Expected: import errors for the normalized fact and CPU window names.

- [ ] **Step 3: Implement immutable normalized facts and CPU integration**

Use these exact value shapes:

```python
class MemoryPressureLevel(IntEnum):
    NORMAL = 1
    WARN = 2
    CRITICAL = 4

@dataclass(frozen=True)
class CpuUsage:
    percent: float
    window_started_mono_s: float
    window_completed_mono_s: float

@dataclass(frozen=True)
class MemoryPressure:
    level: MemoryPressureLevel

@dataclass(frozen=True)
class OsThermalState:
    thermal_warning_level: int
    performance_warning_level: int

@dataclass(frozen=True)
class DiskCapacity:
    volume_id: str
    volume_name: str
    mount_point: str
    total_bytes: int
    free_bytes: int

@dataclass(frozen=True)
class ProcessFact:
    pid: int
    name: str
    cpu_percent: float
    elapsed_seconds: Optional[int]

@dataclass(frozen=True)
class CpuWindow:
    samples: Tuple[Sample[CpuUsage], ...] = ()
    last_attempt_revision: int = 0

@dataclass(frozen=True)
class CpuWindowSummary:
    duration_s: float
    mean_percent: float
    started_mono_s: float
    completed_mono_s: float
```

`reduce_cpu_window` processes each completed attempt at most once, clears on a failed latest attempt, replaces on overlap/reversal/gap, and retains only the represented samples needed for a 15-second window plus its clipped boundary. `cpu_window_summary` must integrate `percent * represented_duration`; it must not average sample counts.

- [ ] **Step 4: Run focused and cumulative core tests**

Run:

```bash
python3 -m unittest tests.test_core.CpuWindowTests -v
python3 -m unittest tests.test_core -v
```

Expected: all core tests pass.

- [ ] **Step 5: Commit normalized facts**

```bash
git add porkyhud_core.py tests/test_core.py
git commit -m "feat: normalize health facts and CPU windows"
```

---

### Task 3: Exact warning and recovery latches

**Files:**
- Modify: `porkyhud_core.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- Consumes: normalized facts, `CpuWindow`, `SourceState`, `is_decision_ready`
- Produces: `HealthSeverity`, `LatchPhase`, `RuleId`, `TriggerEvidence`, `RuleLatch`, `empty_latch`, `reduce_memory_latch`, `reduce_thermal_latch`, `reduce_storage_latch`, and `reduce_cpu_latch`

- [ ] **Step 1: Write failing rule-entry and recovery matrices**

Create `RuleLatchTests`. Encode thresholds as named cases, not random values:

```python
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
            source = self.disk_source(total, free, revision=1, completed=30.0)
            latch = reduce_storage_latch(empty_latch(RuleId.STORAGE_WATCH),
                                         source, 30.0)
            self.assertEqual(latch.phase is not LatchPhase.CLEAR,
                             expected_active)

def test_only_memory_four_creates_critical_latch(self):
    for level, severity in ((1, None), (2, HealthSeverity.WATCH),
                            (4, HealthSeverity.CRITICAL)):
        with self.subTest(level=level):
            latch = reduce_memory_latch(
                empty_latch(RuleId.MEMORY_WATCH),
                self.memory_source(level, 1, 1.0), 1.0,
            )
            self.assertEqual(latch.severity, severity)
```

Add deterministic sequences for: ten normal memory samples spanning exactly 9 seconds; critical remaining critical until five non-4 samples span exactly 4 seconds; critical-to-elevated transition; three clear thermal samples spanning exactly 10 seconds; CPU 15-second entry and 10-second recovery; two storage recovery samples spanning exactly 30 seconds; recovery failure/stale/gap resets; storage volume identity change resets; and `RECOVERING` truth state.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest tests.test_core.RuleLatchTests -v`

Expected: missing latch imports.

- [ ] **Step 3: Implement reusable latch state and four reducers**

Use this state shape:

```python
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
```

Each reducer is idempotent by source attempt revision. A non-ready or stale source clears the recovery streak but retains an active trigger internally; later assessment code must still refuse to present that trigger as current. Implement the exact entry/recovery rules and gap budgets from the approved spec, without adding temperature, fan, watt, swap, throughput, or GPU rules.

- [ ] **Step 4: Run latch and cumulative tests**

Run:

```bash
python3 -m unittest tests.test_core.RuleLatchTests -v
python3 -m unittest tests.test_core -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit exact policy latches**

```bash
git add porkyhud_core.py tests/test_core.py
git commit -m "feat: add conservative health recovery policy"
```

---

### Task 4: Conservative CPU contributor selection

**Files:**
- Modify: `porkyhud_core.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- Consumes: `ProcessFact`, `SourceState[Tuple[ProcessFact, ...]]`
- Produces: `ContributorState`, `CpuContributor`, `ProcessHistory`, `reduce_process_history`, and `select_cpu_contributor`

- [ ] **Step 1: Write failing attribution tests**

Create `ContributorTests` and use two successful source revisions:

```python
def test_requires_stable_identity_and_clear_dominance(self):
    first = (ProcessFact(42, "Xcode", 30.0, 100),
             ProcessFact(7, "runner", 10.0, 50))
    second = (ProcessFact(42, "Xcode", 36.0, 102),
              ProcessFact(7, "runner", 20.0, 52))
    source = self.process_source(first, revision=1, completed=1.0)
    history = reduce_process_history(ProcessHistory(), source)
    source = self.process_source(second, revision=2, completed=3.0,
                                 previous=source)
    history = reduce_process_history(history, source)
    contributor = select_cpu_contributor(history, source, 3.0)
    self.assertEqual(contributor.state, ContributorState.LEADING)
    self.assertEqual(contributor.name, "Xcode")
```

Also assert no leader for exact sub-25%, exact 1.5 ratio acceptance, a ratio below 1.5, tie, PID reuse, renamed process, decreasing/missing elapsed time, nonconsecutive revisions, stale age over 6 seconds, latest failure, and empty valid tables.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_core.ContributorTests -v`

Expected: missing contributor imports.

- [ ] **Step 3: Implement two-sample history and selection**

```python
class ContributorState(str, Enum):
    LEADING = "leading"
    NO_CLEAR_LEADER = "no_clear_leader"
    UNAVAILABLE = "unavailable"

@dataclass(frozen=True)
class CpuContributor:
    state: ContributorState
    name: Optional[str] = None
    source_revision: Optional[int] = None

@dataclass(frozen=True)
class ProcessHistory:
    previous: Tuple[ProcessFact, ...] = ()
    current: Tuple[ProcessFact, ...] = ()
    previous_revision: int = 0
    current_revision: int = 0
    last_attempt_revision: int = 0
```

The core accepts only already-sanitized `ProcessFact.name`; it never accepts raw commands. The selected leader is independent of the user's display sort.

- [ ] **Step 4: Run contributor and all core tests**

Run: `python3 -m unittest tests.test_core -v`

Expected: all tests pass.

- [ ] **Step 5: Commit contributor policy**

```bash
git add porkyhud_core.py tests/test_core.py
git commit -m "feat: add evidence-based CPU attribution"
```

---

### Task 5: Whole-policy evidence and assessment reducer

**Files:**
- Modify: `porkyhud_core.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- Consumes: all Tasks 1-4 core interfaces
- Produces: `EvidenceRole`, `EvidenceRef`, `EvidenceState`, `PolicyState`, `HealthFacts`, `HealthAssessment`, `initial_policy_state`, `reset_policy_state`, and `reduce_health`

The reducer owns these exact headline reasons:

| State | Reason |
| --- | --- |
| Memory critical | `macOS reports critical memory pressure.` |
| Thermal watch | `macOS reports an active thermal or performance warning.` |
| Memory watch | `macOS reports elevated memory pressure.` |
| Storage watch | `Storage headroom is low: {free} remains on {volume}.` |
| CPU watch | `CPU capacity has averaged at least 90% for 15 seconds.` |
| Normal | `Fresh core evidence shows no active CPU, memory, thermal, or storage pressure.` |
| Unknown | `No recommendation is available from incomplete evidence.` |
| One-shot CPU incomplete | `CPU trend requires live monitoring.` |
| Memory recovering normal | `macOS memory pressure is now normal; recovery confirmation is in progress.` |
| Memory recovering elevated | `macOS memory pressure has eased from critical to elevated; recovery confirmation is in progress.` |
| Thermal recovering | `macOS warnings are now clear; thermal recovery confirmation is in progress.` |
| CPU recovering | `CPU use has eased; recovery confirmation is in progress.` |
| Storage recovering | `Storage headroom has improved; recovery confirmation is in progress.` |

- [ ] **Step 1: Write failing policy, precedence, and evidence-state tests**

Create `HealthPolicyTests`. Include exact precedence and normal gates:

```python
def test_critical_memory_owns_headline_and_keeps_other_rules(self):
    policy, assessment = reduce_health(
        initial_policy_state(),
        self.ready_facts(memory=4, thermal=1, cpu_percent=95.0,
                         cpu_duration=15.0, storage_low=True),
        20.0,
    )
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
    _, normal = reduce_health(initial_policy_state(),
                              self.ready_facts(cpu_duration=15.0), 20.0)
    self.assertEqual(normal.severity, HealthSeverity.NORMAL)
```

Build an overlap table over latest outcome x prior valid sample x freshness x decisive warning and assert exactly one of `LIVE`, `DEGRADED`, `INCOMPLETE`, or `DELAYED`. Add exact tests for every headline/recovery reason, active-rule order, current and trigger evidence roles, warning-with-missing-peer degradation, decisive-source failure invalidation, one-point snapshot unknown, maximum current evidence age excluding an old trigger, blockers, CPU baseline coverage, and contributor text only during CPU pressure.

Add module-level `make_ready_policy_case() -> Tuple[HealthFacts, float]` for the deterministic benchmark. It returns `now=20.0`, memory normal at revision 1/time 20, thermal clear at revision 1/time 20, healthy storage at revision 1/time 20, an empty successful process table at revision 1/time 20, and contiguous 1-second 10% CPU windows covering seconds 5 through 20.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_core.HealthPolicyTests -v`

Expected: missing policy imports.

- [ ] **Step 3: Implement the immutable policy and assessment**

```python
@dataclass(frozen=True)
class PolicyState:
    cpu_window: CpuWindow
    cpu_latch: RuleLatch
    memory_latch: RuleLatch
    thermal_latch: RuleLatch
    storage_latch: RuleLatch
    process_history: ProcessHistory

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

@dataclass(frozen=True)
class HealthFacts:
    cpu: SourceState[CpuUsage]
    memory: SourceState[MemoryPressure]
    thermal: SourceState[OsThermalState]
    disk: SourceState[DiskCapacity]
    processes: SourceState[Tuple[ProcessFact, ...]]

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
```

Validate each `HealthFacts` field's source ID. Use precedence `memory critical > thermal > memory watch > storage > CPU > normal > unknown`. `LIVE` requires all four core domains ready; a current warning with another blocked domain is `DEGRADED`; otherwise classify `INCOMPLETE` before `DELAYED` exactly as the spec defines. Use trigger evidence only during truthful recovery copy and never count its age as a current-evidence age.

- [ ] **Step 4: Run complete pure-core suite and microbenchmark**

Run:

```bash
python3 -m unittest tests.test_core -v
python3 - <<'PY'
from time import perf_counter
from tests.test_core import make_ready_policy_case
from porkyhud_core import initial_policy_state, reduce_health
facts, now = make_ready_policy_case()
state = initial_policy_state()
start = perf_counter()
for _ in range(10_000):
    state, result = reduce_health(state, facts, now)
elapsed_ms = (perf_counter() - start) * 1000
print(f"10k policy reductions: {elapsed_ms:.2f} ms")
assert elapsed_ms / 10_000 <= 1.0
PY
```

Expected: all tests pass; mean reduction is at most 1 ms. Preserve the measured value for Task 15.

- [ ] **Step 5: Commit the pure policy boundary**

```bash
git add porkyhud_core.py tests/test_core.py
git commit -m "feat: derive trusted health assessments"
```

---

### Task 6: Bounded structured command runner

**Files:**
- Modify: `porkyhud.py::run_command`
- Modify: `tests/test_porkyhud.py`

**Interfaces:**
- Consumes: `AttemptOutcome`, `FailureCode`
- Produces: `CommandResult` and `run_structured_command`

- [ ] **Step 1: Write failing structured-runner tests**

Add `StructuredCommandTests` using `sys.executable -c` commands. Cover success and environment:

```python
def test_success_is_bounded_classified_and_uses_c_locale(self):
    result = porkyhud.run_structured_command(
        [sys.executable, "-c",
         "import os; print(os.environ.get('LC_ALL')); print('ok')"],
        category="fixture", timeout_s=1.0,
        stdout_limit=1024, stderr_limit=1024,
    )
    self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
    self.assertEqual(result.stdout, "C\nok\n")
    self.assertEqual(result.stderr, "")
    self.assertEqual(result.returncode, 0)
    self.assertIsNone(result.failure)
```

Add tests for missing executable, nonzero exit, invalid UTF-8, exact stdout and stderr cap crossings, timeout, and a timed-out child that spawns a grandchild. The descendant test records the grandchild PID in a temporary file and asserts `os.kill(pid, 0)` raises `ProcessLookupError` after the runner returns.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_porkyhud.StructuredCommandTests -v`

Expected: `run_structured_command` is missing.

- [ ] **Step 3: Implement incremental bounded collection and process-group cleanup**

Add:

```python
@dataclass(frozen=True)
class CommandResult:
    category: str
    stdout: str
    stderr: str
    returncode: Optional[int]
    started_mono_s: float
    completed_mono_s: float
    completed_at_utc: datetime
    outcome: AttemptOutcome
    failure: Optional[FailureCode]
```

Start with `subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, start_new_session=True, env={**os.environ, "LC_ALL": "C"})`. Register both pipe file descriptors with `selectors.DefaultSelector`, read bounded byte chunks until EOF/deadline, and terminate the process group with `os.killpg(process.pid, signal.SIGTERM)` followed by a short reap window and `SIGKILL` fallback. Crossing either cap terminates/reaps and returns `COMMAND_ERROR + OUTPUT_LIMIT`; decoding uses strict UTF-8 and maps failure to `COMMAND_ERROR + DECODE_ERROR`. Never invoke a parser on a failed or truncated result.

- [ ] **Step 4: Run focused tests and existing regressions**

Run:

```bash
python3 -m unittest tests.test_porkyhud.StructuredCommandTests -v
python3 -m unittest tests.test_porkyhud -v
```

Expected: all tests pass; legacy `run_command` behavior remains unchanged for unmigrated descriptive collectors.

- [ ] **Step 5: Commit the structured boundary**

```bash
git add porkyhud.py tests/test_porkyhud.py
git commit -m "feat: classify bounded command outcomes"
```

---

### Task 7: Decision-driving macOS adapters and CPU provenance

**Files:**
- Modify: `porkyhud.py::MacCpuSampler`
- Modify: `porkyhud.py::sensor_info`
- Modify: `porkyhud.py::disk_info`
- Modify: `tests/test_porkyhud.py`

**Interfaces:**
- Consumes: `CommandResult`, core normalized facts
- Produces: `AdapterResult[T]`, `parse_memory_pressure_level`, `collect_memory_pressure`, `parse_os_thermal_state`, `collect_os_thermal_state`, `collect_disk_capacity`, and `MacCpuSampler.sample_usage`

- [ ] **Step 1: Write failing adapter fixture tests**

Add `DecisionAdapterTests`. Use exact matrices:

```python
def test_memory_pressure_accepts_only_public_levels(self):
    accepted = {"1\n": core.MemoryPressureLevel.NORMAL,
                "2": core.MemoryPressureLevel.WARN,
                "4\n": core.MemoryPressureLevel.CRITICAL}
    for raw, expected in accepted.items():
        with self.subTest(raw=raw):
            self.assertEqual(
                porkyhud.parse_memory_pressure_level(raw).level, expected)
    for raw in ("", "0", "3", "8", "normal", "1 warning"):
        with self.subTest(raw=raw):
            with self.assertRaises(ValueError):
                porkyhud.parse_memory_pressure_level(raw)

def test_thermal_requires_both_explicit_signals(self):
    raw = ("Note: No thermal warning level has been recorded\n"
           "Note: No performance warning level has been recorded\n")
    self.assertEqual(porkyhud.parse_os_thermal_state(raw),
                     core.OsThermalState(0, 0))
    for partial in ("Thermal Warning Level = 1\n",
                    "localized unknown output\n", ""):
        with self.subTest(raw=partial):
            with self.assertRaises(ValueError):
                porkyhud.parse_os_thermal_state(partial)
```

Cover numeric zero, either warning active, both active, negative levels, timeout, nonzero exit, empty success, and parser rejection. Mock filesystem calls to prove `/System/Volumes/Data` is selected only when existing and read-write, `/` is the read-write fallback, `os.statvfs(path).f_flag & os.ST_RDONLY` rejects read-only volumes, and identity/mount/name/bytes are preserved. Add a fake Mach counter/clock test proving `CpuUsage.window_*` uses the interval between counter reads and that resets/failures create typed failures rather than zeros.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_porkyhud.DecisionAdapterTests -v`

Expected: missing adapter types/functions.

- [ ] **Step 3: Implement typed adapter results and exact commands**

```python
@dataclass(frozen=True)
class AdapterResult(Generic[T]):
    value: Optional[T]
    started_mono_s: float
    completed_mono_s: float
    completed_at_utc: datetime
    outcome: AttemptOutcome
    failure: Optional[FailureCode]
```

Use `/usr/sbin/sysctl -n kern.memorystatus_vm_pressure_level` with 1 second and 8 KiB/8 KiB limits. Use `/usr/bin/pmset -g therm` with 1.5 seconds and 8 KiB/8 KiB limits. Parser exceptions become `PARSE_ERROR + EMPTY_OUTPUT|PARSER_REJECTED`; generic command failures retain their structured class unless a tested adapter contract proves a narrower outcome.

Change `MacCpuSampler.sample_usage() -> AdapterResult[CpuUsage]` and expose `last_per_core: Tuple[float, ...]` for display only. Reset its raw/time baseline after counter reversal or core-count change and return a typed parse/native failure for that attempt. Change `sensor_info(os_thermal: Optional[OsThermalState] = None) -> SensorInfo`; remove its independent `pmset` and private thermal sysctl probes, and render missing independent state as unavailable rather than nominal.

- [ ] **Step 4: Run adapter, parser, and full regression tests**

Run:

```bash
python3 -m unittest tests.test_porkyhud.DecisionAdapterTests -v
python3 -m unittest discover -v
```

Expected: all tests pass; the normal-pressure/high-RAM fixture never produces advice because advice is not in adapters.

- [ ] **Step 5: Commit the decision adapters**

```bash
git add porkyhud.py tests/test_porkyhud.py
git commit -m "feat: collect canonical macOS health evidence"
```

---

### Task 8: Independent process and AGX evidence

**Files:**
- Modify: `porkyhud.py::ProcessRow`
- Modify: `porkyhud.py::GPUProcessSampler`
- Modify: `porkyhud.py::collect_process_rows`
- Modify: `porkyhud.py::sort_process_rows`
- Modify: `porkyhud.py::draw_process_panel`
- Modify: `tests/test_porkyhud.py`

**Interfaces:**
- Consumes: `AdapterResult`, `SourceState`, `ProcessFact`
- Produces: `process_identity`, `parse_elapsed_seconds`, `parse_process_rows`, independent `collect_process_rows`, `GpuCounterSnapshot`, `GpuProcessSample`, `parse_agx_counters`, `GPUProcessSampler.update`, `collect_gpu_process_sample`, `merge_gpu_process_rows`, and `process_facts`

- [ ] **Step 1: Write failing process/AGX truth tests**

Add `ProcessEvidenceTests` with fixtures for two complete ioreg samples. Assert:

```python
def test_process_table_succeeds_without_agx(self):
    with mock.patch.object(porkyhud, "run_structured_command",
                           return_value=self.command_success(PS_FIXTURE)):
        result = porkyhud.collect_process_rows()
    self.assertEqual(result.outcome, core.AttemptOutcome.SUCCESS)
    self.assertTrue(result.value)
    self.assertTrue(all(row.gpu is None for row in result.value))

def test_gpu_merge_requires_fresh_complete_identity_match(self):
    rows = (porkyhud.ProcessRow(42, 80.0, 2.0, None,
                                "R", "00:10", "/Applications/Xcode.app/Xcode"),)
    gpu = porkyhud.GpuProcessSample({42: 37.5}, {42: "Xcode"},
                                    complete=True, baseline_ready=True)
    state = self.successful_gpu_state(gpu, completed=10.0)
    merged = porkyhud.merge_gpu_process_rows(rows, state, 10.5)
    self.assertEqual(merged[0].gpu, 37.5)
```

Add assertions for baseline-only `None`, valid complete nonmatch `0.0`, partial enumeration `None`, stale/latest failure `None`, PID reuse/identity mismatch `None`, parser failure `None`, valid match measured value, successful response with syntactic objects but no activity, and empty raw output rejected. Assert GPU sorting puts `None` last and the renderer prints `--` rather than `0.0`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_porkyhud.ProcessEvidenceTests -v`

Expected: old coupled process function or missing AGX types causes failure.

- [ ] **Step 3: Implement independent process and GPU collection**

Change `ProcessRow.gpu` to `Optional[float]` without storing raw commands in core. Add:

```python
@dataclass(frozen=True)
class GpuCounterSnapshot:
    counters_ns: Mapping[int, int]
    identities: Mapping[int, str]
    complete: bool

@dataclass(frozen=True)
class GpuProcessSample:
    percentages: Mapping[int, float]
    identities: Mapping[int, str]
    complete: bool
    baseline_ready: bool
```

Run `ps` and ioreg independently with 8 MiB stdout/64 KiB stderr limits. `process_identity` removes Unicode control/format characters, collapses whitespace, retains only basename or `.app` display name, and is used before constructing `ProcessFact`. Add `display_cell_width(text: str) -> int` and `truncate_display_cells(text: str, limit: int) -> str` using `unicodedata.combining` and `unicodedata.east_asian_width`; cap contributor names at 32 display cells before curses escaping. Test combining marks, double-width characters, control/format removal, `.app` extraction, and a 32-cell boundary. Require syntactically complete AGX objects before `complete=True`. Remove `gpu_sampler` and `system_gpu_percent` parameters from `collect_process_rows` and `process_rows`.

- [ ] **Step 4: Run focused, privacy, rendering, and snapshot regressions**

Run:

```bash
python3 -m unittest tests.test_porkyhud.ProcessEvidenceTests -v
python3 -m unittest tests.test_porkyhud.CommandPrivacyTests -v
python3 -m unittest tests.test_porkyhud.LayoutAndRenderingTests -v
python3 -m unittest tests.test_porkyhud.RuntimeProcessAndSnapshotTests -v
```

Expected: all pass and unavailable GPU remains `None` through rendering/serialization.

- [ ] **Step 5: Commit independent process evidence**

```bash
git add porkyhud.py tests/test_porkyhud.py
git commit -m "fix: separate process rows from GPU attribution"
```

---

### Task 9: Typed single-flight source polling and retry control

**Files:**
- Modify: `porkyhud.py::AsyncPoller`
- Modify: `tests/test_porkyhud.py`

**Interfaces:**
- Consumes: `AdapterResult[T]`, source reducers
- Produces: `SourcePoller[T]`

- [ ] **Step 1: Write failing poller lifecycle tests**

Add `SourcePollerTests` using `threading.Event` collectors. Cover single-flight and queued force:

```python
def test_force_while_running_queues_exactly_one_follow_up(self):
    collector = BlockingAdapterCollector()
    poller = porkyhud.SourcePoller(
        core.new_source(core.SourceId.MEMORY_PRESSURE, 1.0, 2.5),
        collector,
    )
    poller.tick(0.0, force=True)
    self.assertTrue(collector.started.wait(1.0))
    poller.tick(0.1, force=True)
    poller.tick(0.2, force=True)
    collector.release.set()
    self.assertTrue(collector.wait_for_calls(2, timeout=2.0))
    self.assertEqual(collector.calls, 2)
    poller.close()
```

Use a fake completion clock to assert failure due times at +1, +2, +4, +10, +10 seconds, success reset, unsupported/not-applicable automatic stop, forced recheck/reset, retained last-valid/latest failure, mismatched revision protection, `close()` join, and stable worker count over 1,000 ticks.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_porkyhud.SourcePollerTests -v`

Expected: `SourcePoller` is missing.

- [ ] **Step 3: Implement a typed poller beside legacy `AsyncPoller`**

```text
SourcePoller.__init__(initial_state: SourceState[T],
                      collector: Callable[[], AdapterResult[T]]) -> None
SourcePoller.tick(now_mono_s: float, force: bool = False) -> SourceState[T]
SourcePoller.snapshot() -> SourceState[T]
SourcePoller.close(timeout_s: float = 1.0) -> None
```

Protect state, worker, force flag, failure count, and next due time with one lock. Start at most one daemon worker. Completion uses adapter timestamps and the active source revision. `force=True` clears backoff/stopped state; during an active attempt it sets one boolean follow-up. Leave `AsyncPoller` for descriptive sources only.

- [ ] **Step 4: Run poller and full tests**

Run:

```bash
python3 -m unittest tests.test_porkyhud.SourcePollerTests -v
python3 -m unittest discover -v
```

Expected: all tests pass and no test leaves a worker alive.

- [ ] **Step 5: Commit typed scheduling**

```bash
git add porkyhud.py tests/test_porkyhud.py
git commit -m "feat: schedule typed telemetry sources"
```

---

### Task 10: Progressive live orchestration and independent cadences

**Files:**
- Modify: `porkyhud.py::memory_stats`
- Modify: `porkyhud.py::collect_runtime_stats`
- Modify: `porkyhud.py::sensor_info`
- Modify: `porkyhud.py::hud`
- Modify: `tests/test_porkyhud.py`

**Interfaces:**
- Consumes: six `SourcePoller`s, `reduce_health`, existing descriptive collectors/render data
- Produces: `HealthFrame`, `memory_detail`, `swap_stats`, `merge_memory_stats`, `build_health_frame`, `initial_health_frame`, and `LiveHealthCoordinator`

- [ ] **Step 1: Write failing startup, cadence, and frame tests**

Add `LiveOrchestrationTests`. Use a fake screen and patched collectors to assert first refresh order:

```python
def test_checking_shell_refreshes_before_any_collection(self):
    events = []
    screen = FakeScreen(80, 24, keys=[ord("q")], on_refresh=lambda: events.append("refresh"))
    def collector(name, value):
        def run():
            events.append(name)
            return value
        return run
    with self.patch_live_collectors(collector), \
         mock.patch.object(porkyhud, "draw_initial_shell",
                           side_effect=lambda *args: events.append("shell")):
        porkyhud.hud(screen)
    self.assertEqual(events[:2], ["shell", "refresh"])
```

Add a fake 60-second schedule that asserts command launch allowances: pressure 60, vm_stat 30, swap 4, network 60, process 24, thermal 12, battery plus battery ioreg 8, capacity 2, disk activity 12, AGX 12, powermetrics 4, retry reserve 12, total at most 240. Assert process-table frames can arrive before AGX, a failed source updates the assessment immediately, a monotonic reversal, a tick gap over 30 seconds, or a wall/monotonic delta disagreement over 5 seconds calls `reset_policy_state`, `r` forces each six typed source once, and no draw function invokes either command runner.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_porkyhud.LiveOrchestrationTests -v`

Expected: missing frame/coordinator interfaces and synchronous startup ordering failure.

- [ ] **Step 3: Split descriptive cadences and cache static memory size**

Implement:

```python
def memory_detail() -> Dict[str, float]:
    """Return vm_stat-derived detail using the cached physical byte size."""

def swap_stats() -> Tuple[float, float]:
    """Return swap used/total bytes from one bounded legacy probe."""

def merge_memory_stats(detail: Mapping[str, float],
                       swap: Tuple[float, float]) -> Dict[str, float]:
    result = dict(detail)
    result["swap_used"] = swap[0]
    result["swap_total"] = swap[1]
    return result
```

Cache `hw.memsize` once. Retain `memory_stats()` as the snapshot convenience composition. Give descriptive sources their exact design cadences; reuse the independent thermal state in `sensor_info`. A timed-out `powermetrics` attempt ends its absolute deadline rather than trying every fallback.

- [ ] **Step 4: Implement `HealthFrame` and live coordinator**

Construct typed pollers with this exact registry:

| Source | Cadence | Stale after |
| --- | ---: | ---: |
| `cpu.usage` | 1 s | 2.5 s |
| `memory.pressure` | 1 s | 2.5 s |
| `thermal.os` | 5 s | 12 s |
| `disk.capacity` | 30 s | 75 s |
| `process.table` | 2.5 s | 7.5 s |
| `process.gpu` | 5 s | 15 s |

Descriptive scheduling remains separate: memory detail 2 seconds, swap 15 seconds, network totals 1 second, battery 15 seconds, SMC/powermetrics 15 seconds, disk activity completion plus 4 seconds, and static discovery once in the background.

```python
@dataclass(frozen=True)
class HealthFrame:
    assessment: HealthAssessment
    source_views: Mapping[SourceId, SourceView]
    cpu_percent: Optional[float]
    memory_pressure: Optional[MemoryPressure]
    thermal: Optional[OsThermalState]
    disk_capacity: Optional[DiskCapacity]
    contributor: CpuContributor
```

```text
LiveHealthCoordinator.__init__() -> None
LiveHealthCoordinator.tick(now_mono_s: float, force: bool = False) -> HealthFrame
LiveHealthCoordinator.source_state(source_id: SourceId) -> SourceState[Any]
LiveHealthCoordinator.process_rows(now_mono_s: float) -> Tuple[ProcessRow, ...]
LiveHealthCoordinator.per_core() -> Tuple[float, ...]
LiveHealthCoordinator.set_system_gpu_percent(percent: Optional[float]) -> None
LiveHealthCoordinator.observe_time(now_mono_s: float, now_utc: datetime) -> bool
LiveHealthCoordinator.reset_policy() -> None
LiveHealthCoordinator.close() -> None
```

`build_health_frame` converts process rows to sanitized `ProcessFact`s, calls the pure reducer, and derives source views at the supplied time. `observe_time` retains the previous monotonic/UTC pair and conservatively resets latches/history on monotonic reversal, a gap over 30 seconds (sleep/wake), or an absolute wall-delta versus monotonic-delta difference over 5 seconds. `hud` supplies both clocks, must draw and refresh an `UNKNOWN/INCOMPLETE` initial shell before constructing the coordinator, static poller, CPU sampler, or any slow descriptive collector, and starts static discovery in the background. Close typed pollers in `hud`'s `finally` path.

- [ ] **Step 5: Run live-focused and full tests**

Run:

```bash
python3 -m unittest tests.test_porkyhud.LiveOrchestrationTests -v
python3 -m unittest discover -v
```

Expected: all pass, no command is launched by a draw call, and schedule total is at most 240/min.

- [ ] **Step 6: Commit progressive live orchestration**

```bash
git add porkyhud.py tests/test_porkyhud.py
git commit -m "feat: start a progressive health cockpit"
```

---

### Task 11: Truthful snapshot and text projections

**Files:**
- Modify: `porkyhud.py::system_read`
- Modify: `porkyhud.py::collect_snapshot`
- Modify: `porkyhud.py::print_text_snapshot`
- Modify: `tests/test_porkyhud.py`

**Interfaces:**
- Consumes: ephemeral core source states and `HealthAssessment`
- Produces: `source_state_from_result` and `legacy_read_projection`; removes all live/snapshot use of `system_read`

- [ ] **Step 1: Write failing snapshot truth regressions**

Extend `RuntimeProcessAndSnapshotTests`:

```python
def test_one_shot_without_immediate_warning_is_unknown(self):
    snapshot = self.collect_snapshot_with(
        memory_pressure=1, thermal=(0, 0), free_fraction=0.5,
        cpu_percent=5.0,
    )
    self.assertEqual(snapshot["read"], {
        "message": "read: CPU trend requires live monitoring.",
        "level": "unknown",
    })

def test_normal_pressure_never_advises_closing_apps_from_ram_detail(self):
    snapshot = self.collect_snapshot_with(
        memory_pressure=1, ram_used_percent=99.0,
        compressed_bytes=64 * 1024 ** 3,
        swap_used_bytes=32 * 1024 ** 3,
    )
    self.assertNotIn("close", snapshot["read"]["message"].lower())
    self.assertNotIn("quit", snapshot["read"]["message"].lower())
```

Add immediate memory/thermal/storage warnings, adapter failure never nominal, one CPU point never sustained/normal, unavailable GPU JSON `null`, existing top-level keys, JSON serializability, known secret redaction, hostname presence/privacy contract, and text snapshot level mapping.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_porkyhud.RuntimeProcessAndSnapshotTests -v`

Expected: old `system_read` produces unsafe or overconfident output.

- [ ] **Step 3: Build ephemeral sources and replace the heuristic**

```python
READ_LEVELS = {
    HealthSeverity.NORMAL: "good",
    HealthSeverity.WATCH: "watch",
    HealthSeverity.CRITICAL: "hot",
    HealthSeverity.UNKNOWN: "unknown",
}

def legacy_read_projection(assessment: HealthAssessment) -> Dict[str, str]:
    return {
        "message": f"read: {assessment.reason}",
        "level": READ_LEVELS[assessment.severity],
    }
```

Collect memory pressure, thermal, disk, CPU, process table, and AGX independently. Convert each `AdapterResult` into an ephemeral `SourceState`; pass one CPU point to a new policy state so it cannot complete 15 seconds. Preserve the unversioned payload shape except the intentional `gpu: null` accuracy change and the replaced `read` values. Delete or leave `system_read` unused only after `rg` proves no caller remains; prefer deletion once its tests are replaced.

- [ ] **Step 4: Run snapshot, privacy, and full tests**

Run:

```bash
python3 -m unittest tests.test_porkyhud.RuntimeProcessAndSnapshotTests -v
python3 -m unittest tests.test_porkyhud.CommandPrivacyTests -v
python3 -m unittest discover -v
```

Expected: all pass and no snapshot claims sustained CPU or global normal from one point.

- [ ] **Step 5: Commit truthful one-shot output**

```bash
git add porkyhud.py tests/test_porkyhud.py
git commit -m "fix: make one-shot health output evidence based"
```

---

### Task 12: Decision header, evidence ribbon, and ASCII semantics

**Files:**
- Modify: `porkyhud.py::draw_header`
- Modify: `porkyhud.py::read_attr`
- Modify: `porkyhud.py::draw_small_screen`
- Modify: `tests/test_porkyhud.py::LayoutAndRenderingTests`

**Interfaces:**
- Consumes: `HealthFrame`, `HealthAssessment`, `SourceView`
- Produces: `display_mode_name`, `semantic_text`, `format_evidence_age`, `compact_reason`, `decision_header_lines`, `evidence_ribbon_lines`, `draw_decision_header`, `draw_evidence_ribbon`, and `draw_initial_shell`

- [ ] **Step 1: Write exact string and minimum-viewport tests**

Add table-driven tests for `CHECKING`, normal, every watch/critical reason, recovery, incomplete, delayed, stale, locked, permission denied, missing, unsupported, not applicable, not exposed, and error. Include ASCII exactness:

```python
def test_ascii_projection_is_exact(self):
    text = "CPU averaged ≥90% for 15s. · 118×34 – evidence ≤2s"
    self.assertEqual(porkyhud.semantic_text(text, False),
                     "CPU averaged >=90% for 15s. | 118x34 - evidence <=2s")

def test_minimum_header_and_ribbon_show_every_cited_source(self):
    frame = make_health_frame_for_test(severity="normal")
    screen = FakeScreen(76, 22)
    next_y = porkyhud.draw_decision_header(screen, frame,
                                           porkyhud.HudConfig(), 20.0)
    next_y = porkyhud.draw_evidence_ribbon(screen, next_y, frame, 20.0)
    rendered = screen.text()
    self.assertEqual(next_y, 3)
    for token in ("CPU", "MEM", "THERM", "DISK"):
        self.assertIn(token, rendered)
```

Add module-level `make_health_frame_for_test(severity: str = "normal") -> HealthFrame`. Its default contains four `LIVE` source views at revision 1/time 19, a `NORMAL/LIVE` assessment at `now=20`, CPU 10%, normal memory pressure, clear thermal state, 50% free storage, no clear CPU leader, and evidence references for all four core sources. Named severity cases replace only the assessment/fact needed by their table row.

Assert the full first line shape `PorkyHUD · 1 OVERVIEW · WATCH · LIVE · evidence 2s` and these exact compact reasons below 90 columns:

| State | Compact reason |
| --- | --- |
| Normal | `No active CPU, memory, thermal, or storage pressure.` |
| Memory watch | `macOS memory pressure is elevated.` |
| Memory critical | `macOS memory pressure is critical.` |
| Thermal watch | `macOS thermal/performance warning is active.` |
| CPU watch | `CPU averaged ≥90% for 15s.` |
| Storage watch | `Storage low · {free} free on {volume}.` |
| Delayed/incomplete | `Health incomplete · {source} {state}.` |

Assert age formats `<1s`, `18s`, `2m 05s`; bootstrap `CHECKING n/4`; CPU baseline `CHECKING CPU baseline 7/15s`; one-shot never checking; `NO_COLOR` still includes severity/evidence words; ASCII output contains only code points below 128; and no line exceeds the screen width.

- [ ] **Step 2: Run rendering tests and verify RED**

Run: `python3 -m unittest tests.test_porkyhud.LayoutAndRenderingTests -v`

Expected: decision presentation functions are missing.

- [ ] **Step 3: Implement pure text construction before curses drawing**

```python
def semantic_text(text: str, unicode_enabled: bool) -> str:
    if unicode_enabled:
        return text
    replacements = ((" · ", " | "), ("≥", ">="), ("≤", "<="),
                    ("×", "x"), ("–", "-"), ("—", "-"))
    for before, after in replacements:
        text = text.replace(before, after)
    return text

```

```text
decision_header_lines(frame: HealthFrame, mode: str, width: int,
                      now_mono_s: float) -> Tuple[str, str]
evidence_ribbon_lines(frame: HealthFrame, width: int,
                      now_mono_s: float) -> Tuple[str, ...]
```

Keep text construction side-effect-free and benchmarkable. Use compact reasons below 90 columns; never silently truncate the meaning. Reserve rows 0-1 for the decision header and row 2 for one core ribbon at supported sizes. Move decorative branding into a Cinema body helper; it must not replace the decision header.

- [ ] **Step 4: Run rendering and microbenchmark tests**

Run:

```bash
python3 -m unittest tests.test_porkyhud.LayoutAndRenderingTests -v
python3 - <<'PY'
from time import perf_counter
from tests.test_porkyhud import make_health_frame_for_test
from porkyhud import decision_header_lines, evidence_ribbon_lines
frame = make_health_frame_for_test(severity="normal")
start = perf_counter()
for _ in range(10_000):
    decision_header_lines(frame, "balanced", 80, 20.0)
    evidence_ribbon_lines(frame, 80, 20.0)
elapsed_ms = (perf_counter() - start) * 1000
print(f"10k frame projections: {elapsed_ms:.2f} ms")
assert elapsed_ms / 10_000 <= 5.0
PY
```

Expected: tests pass and mean pure frame projection is at most 5 ms. Preserve the measured value for Task 15.

- [ ] **Step 5: Commit the decision presentation**

```bash
git add porkyhud.py tests/test_porkyhud.py
git commit -m "feat: render visible health evidence"
```

---

### Task 13: Responsive modes, operational composition, and controls

**Files:**
- Modify: `porkyhud.py::LAYOUTS`
- Modify: `porkyhud.py::HudConfig`
- Modify: `porkyhud.py::draw_help_overlay`
- Modify: `porkyhud.py::shortcut_status`
- Modify: `porkyhud.py::hud`
- Modify: `tests/test_porkyhud.py::LayoutAndRenderingTests`

**Interfaces:**
- Consumes: decision header/ribbon and existing panel renderers
- Produces: `DIRECT_MODE_KEYS`, `supported_modes`, `select_mode`, `cycle_mode`, `normalize_mode_for_viewport`, `draw_overview_body`, and `draw_mode_body`

- [ ] **Step 1: Write failing viewport and direct-control tests**

Test all required boundaries `76x22`, `80x24`, `117x33`, `118x34`, and `132x42`. Add:

```python
def test_cinema_rejects_narrow_view_without_mutation(self):
    config = porkyhud.HudConfig(layout_index=0)
    message = porkyhud.select_mode(config, "cinema", 33, 117)
    self.assertEqual(config.layout_name, "balanced")
    self.assertEqual(message,
                     "Cinema requires 118×34 · staying in Overview")

def test_cycle_excludes_compact_and_unsupported_cinema(self):
    config = porkyhud.HudConfig(layout_index=0)
    visited = []
    for _ in range(7):
        visited.append(config.layout_name)
        porkyhud.cycle_mode(config, 24, 80)
    self.assertEqual(set(visited),
                     {"balanced", "compute", "thermals", "io", "processes"})
    self.assertNotIn("compact", visited)
    self.assertNotIn("cinema", visited)
```

For keys 1-5 at narrow sizes, render sentinels and assert both the mode label and body signature change. Assert key 6 works wide, Cinema resize returns Overview with explicit copy, rows 0-2/body 3-20/footer 21 never overlap at `76x22`, unavailable GPU columns are suppressed, selecting Cinema preserves animation mode, `a` cycles off/calm/vivid/off, and `u` appears/acts only for proven advanced-sensor lock.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m unittest tests.test_porkyhud.LayoutAndRenderingTests -v`

Expected: direct mode and fixed composition tests fail.

- [ ] **Step 3: Implement mode selection, semantic colors, and default motion**

```python
LAYOUTS = ["balanced", "compute", "thermals", "io", "processes", "cinema"]
DIRECT_MODE_KEYS = {
    "1": "balanced", "2": "compute", "3": "thermals",
    "4": "io", "5": "processes", "6": "cinema",
}

@dataclass
class HudConfig:
    theme_index: int = 0
    animation_mode: int = 0
    layout_index: int = 0
```

Name the existing pair IDs by semantic role and map every theme through the same roles:

```python
PAIR_PRIMARY = 1
PAIR_INFO = 2
PAIR_NOMINAL = 3
PAIR_WATCH = 4
PAIR_CRITICAL = 5
PAIR_STRUCTURE = 6
PAIR_SECONDARY = 7
PAIR_BRAND = 8
```

Add tests that each severity uses both its visible word and the matching semantic role, that no operational mode renders more than one nonsemantic collection marker, and that an unchanged motion-off frame does not refresh again until input, resize, source/policy revision, or the next one-second age boundary.

`supported_modes` returns five operational modes below `118x34` and all six at/above it. `select_mode` mutates only on success and returns exact user copy. `normalize_mode_for_viewport` mutates Cinema to Overview only after a resize below the threshold and returns the explanatory message. `l` cycles the supported tuple.

- [ ] **Step 4: Implement deterministic body dispatch**

```text
draw_mode_body(
    screen: curses.window, mode: str, body_y: int, body_h: int, width: int,
    frame: HealthFrame, per_core: Sequence[float], static: Mapping[str, Any],
    mem: Mapping[str, float], battery: BatteryInfo, sensor: SensorInfo,
    disk: DiskInfo, activity: DiskActivity, net_down: float, net_up: float,
    history: MetricHistory, rows: Sequence[ProcessRow], scroll: int,
    sort_mode: str, phase: int,
) -> int
```

At `76x22`, pass `body_y=3`, `body_h=18`, and keep footer row 21 global. `draw_overview_body` adds a second compact evidence row, short sanitized process list, exceptional-state line, and history only when space permits. Specialist modes reuse existing panels in fixed rectangles with these exact priorities:

| Mode | Mandatory body rows | Drop first when constrained |
| --- | --- | --- |
| Compute | aggregate CPU plus 15-second history; memory used/compressed/swap; two top CPU rows | per-core history, GPU detail, extra process rows |
| Thermals | OS thermal/performance state; optional-source state badges; available fan summary; three mapped hottest sensors | power/residency, extra sensors, decorative gauges |
| I/O | selected-volume capacity; disk activity state/value; network rate/state; one capacity/history line | battery detail, extra history, decorative flow |
| Processes | sort header plus up to 13 process rows | GPU column when unavailable, then elapsed/state/PID before identity |

No specialist renderer derives health or launches collection. Cinema may render mascot/decorative content in its body only.

- [ ] **Step 5: Align help and input behavior**

Numeric keys call `select_mode`; `m` changes only the secondary table order; `Home` and `End` remain; `a` changes only motion; `r` clears source backoff; and `u` is guarded by advanced sensor lock. Use one global footer: `1–5 mode · h help · m sort · r refresh · q quit` at minimum width, with the direct `6` Cinema entry in wider help.

- [ ] **Step 6: Run rendering, interaction, and terminal regressions**

Run:

```bash
python3 -m unittest tests.test_porkyhud.LayoutAndRenderingTests -v
python3 -m unittest discover -v
```

Expected: all pass across every viewport, ASCII, no-color, and limited-color fixture.

- [ ] **Step 7: Commit the responsive cockpit**

```bash
git add porkyhud.py tests/test_porkyhud.py
git commit -m "feat: add responsive direct cockpit modes"
```

---

### Task 14: Repository validation, packaging, documentation, and demo

**Files:**
- Create: `tools/validate_docs.py`
- Create: `tools/measure_release.py`
- Create: `tests/test_repository_tools.py`
- Track and modify: `tools/render_demo_gif.py`
- Modify: `.gitignore`
- Modify: `assets/porkyhud-demo.gif`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `PorkyHUD.command`
- Modify: `porkyhud.py` module/argparse/help copy
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/superpowers/specs/2026-08-11-porkyhud-decision-cockpit-design.md`

**Interfaces:**
- Consumes: final Phase 1 behavior and symbols
- Produces: reproducible mechanical docs checks, a two-module wheel, current support/privacy/control contracts, and updated illustrative media

- [ ] **Step 1: Write and run the repository validator in failing mode**

First change `.gitignore` so only the reviewed repository tools are visible:

```gitignore
tools/*
!tools/validate_docs.py
!tools/render_demo_gif.py
!tools/measure_release.py
```

Then create a standard-library tool with this public CLI:

```text
tracked_files(root: Path) -> Tuple[str, ...]
mapped_files(agent_text: str) -> Tuple[str, ...]
symbol_anchors(markdown_text: str) -> Tuple[Tuple[str, str], ...]
validate_tracked_map(root: Path) -> List[str]
validate_symbol_anchors(root: Path) -> List[str]
validate_local_links(root: Path) -> List[str]
validate_control_contract(root: Path) -> List[str]
main(argv: Optional[Sequence[str]] = None) -> int
```

`tracked_files` calls `git ls-files --cached --others --exclude-standard` so a newly unignored intended source is caught before commit; `mapped_files` parses only the complete tracked-file table in `AGENTS.md`; symbol validation parses Python AST only for `path::qualname` anchors found in `AGENTS.md`, while every mapped path is checked for exact-case existence; local-link validation resolves every tracked Markdown inline/reference/HTML link outside fenced code while rejecting decoded traversal, untracked targets, symlink escapes, and case mismatches; control validation checks semantic key groups including `DIRECT_MODE_KEYS` without requiring duplicated full prose. The CLI emits sorted private diagnostics with success/failure/usage exits 0/1/2.

Run: `python3 tools/validate_docs.py`

Expected: nonzero with explicit missing map rows for the three newly unignored tool paths plus `tests/test_repository_tools.py` before AGENTS is updated; core, core tests, and this plan were already mapped in their creation commits.

- [ ] **Step 2: Review all reproducible tools and update packaging/CI**

Review the existing renderer for sanitization and update its generated view to the decision header, core evidence ribbon, Overview hierarchy, restrained graphite/cyan/green/amber/red semantics, motion-off still clarity, and the exact visible `ILLUSTRATIVE DEMO · SYNTHETIC DATA` badge in every frame. It remains a development tool and may use only fixed generic values. It must not import app internals, inspect the host/network, accept `--no-sandbox`, follow an output symlink, write outside the repository, or leave a timed-out subprocess group alive.

Add the bounded standard-library `tools/measure_release.py` harness consumed by Task 15. It supports environment, startup, snapshot, pure, live, soak, and timeout-cleanup batches, nearest-rank statistics, no-shell subprocess groups, private temporary artifacts, exact launch/thread lifecycle accounting, and sanitized machine-readable JSON. Public results may include only allowlisted model/chip/RAM/macOS/architecture/Python facts and aggregates—never hostname, user/home, raw argv, process text, PIDs, or PTY content. Task 14 validates this harness; Task 15 owns running it and recording host evidence.

Set `py-modules = ["porkyhud", "porkyhud_core"]`. Expand CI matrix to `3.9`, `3.10`, `3.11`, `3.12`, `3.13`, `3.14`; compile both modules, all three tools, and the repository-tool test module; run discovery and launcher validation; run `tools/validate_docs.py`; build a wheel; assert the wheel excludes tools/tests; install it into a temporary target; and import both production modules from outside the checkout.

- [ ] **Step 3: Update all user-facing contracts**

Rewrite README sections to cover:

- the dependency-free Apple-Silicon-focused decision cockpit promise
- `CHECKING`, `LIVE`, `DEGRADED`, `INCOMPLETE`, `DELAYED`, `STALE`, `LOCKED`, `PERMISSION DENIED`, `UNSUPPORTED`, `NOT APPLICABLE`, `NOT EXPOSED`, and `ERROR`
- exact health inputs and the fact that descriptive telemetry cannot drive severity
- direct keys 1-6, supported-mode cycling, motion off default, Cinema threshold, Home/End, and temporary advanced-sensor unlock
- Apple Silicon target, exact physically verified record, fixture-validated cases, best-effort scope, Intel unverified status, Python >=3.9, and Python 3.9 EOL warning
- unversioned snapshot status, hostname inclusion, known-flag redaction, and sanitized-but-not-guaranteed-private process commands
- reproducible demo command and its Chrome/Chromium plus ffmpeg development dependencies

Shorten module/launcher copy to `Press h for current shortcuts` instead of duplicating the table. Update argparse only where CLI meaning/support wording changed.

- [ ] **Step 4: Update the maintainer map and design record**

Add every tracked path to `AGENTS.md`; add `area:health-policy` and `area:structured-adapters`; update live/snapshot flows, task routes, invariants, validation, package contents, demo policy, and future MCP boundary. Set the design status exactly to `implementation complete; host validation pending` only after Task 14 code is green. Do not append Task 15 performance or physical-host claims in this task.

- [ ] **Step 5: Regenerate and visually inspect the demo**

Run:

```bash
python3 tools/render_demo_gif.py --frames 36 --fps 12
file assets/porkyhud-demo.gif
```

Expected: exactly 36 frames at 12 fps and 960 pixels wide, preferably no more than 1 MiB. Inspect the first, middle, and last frames; confirm visible decision hierarchy, no clipped content, no private host/process data, restrained semantic colors, and exact synthetic-only provenance in every frame.

- [ ] **Step 6: Run packaging and documentation checks**

Run:

```bash
python3 tools/validate_docs.py
python3 -m py_compile porkyhud.py porkyhud_core.py tools/validate_docs.py tools/render_demo_gif.py tools/measure_release.py tests/test_repository_tools.py
python3 -W error -m unittest discover -v
zsh -n PorkyHUD.command
python3 -m pip wheel --no-deps --wheel-dir dist .
```

Install the newest wheel into a fresh `mktemp -d` target and run `PYTHONPATH=<target> python3 -c 'import porkyhud, porkyhud_core'`. Expected: every command passes.

- [ ] **Step 7: Commit the complete contracts**

```bash
git add .gitignore .github/workflows/ci.yml AGENTS.md README.md \
  PorkyHUD.command pyproject.toml porkyhud.py assets/porkyhud-demo.gif \
  tests/test_repository_tools.py tools/validate_docs.py \
  tools/render_demo_gif.py tools/measure_release.py \
  docs/superpowers/specs/2026-08-11-porkyhud-decision-cockpit-design.md \
  docs/superpowers/plans/2026-08-11-porkyhud-trusted-decision-cockpit.md
git commit -m "docs: publish the trusted cockpit contract"
```

---

### Task 15: Performance evidence, expert review, and release-candidate verification

**Files:**
- Modify as defects require: `porkyhud_core.py`, `porkyhud.py`, tests, README, AGENTS, demo, spec
- Modify: `docs/superpowers/specs/2026-08-11-porkyhud-decision-cockpit-design.md` validation record

**Interfaces:**
- Consumes: all Phase 1 behavior and validation tools
- Produces: reviewed release-candidate commit with deterministic and host-specific evidence

- [ ] **Step 1: Run independent specification and code-quality reviews**

Dispatch one reviewer against the approved spec and one reviewer against architecture/maintainability. Require them to check: no fabricated evidence, exact thresholds/recovery, source independence, process privacy, structured-command cleanup, no draw-time effects, no redundant health logic, narrow/wide mode behavior, and docs/map accuracy. Fix every validated issue with a focused failing test first.

- [ ] **Step 2: Measure the two hard startup gates**

Across 20 independent launches on the current physically verified host, record model, macOS, Python, AC state, and p95 for:

- initial `CHECKING` shell: must be <=250 ms
- first CPU/memory/thermal point evidence: must be <=3 seconds

Use `80x24`, `NO_COLOR=1`, motion off, and a PTY harness that timestamps the first refresh containing each marker. A miss blocks completion and must be optimized before proceeding.

- [ ] **Step 3: Measure snapshot and pure projections**

Run 20 independent `./porkyhud.py --json` samples and record p95 wall time plus maximum RSS; target p95 <=3 seconds. Re-run the Task 5 10,000-policy and Task 12 10,000-frame commands and record p95/mean values; enforce <=1 ms and <=5 ms per operation respectively.

- [ ] **Step 4: Measure steady live resources and lifecycle**

After a 30-second warmup, sample parent plus descendants once per second for five minutes in Overview at `80x24`, `NO_COLOR=1`, motion off. Record average CPU (target <=2% of one core), p95 RSS (target <=64 MiB), and child launches (must be <=240/min). Run a 30-minute soak and assert live worker/thread count does not grow. Verify timeout descendants are absent.

- [ ] **Step 5: Perform manual terminal and visual acceptance**

On Terminal.app and one available alternate terminal or PTY, check `76x22`, `80x24`, `117x33`, `118x34`, `132x42`; all themes; forced palette; `NO_COLOR`; ASCII; 8/16/256 colors; loading/stale/incomplete/degraded/error/recovery; resize; q/Escape; interrupt/termination; and palette/signal restoration. Record measured forced-palette contrast: primary/secondary text >=4.5:1 and meaningful borders/indicators >=3:1, or fix/remove the failing theme. Explicitly list untested hardware classes. Do not run sensor setup/removal.

- [ ] **Step 6: Run the complete release-candidate command set**

```bash
python3 -m py_compile porkyhud.py porkyhud_core.py \
  tools/validate_docs.py tools/render_demo_gif.py tools/measure_release.py \
  tests/test_repository_tools.py
python3 -W error -m unittest discover -v
zsh -n PorkyHUD.command
python3 tools/validate_docs.py
./porkyhud.py --help
./porkyhud.py --version
./porkyhud.py --json | python3 -m json.tool >/dev/null
./porkyhud.py --snapshot >/dev/null
python3 -m pip wheel --no-deps --wheel-dir dist .
git diff --check
git status --short
```

Expected: all commands succeed; only intentional tracked changes are present before the final commit.

- [ ] **Step 7: Inspect the built wheel and final diff**

Confirm the wheel contains `porkyhud.py` and `porkyhud_core.py`, install/import it from a clean target, compare every tracked path with the AGENTS table, resolve every `path::symbol`, check every local Markdown link, search duplicate control/support/privacy claims, inspect demo frames, and review the diff for stale line numbers or aspirational claims written as current behavior.

- [ ] **Step 8: Record evidence and create the final release-candidate commit**

Append exact environment, commands, sample counts, percentiles, resource measurements, manual checks, misses, and untested scope to the design spec's validation record. Re-run Step 6 after that edit.

```bash
git add -A
git commit -m "feat: deliver the trusted PorkyHUD decision cockpit"
```

Do not tag, publish, push, update Homebrew, or mutate sudoers in this task.

---

## Plan self-review record

- Spec coverage: every Phase 1 scope item maps to Tasks 1-15; later public snapshot versioning, full specialist redesign, broad compatibility corpus, GUI, and MCP remain excluded.
- Truth coverage: legal outcomes, last-valid/last-attempt separation, stale/failure eligibility, public memory/thermal sources, CPU windows, exact latches, storage provenance, contributor proof, and AGX null/zero semantics all have deterministic tests before integration.
- UX coverage: progressive shell, decision hierarchy, cited evidence, five narrow operational modes, honest Cinema rejection, motion/accessibility, and control copy have explicit tests and manual gates.
- Performance coverage: draw-time purity and cadence are deterministic; launch, snapshot, live resource, child-count, thread-soak, and contrast measurements are host-specific release evidence rather than mocked claims.
- Maintainability coverage: the core/effects boundary, interface ledger, three reviewed tools, two-module wheel, CI matrix, exhaustive tracked map, symbol anchors, local links, and future-agent routes are verified mechanically.
- Type consistency: `HealthFacts` uses core `SourceState` values; adapters return `AdapterResult`; `SourcePoller` consumes those results; `HealthFrame` is the only new presentation input; unavailable process GPU is `Optional[float]` end-to-end.
- Safety coverage: no automated sudoers mutation, no shell runner, bounded process groups, redaction at output boundaries, no raw stderr or privileged detail projection, and no release/publish side effects.
