# PorkyHUD

PorkyHUD is a portable, dependency free macOS terminal dashboard for system status.


## Run

Double-click:

```text
PorkyHUD.command
```

From Terminal:

```bash
./PorkyHUD.command
```

## Keyboard Shortcuts

| Key | Action |
| --- | --- |
| `h` or `?` | Show or hide shortcut help |
| `t` | Cycle visual theme |
| `a` | Cycle animation mode: off, calm, vivid |
| `u` | Retry advanced sensor unlock with `sudo` |
| `m` | Toggle process sort by CPU or memory |
| `r` | Rescan system and sensor state |
| `Up` / `Down` or `j` / `k` | Scroll process list |
| `PageUp` / `PageDown` | Fast scroll process list |
| `q` or `Esc` | Quit |

## macOS Sensor Access

Most PorkyHUD data is available to a normal user account. Modern macOS protects deeper CPU/GPU power, die temperature, and fan-style sensor data behind administrator-only tools such as `powermetrics`.

PorkyHUD handles this in two ways:

- At launch, it can ask whether to unlock advanced sensors with `sudo`.
- Inside the dashboard, press `u` to try the unlock again.

If admin access has not been granted, PorkyHUD shows one unlock hint. If admin access is available but the Mac model still does not publish a value, PorkyHUD hides that field instead of inventing a reading.

## Requirements

- macOS
- Python 3 available as `python3`
- No Python packages required


```bash
chmod +x PorkyHUD.command porkyhud.py
```
