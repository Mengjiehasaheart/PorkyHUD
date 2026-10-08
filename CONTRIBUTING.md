# Contributing to PorkyHUD

PorkyHUD uses only the Python standard library at runtime. The existing flat
modules preserve installed entry points; optional professional tools live in
`porkyhud_tools/`, separated by responsibility.

## Development

```bash
python3 -m py_compile porkyhud*.py porkyhud_tools/*.py tools/*.py
python3 -W error -m unittest discover -v
zsh -n PorkyHUD.command
python3 tools/validate_docs.py
python3 -m pip wheel --no-deps --wheel-dir dist .
git diff --check
./porkyhud.py --help
./porkyhud.py --version
```

Inspect the wheel as a ZIP. Require all five flat production modules and the
`porkyhud_tools` package; exclude tests, repository tools, local agent guidance,
and design/planning records. Install into a temporary target and import from
outside the checkout. Python 3.9–3.14 run in macOS CI.

## Architecture

- `porkyhud_core.py` establishes global health severity and stays effect-free.
- Existing collectors publish typed samples independently of presentation.
- Optional collectors accept a bounded, cancellation-aware command runner.
- `ToolReport` is the shared CLI/HUD output contract; tool failures remain explicit.
- `bridge.py` projects cached readings into v1 frames with units, status, age and revision.
- Session recording consumes frames, with bounded memory and one bounded writer queue.
- Replay opens no collectors. Comparisons use observed samples and preserve gaps.
- Profiles save presentation preferences. Personal watches never alter health rules.
- Collectors and pure calculations do not import the dashboard or curses.

Keep each optional feature behind its service interface. Share models and data,
keep slow work off the render loop, and avoid speculative plugin frameworks.
When changing CLI, keyboard controls, output fields or behavior, update README
and focused tests together. Update the table below whenever tracked files change.

## Reading and file contracts

Missing or failed readings remain unavailable, never invented zero. Process
identity combines PID with reported start time; `ps` start time has second
precision. Summed workload RSS can count shared pages more than once. Folder
scans report logical bytes and are bounded between filesystem operations;
filesystem calls themselves can stall. APFS volumes share container capacity.

New session, support and tool reports use schema version 1. Additive optional
fields may be introduced within v1; incompatible field meanings require a new
version. Readers reject unsupported versions and malformed/nonfinite values.
Private-default exports omit host names, commands, process rows, paths and
addresses. Detailed selected inspectors can display local names/endpoints.

## Validation limits

Fixture tests verify contracts, cleanup and known parser forms. They do not
qualify all Mac generations, external devices, terminal fonts, protected process
coverage or sensor permissions. Test new native sources on representative hosts
and keep support claims scoped. Never install or remove sensor privileges in tests.

The release measurement tool remains a development utility. Historical measured
results summarized in README describe their original build; measure fresh
baselines before making claims about current overhead.

## Complete tracked-file map

| Path | Responsibility | Change with |
| --- | --- | --- |
| `.github/workflows/ci.yml` | macOS Python compatibility, build and validation | Relevant implementation, tests and user documentation |
| `.gitignore` | Local guidance, caches, builds and recording exclusions | Relevant implementation, tests and user documentation |
| `CONTRIBUTING.md` | Development guide, architecture, file map and validation | Relevant implementation, tests and user documentation |
| `LICENSE` | MIT license | Relevant implementation, tests and user documentation |
| `PorkyHUD.command` | macOS source/installed launcher and terminal lifecycle | Relevant implementation, tests and user documentation |
| `README.md` | Installation, controls, privacy, supported readings and workflows | Relevant implementation, tests and user documentation |
| `assets/porkyhud-demo.gif` | Fixed synthetic dashboard demonstration | Relevant implementation, tests and user documentation |
| `porkyhud.py` | CLI entry point, macOS effects and dashboard orchestration | Relevant implementation, tests and user documentation |
| `porkyhud_core.py` | Pure immutable health and source-state policy | Relevant implementation, tests and user documentation |
| `porkyhud_insights.py` | Pure descriptive CPU and VM calculations | Relevant implementation, tests and user documentation |
| `porkyhud_inventory.py` | Read-only device, network and TCP inventory adapters | Relevant implementation, tests and user documentation |
| `porkyhud_tools/__init__.py` | Optional tools package boundary | Relevant implementation, tests and user documentation |
| `porkyhud_tools/bridge.py` | Projection of existing readings into v1 observation frames | Relevant implementation, tests and user documentation |
| `porkyhud_tools/cli.py` | Tool, session and workspace command-line adapter | Relevant implementation, tests and user documentation |
| `porkyhud_tools/controller.py` | HUD tool state, cancellation and background workflows | Relevant implementation, tests and user documentation |
| `porkyhud_tools/model.py` | Shared report contract and display-safe formatting | Relevant implementation, tests and user documentation |
| `porkyhud_tools/network.py` | Port owners, explicit network tests and interface counters | Relevant implementation, tests and user documentation |
| `porkyhud_tools/observations.py` | Pure observation, inventory-change and replay projections | Relevant implementation, tests and user documentation |
| `porkyhud_tools/power.py` | Sleep requests and observed power timelines | Relevant implementation, tests and user documentation |
| `porkyhud_tools/processes.py` | Selected process details, workload grouping and job progress | Relevant implementation, tests and user documentation |
| `porkyhud_tools/profiles.py` | Validated workspace persistence and independent personal watches | Relevant implementation, tests and user documentation |
| `porkyhud_tools/service.py` | Optional tool registry and injected adapter composition | Relevant implementation, tests and user documentation |
| `porkyhud_tools/sessions.py` | Bounded recording, replay validation, comparisons and exports | Relevant implementation, tests and user documentation |
| `porkyhud_tools/storage.py` | APFS discovery and scoped cancellable folder scans | Relevant implementation, tests and user documentation |
| `porkyhud_visual.py` | Pure terminal mascot artwork | Relevant implementation, tests and user documentation |
| `pyproject.toml` | Package metadata, runtime modules and console entry points | Relevant implementation, tests and user documentation |
| `tests/__init__.py` | Test discovery package | Test layout |
| `tests/test_core.py` | Focused behavioral contracts for core | Relevant implementation, tests and user documentation |
| `tests/test_insights.py` | Focused behavioral contracts for insights | Relevant implementation, tests and user documentation |
| `tests/test_inventory.py` | Focused behavioral contracts for inventory | Relevant implementation, tests and user documentation |
| `tests/test_network_tools.py` | Focused behavioral contracts for network tools | Relevant implementation, tests and user documentation |
| `tests/test_performance.py` | Focused behavioral contracts for performance | Relevant implementation, tests and user documentation |
| `tests/test_porkyhud.py` | Focused behavioral contracts for porkyhud | Relevant implementation, tests and user documentation |
| `tests/test_profiles.py` | Focused behavioral contracts for profiles | Relevant implementation, tests and user documentation |
| `tests/test_repository_tools.py` | Focused behavioral contracts for repository tools | Relevant implementation, tests and user documentation |
| `tests/test_sessions.py` | Focused behavioral contracts for sessions | Relevant implementation, tests and user documentation |
| `tests/test_upgrade.py` | Focused behavioral contracts for upgrade | Relevant implementation, tests and user documentation |
| `tests/test_visual.py` | Focused behavioral contracts for visual | Relevant implementation, tests and user documentation |
| `tests/test_workbench_inspectors.py` | Focused behavioral contracts for workbench inspectors | Relevant implementation, tests and user documentation |
| `tests/test_workbench_ui.py` | Focused behavioral contracts for workbench ui | Relevant implementation, tests and user documentation |
| `tools/measure_release.py` | Bounded aggregate release measurements; excludes private output | Relevant implementation, tests and user documentation |
| `tools/render_demo_gif.py` | Fixed synthetic demo generator; Chrome/ffmpeg development tools | Relevant implementation, tests and user documentation |
| `tools/validate_docs.py` | Tracked map, AST anchors, local links and controls validator | Relevant implementation, tests and user documentation |
| `tests/test_tool_bridge.py` | Bridge, shared observation and headless cleanup contracts | Relevant implementation, tests and user documentation |
