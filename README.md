# PorkyHUD

PorkyHUD is a dependency-free terminal dashboard for Apple Silicon Macs. Watch CPU, memory, temperatures, battery, storage, network traffic, and active processes together, then explore your devices, network settings, and TCP connections. Its health summary uses four specific checks: sustained CPU use, macOS memory pressure, macOS thermal warnings, and writable storage headroom.

<p align="center">
  <img src="assets/porkyhud-demo.gif" alt="PorkyHUD dashboard with fixed synthetic readings" width="960">
</p>

<p align="center">
  <sub>Illustrative demo using fixed synthetic data.</sub>
</p>

## Install

Homebrew 6 trusts third-party taps explicitly:

```bash
brew trust --tap Mengjiehasaheart/porkyhud
brew tap Mengjiehasaheart/porkyhud
brew install porkyhud
porkyhud
```

The latest published Homebrew formula may lag this source checkout. The checkout
instructions below match the behavior documented on this page; verify the formula's
published version before expecting the features shown here.

Installed packages from version 0.2.1 also provide `porkhud` as an alias for `porkyhud`.

Optional one-time advanced sensor setup:

```bash
porkyhud --setup-sensors
```

This asks for an administrator password and installs `/etc/sudoers.d/porkyhud`. The rule permits admin users to run only the bounded `powermetrics` sample commands that PorkyHUD generates. Check or remove it with:

```bash
porkyhud --sensor-access-status
porkyhud --remove-sensor-access
```

From a checkout, double-click `PorkyHUD.command` or run:

```bash
./PorkyHUD.command
```

The launcher and live dashboard normally apply a dark terminal palette. To preserve the terminal's foreground, background, and cursor colors:

```bash
NO_COLOR=1 porkyhud
```

## Make it yours

Overview puts a shaded, gently sculpted pig beside your Mac's key readings, with memory, storage, I/O, and top processes below. Larger terminals add CPU core activity and dedicated thermal panels. The default Graphite palette is restrained; `t` cycles four palettes and `s` switches between sculpted, outline, and ASCII artwork. The mascot uses eight Braille dots per terminal cell for depth, light, and small facial details.

A launch flourish lasts less than a second while collectors start independently. Any key skips it; `--no-intro` or explicit `--motion off` disables it. Continuous dashboard motion starts off. Enable calm motion with `a`, or choose a presentation when launching:

```bash
porkyhud --mode cinema --motion calm --theme graphite
porkyhud --style outline
porkyhud --style ascii --no-intro
```

Fonts and text size belong to your terminal profile. Use a monospace font with Braille support for the sculpted style; ASCII is available when its glyphs do not render well. PorkyHUD does not change your font preferences.

## Explore your Mac

Press `Tab` to move to the next view, `Shift+Tab` to go back, or `d` to open the view menu. The footer shows available views and relevant tools. Use arrows and Enter in the menu, or select a view directly:

| Key | View | What it reports |
| --- | --- | --- |
| `7` | Insights | Fresh recent CPU sample mean, peak, count and observed span; compression, decompression, swap-in and swap-out page rates. |
| `8` | Devices | Reported USB and Thunderbolt topology, device/vendor identifiers, speed and port/location where exposed, and connected displays. |
| `9` | Network | Interface link state, hardware port, medium, MTU, IPv4/IPv6 and MAC addresses, IPv4 default route, and configured DNS server addresses. |
| `0` | Connections | Observed numeric TCP listeners and peers, connection state, and receive/send queue sizes in bytes. |
| `i` | This Mac | Mac hardware, macOS version, uptime, battery status and details already collected by the dashboard. |

Press `/` to search processes or Devices, Network, and Connections. Type a query, edit with Backspace or `Ctrl+U`, and press Enter to keep the filter. `Esc` clears it. Searches ignore case and require every word to match. Process searches cover names, IDs, state, and sanitized commands; inventory searches cover each record's displayed fields. Each view remembers its own query when you switch away. In Connections, `f` cycles all observed TCP sockets, listeners, established connections, and other states.

Scroll long lists with arrows, PageUp/PageDown, or Home/End. `r` refreshes the selected view and live readings. Discovery runs only while its view is selected: connections refresh every 10 seconds; devices and network every 30 seconds. The dashboard stays responsive during discovery.

These are local, read-only observations. PorkyHUD does not scan remote hosts, identify a service from its port, test Internet reachability, or change devices, routes or firewall rules. Link speed and topology appear only when macOS exposes them. TCP visibility can be partial, especially for protected processes; lists cap at 128 records and explicitly show truncation. A displayed peer is an observed socket endpoint, not proof of a server's identity or availability.

Use the same views from scripts or a terminal without curses:

```bash
porkyhud --inspect devices
porkyhud --inspect network --search en0 --json
porkyhud --inspect connections --state listeners --json
porkyhud --mode processes --search Safari
porkyhud --state established
porkyhud --mode insights
porkyhud --mode system
```

`--search TEXT` starts with a search filter and also works with `--inspect`. `--state all|listeners|established|other` filters TCP connections: it selects that live view unless an incompatible `--mode` is explicitly supplied, and works with `--inspect connections`. Snapshot reports without `--inspect` reject these filters.

Inventory JSON is unversioned. `--inspect` chooses the report; `--json` chooses its format. The report includes a `summary` of the original returned records and a `filter` section with the query, selected TCP group, and matching/returned counts; completeness and truncation still describe collection. Sensor setup/status/removal flags retain precedence over reports. Device serial numbers and process commands are omitted from inventories, but friendly device names, IP/MAC addresses and remote endpoints can be sensitive. Review reports before sharing them.

## Understanding status

The headline shows health (`NORMAL`, `WATCH`, `CRITICAL`, or `UNKNOWN`) and whether the required readings are current (`LIVE`, `DEGRADED`, `INCOMPLETE`, or `DELAYED`). `CHECKING` appears while readings or the initial 15-second CPU baseline are still being collected.

The four health inputs are deliberately narrow:

- CPU: a complete 15-second aggregate Mach CPU window without excessive gaps; sustained use of at least 90% produces `WATCH`.
- Memory: the recognized macOS memory-pressure value; `2` produces `WATCH` and `4` is the only input that can produce `CRITICAL`.
- Thermal: an explicit macOS thermal or performance warning produces `WATCH`.
- Storage: the selected writable Data volume, or writable root fallback, produces `WATCH` only when free space is both below 10% and below 20 GiB.

RAM percentage, swap allocation, compression, temperatures, fan RPM, watts, GPU use, disk transfer, IOPS, network rate, and process activity are descriptive. They provide context but never drive severity.

Status labels describe what PorkyHUD can currently report:

| Label | Meaning |
| --- | --- |
| `CHECKING` | The initial attempt, retry, or required history is still in progress. |
| `LIVE` | Every required reading is recognized and current, with enough CPU history. |
| `DEGRADED` | A fresh warning is supported while another core input is incomplete. |
| `INCOMPLETE` | A required reading never arrived, is unsupported/not applicable, is unrecognized, or lacks enough history. |
| `DELAYED` | A previously valid required reading is now too old or its latest refresh failed. |
| `STALE` | A retained last-good value is older than its source freshness budget. |
| `LOCKED` | Only the optional advanced-sensor path needs credentials that `u` can temporarily unlock. |
| `PERMISSION DENIED` | A different source proved an access denial; no unrelated unlock is offered. |
| `UNSUPPORTED` | The capability is affirmatively unavailable on this environment. |
| `NOT APPLICABLE` | The reading does not apply, such as battery details on a confirmed batteryless Mac. |
| `NOT EXPOSED` | A valid response contains no applicable optional field. |
| `ERROR` | A bounded command, decoder, or parser failed; PorkyHUD does not guess. |

Missing readings prevent `NORMAL` but cannot hide a confirmed warning. Previous good readings remain visible with their age and current status.

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `1`–`5` | Select Overview, Compute, Thermals, I/O, or Processes directly. |
| `6` | Select Cinema when the terminal is at least 118×34. |
| `Tab` / `Shift+Tab` | Move to the next or previous supported view. |
| `d` | Open or close the view menu; arrows and Enter select a view. |
| `7` / `8` / `9` / `0` | Select Insights, Devices, Network, or Connections. |
| `i` | Select This Mac. |
| `/` | Search in Overview, Compute, Processes, Cinema, Devices, Network, or Connections. |
| `Enter` / `Backspace` / `Ctrl+U` | Keep a search filter, erase one character, or clear the text while editing. |
| `f` | Cycle connection filters: all, listeners, established, other. |
| `l` | Cycle only modes supported by the current terminal size. |
| `h` or `?` | Show or hide current shortcut help. |
| `t` | Cycle the visual theme. |
| `s` | Cycle sculpted, outline, or ASCII artwork. |
| `a` | Cycle continuous motion off → calm → vivid; it defaults to off. |
| `u` | Attempt a temporary advanced-sensor credential unlock when that source is `LOCKED`. |
| `m` | Cycle process sorting by CPU, memory, or GPU. |
| `r` | Refresh live telemetry and the selected inventory; reset core retry backoff. |
| `Up` / `Down` or `j` / `k` | Scroll processes or inventory; select a menu item. |
| `PageUp` / `PageDown` | Scroll processes or inventory by a page. |
| `Home` / `End` | Jump to the first or last process or inventory page. |
| `Esc` | Close help/menu, clear a search or TCP filter, or quit when none are active. |
| `q` | Quit. |

Cinema uses the same health checks in an optional wide presentation. Resizing below its threshold returns to a supported view. While editing a search, letters and number keys enter text instead of activating shortcuts.

## Snapshot interface and privacy

For diagnostics and future adapters:

```bash
./porkyhud.py --snapshot
./porkyhud.py --json
```

The snapshot schema is currently unversioned and is not a stable external API. It includes the machine hostname, process IDs, and sanitized process commands. Known secret-bearing flag values are redacted, but arbitrary positional arguments and unknown flag conventions may remain: process commands are sanitized, not guaranteed private. Review output before sharing it.

Snapshot collection is separate from the live HUD and cannot complete the sustained CPU history required for a global `NORMAL` conclusion. Raw command stderr and privileged command details are not included. The `memory_activity` section contains recognized cumulative VM counters with explicit page units; rates are `null` with `LIVE_ONLY` status because a snapshot has no live delta window.

## Advanced sensor access

The four health checks work as a normal user. PorkyHUD reads AppleSMC directly for read-only fan and temperature keys when macOS exposes them. Deeper CPU/GPU power, die temperature, and residency counters may require the bounded `powermetrics` access described above.

Inside the dashboard, `u` performs only a temporary sudo credential refresh; it does not install the durable rule. If access is available but the model exposes no applicable field, the UI reports `NOT EXPOSED` instead of inventing a reading. Optional sensors never affect global severity or readiness.

## Compatibility and validation

- Target: Apple Silicon on macOS, with Python 3.9 through 3.14 tested in CI.
- Historical Phase 1 release-candidate record: deterministic hard gates, startup hard gates, snapshot/RSS/launch targets, timeout cleanup, a 30-minute no-growth soak, and the raw-PTY interaction matrix passed on one Mac17,6 / Apple M5 Max running macOS 26.6. Instrumented live CPU averaged 63.473% of one core and missed the scoped <=2% target. Physical Terminal.app visual/profile contrast, other terminal emulators, privileged sensors, and broad device coverage remain unverified.
- Fixture-validated, hardware-unverified: deterministic tests cover the changed memory-pressure, thermal, CPU-window, disk, process-attribution, responsive-layout, terminal, battery-present, and affirmed batteryless boundaries.
- Best effort: untested Apple Silicon devices and earlier macOS releases.
- Outside verified support: Intel Macs remain unverified until Intel-specific tests and hardware checks exist.

A fixture can prevent a known regression; it cannot prove that a physical model works. M1–M5 device breadth, fanless/one-fan/multi-fan and Ultra/multi-die systems, partial sensors, sleep/wake, UPS behavior, and broad terminal/device qualification remain hardware-unverified unless a release record says otherwise.

Python 3.9 is upstream end-of-life. PorkyHUD's compatibility tests do not restore Python security maintenance; prefer a currently supported Python release.

The 0.2 series has focused current-host checks for VM rates and read-only device, network and TCP discovery. These checks do not replace the historical performance record or qualify other hardware and terminal profiles.

## Metric notes

- Insights use actual collector completion times. VM page rates require two complete samples and reset after failures, counter/page-size changes, or collection gaps. Pages/s describes VM events, not physical disk bytes/s; CPU sample means are descriptive observations, separate from the policy’s time-weighted window. Apple documents [system VM statistics](https://developer.apple.com/library/archive/documentation/Performance/Conceptual/ManagingMemory/Articles/VMPages.html) and the [compression/swap counter definitions](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/mach/vm_statistics.h).
- Network rates include measured idle zero and reset after counter reversal or a collection gap; failed probes do not manufacture idle readings.
- RAM follows an Activity Monitor-style split: app + wired + compressed count as used, while file-backed cache remains available cache.
- Disk capacity selects the writable APFS Data volume where discoverable; disk activity runs on its own bounded background cadence.
- CPU workers come from macOS Mach counters; Apple Silicon cluster labels use reported IORegistry and `hw.perflevel*` topology.
- Process GPU attribution uses AGX accumulated-time deltas only when a complete, identity-safe sample proves a value; unavailable attribution is not shown as zero.
- Pollers are single-flight, keep a last-good sample after failure, and do not let one slow source stop unrelated updates.

## Reproducing the illustrative demo

The tracked renderer accepts only fixed synthetic content and a repository-contained GIF output. It requires Chrome or Chromium and `ffmpeg` as development tools, and reuses the pure mascot renderer; neither is a PorkyHUD runtime dependency.

```bash
python3 tools/render_demo_gif.py --frames 36 --fps 12
file assets/porkyhud-demo.gif
```

The output is exactly 960 pixels wide and every frame visibly identifies itself as synthetic illustrative data.

## Requirements

- Apple Silicon Mac target; other Mac hardware follows the compatibility limits above
- Python 3.9 or newer (`python3`)
- No third-party Python runtime packages

For a source checkout:

```bash
chmod +x PorkyHUD.command porkyhud.py
```

<p align="center">
  <sub><em>Visit <a href="https://drmatchastudio.com">DMS</a>.</em></sub>
</p>
