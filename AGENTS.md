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
| Pure health/source contracts and rule behavior | `porkyhud_core.py` plus `tests/test_core.py` |
| CLI flags and precedence | `porkyhud.py::main` and its argparse declarations |
| Keyboard behavior | `porkyhud.py::hud`; `porkyhud.py::draw_help_overlay` is the user-facing projection |
| Snapshot payload | `porkyhud.py::collect_snapshot` and `tests/test_porkyhud.py::RuntimeProcessAndSnapshotTests` |
| Local and CI validation | `.github/workflows/ci.yml` and `tools/validate_docs.py` |
| Legal terms | `LICENSE` |

Generated build metadata, caches, ignored files, and external package formulas are not repository truth. If prose conflicts with tested behavior, verify the behavior and update the stale surface in the same change.

## Complete tracked-file map

Keep this table exhaustive. Add, move, or remove a row in the same commit as the corresponding tracked path change.

| Path | Responsibility | Change with |
| --- | --- | --- |
| `AGENTS.md` | Agent routing, architecture, invariants, and maintenance contract | Any boundary, file ownership, runtime flow, validation, or documentation-policy change |
| `README.md` | User-facing installation, controls, sensor access, privacy, and metric semantics | User-visible behavior, requirements, CLI workflows, or support changes |
| `docs/superpowers/specs/2026-08-11-porkyhud-decision-cockpit-design.md` | Implemented Phase 1 design, acceptance contract, and pending host-validation record | Scope, architecture, UX, compatibility, performance, acceptance, or recorded validation changes |
| `docs/superpowers/plans/2026-08-11-porkyhud-trusted-decision-cockpit.md` | Test-first Phase 1 execution contract and fixed cross-task interfaces | Task boundaries, interface names, validation sequence, or approved execution changes |
| `porkyhud.py` | macOS effects: adapters, collectors, runtime dashboard, rendering, snapshots, and CLI | Focused tests plus every affected user or agent contract |
| `porkyhud_core.py` | Pure immutable source-state models, reducers, freshness, eligibility, and source views | Source contract tests and every consumer of telemetry evidence |
| `PorkyHUD.command` | Double-click and shell launcher, terminal setup/restoration, source-versus-installed dispatch | Launcher, palette, executable discovery, or launch guidance changes |
| `pyproject.toml` | Setuptools build and package metadata | Version, Python support, packaging, entry point, dependency, or project URL changes |
| `tests/test_core.py` | Behavioral contract tests for pure immutable telemetry source state | Every `porkyhud_core.py` behavior change |
| `tests/test_porkyhud.py` | Unit and contract tests for current application behavior | Every production behavior change |
| `tests/test_repository_tools.py` | Repository validator, demo renderer, and release-measurement tool contracts | Every reproducible source-tree tool or repository-validation change |
| `tests/__init__.py` | Test package and `unittest` discovery marker | Test layout changes |
| `tools/validate_docs.py` | Standard-library tracked-map, AST-anchor, local-link, and controls validator | Tracked paths, anchor syntax, documentation links, control projections, or validation policy |
| `tools/render_demo_gif.py` | Fixed synthetic README-demo renderer with bounded Chrome/ffmpeg development subprocesses | Demo design, provenance label, dimensions, limits, dependencies, or asset generation |
| `tools/measure_release.py` | Bounded, privacy-minimized Task 15 measurement harness and JSON projection | Release measurement procedures, statistics, lifecycle probes, privacy, or timeout cleanup |
| `.github/workflows/ci.yml` | macOS build and validation matrix | Supported Python versions or validation contract changes |
| `.gitignore` | Generated, cached, local-only, and build-output boundaries | Any new reproducible source or generated artifact policy |
| `assets/porkyhud-demo.gif` | Sanitized illustrative README media | User-facing visual changes; do not treat it as runtime evidence |
| `LICENSE` | MIT license text | Legal changes only |

Unlisted files under `tools/` remain local-only and ignored. The three mapped tools are reviewed source-tree utilities, not runtime package modules. Do not present another tool as reproducible unless it is selectively unignored, tested, tracked, mapped here, and documented with its non-runtime dependencies.

## Architecture and dependency direction

The intended direction is:

```text
entry points and presentation
              ↓
     effects orchestration
       ↙                 ↘
pure policy and       collectors and
shared models         effect adapters
                            ↓
                  macOS commands and
                     native APIs
```

Effects orchestration may depend on both the pure policy module and collectors/adapters; neither branch depends on the other. `porkyhud_core.py` may depend only on the standard-library pure-model allowlist (`dataclasses`, `datetime`, `enum`, `math`, and `typing`) and must not import `porkyhud.py`, clocks, I/O, or platform APIs. Rendering may consume models and formatter helpers. Collectors must not depend on curses or panel geometry. Native and subprocess adapters must not decide layout. Snapshot and future integration adapters should consume collector results without driving the interactive HUD.

### `area:source-state`

- Owns immutable telemetry source state, attempts, successful samples, freshness, decision eligibility, and status views.
- Start at `porkyhud_core.py::new_source`, `porkyhud_core.py::start_attempt`, `porkyhud_core.py::complete_success`, `porkyhud_core.py::complete_failure`, `porkyhud_core.py::is_decision_ready`, and `porkyhud_core.py::source_view`.
- Primary tests: `tests/test_core.py::SourceStateTests`.
- A failed refresh retains `last_valid` for display but immediately makes the source decision-ineligible; freshness is inclusive at `age <= stale_after_s`.

### `area:health-policy`

- Owns normalized health facts, CPU/process history, exact entry and recovery rules, evidence classification, precedence, and CPU-contributor attribution.
- Start at `porkyhud_core.py::HealthFacts`, `porkyhud_core.py::PolicyState`, `porkyhud_core.py::reduce_health`, `porkyhud_core.py::reduce_cpu_window`, and `porkyhud_core.py::select_cpu_contributor`.
- Primary tests: `tests/test_core.py::CpuWindowTests`, `tests/test_core.py::RuleLatchTests`, `tests/test_core.py::ContributorTests`, and `tests/test_core.py::HealthPolicyTests`.
- This module is the only authority for global severity. Descriptive metrics and rendering helpers must not reproduce or extend its rule logic.

### `area:configuration-models`

- Owns refresh constants, themes, layout names, global caches, and shared dataclasses.
- Start at `porkyhud.py::HudConfig`, `porkyhud.py::ProcessRow`, `porkyhud.py::BatteryInfo`, `porkyhud.py::SensorInfo`, `porkyhud.py::DiskInfo`, and `porkyhud.py::RuntimeStats`.
- Changes can affect live rendering, snapshot serialization, tests, and future consumers because several outputs use `dataclasses.asdict`.

### `area:polling-state`

- Owns bounded history and single-flight background collection.
- Start at `porkyhud.py::MetricHistory` and `porkyhud.py::AsyncPoller`.
- Primary tests: `tests/test_porkyhud.py::AsyncPollerTests`.
- `AsyncPoller` keeps the last good value, measures cadence from collector completion, prevents overlapping runs for one source, and queues at most one forced follow-up while a collector is active.

### `area:structured-adapters`

- Owns bounded no-shell command execution, typed parser outcomes, source-specific polling/backoff, and conversion from effect results into pure source state.
- Start at `porkyhud.py::CommandResult`, `porkyhud.py::AdapterResult`, `porkyhud.py::run_structured_command`, `porkyhud.py::collect_memory_pressure`, `porkyhud.py::collect_os_thermal_state`, `porkyhud.py::collect_disk_capacity`, `porkyhud.py::SourcePoller`, and `porkyhud.py::LiveHealthCoordinator`.
- Primary tests: `tests/test_porkyhud.py::StructuredCommandTests`, `tests/test_porkyhud.py::DecisionAdapterTests`, `tests/test_porkyhud.py::SourcePollerTests`, and `tests/test_porkyhud.py::LiveOrchestrationTests`.
- Structured results keep outcome and failure class distinct. Raw argv, stderr, private output, PIDs, and monotonic timestamps do not cross into health facts or user-facing errors.

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

- Owns process-command redaction, identity-first process display, sanitized source-state copying, and the legacy snapshot read projection.
- Start at `porkyhud.py::sanitize_command`, `porkyhud.py::visible_command`, `porkyhud.py::_sanitized_process_state`, and `porkyhud.py::legacy_read_projection`.
- Primary tests: `tests/test_porkyhud.py::CommandPrivacyTests` plus snapshot tests.
- Redaction is an output-boundary invariant. Raw `ProcessRow.command` values exist in memory, so every new output path must sanitize them explicitly.

### `area:layout-rendering`

- Owns curses-safe writes, drawing primitives, responsive sizing, panels, help, colors, input handling, and live orchestration.
- Start at `porkyhud.py::safe_add`, `porkyhud.py::decision_header_lines`, `porkyhud.py::evidence_ribbon_lines`, `porkyhud.py::draw_initial_shell`, `porkyhud.py::draw_decision_header`, `porkyhud.py::draw_evidence_ribbon`, `porkyhud.py::draw_mode_body`, `porkyhud.py::draw_help_overlay`, and `porkyhud.py::hud`.
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
- The wheel contains only `porkyhud.py` and `porkyhud_core.py` plus distribution metadata. Tests and repository tools stay outside the runtime package.
- A downstream Homebrew formula must install both production modules and test an actual import and launch from the installed prefix. A version-only formula update that copies only `porkyhud.py` is incomplete.

### `area:repository-tools`

- Owns mechanical documentation freshness, fixed synthetic media generation, and bounded host-validation measurements.
- Start at `tools/validate_docs.py::tracked_files`, `tools/validate_docs.py::validate_symbol_anchors`, `tools/render_demo_gif.py::render`, and `tools/measure_release.py::build_parser`.
- Primary tests: `tests/test_repository_tools.py::RepositoryValidatorTests`, `tests/test_repository_tools.py::DemoRendererTests`, and `tests/test_repository_tools.py::MeasurementHarnessTests`.
- The demo is fixed synthetic presentation, never runtime or hardware evidence. Chrome/Chromium and ffmpeg are development-only renderer dependencies.
- Measurement JSON may contain allowlisted model/chip/RAM/macOS/architecture/Python facts and aggregate statistics; it must not contain hostname, user/home paths, raw argv, command/process text, PIDs, or PTY content. Task 15 owns physical measurements and claims.

## Execution flows

### Live dashboard

```text
main
  → terminal capability and palette setup
  → curses.wrapper(hud)
  → effect-free CHECKING shell before discovery
  → independent SourcePollers for CPU, memory pressure, OS thermal, disk capacity, and process evidence
  → pure reduce_health policy state and immutable HealthFrame
  → independent descriptive AsyncPollers for memory detail, swap, network, battery, optional sensors, and disk activity
  → decision header, core-evidence ribbon, responsive mode body, and status/help output
  → terminal and signal restoration on every exit path
```

Each decision source is single-flight and publishes atomically. A failed latest attempt immediately removes decision eligibility while retaining an aged last-good value for display. Process evidence is independent and can only explain sustained CPU pressure. Descriptive pollers cannot change severity or core readiness.

### Snapshot mode

```text
main --json/--snapshot
  → collect_snapshot
  → bounded concurrent typed attempts for the four core sources and process evidence
  → independent best-effort static, battery, optional-sensor, disk-activity, and AGX collection
  → snapshot-origin health frame (no fabricated live CPU history)
  → command sanitization and legacy unversioned projection
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
| Change a health threshold, precedence, or recovery rule | `porkyhud_core.py::reduce_health` and the owning latch/window reducer | Core boundary/overlap tests, presentation reason/evidence tests, README health semantics, approved spec |
| Add or change a decision-driving command source | `porkyhud.py::run_structured_command`, typed parser/adapter, and `SourcePoller` registry | Pure source/fact contract, failure matrix, privacy/cleanup tests, live and snapshot wiring, README source semantics |
| Add or change a layout/panel | `LAYOUTS`, sizing helpers, `draw_*`, and `hud` | In-app help, README shortcuts, narrow and wide layout tests |
| Change process output | `collect_process_rows`, sorting, and `draw_process_panel` | `sanitize_command`, snapshot projection, privacy tests, README privacy statement |
| Change snapshot JSON | Models and `porkyhud.py::collect_snapshot` | Serialization/redaction, schema assertions, text projection, README integration wording, compatibility decision |
| Change palette, terminal, or signal behavior | `PorkyHUD.command` and `area:terminal-lifecycle` | `NO_COLOR`, all exit paths, signal tests, README launch guidance |
| Cut a release | `pyproject.toml` and `porkyhud.py::VERSION` | Full CI, tag/release state, README install claims, external Homebrew formula installation of both production modules, and installed import/launch test |
| Add, move, or remove a tracked file | The file itself | Complete tracked-file map, relevant area/route, local links, CI if validation changes |
| Introduce MCP or another machine consumer | `area:snapshot-interface` and collectors | Schema/versioning, privacy, read-only authority, evidence provenance, tests, new adapter map and routes |
| Change repository navigation or documentation mechanics | `tools/validate_docs.py` and `AGENTS.md` | Repository-tool tests, CI validation, complete map, anchor/link/control contracts |
| Change the illustrative demo | `tools/render_demo_gif.py` | Renderer safety tests, README labeling/dependencies, regenerated GIF, representative-frame inspection |
| Record release measurements | `tools/measure_release.py` and Task 15 controller | Repository-tool tests, exact environment/sample counts, sanitized JSON, spec validation record; never sensor setup/removal |

## Load-bearing invariants

- Runtime remains dependency-free beyond the Python standard library.
- `porkyhud_core.py` remains pure and effect-free; only its reducer establishes global health severity.
- A global `NORMAL` requires all four fresh recognized core domains and the complete CPU window. A fresh warning may be `DEGRADED`; missing evidence never becomes nominal.
- Descriptive RAM percentage, swap, compression, temperature, fan, power, GPU, disk activity, IOPS, network, and process metrics never drive severity.
- Slow collectors stay off the render loop. One slow or failed source must not freeze unrelated UI updates.
- Pollers remain single-flight and retain the last good sample after a collector error.
- Missing or inaccessible data preserves its typed state (`CHECKING`, `STALE`, `LOCKED`, `PERMISSION DENIED`, `UNSUPPORTED`, `NOT APPLICABLE`, `NOT EXPOSED`, or `ERROR`). Never invent a reading or collapse distinct failures.
- Structured commands use no shell, force bounded output/time, and terminate and reap their whole child process group on every timeout, overflow, cancellation, and interruption path.
- The sudoers rule grants only the exact bounded `powermetrics` commands PorkyHUD generates and uses.
- Known secret-bearing process arguments are redacted from every human-readable or machine-readable output.
- Terminal colors and installed signal handlers are restored on normal exit, exceptions, interrupts, and handled termination signals.
- `NO_COLOR` bypasses both OSC palette changes and curses color-pair use.
- Responsive layout math keeps panels within their assigned rectangles and preserves panel borders and footers.
- Snapshot output stays JSON-serializable. Any compatibility promise requires an explicit schema version and tests.
- Snapshots include hostname, PIDs, and sanitized-but-not-guaranteed-private commands; known secret flags remain redacted at every output boundary.
- Motion starts off; hue and animation never carry severity, freshness, or selection without text.
- Demo frames are visibly fixed synthetic data. Repository tools are excluded from the wheel and may not broaden runtime dependencies.
- User-facing claims distinguish verified behavior from hardware-dependent best effort.
- Empty static-analysis results are not proof of no impact. Confirm dynamic, native, subprocess, string-based, and external relationships from a second source.

## Validation

Run focused tests while editing, then run the same core checks as CI:

```bash
python3 -m py_compile porkyhud.py porkyhud_core.py \
  tools/validate_docs.py tools/render_demo_gif.py tools/measure_release.py \
  tests/test_repository_tools.py
python3 -W error -m unittest discover -v
zsh -n PorkyHUD.command
python3 tools/validate_docs.py
python3 -m pip wheel --no-deps --wheel-dir dist .
```

Also run:

```bash
git diff --check
./porkyhud.py --help
./porkyhud.py --version
```

Inspect the wheel as a ZIP, require both production modules, reject `tools/` and `tests/`, install it to a fresh temporary target, and import both modules from outside the checkout. Use `tools/measure_release.py` only for the explicit Task 15 batches; passing mocked tests is not physical or performance evidence.

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

1. Run `python3 tools/validate_docs.py`; its Git inventory must exactly match the complete file map.
2. Resolve every `path::symbol` anchor against the current AST or declaration.
3. Check every tracked Markdown local link, including decoded traversal, exact case, tracking, and symlink containment.
4. Search for duplicate user-facing claims in README, launcher copy, module docstrings, argparse help, and in-app help.
5. Run the complete validation set and inspect package contents.
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

MCP is not implemented. When it is introduced, begin with read-only resources and tools over the pure health/source boundary and a stabilized, versioned adapter projection rather than importing curses or calling `hud`.

Use stable IDs such as `area:health-policy`, `area:layout-rendering`, and `python:porkyhud:hud`. Return typed source outcome/failure/freshness, evidence anchors, and explicit complete/truncated/unknown/stale states. Compute symbol spans, references, and changed areas from source and git rather than copying them into prose. Reuse `tools/validate_docs.py` inventory/map/anchor rules in CI and MCP so human and machine navigation cannot silently diverge.

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
