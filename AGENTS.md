# PorkyHUD Agent Guide

This file is the canonical maintainer and agent navigation layer for PorkyHUD. Use it to find the smallest safe edit surface, understand cross-cutting invariants, and keep repository documentation accurate as the project grows.

`README.md` remains the end-user guide. Do not turn it into an internal architecture manual.

## Start here

1. Read `README.md` for the user-visible product contract.
2. Read the relevant area and task route below before editing.
3. Inspect the named symbols and their tests. Anchors use `path::symbol`, not line numbers, because lines drift.
4. Check all callers and output surfaces when a shared model, collector, command, or schema changes.
5. Run the focused tests during development, then run the complete validation set before claiming completion.

PorkyHUD currently has an effects module (`porkyhud.py`) and a pure policy module (`porkyhud_core.py`). The remaining areas below are conceptual boundaries, not separate packages. Preserve their dependency direction until there is a justified, tested module extraction.

## Canonical sources of truth

| Concern | Canonical source |
| --- | --- |
| User installation, supported workflows, permissions, privacy summary, and metric semantics | `README.md` |
| Agent routing, architecture, invariants, and documentation maintenance | `AGENTS.md` |
| Package name, Python floor, dependencies, entry point, and project URLs | `pyproject.toml` |
| CLI flags and precedence | `porkyhud.py::main` and its argparse declarations |
| Keyboard behavior | `porkyhud.py::hud`; `porkyhud.py::draw_help_overlay` is the user-facing projection |
| Snapshot payload | `porkyhud.py::collect_snapshot` and `tests/test_porkyhud.py::RuntimeProcessAndSnapshotTests` |
| Local and CI validation | `.github/workflows/ci.yml` |
| Legal terms | `LICENSE` |

Generated build metadata, caches, ignored files, and external package formulas are not repository truth. If prose conflicts with tested behavior, verify the behavior and update the stale surface in the same change.

## Complete tracked-file map

Keep this table exhaustive. Add, move, or remove a row in the same commit as the corresponding tracked path change.

| Path | Responsibility | Change with |
| --- | --- | --- |
| `AGENTS.md` | Agent routing, architecture, invariants, and maintenance contract | Any boundary, file ownership, runtime flow, validation, or documentation-policy change |
| `README.md` | User-facing installation, controls, sensor access, privacy, and metric semantics | User-visible behavior, requirements, CLI workflows, or support changes |
| `docs/superpowers/specs/2026-08-11-porkyhud-decision-cockpit-design.md` | Phase 1 design and acceptance proposal for the decision-cockpit foundation; not current behavior until implementation lands | Scope, architecture, UX, compatibility, performance, or acceptance changes to this milestone |
| `docs/superpowers/plans/2026-08-11-porkyhud-trusted-decision-cockpit.md` | Test-first Phase 1 implementation sequence and fixed cross-task interfaces; not current behavior until implementation lands | Task boundaries, interface names, validation sequence, or approved Phase 1 execution changes |
| `porkyhud.py` | macOS effects: adapters, collectors, runtime dashboard, rendering, snapshots, and CLI | Focused tests plus every affected user or agent contract |
| `porkyhud_core.py` | Pure immutable source-state models, reducers, freshness, eligibility, and source views | Source contract tests and every consumer of telemetry evidence |
| `PorkyHUD.command` | Double-click and shell launcher, terminal setup/restoration, source-versus-installed dispatch | Launcher, palette, executable discovery, or launch guidance changes |
| `pyproject.toml` | Setuptools build and package metadata | Version, Python support, packaging, entry point, dependency, or project URL changes |
| `tests/test_core.py` | Behavioral contract tests for pure immutable telemetry source state | Every `porkyhud_core.py` behavior change |
| `tests/test_porkyhud.py` | Unit and contract tests for current application behavior | Every production behavior change |
| `tests/__init__.py` | Test package and `unittest` discovery marker | Test layout changes |
| `.github/workflows/ci.yml` | macOS build and validation matrix | Supported Python versions or validation contract changes |
| `.gitignore` | Generated, cached, local-only, and build-output boundaries | Any new reproducible source or generated artifact policy |
| `assets/porkyhud-demo.gif` | Sanitized illustrative README media | User-facing visual changes; do not treat it as runtime evidence |
| `LICENSE` | MIT license text | Legal changes only |

The ignored `tools/` directory is local-only. Do not reference a file under it as a reproducible project tool unless the file is intentionally unignored, reviewed, tracked, mapped here, and documented with its non-runtime dependencies.

## Architecture and dependency direction

The intended direction is:

```text
entry points and presentation
        ↓
effects orchestration
        ↓
pure policy and shared models
        ↓
macOS commands and native adapters
```

`porkyhud_core.py` may depend only on the standard-library pure-model allowlist (`dataclasses`, `datetime`, `enum`, `math`, and `typing`) and must not import `porkyhud.py`, clocks, I/O, or platform APIs. Rendering may consume models and formatter helpers. Collectors must not depend on curses or panel geometry. Native and subprocess adapters must not decide layout. Snapshot and future integration adapters should consume collector results without driving the interactive HUD.

### `area:source-state`

- Owns immutable telemetry source state, attempts, successful samples, freshness, decision eligibility, and status views.
- Start at `porkyhud_core.py::new_source`, `porkyhud_core.py::start_attempt`, `porkyhud_core.py::complete_success`, `porkyhud_core.py::complete_failure`, `porkyhud_core.py::is_decision_ready`, and `porkyhud_core.py::source_view`.
- Primary tests: `tests/test_core.py::SourceStateTests`.
- A failed refresh retains `last_valid` for display but immediately makes the source decision-ineligible; freshness is inclusive at `age <= stale_after_s`.

### `area:configuration-models`

- Owns refresh constants, themes, layout names, global caches, and shared dataclasses.
- Start at `porkyhud.py::HudConfig`, `porkyhud.py::ProcessRow`, `porkyhud.py::BatteryInfo`, `porkyhud.py::SensorInfo`, `porkyhud.py::DiskInfo`, and `porkyhud.py::RuntimeStats`.
- Changes can affect live rendering, snapshot serialization, tests, and future consumers because several outputs use `dataclasses.asdict`.

### `area:polling-state`

- Owns bounded history and single-flight background collection.
- Start at `porkyhud.py::MetricHistory` and `porkyhud.py::AsyncPoller`.
- Primary tests: `tests/test_porkyhud.py::AsyncPollerTests`.
- `AsyncPoller` keeps the last good value, measures cadence from collector completion, prevents overlapping runs for one source, and queues at most one forced follow-up while a collector is active.

### `area:terminal-lifecycle`

- Owns TTY capability checks, palette setup/restoration, curses lifecycle, signals, and launcher behavior.
- Start at `PorkyHUD.command`, `porkyhud.py::enforce_dark_terminal`, `porkyhud.py::restore_terminal_colors`, `porkyhud.py::terminal_ui_problem`, and `porkyhud.py::main`.
- Primary tests: `tests/test_porkyhud.py::LayoutAndRenderingTests` and `tests/test_porkyhud.py::RuntimeProcessAndSnapshotTests`.

### `area:sensors-privilege`

- Owns direct AppleSMC reads, bounded `powermetrics` invocation, sensor parsing/merging, sudoers installation, status, removal, and temporary unlock.
- Start at `porkyhud.py::AppleSMCReader`, `porkyhud.py::read_smc_sensors`, `porkyhud.py::powermetrics_args`, `porkyhud.py::sudoers_contents`, `porkyhud.py::install_sensor_access`, `porkyhud.py::unlock_privileged_sensors`, and `porkyhud.py::sensor_info`.
- Parser coverage begins in `tests/test_porkyhud.py::FormatterAndParserTests`.
- Treat permission, unsupported hardware, missing tools, timeouts, and parser drift as distinct possibilities even where the current adapter collapses them into an empty value.

### `area:telemetry-collectors`

- Owns static Mac discovery and live CPU, memory, battery, disk, network, process, and GPU process attribution.
- Start at `porkyhud.py::MacCpuSampler`, `porkyhud.py::collect_static_info`, `porkyhud.py::memory_stats`, `porkyhud.py::battery_info`, `porkyhud.py::disk_info`, `porkyhud.py::disk_activity`, `porkyhud.py::network_bytes`, `porkyhud.py::collect_runtime_stats`, `porkyhud.py::GPUProcessSampler`, and `porkyhud.py::collect_process_rows`.
- Primary tests: `tests/test_porkyhud.py::FormatterAndParserTests`, `tests/test_porkyhud.py::BatteryBehaviorTests`, and `tests/test_porkyhud.py::RuntimeProcessAndSnapshotTests`.

### `area:privacy-interpretation`

- Owns process-command redaction, display formatting, and the plain-language system assessment.
- Start at `porkyhud.py::sanitize_command`, `porkyhud.py::visible_command`, and `porkyhud.py::system_read`.
- Primary tests: `tests/test_porkyhud.py::CommandPrivacyTests` plus snapshot tests.
- Redaction is an output-boundary invariant. Raw `ProcessRow.command` values exist in memory, so every new output path must sanitize them explicitly.

### `area:layout-rendering`

- Owns curses-safe writes, drawing primitives, responsive sizing, panels, help, colors, input handling, and live orchestration.
- Start at `porkyhud.py::safe_add`, `porkyhud.py::proportional_heights`, `porkyhud.py::wide_column_widths`, `porkyhud.py::draw_header`, `porkyhud.py::draw_cpu_panel`, `porkyhud.py::draw_process_panel`, `porkyhud.py::draw_help_overlay`, and `porkyhud.py::hud`.
- Primary tests: `tests/test_porkyhud.py::LayoutAndRenderingTests`.
- `porkyhud.py::hud` is the cross-area wiring hotspot. Inspect it whenever polling cadence, shared state, keyboard behavior, or panel inputs change.

### `area:snapshot-interface`

- Owns one-shot collection, JSON assembly, and the text snapshot projection.
- Start at `porkyhud.py::collect_snapshot` and `porkyhud.py::print_text_snapshot`.
- Primary tests: `tests/test_porkyhud.py::RuntimeProcessAndSnapshotTests`.
- Snapshot orchestration is separate from the live HUD. It currently omits live network-rate and history state, and its schema is unversioned. Do not describe it as stable for external consumers without first defining a compatibility policy and schema version.

### `area:packaging-release`

- Owns build metadata, executable entry points, compatibility matrix, version synchronization, tags, and downstream Homebrew coordination.
- Start at `pyproject.toml`, `porkyhud.py::VERSION`, `.github/workflows/ci.yml`, and `PorkyHUD.command`.
- The in-repository version declarations must match. A checkout version is not a published release until its tag and external Homebrew formula are also verified.

## Execution flows

### Live dashboard

```text
main
  → terminal capability and palette setup
  → curses.wrapper(hud)
  → one-time static discovery and initial runtime sample
  → AsyncPoller instances for runtime, processes, sensors, and disk activity
  → state deltas and MetricHistory
  → responsive panel composition
  → sanitized rendering and status/help output
  → terminal and signal restoration on every exit path
```

The runtime poller refreshes the bundled memory, battery, disk-capacity, network-total, and load sample. CPU sampling and network-rate deltas advance when a fresh runtime bundle arrives. Process, sensor, and disk-activity collectors use their own cadences.

### Snapshot mode

```text
main --json/--snapshot
  → collect_snapshot
  → CPU/GPU delta priming
  → concurrent static, memory, battery, sensor, disk, and disk-activity collection
  → process collection
  → command sanitization and dataclass serialization
  → JSON or text projection to stdout
```

Treat snapshot changes as interface changes even while the schema remains formally unstable.

### Privileged sensor setup

```text
main --setup-sensors
  → generate exact bounded sudoers commands from powermetrics_args
  → write a temporary rule
  → validate with visudo
  → privileged install to /etc/sudoers.d/porkyhud
  → validate the installed rule
  → remove it on post-install validation failure
```

`--remove-sensor-access` is the inverse durable mutation. Pressing `u` performs only a temporary sudo credential refresh. Never run setup or removal as part of automated tests.

## Task-to-edit routes

| Task | Start here | Also inspect and update |
| --- | --- | --- |
| Add or change a CLI flag | `porkyhud.py::main` | README workflow, precedence, help text, CLI tests |
| Add a metric | Relevant dataclass and collector in `area:telemetry-collectors` | Live poller input, panel, snapshot inclusion, fallback semantics, parser and rendering tests, README metric notes |
| Change advanced sensor commands | `porkyhud.py::powermetrics_args` | Sampler fallbacks, `sudoers_contents`, access check, setup/status/remove copy, launcher, README security wording, tests |
| Change polling cadence or failure behavior | Refresh constants and `porkyhud.py::AsyncPoller` | `hud`, stale-age labels, history semantics, poller tests, README timing claims |
| Add or change a layout/panel | `LAYOUTS`, sizing helpers, `draw_*`, and `hud` | In-app help, README shortcuts, narrow and wide layout tests |
| Change process output | `collect_process_rows`, sorting, and `draw_process_panel` | `sanitize_command`, snapshot projection, privacy tests, README privacy statement |
| Change snapshot JSON | Models and `porkyhud.py::collect_snapshot` | Serialization/redaction, schema assertions, text projection, README integration wording, compatibility decision |
| Change palette, terminal, or signal behavior | `PorkyHUD.command` and `area:terminal-lifecycle` | `NO_COLOR`, all exit paths, signal tests, README launch guidance |
| Cut a release | `pyproject.toml` and `porkyhud.py::VERSION` | Full CI, tag/release state, README install claims, external Homebrew formula and its version test |
| Add, move, or remove a tracked file | The file itself | Complete tracked-file map, relevant area/route, local links, CI if validation changes |
| Introduce MCP or another machine consumer | `area:snapshot-interface` and collectors | Schema/versioning, privacy, read-only authority, evidence provenance, tests, new adapter map and routes |

## Load-bearing invariants

- Runtime remains dependency-free beyond the Python standard library.
- Slow collectors stay off the render loop. One slow or failed source must not freeze unrelated UI updates.
- Pollers remain single-flight and retain the last good sample after a collector error.
- Missing or inaccessible hardware data is reported as unavailable, locked, or hidden. Never invent a reading.
- The sudoers rule grants only the exact bounded `powermetrics` commands PorkyHUD generates and uses.
- Known secret-bearing process arguments are redacted from every human-readable or machine-readable output.
- Terminal colors and installed signal handlers are restored on normal exit, exceptions, interrupts, and handled termination signals.
- `NO_COLOR` bypasses both OSC palette changes and curses color-pair use.
- Responsive layout math keeps panels within their assigned rectangles and preserves panel borders and footers.
- Snapshot output stays JSON-serializable. Any compatibility promise requires an explicit schema version and tests.
- User-facing claims distinguish verified behavior from hardware-dependent best effort.
- Empty static-analysis results are not proof of no impact. Confirm dynamic, native, subprocess, string-based, and external relationships from a second source.

## Validation

Run focused tests while editing, then run the same core checks as CI:

```bash
python3 -m py_compile porkyhud.py porkyhud_core.py
python3 -m unittest discover -v
zsh -n PorkyHUD.command
python3 -m pip wheel --no-deps --wheel-dir dist .
```

Also run:

```bash
git diff --check
./porkyhud.py --help
./porkyhud.py --version
```

Do not claim live hardware coverage from mocked unit tests. Privileged sudoers mutations, full interactive curses behavior, palette fidelity, and telemetry correctness across Mac generations require explicit manual validation.

## Documentation maintenance contract

Update documentation in the same change when any of these move:

- tracked paths or area ownership
- entry points, flags, shortcuts, or precedence
- polling/data flows or external command usage
- user-visible support, fallback, privacy, or permission behavior
- snapshot fields or compatibility status
- tests and validation commands
- version, packaging, release, or Homebrew coordination steps
- invariants or future integration boundaries

Before finishing a documentation-sensitive change:

1. Compare the tracked tree with the complete file map.
2. Resolve every `path::symbol` anchor against the current AST or declaration.
3. Check local Markdown links.
4. Search for duplicate user-facing claims in README, launcher copy, module docstrings, argparse help, and in-app help.
5. Run the validation set.
6. Review the diff for stale line numbers, aspirational claims written as current behavior, and ignored/generated paths presented as canonical.

Prefer stable responsibilities, symbols, flows, and invariants over exhaustive function lists. Automation should own mechanical inventory and anchor validation. Prose should own intent, edit routing, authority boundaries, and facts that static analysis cannot infer.

## Growth and modularity rules

Do not keep adding unrelated responsibilities to `porkyhud.py` indefinitely. Extract a module when a conceptual area has a stable interface, independent tests, and enough internal complexity that it can be understood in isolation. When extraction becomes justified:

- split by responsibility and change coupling, not generic technical layers
- keep shared models free of curses and subprocess side effects
- keep collectors free of presentation decisions
- make UI orchestration depend on collector interfaces
- keep CLI, snapshot, GUI, and MCP surfaces as adapters over shared application services
- colocate focused tests with the boundary they protect
- add a nested `AGENTS.md` only when a subtree has genuinely local rules that would overload this root guide
- keep this root file as the short routing index and move details downward only after real modules exist

Every module should make three questions easy to answer: what does it do, how is it used, and what does it depend on?

## Future MCP boundary

MCP is not implemented. When it is introduced, begin with read-only resources and tools over a stabilized service/snapshot layer rather than importing curses or calling `hud`.

Use stable IDs such as `area:layout-rendering` and `python:porkyhud:hud`. Return evidence anchors and distinguish complete, truncated, unknown, and stale results. Compute symbol spans, references, and changed areas from source and git rather than copying them into prose. Reuse the same map parser and validation rules in CI and MCP so human and machine navigation cannot silently diverge.

Any MCP tool capable of mutation, privileged sensor setup, file edits, or command execution requires an explicit authority and safety design beyond this guide.

## Milestone acceptance criteria

The documentation-navigation milestone is complete when:

- README is accurate, concise, and user-facing
- this guide maps every tracked file and current conceptual area
- task routes name the relevant code, tests, and cross-cutting surfaces
- live, snapshot, and privileged flows are explicit
- load-bearing invariants and known validation limits are recorded
- stable file and symbol references are checked automatically without a new runtime dependency
- future agents are instructed to update the maps when structure or behavior changes
- no generated wiki, graph database, or premature module split is introduced
