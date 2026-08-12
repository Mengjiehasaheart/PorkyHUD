# PorkyHUD Trusted Decision Cockpit — Phase 1 Design

Status: release-candidate validated for deterministic, current-host performance, and raw-PTY scope; Terminal.app visual qualification unavailable

Date: 2026-08-11

## Decision

PorkyHUD remains a dependency-free, terminal-first macOS application. Phase 1 turns its default view into a trusted decision cockpit and introduces one pure health-policy boundary that future interfaces can reuse.

The product must quickly tell the user:

1. what PorkyHUD currently knows
2. whether the Mac has a supported pressure condition
3. which fresh evidence supports that conclusion
4. where to inspect the relevant detail

Phase 1 health and process-attribution fields must never fill an evidence gap with a plausible zero, `nominal`, `locked`, or confident advice. Unmigrated optional fields keep their existing clearly documented best-effort semantics until Phase 2 and cannot drive health. The current physically verified tier must render a responsive `CHECKING` shell within 250 ms p95 and available point evidence plus any immediate operating-system warning within three seconds p95. A global `NORMAL` conclusion appears only after the required 15-second CPU window and all other core evidence are ready.

This is the first vertical slice of a larger program. Snapshot versioning, full specialist-mode redesign, optional-sensor normalization, deeper scheduler optimization, and broad device qualification each receive a separate design and implementation cycle after this boundary is validated.

## Why this slice comes first

PorkyHUD already collects more telemetry than it can safely interpret. Current behavior collapses materially different states:

- a missing command, timeout, nonzero exit, parse failure, unsupported capability, and permission denial can all become an empty string
- a failed thermal probe can fall back to `nominal`
- unavailable process GPU attribution can appear as `0.0`
- retained last-good samples can support prose that says "right now"
- high RAM use or historical swap allocation can recommend closing applications even while macOS reports normal memory pressure
- narrow layouts can hide the evidence cited by the headline and make layout changes appear to do nothing

A more persuasive visual treatment over those ambiguities would reduce trust. A broad module rewrite would distribute uncertain semantics. Phase 1 therefore establishes explicit source state, conservative policy, and one useful Overview before extracting more of the monolith.

## Program decomposition

The expert panel rejected a single milestone that combined core contracts, every collector, snapshot schema migration, a full visual redesign, performance rearchitecture, and physical qualification. The program is split as follows:

1. **Phase 1 — trusted decision cockpit:** source state for decision inputs, conservative health policy, progressive startup, Overview hierarchy, direct mode behavior, truth regressions, and honest support claims.
2. **Phase 2 — public projection and specialist experience:** versioned/minimized snapshot schema, all optional-source states, complete Compute/Thermals/I/O/Processes redesign, and future-adapter privacy boundary.
3. **Phase 3 — runtime efficiency and compatibility qualification:** cadence/render/lifecycle optimization, extended PTY tests, complete capability fixtures, and physical-device evidence.
4. **Later adapters:** focused collector extraction, native/menu-bar design, then read-only MCP resources over the stabilized public projection.

Each phase requires its own approved spec, implementation plan, tests, documentation update, and commit. No later phase is silently pulled into Phase 1.

## Research basis

The truth model uses primary macOS sources rather than similarly named metrics:

- Apple’s [`memory_pressure` implementation](https://github.com/apple-oss-distributions/system_cmds/blob/408bba7453608006b89772db185defbac8fe2fd0/memory_pressure/memory_pressure.c#L380) reads `kern.memorystatus_vm_pressure_level` for the pressure state.
- Apple’s [libdispatch public constants](https://github.com/apple-oss-distributions/libdispatch/blob/2361ffb78a76f7ee488cd052eb0bc5c767118bf9/dispatch/source.h#L235-L257) define normal, warning, and critical as `0x01`, `0x02`, and `0x04`.
- Apple’s [`pmset` implementation](https://github.com/apple-oss-distributions/PowerManagement/blob/d415e45501842834a280930c3eed9186544a67f0/pmset/pmset.m#L4142-L4189) distinguishes a missing recorded warning, a successful numeric warning level, and an API error. Phase 1 preserves those distinctions.

GitNexus was studied as a navigation and provenance reference, not selected as a dependency. Its [multi-phase graph pipeline](https://github.com/abhigyanpatwari/GitNexus/blob/5f9648744cb47ac3f73ed565bc9dc19a9bb72e24/ARCHITECTURE.md#L72-L101), [explicit unknown-result guidance](https://github.com/abhigyanpatwari/GitNexus/blob/5f9648744cb47ac3f73ed565bc9dc19a9bb72e24/gitnexus/src/cli/ai-context.ts#L201-L240), and [cache/freshness guardrails](https://github.com/abhigyanpatwari/GitNexus/blob/5f9648744cb47ac3f73ed565bc9dc19a9bb72e24/GUARDRAILS.md#L26-L37) informed PorkyHUD’s stable area IDs, evidence provenance, explicit unknown/stale states, and structural documentation checks. Its persistent Node/native graph stack and generated wiki are unnecessary for PorkyHUD’s present scale; curated docs own intent while Python AST/git checks own mechanical freshness.

## Phase 1 scope

Phase 1 includes:

- one pure module, `porkyhud_core.py`, for source-state reduction and deterministic health policy
- structured command outcomes for command-backed health sources, the process table, and AGX availability
- atomic health sources for CPU usage, macOS memory pressure, macOS thermal warning, and disk capacity
- an independent process-table source used only for CPU contributor attribution
- explicit last-attempt versus last-valid semantics
- conservative, exact entry/recovery rules and evidence precedence
- replacement of the current global `system_read` heuristics in live and one-shot output
- progressive startup with a nonblocking `CHECKING` shell
- a minimum-size Overview with cited evidence visible
- direct keys `1`–`6`, responsive specialist selection, and no apparent no-op
- motion off by default and semantic color/accessibility checks
- targeted cadence separation needed to keep health sources independent
- deterministic unit, parser, rendering, interaction, snapshot-regression, and current-host smoke tests
- README, agent map, package metadata, help, launcher, demo, and validation updates required by the behavior

Phase 1 does not include:

- a native GUI, menu-bar app, browser UI, daemon, service, or MCP server
- a public or stable snapshot schema
- a three-file core package or broad collector/renderer extraction
- a rewrite to `asyncio`
- demand-driven collection by active view
- persistent preferences or history
- per-process memory, thermal, disk-capacity, or I/O blame
- global severity from raw temperature, fan RPM, watts, GPU use, IOPS, or throughput
- complete normalization of SMC, `powermetrics`, battery, UPS, and every optional capability state
- automatic sensor-access setup or removal during validation
- claiming hardware or macOS-version support without evidence

## Product contract

The user's scarce resource is attention. The primary output is a justified conclusion; raw metrics are supporting evidence.

The default user is a terminal-comfortable Mac developer, creator, or performance enthusiast investigating slowdown, heat, fan noise, battery drain, or a demanding workload. Phase 1 optimizes for operational clarity, not maximum simultaneous telemetry.

The product promise becomes:

> A dependency-free, Apple-Silicon-focused terminal health monitor that distinguishes facts, uncertainty, and unavailable evidence.

Success is not the number of panels, themes, animations, or private metrics. Success is a conservative answer, visible evidence, and an obvious path to relevant detail.

## Architecture

Phase 1 creates one pure module and keeps effects in the existing entry module:

```text
porkyhud.py
  subprocess, ctypes, curses, clocks, collectors, scheduling,
  CLI, rendering, redaction, and the legacy snapshot projection
        ↓
porkyhud_core.py
  enums, immutable source state, reducers, normalized policy facts,
  rolling policy state, and deterministic health assessment
```

`porkyhud_core.py` may import only standard-library `dataclasses`, `datetime`, `enum`, `math`, and `typing`. It must not import `porkyhud`, curses, `ctypes`, `subprocess`, platform APIs, or perform I/O.

Existing collector dataclasses remain in `porkyhud.py`. Root code converts eligible observations into small pure fact values. This establishes a real boundary without moving every model prematurely.

The internal live state may still contain raw process commands. The core receives only numeric facts and a sanitized contributor display name. No future GUI or MCP adapter may consume this internal state; Phase 2 will define a minimized, versioned public projection first.

### Phase 1 files

The implementation is expected to add:

- `porkyhud_core.py` — pure state and health policy
- `tests/test_core.py` — pure state and policy contract tests

It modifies the current executable, tests, package metadata, README, agent map, launcher/help copy, the Python 3.9–3.14 CI matrix, and the illustrative demo asset. No additional runtime dependency or generic framework is introduced.

## Source-state contract

An observation is not the same thing as a collection attempt. A failed refresh must not erase the last valid sample, and a retained sample must not hide the latest failure.

The pure model is equivalent to:

```python
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
    failure: FailureCode | None = None


@dataclass(frozen=True)
class SourceState(Generic[T]):
    source_id: SourceId
    expected_cadence_s: float
    stale_after_s: float
    revision: int = 0
    active_revision: int | None = None
    active_started_mono_s: float | None = None
    last_attempt: CollectionAttempt | None = None
    last_valid: Sample[T] | None = None
```

The reducers enforce these invariants:

- starting an attempt allocates `revision + 1`; revisions are never reused
- only a successfully parsed value creates a `Sample`
- a failed completion replaces `last_attempt` but never `last_valid`
- `last_valid.revision <= last_attempt.revision <= revision` whenever those values exist
- completion for a revision other than `active_revision` is rejected
- age is `now_mono_s - last_valid.completed_mono_s`
- `CHECKING` and `STALE` are derived view states, not attempt outcomes
- an in-flight refresh does not invalidate a still-fresh valid sample
- after a completed failed refresh, that source is decision-ineligible until a successful refresh; the retained value is display-only
- raw monotonic times never appear in portable output; RFC 3339 UTC is derived from `completed_at_utc` when an output needs wall time
- `expected_cadence_s` and `stale_after_s` are finite and positive, and stale-after is not shorter than cadence
- monotonic start/completion values are finite and completion is not earlier than start
- `completed_at_utc` is timezone-aware and normalized to UTC
- `SUCCESS` requires `failure is None`; every non-success outcome requires one compatible failure code
- `NOT_APPLICABLE → NOT_APPLICABLE`; `UNSUPPORTED → UNSUPPORTED_CAPABILITY`; `PERMISSION_DENIED → PERMISSION_DENIED`; `MISSING_DEPENDENCY → MISSING_EXECUTABLE`; `TIMEOUT → COMMAND_TIMEOUT|DEADLINE_EXCEEDED`; `COMMAND_ERROR → NONZERO_EXIT|DECODE_ERROR|OUTPUT_LIMIT`; `PARSE_ERROR → EMPTY_OUTPUT|PARSER_REJECTED`; `CANCELLED → USER_CANCELLED`; and `UNKNOWN_ERROR → UNEXPECTED_EXCEPTION` are the only legal non-success pairs
- duplicate completion, completion without an active revision, and invalid outcome/failure combinations raise `ValueError` without changing state

Fanless hardware and a batteryless desktop are `NOT_APPLICABLE` only after affirmative capability evidence. Empty output or a failed probe cannot establish non-applicability.

The source view is derived in this order:

1. no completed attempt and an active attempt is `CHECKING`
2. a latest completed non-success is its proven outcome (`NOT APPLICABLE`, `LOCKED`, `PERMISSION DENIED`, `MISSING`, `UNSUPPORTED`, or `ERROR`), with `checking` appended if a retry is active and with last-good age when available
3. a successful but expired last sample is `STALE`, with `checking` appended if a refresh is active
4. a fresh successful sample is `LIVE`, with a nonsemantic activity marker if a refresh is active
5. no attempt and no sample is `CHECKING`

An active refresh with a fresh successful sample remains `LIVE`; a small activity marker may show the refresh without changing evidence eligibility.

## Atomic source registry

Phase 1 health depends only on leaf sources. A combined runtime or sensor status cannot decide health.

| Source ID | Value | Cadence | Stale after | Phase 1 role |
| --- | --- | ---: | ---: | --- |
| `cpu.usage` | aggregate Mach busy percentage | 1 s | 2.5 s | core health |
| `memory.pressure` | macOS pressure level `1`, `2`, or `4` | 1 s | 2.5 s | core health |
| `thermal.os` | parsed thermal and performance warning levels | 5 s | 12 s | core health |
| `disk.capacity` | selected writable volume total/free bytes | 30 s | 75 s | core health |
| `process.table` | CPU/memory process rows without AGX dependency | 2.5 s | 7.5 s | CPU attribution only |
| `process.gpu` | PID-to-GPU attribution and explicit availability | 5 s | 15 s | optional attribution |
| `memory.stats` | RAM/compression/swap detail | 2 s; swap 15 s | 6 s | descriptive only |
| `network.totals` | interface counters | 1 s | 3 s | descriptive only |
| `disk.activity` | bytes and operations over sample window | completion + 4 s | 12 s | descriptive only |
| `battery` | internal battery state | 15 s | 45 s | descriptive only |
| `sensor.smc` | mapped fans and temperatures | 15 s | 45 s | descriptive only |
| `sensor.powermetrics` | power/residency metrics | 15 s when usable | 45 s | descriptive only |
| `system.static` | model, OS, topology, display identity | once in background | session | descriptive only |

The first six sources must use the complete `SourceState` contract in Phase 1. The other IDs reserve the intended decomposition and document that their values cannot drive health until a later migration gives them equally explicit outcomes.

`process.table` must succeed independently of `process.gpu`. A successful AGX response with no active rows is a valid empty map; unsupported, failed, or unparsed AGX evidence is unavailable. In the latter case, the UI shows GPU as unavailable rather than fabricating `0.0`. This is an accuracy correction to the unversioned snapshot as well as the TUI.

AGX merge is identity-safe: an invalid or stale `process.gpu` source produces `null`; a valid PID sample merges only when the process PID and normalized identity match the process-table row; and a valid complete enumeration with no matching activity produces `0.0` only when the parser's fixture-backed contract proves absence means zero. Partial enumeration never proves zero.

`cpu.usage` carries `CpuUsage(percent, window_started_mono_s, window_completed_mono_s)`. The window is the Mach counter delta between the two reads, not the collection-attempt duration. Policy integrates `percent × represented_duration`, clips a boundary segment to the requested interval, requires continuous coverage of at least 15.0 s for entry or 10.0 s for recovery, rejects overlap/reversal/reset, and resets the rolling window on a failed read or a gap over 2.5 s. A failed read advances neither coverage nor policy time.

## Structured command boundary

The current `porkyhud.py::run_command` remains temporarily for legacy descriptive collectors. Phase 1 adds a structured companion for command-backed health sources, the process table, and AGX attribution.

The structured result preserves:

- stdout for a designated parser
- stderr only inside the effects layer for bounded classification
- start/completion monotonic times and a timezone-aware UTC completion time
- exit code when available
- missing executable, timeout, nonzero exit, decoding error, and unexpected exception
- output-limit failure before an unbounded payload can exhaust memory
- a stable command category such as `memory-pressure`, not a user-facing full command string

The runner never uses a shell. Text parsers run with `LC_ALL=C`. A timeout must terminate and reap the whole child process group; it cannot leave a privileged sampler behind. The generic runner reports facts such as nonzero exit. An adapter may translate a result to permission denial or unsupported capability only from an explicit, tested exit/text contract.

Per-command output limits are explicit: 8 KiB each for memory pressure and thermal, 8 MiB stdout/64 KiB stderr for process and AGX adapters, and 1 MiB stdout/64 KiB stderr for other structured commands. The runner reads pipes incrementally into bounded buffers; crossing either cap terminates/reaps the process group and produces `COMMAND_ERROR + OUTPUT_LIMIT`. Parsers never consume truncated output. A future streaming process parser requires a separate completeness contract.

Raw stderr, full command arguments, raw/unredacted process commands, and privileged details never enter health facts, UI messages, JSON, logs, or future adapters. Contributor names are derived before entering the core: Unicode control/format characters are removed, whitespace is collapsed, only a basename or `.app` display name is retained, terminal width is measured safely, and the result is capped at 32 display cells before output escaping.

## State and data flow

```text
launch
  → initialize curses and render CHECKING shell
  → start independent source attempts
  → structured/native result and parser
  → complete SourceState revision
  → normalize eligible facts
  → reduce rolling PolicyState
  → build immutable HealthAssessment
  → render on state/input/resize or the 1 Hz age boundary
```

Static discovery begins after the shell renders. No slow collector or health derivation runs inside a draw function. Independent source revisions remain independent; a UI frame does not pretend that every value came from one instant.

Health is recomputed when a source completes, a policy window crosses a threshold, or a cited source crosses its stale boundary. Rendering consumes the latest immutable assessment and source views.

Failed core sources retry with per-source exponential backoff of 1, 2, 4, then at most 10 s, while respecting single-flight collection. A successful completion resets backoff. `r` clears the backoff and requests one immediate attempt; if an attempt is active, it queues exactly one follow-up. A proven `UNSUPPORTED` or `NOT_APPLICABLE` outcome does not auto-retry during the session; a capability-changing user action or `r` may recheck it.

Snapshot collection creates an ephemeral policy state. Immediate memory, thermal, or storage pressure can still produce a warning. One CPU point cannot establish a sustained CPU condition or a global normal conclusion.

The pure boundary uses immutable normalized values (`CpuUsage`, `MemoryPressure`, `OsThermalState`, `DiskCapacity`, and sanitized `CpuContributor`) rather than root collector dataclasses. `HealthFacts` pairs each value with its source ID and revision. `PolicyState` contains only CPU coverage plus explicit warning/recovery latches. The pure reducer takes `(policy_state, health_facts, now_mono_s)` and returns a new state plus:

```python
@dataclass(frozen=True)
class EvidenceRef:
    source_id: SourceId
    revision: int
    role: str  # "current" or "trigger"


@dataclass(frozen=True)
class HealthAssessment:
    severity: HealthSeverity
    evidence_state: EvidenceState
    reason: str
    evidence: tuple[EvidenceRef, ...]
    active_rules: tuple[str, ...]
    contributor_name: str | None = None
```

The core reads no clock implicitly. Trigger references may outlive the corresponding current sample only inside a recovery latch; output renders them as historical triggers, never current observations.

## Exact health policy

### Axes and eligibility

Health severity is exactly one of:

- `NORMAL`
- `WATCH`
- `CRITICAL`
- `UNKNOWN`

Evidence state is exactly one of, selected in this precedence order:

1. `LIVE` — all four core domains are decision-ready and fresh
2. `DEGRADED` — a fresh decisive `WATCH` or `CRITICAL` rule is reportable, but another core domain is not decision-ready
3. `INCOMPLETE` — no conclusion is possible and at least one blocking core domain has never supplied valid evidence, has a latest outcome of `NOT_APPLICABLE` or `UNSUPPORTED`, has an unrecognized/contradictory value, or has not completed required policy history
4. `DELAYED` — no conclusion is possible; no blocker satisfies `INCOMPLETE`; and every blocker previously supplied valid evidence that is now stale or whose latest retry failed with permission, missing dependency, timeout, command, parse, cancellation, or unknown error

These predicates are deliberately disjoint by precedence. A source that once worked but now explicitly reports unsupported is `INCOMPLETE`, not delayed. An overlap-table test enumerates every latest-outcome × last-valid × freshness × decisive-rule combination and asserts exactly one evidence state.

During bootstrap, the internal assessment is `UNKNOWN · INCOMPLETE`, while the UI displays `CHECKING · n/4 core sources`; `CHECKING` is a lifecycle label, not a fifth evidence state. Once all four sources have valid point samples but the CPU history is still building, it displays `CHECKING · CPU baseline 7/15s`. A supported warning may replace `CHECKING` immediately. A source failure replaces it with `UNKNOWN` immediately rather than waiting for the CPU baseline. Completed one-shot output never says `CHECKING`: without a sustained CPU window and without another warning it reports `UNKNOWN · INCOMPLETE · CPU trend requires live monitoring`.

A source is decision-ready only when:

- it has a last valid sample
- that sample is within its freshness budget
- its latest completed attempt succeeded
- its parser accepted a recognized value
- any rule-specific history window is complete

Optional advanced telemetry never changes `LIVE` to `DEGRADED`. A missing optional contributor changes the explanation only.

### Canonical inputs

- Memory pressure comes from `sysctl -n kern.memorystatus_vm_pressure_level`. Apple’s `memory_pressure` implementation reads this same sysctl, and the public dispatch constants define `1 = normal`, `2 = warn`, and `4 = critical`. Any other value is a parse error, not a new inferred state.
- Thermal state comes from `/usr/bin/pmset -g therm`. Each of the thermal and performance signals must parse as either its explicit "no warning level has been recorded" marker or an integer `... Warning Level = n`. Zero/no-record is clear; a positive recognized integer is active; a negative, missing half, localized, or malformed result is a parse error. Phase 1 treats any active OS warning as `WATCH`, never infers `CRITICAL` from undocumented platform-specific values, and does not use the private `machdep.xcpm.cpu_thermal_level` fallback for health.
- CPU use is the time-weighted mean of aggregate Mach busy percentage, normalized to `0..100` across logical workers.
- Storage capacity uses the selected writable APFS Data volume, falling back to the writable root volume only when no Data volume is discoverable. The selected path and provenance remain visible in I/O detail.

### Rule matrix

| Rule | Entry | Recovery | Exact headline reason |
| --- | --- | --- | --- |
| Memory `WATCH` | one valid pressure value `2` | ten consecutive `1` samples spanning at least 9 s | `macOS reports elevated memory pressure.` |
| Memory `CRITICAL` | one valid pressure value `4` | remain critical until five consecutive non-`4` samples span at least 4 s; then remain watch until the normal recovery above | `macOS reports critical memory pressure.` |
| Thermal `WATCH` | one valid sample with either warning signal active | three consecutive explicit-clear samples spanning at least 10 s | `macOS reports an active thermal or performance warning.` |
| CPU `WATCH` | a complete 15 s window, no observation gap over 2.5 s, time-weighted mean at least 90% | a complete 10 s window, no gap over 2.5 s, mean at most 70% | `CPU capacity has averaged at least 90% for 15 seconds.` |
| Storage `WATCH` | free space is both below 10% of total and below 20 GiB | two consecutive samples spanning at least 30 s where free is at least 12% or at least 25 GiB | `Storage headroom is low: {free} remains on {volume}.` |
| Global `NORMAL` | all four core domains decision-ready/fresh; all latches clear; CPU and storage watch false | any active rule or loss of a required domain removes normal immediately | `Fresh core evidence shows no active CPU, memory, thermal, or storage pressure.` |

Each warning latch records its trigger revision, trigger time, and trigger value. If current evidence no longer satisfies entry but recovery is not complete, the latch state is `RECOVERING` and uses truthful copy:

| Recovering latch | Exact reason |
| --- | --- |
| memory from critical/watch | `macOS memory pressure is now normal; recovery confirmation is in progress.` |
| memory critical with current value `2` | `macOS memory pressure has eased from critical to elevated; recovery confirmation is in progress.` |
| thermal | `macOS warnings are now clear; thermal recovery confirmation is in progress.` |
| CPU | `CPU use has eased; recovery confirmation is in progress.` |
| storage | `Storage headroom has improved; recovery confirmation is in progress.` |

The latched severity remains in force while recovering, but the assessment cites both the original trigger and current sample. The trigger is historical policy evidence, not represented as a current source sample.

Recovery streaks reset on any failed, unrecognized, or stale attempt; any inter-sample gap above the source stale-after budget; system sleep/wake; wall-clock discontinuity; or source-identity change. Memory consecutive samples allow at most 2.5 s between points, thermal at most 12 s, and storage at most 75 s. Storage recovery also requires the same volume identity and mount point for the whole streak. A forced refresh participates normally and cannot bridge a reset.

Memory swap allocation, RAM percentage, compression, temperature, fan speed, power, GPU use, disk transfer, IOPS, and network rate are descriptive. They never enter this matrix.

Phase 1 has no CPU-, thermal-, storage-, temperature-, watt-, fan-, throughput-, or IOPS-based `CRITICAL` rule. Only macOS memory pressure value `4` can establish global critical severity.

### Precedence and missing evidence

Precedence is:

1. memory `CRITICAL`
2. thermal `WATCH`
3. memory `WATCH`
4. storage `WATCH`
5. CPU `WATCH`
6. `NORMAL`
7. `UNKNOWN`

The highest active rule owns the headline. Every other active rule remains visible in the evidence list.

A fresh warning remains reportable when a different core domain is missing; it displays `WATCH|CRITICAL · DEGRADED` and appends `Other core evidence is incomplete.` Missing evidence can prevent `NORMAL`, but cannot suppress a supported warning. If the decisive source itself fails or becomes stale, its retained value is display-only and cannot keep the warning current.

When no rule establishes pressure and any core domain is not decision-ready, severity is `UNKNOWN`. It pairs with `DELAYED` or `INCOMPLETE` using the definitions above and displays `No recommendation is available from incomplete evidence.`

### Contributor attribution

Phase 1 attributes only sustained CPU pressure. It names a contributor when all conditions hold:

- `process.table` is valid and at most 6 s old
- the same PID and normalized executable/application name appear in two consecutive process samples
- parsed elapsed time is nondecreasing, preventing obvious PID reuse
- the highest recent `ps pcpu` is at least 25% of one logical CPU
- the highest value is at least 1.5 times the runner-up

The sanitized copy is `{name} is the leading recent CPU process.` A valid table with no eligible leader says `No single recent CPU process is clearly dominant.` An invalid, stale, or failed process source says `Process attribution is unavailable.`

Phase 1 never blames a process for memory pressure, thermal warnings, storage capacity, disk activity, or network activity.

The displayed contributor is derived independently of the user's process-table sort and appears in the CPU evidence region even if it is not among the visible sorted rows. `m` changes only secondary process-row order; it never changes health or contributor selection. The active sort label remains visible anywhere process rows appear.

### Age semantics

`evidence <age>` is the greatest age among the current latest-valid samples cited by the headline. A recovering latch also displays `triggered {duration} ago` separately so an old trigger cannot masquerade as fresh evidence. For `NORMAL`, all four core sources are cited. A rolling CPU window displays its window length separately; the headline age is based on the latest CPU completion, not the oldest point in the window.

Ages render as `<1s`, `18s`, or `2m 05s` and update at most once per second. Source-specific ages appear only for exceptional states such as `STALE · 18s` or `ERROR · last good 18s ago`.

When `unicode_ok()` is false, every semantic string uses a fixed ASCII projection: ` · ` becomes ` | `, `≥` becomes `>=`, `≤` becomes `<=`, Unicode bar/glyph patterns become their existing ASCII equivalents, and no state/reason depends on a non-ASCII character. Tests assert the exact ASCII header, compact reasons, evidence badges, and ages.

## Overview and navigation

The current internal identifier `balanced` remains the backing identifier for user-facing **Overview** during Phase 1. `compute`, `thermals`, `io`, `processes`, and `cinema` remain backing identifiers. `compact` remains only as responsive layout policy and is no longer selectable or included in layout cycling.

Every supported size reserves a two-line decision header:

```text
PorkyHUD · 1 OVERVIEW · WATCH · LIVE · evidence 2s
CPU capacity has averaged at least 90% for 15 seconds. · Xcode is leading.
```

At widths below 90 columns, the second line uses fixed compact variants so it never truncates the meaning:

| State | Compact reason |
| --- | --- |
| Normal | `No active CPU, memory, thermal, or storage pressure.` |
| Memory watch | `macOS memory pressure is elevated.` |
| Memory critical | `macOS memory pressure is critical.` |
| Thermal watch | `macOS thermal/performance warning is active.` |
| CPU watch | `CPU averaged ≥90% for 15s.` |
| Storage watch | `Storage low · {free} free on {volume}.` |
| Delayed/incomplete | `Health incomplete · {source} {state}.` |

At the minimum `76×22` viewport, Overview uses this hierarchy:

1. two-line decision header
2. two compact evidence rows covering CPU, memory pressure, thermal state, and storage capacity
3. a short process list prioritizing application/executable name and relevant resource
4. one exceptional-state line when needed
5. footer: `1–5 mode · h help · m sort · r refresh · q quit`

The evidence cited by the headline must be visible. PID, state, elapsed time, and full command move to Processes. Static hostname/build/topology do not dominate the operational view. Large mascot art and animated dividers are reserved for Cinema.

At minimum width, contributor identity appears in the CPU evidence/process region rather than the decision-reason line. The complete compact reason is never silently truncated; if the terminal is smaller than `76×22`, PorkyHUD uses the existing explicit resize screen and safe quit path.

Every operational mode keeps a one-row core evidence ribbon under the decision header. Specialist content may replace the Overview process/detail region, but it cannot hide CPU, memory-pressure, thermal, or storage state cited by the current assessment.

At larger sizes, Overview adds history and rows without changing the hierarchy. At `118×34` and above it may use columns, but it must not revert to an equal-weight metric wall.

### Direct modes

| Key | `76×22` through `117×33` | `118×34` and larger |
| --- | --- | --- |
| `1` | Overview | expanded Overview |
| `2` | existing CPU and memory panel renderers stacked in a fixed rectangle; unavailable GPU row suppressed | current Compute composition shifted below the decision header |
| `3` | existing thermal/sensor renderers stacked in a fixed rectangle plus OS state first | current Thermals composition shifted below the decision header |
| `4` | existing storage/network renderers stacked in a fixed rectangle | current I/O composition shifted below the decision header |
| `5` | existing process renderer expanded to the available body | current Processes composition shifted below the decision header |
| `6` | reject with `Cinema requires 118×34 · staying in {mode}` | Cinema composition |

At `76×22`, rows 0–1 are the decision header, row 2 is the evidence ribbon, rows 3–20 are the 18-row specialist body, and row 21 is the single global footer. The body's fixed priority is:

| Mode | Mandatory body rows | Dropped first when vertical space is constrained |
| --- | --- | --- |
| Compute | aggregate CPU + 15 s history, memory used/compressed/swap, two top CPU rows | per-core history, GPU detail, extra process rows |
| Thermals | OS thermal/performance state, optional-source state badges, available fan summary, three mapped hottest sensors | power/residency, extra sensors, decorative gauges |
| I/O | selected volume capacity, disk activity state/value, network rate/state, one capacity/history line | battery detail, extra history, decorative flow |
| Processes | sort header plus up to 13 process rows | GPU column when unavailable, elapsed/state/PID before process identity |

An unmigrated optional source may be shown only with its legacy best-effort label and may not be cited by the assessment. Where the legacy value cannot distinguish zero from unavailable, that field is suppressed rather than rendered in a focused composition.

Phase 1 does not invent new specialist visual components: it adds only the decision header/evidence ribbon, assigns deterministic rectangles to existing panel renderers, and suppresses ambiguous optional fields. Typography, panel content, and specialist information architecture beyond this reuse remain Phase 2.

Every successful selection immediately changes both the mode label and visible composition. `l` cycles only modes supported by the current viewport. It never includes `compact` as a separate stop. Resizing Cinema below `118×34` returns to Overview and explains why.

Messages say `{mode} selected`, never `saved`, because Phase 1 has no persistent preferences. Existing `Home` and `End` scrolling is retained and documented in README and in-app help.

## Visual and interaction system

The direction is **Signal-first Neon on Graphite**:

- graphite background and restrained structure
- cyan for navigation and neutral information
- green only for explicitly supported nominal evidence
- amber for watch
- red for critical
- magenta only for brand/nonsemantic accent
- white for primary values and conclusions

Every theme maps to the same semantic roles. Amber and red are never decorative. Bars show current magnitude/capacity; sparklines show change; text badges show state. Operational modes permit at most one optional nonsemantic collection-activity pulse.

Motion defaults off on every launch. `a` cycles `off → calm → vivid → off`. Cinema never changes motion automatically. Motion cannot carry severity, freshness, or selection; text age/state is the only freshness signal.

With motion off, the app renders only on state, input, resize, a policy boundary, or the once-per-second age change. Decorative comets, moving divider marks, pulse diamonds, and mascot motion do not compete with operational evidence.

Manual visual acceptance covers every theme under the forced palette:

- primary and secondary text must meet at least 4.5:1 contrast in the captured forced-palette review
- meaningful borders and non-text indicators must meet at least 3:1 in the same review
- severity always has a visible word label and applicable numeric evidence
- `NO_COLOR`, ASCII, 8/16-color, and limited-color modes remain understandable without hue or motion
- focus/selection and exceptional states remain distinguishable by text and shape

A built-in theme that fails the recorded forced-palette thresholds is fixed or removed before release. Under `NO_COLOR`, PorkyHUD cannot control the terminal's foreground/background contrast; acceptance therefore requires complete text labels, no hue-only meaning, and readable output in the documented Terminal.app reference profile while clearly assigning host-palette contrast to the terminal configuration.

Terminal palette control can vary by host, so contrast checks are recorded as manual evidence rather than claimed as universal compliance from unit tests.

## One-shot and legacy JSON behavior

Phase 1 does not declare the existing `--json` payload stable and does not add a schema version. Phase 2 owns that breaking interface decision.

Phase 1 does replace the unsafe `read` heuristic:

- immediate memory, thermal, and storage rules may produce a warning
- one CPU sample cannot produce sustained CPU pressure or global normal
- no supported warning plus an incomplete CPU window produces `UNKNOWN`, not reassuring prose
- existing `read.message` and `read.level` fields remain, with level mapping `NORMAL → good`, `WATCH → watch`, `CRITICAL → hot`, and `UNKNOWN → unknown`
- missing GPU attribution becomes JSON `null`, an intentional accuracy correction in the explicitly unversioned interface

The README must warn that snapshots include hostname and sanitized-but-not-guaranteed-private process commands. Phase 1 must not call them privacy-safe. Raw stderr and privileged details remain omitted, and known secret-bearing flags remain redacted.

## Error and exceptional-state language

User-facing terms are fixed:

| Condition | Text |
| --- | --- |
| initial/in-flight without a usable sample | `CHECKING` |
| retained value past freshness | `STALE · 18s` |
| proven advanced `sensor.powermetrics` denial | `LOCKED · u: temporary unlock` |
| proven permission denial for any other source | `PERMISSION DENIED` |
| confirmed unsupported capability | `UNSUPPORTED` |
| affirmatively not applicable | `NOT APPLICABLE` |
| source exposes no applicable field after a valid response | `NOT EXPOSED` |
| executable absent | `MISSING` |
| timeout/nonzero/parse/unknown failure | `ERROR · last good 18s ago` or `ERROR · no valid sample` |

`LOCKED` is used only for the advanced sensor path whose permission can actually be changed by `u`. Other proven permission failures say `PERMISSION DENIED` and offer no unrelated action. Fanless and no-internal-battery states require affirmative device evidence. A parser that receives unknown localized or drifted output returns an error rather than guessing.

## Compatibility and support claims

Phase 1 aligns public wording with current evidence:

- target architecture: Apple Silicon
- Python: every minor allowed by `requires-python`; the current `>=3.9` floor remains for this phase and CI covers 3.9 through 3.14
- physically verified tier: the exact current-host model/macOS/Python record captured during release validation
- fixture-validated, hardware-unverified tier: named Apple Silicon topologies and form factors represented by deterministic fixtures
- best-effort tier: earlier macOS versions and untested Apple Silicon devices
- outside verified support: Intel Macs until Intel-specific fixtures and physical evidence exist

The README must not say undifferentiated "macOS support." It must distinguish target, verified, fixture-validated, best-effort, and unsupported/unverified scope. A fixture can prevent a known regression; it cannot prove a physical model works.

The README must also disclose that Python 3.9 is upstream end-of-life: PorkyHUD compatibility testing does not restore upstream security maintenance, and users should prefer a supported Python release.

Phase 1 fixtures cover the boundaries it changes:

| Area | Required cases |
| --- | --- |
| memory pressure | `1`, `2`, `4`, unexpected integer, empty success, timeout, nonzero exit, and high RAM/compression/swap under `1` |
| OS thermal | two explicit no-warning markers, numeric zero, thermal active, performance active, both active, partial markers, malformed/localized output, timeout, and nonzero exit |
| CPU policy | exact threshold boundaries, irregular cadence, stale gap, sustained entry, recovery, counter reset/wrap, and missing workers |
| disk policy | exact byte/fraction boundaries, Data-volume selection, root fallback, read-only/missing volume, entry, and hysteresis |
| process attribution | stable identity, PID reuse, tie/near-tie, missing/stale rows; AGX complete-empty, partial enumeration, stale result, identity mismatch/PID reuse, parse failure, and valid matching activity |
| responsive UI | `76×22`, `80×24`, `117×33`, `118×34`, and `132×42`; every direct key; Cinema resize rejection |
| terminal | forced palette, `NO_COLOR`, 8/16/256 colors, ASCII, `TERM=dumb`, non-TTY, resize, signal, and restoration paths |

The broader M1–M5, fanless/one-fan/multi-fan, Ultra/multi-die, battery/desktop/UPS, partial SMC, `powermetrics`, sleep/wake, and multi-terminal qualification corpus belongs to Phase 3. Until then, those configurations remain explicitly hardware-unverified unless physical evidence already exists.

## Performance design and measurement

The measured pre-change reference on the current M5 Max host is:

- snapshot: 1.55–2.36 s and about 48–50 MiB maximum RSS
- runtime bundle: about 53–63 ms
- process collection with AGX attribution: about 150–250 ms
- disk activity sample: about 1.01 s
- cold SMC discovery/read: about 0.71–1.78 s; warm repeats about 0.13–0.22 s
- calm live mode: short 1–3% CPU bursts and about 33–50 MiB resident

Phase 1 has hard structural gates:

- the first `CHECKING` shell renders before static discovery or slow collection
- no command, native sensor read, or health derivation occurs inside a draw function
- no subprocess starts merely because a frame redraws
- collectors remain single-flight and retain last valid samples
- timed-out structured commands are terminated and reaped
- process-table collection does not wait for AGX attribution
- motion defaults off
- live thread count does not grow over a 30-minute soak

Measured targets on the reference host are:

| Surface | Target |
| --- | --- |
| initial `CHECKING` shell | release gate: p95 ≤250 ms across 20 launches on the physically verified tier |
| first CPU/memory/thermal evidence | release gate: p95 ≤3 s across 20 launches on the physically verified tier |
| pure policy reduction | p95 ≤1 ms across 10,000 deterministic reductions |
| pure frame construction | p95 ≤5 ms across 10,000 fake frames |
| snapshot | p95 ≤3 s across 20 runs; no claimed hard wall-clock bound |
| motion-off live CPU | self plus descendants average ≤2% of one core over 5 min |
| live RSS | p95 ≤64 MiB over the same 5 min |
| steady child launches | ≤240/min after the startup minute |
| concurrency | no live-worker growth over the 30-minute soak; bounded-executor design is deferred to Phase 3 |

The live benchmark uses Overview at `80×24`, `NO_COLOR=1`, motion off, a 30-second warmup, then one-second parent-plus-descendant CPU/RSS/thread/process samples for five minutes. Each launch/snapshot percentile uses 20 independent runs with the machine on AC, no intentional stress workload, and the exact model/macOS/Python recorded beside the result. Results from a different device class are not compared as if hardware-independent.

The child-launch target uses this steady-state allowance: pressure 60/min; `vm_stat` 30/min; swap 4/min; network 60/min; process table 24/min; thermal 12/min; battery plus battery IORegistry 8/min; capacity 2/min; disk activity 12/min; AGX 12/min; `powermetrics` 4/min; and explicit-retry reserve 12/min. Static memory size and topology are cached once, SMC reads remain native, `sensor_info` reuses the independent OS thermal state instead of probing it again, and one `powermetrics` attempt uses one absolute deadline with capability backoff rather than trying every fallback after timeout. Total target remains at most 240/min. Phase 1 does not add a daemon, persistent `iostat`, native replacement for every command, or process isolation solely to manufacture a hard timeout.

The two launch targets labeled release gate cannot be waived for the physically verified tier. If another measured target misses, the result and cause are recorded and optimization proceeds in Phase 3; hard truth/safety gates still cannot be waived. Hardware-dependent performance evidence never substitutes for deterministic correctness tests.

## Testing strategy

Implementation uses test-driven development.

### Pure core tests

- revision allocation, mismatched completion rejection, and immutable state
- success/failure transitions with last-valid retention
- active refresh with a fresh sample
- latest failure causing decision ineligibility
- stale-age boundaries using a fake monotonic clock
- every health entry, exact boundary, precedence, recovery, missing/stale/conflicting state, and copy
- evidence state independent of severity
- age and evidence-source calculation
- contributor eligibility and sanitization boundary

### Adapter and regression tests

- structured command result for success, timeout/reap, missing executable, nonzero exit, decoding error, and locale forcing
- exact memory-pressure and thermal fixtures
- normal pressure with high RAM, compression, and swap never recommends closing applications
- thermal failure never becomes nominal
- AGX failure never prevents process CPU/memory rows and never fabricates GPU zero
- AGX complete absence yields `0.0`; partial/stale/mismatched/failed evidence yields `null`; valid identity-matched activity yields the measured value
- disk-capacity byte/fraction thresholds and selected-volume provenance
- one-shot output cannot claim sustained CPU or global normal from one sample
- snapshots remain JSON-serializable and redacted at known secret-bearing flags

### Rendering and interaction tests

- the five viewport boundaries in the compatibility table
- minimum view shows headline and every cited core source
- direct keys visibly change label and composition
- `l` excludes compact and unsupported Cinema
- Cinema resize returns to Overview with explicit copy
- motion defaults off and does not change on Cinema selection
- `NO_COLOR`, ASCII, limited color, loading, stale, incomplete, degraded, error, and recovery states
- mode messages say selected, never saved
- help, README, launcher, module docstring, and input handler share one control contract

### Integration and manual evidence

- real TUI smoke at minimum and wide sizes on the current host
- current-host comparison with the canonical memory-pressure sysctl and `pmset -g therm`
- process contributor comparison with `ps`
- cold/warm startup, snapshot, live resource, and 30-minute thread-soak measurements
- palette and signal restoration in Terminal.app plus one available alternative terminal or PTY
- manual contrast review across every built-in theme and `NO_COLOR`
- explicit record of untested hardware classes
- no automated sudoers installation or removal

## Release gates

Phase 1 cannot be called complete unless:

- every decision-driving failure fixture preserves its failure class
- no decision-driving empty or failed result displays `0`, `nominal`, or `locked` without evidence
- stale or failed decisive evidence cannot support a current conclusion
- memory pressure value `1` cannot recommend closing applications solely because RAM, compression, or swap is high
- a global normal conclusion requires all four fresh core domains and a complete CPU window
- only memory pressure value `4` can produce Phase 1 critical severity
- cited evidence remains visible at `76×22`
- keys `1`–`5` visibly change narrow composition and key `6` rejects honestly when unsupported
- default motion is off and Cinema never changes it
- snapshot `read` no longer emits the unsafe heuristic and unavailable GPU attribution is `null`
- every AGX zero/null fixture obeys the complete-enumeration and identity-safe merge contract
- public support wording matches actual physical and fixture evidence
- all deterministic tests and hard structural gates pass
- measured performance results and test environment are recorded, including misses
- the tracked demo is updated and remains clearly illustrative rather than runtime evidence
- compile, unit discovery, launcher syntax, wheel build/import, help, version, diff, tracked-map, symbol-anchor, and Markdown-link checks pass
- no privileged mutation was performed by automated validation

## Documentation contract

The Phase 1 implementation updates:

- `README.md` for positioning, requirements, support tiers, controls, state language, metric semantics, snapshot instability/privacy limits, and visual evidence
- `AGENTS.md` for new files, the core boundary, source/state flow, health invariants, edit routes, and validation
- `pyproject.toml` so the pure module ships in the wheel and declared Python support matches CI
- module, launcher, in-app help, and argparse copy so controls and support claims do not drift
- the complete tracked-file map and stable `path::symbol` anchors

The README remains user-facing. This spec records the design decision; `AGENTS.md` remains the ongoing maintainer navigation layer. No generated wiki, graph database, new runtime dependency, or ignored local tool is presented as canonical.

## Acceptance outcome

### Task 14 deterministic implementation record

The implementation surfaces and reproducible repository contracts are complete:

- the source tree contains the effects module and pure policy module, with the runtime package restricted to those two standard-library modules
- the Git inventory and exact `AGENTS.md` map contain the same 19 paths; AGENTS-only Python anchors, every tracked Markdown local link, and the semantic control contract validate mechanically
- repository-tool tests cover strict map parsing, unsafe and case-conflicting paths, qualified AST anchors, fenced/reference/HTML links, traversal/symlink/tracking/case failures, deterministic private CLI errors, renderer path/limit/provenance safety, and measurement statistics/privacy/process-cleanup primitives
- CI declares Python 3.9 through 3.14, compiles both modules, all three tools, and repository-tool tests, runs warnings-as-errors discovery and documentation validation, checks launcher/help/version, and verifies the fresh wheel includes both production modules while excluding tools and tests
- the tracked 36-frame, 12 fps, 960-pixel-wide demo is generated only from fixed synthetic values, visibly labels every frame `ILLUSTRATIVE DEMO · SYNTHETIC DATA`, and uses bounded Chrome/Chromium and ffmpeg development subprocesses without weakening the browser sandbox

At Task 14 acceptance, the deterministic implementation record above contained no Task 15 startup, snapshot, resource, soak, contrast, terminal, physical-device, or release-performance result. Passing deterministic or mocked tests was not treated as physical validation. The following Task 15 record is the sole authority for those measurements, misses, unavailable checks, and review closures.

### Task 15 release-candidate validation record

#### Authority, time, and artifact hygiene

- Frozen code SHA: `750387f5726140208a312d627ca5788cceeed25c`.
- Record commit: the commit containing this record. A commit cannot contain its own hash; the final hash is reported externally.
- Authoritative activity window: 2026-08-12 18:51:54.251809 through 19:30:20.175355 UTC (`America/Chicago` host time zone).
- The tree was clean before every authoritative batch and after the final process audit. Measurements from `51b0e201b1f7b9ca20da71e7cdd1deca59613071` were invalidated after the Cinema resize defect was found; its live run was interrupted and cleaned, and no soak was started from that SHA.
- Measurement workspaces used mode `0700` and `umask 077`. Raw PTY bytes, process listings, launch/thread identity logs, timeout identities, and telemetry payloads existed only in auto-removed private temporary workspaces. Retained ignored artifacts are sanitized JSON with mode `0600`; they contain no identities, host paths, raw process text, screenshots, or telemetry payloads and are not repository truth.
- `/etc/sudoers.d/porkyhud` was absent before and after by metadata-only existence checks. Its contents were never read, and no sensor setup, status, removal, sudo credential refresh, or `u` action was performed. No tag, push, publish, Homebrew edit, or release mutation was performed.

#### Recorded environment

| Field | Recorded value |
| --- | --- |
| Host | Mac17,6; Apple M5 Max; arm64 |
| Memory / logical CPUs | 68,719,476,736 bytes (64 GiB) / 18 |
| Operating system | macOS 26.6, build 25G72 |
| Primary Python | Homebrew Python 3.14.6 at `$HOME/.homebrew/bin/python3` |
| Compatibility Python | Xcode Python 3.9.6 at `/usr/bin/python3` |
| Terminal.app | 2.15, Basic profile; identified but not physically automated |
| Alternate interaction surface | Raw macOS PTY, explicitly not an alternate terminal emulator |
| Measurement terminal | `TERM=xterm-256color`, `80x24`, `NO_COLOR=1`, motion off |
| Controller locale | `C.UTF-8`; a separate true US-ASCII locale covered ASCII projection |
| Power / sleep | AC remained stable for every batch; no wall/monotonic discontinuity crossed the rejection threshold |

Batch authority was captured rather than inferred:

| Batch | UTC window | Load start -> end | Wall/monotonic drift | Guard result |
| --- | --- | ---: | ---: | --- |
| environment | 18:51:54.251809-18:51:54.344398 | n/a | n/a | clean frozen SHA |
| excluded startup smoke | 18:52:15.160336-18:52:16.551828 | 5.869 -> 5.959 | 0.000010 s | AC stable |
| startup 20 | 18:52:16.614378-18:52:53.094588 | 5.959 -> 6.013 | 0.000262 s | AC stable |
| snapshot 20 | 18:53:16.871802-18:53:40.222285 | 4.885 -> 4.485 | 0.000167 s | AC stable |
| pure projections | 18:53:40.261259-18:53:41.236369 | n/a | n/a | deterministic |
| forced timeout | 18:53:41.289796-18:53:43.316251 | n/a | n/a | process group absent |
| five-minute live | 18:54:00.963069-18:59:31.161054 | 5.178 -> 4.435 | 0.002379 s | AC stable; zero gaps |
| 30-minute soak | 18:59:49.955804-19:30:20.175355 | 3.950 -> 3.889 | 0.021336 s | AC stable; zero gaps |

#### Independent review and defect closure

The two required read-only reviews and the focused resize re-review closed with no remaining Critical, Important, or Minor findings on the accepted candidate:

| Review | Validated defect and proving boundary | Resolution / re-review |
| --- | --- | --- |
| Specification, truth, privacy, and UX | Still-triggering memory, thermal, storage, and CPU revisions replaced the original `TriggerEvidence`; a `MEMORY_CRITICAL` to `MEMORY_WATCH` transition then needed a new watch trigger rather than retaining the critical one | `51b0e201...` preserves same-rule entry evidence, creates cross-rule evidence at the transition, and corrects the internal PID/provenance documentation. Table-driven reducer tests pass on current Python and 3.9; reviewer PASS. |
| Architecture, effects/core boundary, and lifecycle | The timeout harness fabricated a timeout label, thread event projection assumed append order, and real child-to-grandchild cleanup could classify the result after probing before leader reap | `51b0e201...` drives the real structured command timeout, requires exact `TIMEOUT` / `COMMAND_TIMEOUT`, orders concurrent events deterministically, treats signal `EPERM` as unconfirmed, re-probes after reap, and proves private output plus both descendants absent. Reviewer PASS. |
| Resize transaction | A physical PTY could resize after the loop's size snapshot and make Cinema rendering raise `body geometry must fit the viewport` | `750387f...` binds one logical geometry to each attempt, reacquires after collection, and retries at most three uncommitted frames before refresh. Exhaustive draw-stage injection and churn tests pass; independent 30-run raw-PTY review passed 30/30 with no repeated collection or mixed committed frame. |

The resize review independently ran 45/45 layout tests and 390/390 full tests under Python 3.14.6 and 3.9.6, plus compile, documentation, launcher, and diff gates. All 30 rapid `118x34` to `117x33` PTY runs exited cleanly: 27 observed the explicit Cinema-to-Overview return and three resized before selection and received the honest rejection.

#### Final deterministic, repository, and package gates

After the evidence-only documentation edit, the complete local matrix ran under six actual interpreters rather than treating the CI declaration as execution evidence:

| Python | Warnings-as-errors discovery | Compile | Documentation / CLI | Wheel install and outside-checkout import |
| --- | --- | --- | --- | --- |
| 3.9.6 | 390/390 PASS | PASS | PASS | PASS |
| 3.10.17 | 390/390 PASS | PASS | PASS | PASS |
| 3.11.12 | 390/390 PASS | PASS | PASS | PASS |
| 3.12.10 | 390/390 PASS | PASS | PASS | PASS |
| 3.13.13 | 390/390 PASS | PASS | PASS | PASS |
| 3.14.6 | 390/390 PASS | PASS | PASS | PASS |

Compilation covered `porkyhud.py`, `porkyhud_core.py`, all three tracked tools, and `tests/test_repository_tools.py`. Each interpreter also ran the documentation validator, `--help`, and exact `PorkyHUD 0.1.2` version check. `zsh -n PorkyHUD.command`, real JSON parsing, the text snapshot smoke, and `git diff --check` passed.

The first current-Python post-commit rerun had one release-gate miss: the UTF-8 split-write integration test inherited a one-second end-to-end process deadline and returned the runner's exact typed `TIMEOUT` once under full-suite load. The decode invariant is independent of that deadline; 300 focused executions at the original budget had zero failures and a maximum below 71 ms, while cleanup faults would have produced private `UNKNOWN`. The valid and invalid split-write cases now use the same two-second test-only budget, with no production timeout change. Independent review accepted the bounded fix; structured-command tests, a focused 11-run repetition, and the full six-interpreter 390-test matrix then passed. If the case ever exhausts two seconds, the next action is timestamp instrumentation, not another budget increase.

Every interpreter built into its own empty private wheel directory. Each wheel's only non-`dist-info` members were `porkyhud.py` and `porkyhud_core.py`; tools, tests, assets, and source-tree residue were absent. Each wheel installed into a fresh target and imported both modules from a different working directory. Pre-existing ignored build products were identified as stale, removed only after an exact dry run, and never used as release evidence.

Repository validation found the same 19 paths in Git and the exhaustive `AGENTS.md` map, with zero map, Python-anchor, local-link, or semantic-control errors. The tracked demo SHA-256 is `7e5a2d1a941daf55e5a902101f30ad50ac4b0f589620e29d8f06c5ead26571ed`; it is GIF89a, `960x600`, 36 frames at 12 fps. Original-detail inspection of first, middle, and last frames found the visible `ILLUSTRATIVE DEMO · SYNTHETIC DATA` marker, `NORMAL`, `m sort`, generic fixed values, and no clipping or private material.

#### Startup hard gates

The harness disclosed one excluded smoke (`81.714 ms` shell, `1251.893 ms` point evidence), then performed 20 independent launches with a 500 ms gap and five-second deadline. A shell marker required `CHECKING 0/4 core sources`, `Waiting for core evidence.`, and CPU/MEM/THERM/DISK `CHECKING`. The evidence marker required numeric CPU, a typed memory state, and a typed thermal state. Every run found both markers, exited cleanly after `q`, and left no recorded child.

Exact run-order samples (`run: shell_ms / evidence_ms`) were:

```text
01:  84.334 / 1255.715    02: 102.193 / 1257.676
03:  96.974 / 1242.033    04: 104.534 / 1268.633
05: 103.426 / 1265.798    06: 100.340 / 1334.133
07:  90.177 / 1328.540    08: 103.228 / 1264.899
09:  97.898 / 1257.175    10: 101.874 / 1317.853
11:  88.210 / 1257.350    12: 101.360 / 1337.401
13:  91.043 / 1318.104    14: 104.794 / 1257.195
15: 102.496 / 1339.401    16: 105.657 / 1324.904
17:  88.102 / 1313.989    18:  88.077 / 1317.991
19: 108.019 / 1260.641    20:  92.858 / 1274.792
```

| Marker | Minimum | Median | Mean | Nearest-rank p95 | Maximum | Gate |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| initial shell | 84.334 ms | 100.850 ms | 97.780 ms | 105.657 ms | 108.019 ms | <=250 ms: PASS |
| first point evidence | 1242.033 ms | 1271.713 ms | 1289.711 ms | 1337.401 ms | 1339.401 ms | <=3000 ms: PASS |

There were no missing markers, crashes, cleanup failures, or discarded measured runs. The maxima are reported separately and are not misrepresented as p95 thresholds.

#### Snapshot and pure projection evidence

Twenty independent `--json` commands used the same timed invocation for elapsed time, command maximum RSS, schema validation, redaction validation, and cleanup. RSS is the macOS `/usr/bin/time -l` command maximum, not an invented descendant-tree value.

Exact run-order samples (`run: elapsed_ms / RSS_bytes`) were:

```text
01: 1144.085 / 60473344    02: 1129.722 / 60260352
03: 1133.303 / 60325888    04: 1130.738 / 60424192
05: 1136.374 / 60014592    06: 1144.418 / 60915712
07: 1162.073 / 59834368    08: 1203.939 / 59916288
09: 1132.099 / 63684608    10: 1194.632 / 59899904
11: 1168.341 / 60702720    12: 1249.894 / 60096512
13: 1139.777 / 59834368    14: 1138.999 / 60063744
15: 1154.155 / 59998208    16: 1134.426 / 59932672
17: 1234.056 / 60342272    18: 1129.490 / 59883520
19: 1259.065 / 60915712    20: 1128.989 / 60030976
```

| Measurement | Minimum | Median | Mean | Nearest-rank p95 | Maximum | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| snapshot wall time | 1128.989 ms | 1141.931 ms | 1162.429 ms | 1249.894 ms | 1259.065 ms | scoped <=3 s target: PASS |
| command maximum RSS | 59,834,368 B | 60,080,128 B | 60,377,497.6 B | 60,915,712 B | 63,684,608 B | recorded; maximum below 64 MiB |

Run 19 had the maximum wall time. Run 9 had the maximum RSS. Both are retained, and no snapshot sample, schema result, privacy result, or cleanup was missing.

After 1,000 excluded warmups, GC remained enabled for 10,000 individually timed operations of each pure surface:

| Projection | Minimum | Median | Mean | p95 | Maximum | Hard gate |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| threaded Task 5 policy reduction | 0.025875 ms | 0.028313 ms | 0.029182 ms | 0.031083 ms | 0.279000 ms | mean and p95 <1 ms: PASS |
| Task 12 header plus ribbon | 0.043208 ms | 0.047833 ms | 0.048560 ms | 0.052417 ms | 0.204250 ms | mean and p95 <5 ms: PASS |

The maxima are retained as observed outliers; neither is substituted for the specified mean/p95 gate.

#### Live resources, launch rate, soak, and timeout cleanup

The five-minute live batch used a 30-second warmup followed by 300 one-second samples, with zero gaps. CPU and RSS recursively summed the instrumented root plus descendants. The resident `live-wrapper.py` monkeypatched launch/thread recorders and called `porkyhud.main()` in-process, so recorder overhead is included in the root result; the measurement driver and PTY-drain thread were outside the measured tree.

| Five-minute live measurement | Minimum | Median | Mean | p95 | Maximum | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| CPU, percent of one core | 56.5 | 63.65 | **63.473** | 67.6 | 71.2 | scoped <=2% target: **MISS** |
| RSS | 35,487,744 B | 41,369,600 B | 39,622,246.4 B | 45,875,200 B | 48,136,192 B | scoped p95 <=64 MiB: PASS |
| concurrent descendants | 0 | 0 | 0.250 | 1 | 2 | bounded |
| OS threads | 7 | 7 | 7.357 | 8 | 9 | recorded |

The five exact half-open launch bins were `[231, 228, 227, 229, 228]`, totaling 1,143 launches with a maximum of 231/minute; the <=240/minute scoped target passed. `q` produced a clean exit and cleanup with zero recorded survivors. The 63.473% CPU result is a material, explicitly retained host-target miss. It was not rerun, averaged away, or promoted to a universal result.

The soak used another 30-second warmup followed by 1,800 seconds and 360 five-second samples, again with zero gaps. `q` exited cleanly, cleanup completed, and no recorded identity survived.

| 30-minute soak measurement | Minimum | Median | Mean | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| CPU, percent of one core | 56.1 | 63.25 | 63.217 | 67.6 | 79.7 |
| RSS | 33,734,656 B | 35,323,904 B | 37,026,656.7 B | 42,909,696 B | 50,561,024 B |
| concurrent descendants | 0 | 0 | 0.264 | 1 | 2 |
| OS threads | 7 | 7 | 7.325 | 8 | 9 |

The exact 30 launch bins were `[230, 228, 228, 228, 228, 229, 221, 228, 229, 226, 229, 228, 228, 222, 228, 228, 230, 229, 226, 229, 229, 223, 228, 230, 232, 229, 232, 229, 221, 232]`, totaling 6,837 and peaking at 232/minute. First-window to final-window thread results were:

- OS threads: maximum `9 -> 9`, p95 `8 -> 8`.
- Python threads: maximum `10 -> 9`, p95 `8 -> 8`.
- Prefix maxima: `AsyncPoller 1 -> 1`, `SourcePoller 6 -> 6`, `PorkyHUD-command 2 -> 2`, `Other 0 -> 0`.

The no-growth soak gate passed. This is evidence for the observed 30-minute window plus deterministic lifecycle tests, not a claim of indefinite leak freedom.

The separate nonprivileged Python child-to-grandchild probe drove the real structured command past its deadline. It returned exact typed outcome `timeout` and failure `command_timeout` in 1966.752 ms, reaped the direct child, proved the grandchild absent, classified cleanup as `process_group_terminated`, and exposed no private stdout, stderr, or argv.

#### Manual interaction, terminal protocol, and visual evidence

The raw macOS PTY matrix passed at `76x22`, `80x24`, `117x33`, `118x34`, and `132x42`. At minimum size, direct modes 1-5 had distinct bodies, key 6 gave an honest Cinema rejection, and cycling stayed within supported modes. Help/Escape, Home/End, `q`, Cinema selection, the `118x34` to `117x33` Overview return, resize-back/reselection, and the `132x42` Cinema/help path all completed cleanly.

All four themes cycled at `80x24` under forced palette and `NO_COLOR`. Forced mode emitted OSC 10/11/12 and reset 110/111/112 on exit; `NO_COLOR` emitted none. A true US-ASCII locale kept semantic output ASCII. `TERM=xterm`, `xterm-16color`, and `xterm-256color` rendered, cycled, and exited; these are protocol paths, not emulator visual proof. `q`, Escape, SIGINT, and SIGTERM produced exit codes `0`, `0`, `0`, and `143`, respectively, with no residual process group and palette restoration. The launcher started and closed under forced-palette and `NO_COLOR`; forced mode reset the palette and `NO_COLOR` emitted no palette OSC.

Physical current-host runtime observation covered only `CHECKING -> UNKNOWN -> NORMAL` over 18 seconds. Loading, stale, incomplete, degraded, error, and recovery presentation states are covered by deterministic synthetic tests, not claimed as physically induced states.

Terminal.app 2.15 with the Basic profile was identified, but the available computer-use authority explicitly prohibited controlling Terminal.app. No AppleScript bypass was used. Consequently, physical Terminal.app viewport/theme screenshots, visual palette fidelity, and profile-sampled RGB swatches are unavailable, and this record does **not** say that Terminal.app passed. No alternate terminal emulator was exercised; the alternate surface was a raw PTY.

Forced-palette contrast below is calculated from the shipped xterm-256 role RGB values against the configured background `#05070d`. It is source/protocol-derived, not sampled from Terminal.app pixels or its profile:

| Theme | Primary | Info | Nominal | Watch | Critical | Structure | Secondary | Brand |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Neon Grid | #ffffff 20.141:1 | #00ffff 16.063:1 | #00ff00 14.678:1 | #ffd700 14.360:1 | #ff0000 5.037:1 | #00afff 8.225:1 | #00d7ff 11.667:1 | #ff00ff 6.422:1 |
| Catppuccin | #d7d7ff 14.438:1 | #87afd7 8.769:1 | #87ff5f 15.824:1 | #ffffaf 19.350:1 | #ff5f5f 6.766:1 | #87afff 9.213:1 | #afd7ff 13.415:1 | #d7afff 10.996:1 |
| Solar Circuit | #eeeeee 17.359:1 | #5fffff 16.529:1 | #5fff00 15.145:1 | #ffff00 18.756:1 | #ff5f00 6.607:1 | #0087ff 5.668:1 | #5fffff 16.529:1 | #d700ff 5.115:1 |
| Graphite | #d0d0d0 13.058:1 | #87afd7 8.769:1 | #afd7d7 12.971:1 | #ffd787 14.695:1 | #ff5f5f 6.766:1 | #8a8a8a 5.834:1 | #bcbcbc 10.605:1 | #d7afff 10.996:1 |

The minimum text-role ratio is 5.037:1, the minimum meaningful-structure ratio is 5.668:1, and the minimum decorative brand ratio is 5.115:1. These source values exceed the 4.5:1 text and 3:1 structure thresholds, but they do not establish physical Terminal.app profile contrast. Under `NO_COLOR`, only complete text/shape semantics and absence of hue-only meaning were verified; contrast belongs to the user's terminal.

#### Misses, unavailable checks, and untested scope

- Instrumented live CPU averaged 63.473% of one core and missed the scoped <=2% target; recorder overhead is included. This is the sole measured target miss.
- The startup maxima, snapshot run-9 RSS maximum, pure-operation maxima, soak CPU maximum, and the one post-commit test-budget miss are reported above. There were no missing measurement markers, samples, schemas, redaction checks, gaps, cleanup results, or surviving recorded identities.
- Physical Terminal.app viewport/theme/profile-swatch qualification was unavailable. Palette restoration is proven at the OSC protocol boundary, not by sampling the restored profile. No actual alternate emulator was tested, and the 8/16/256 paths are not emulator visual proof.
- No privileged sensor behavior, sudoers mutation, credential refresh, sleep/wake transition, Homebrew installation, tag, publish, or release workflow was exercised.
- Other Mac generations, fanless/one-fan/multi-fan systems, Ultra or multi-die systems, desktops versus battery systems and UPS configurations, partial SMC exposure, varied `powermetrics` grammars, earlier macOS versions, and Intel Macs remain physically unverified. Fixture coverage is not physical hardware proof.
- The local release matrix records the interpreters actually run. CI declares Python 3.9 through 3.14; that declaration is not substituted for local execution evidence.

#### Scoped release conclusion

The frozen code satisfies both non-waivable startup gates, both deterministic pure-operation gates, the snapshot/RSS/launch-rate scoped targets, exact typed timeout cleanup, the 30-minute no-growth gate, and the raw-PTY interaction/protocol matrix. The current-host live CPU target missed and remains visible. The evidence does not establish physical Terminal.app visual/profile contrast, other-emulator behavior, privileged-sensor behavior, or broad Mac compatibility. Within those explicit boundaries, the result is a release-candidate validated for deterministic correctness, the recorded host measurements, and raw-PTY interaction.

Phase 1 is complete when a user can launch PorkyHUD at `76×22` and understand:

- whether the app is still checking, has a supported conclusion, or lacks evidence
- which fresh core signals support that conclusion
- whether other core evidence is incomplete
- which process is a likely CPU contributor only when the attribution rule supports it
- which direct mode opens the relevant detail

At the same time, a future developer can test source state and health policy without macOS or curses, while effectful collectors, raw commands, and privileged authority remain outside the pure boundary.
