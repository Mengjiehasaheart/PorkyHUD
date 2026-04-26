#!/usr/bin/env python3
"""
PorkyHUD: a dependency-free macOS terminal system monitor.

Double-click PorkyHUD.command to run it in Terminal.
Keys: q quit, m sort process list by CPU/MEM, arrows/page keys scroll.
"""

from __future__ import annotations

import curses
import ctypes
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any


REFRESH_SECONDS = 1.0
PROCESS_REFRESH_SECONDS = 2.5
SENSOR_REFRESH_SECONDS = 12.0
COPYRIGHT_TEXT = "Copyright (c) DMS"
THEMES = [
    {
        "name": "Aurora",
        "colors": {
            1: curses.COLOR_WHITE,
            2: curses.COLOR_CYAN,
            3: curses.COLOR_YELLOW,
            4: curses.COLOR_RED,
            5: curses.COLOR_BLUE,
            6: curses.COLOR_GREEN,
            7: curses.COLOR_MAGENTA,
        },
    },
    {
        "name": "Amber",
        "colors": {
            1: curses.COLOR_WHITE,
            2: curses.COLOR_YELLOW,
            3: curses.COLOR_CYAN,
            4: curses.COLOR_RED,
            5: curses.COLOR_YELLOW,
            6: curses.COLOR_GREEN,
            7: curses.COLOR_WHITE,
        },
    },
    {
        "name": "Violet",
        "colors": {
            1: curses.COLOR_WHITE,
            2: curses.COLOR_MAGENTA,
            3: curses.COLOR_YELLOW,
            4: curses.COLOR_RED,
            5: curses.COLOR_BLUE,
            6: curses.COLOR_CYAN,
            7: curses.COLOR_MAGENTA,
        },
    },
    {
        "name": "Mono",
        "colors": {
            1: curses.COLOR_WHITE,
            2: curses.COLOR_WHITE,
            3: curses.COLOR_WHITE,
            4: curses.COLOR_RED,
            5: curses.COLOR_WHITE,
            6: curses.COLOR_WHITE,
            7: curses.COLOR_WHITE,
        },
    },
]


@dataclass
class HudConfig:
    theme_index: int = 0
    animation_mode: int = 1
    show_help: bool = False
    message: str = ""
    message_until: float = 0.0

    @property
    def theme_name(self) -> str:
        return THEMES[self.theme_index % len(THEMES)]["name"]


@dataclass
class ProcessRow:
    pid: int
    cpu: float
    mem: float
    stat: str
    etime: str
    command: str


@dataclass
class BatteryInfo:
    percent: int | None
    source: str
    state: str
    remaining: str
    external_connected: bool
    is_charging: bool
    fully_charged: bool
    cycle_count: int | None
    design_cycles: int | None
    health_percent: int | None
    temp_c: float | None
    virtual_temp_c: float | None
    voltage_v: float | None
    amperage_a: float | None
    charger_watts: int | None
    charger_name: str


@dataclass
class SensorInfo:
    thermal_warning: str
    performance_warning: str
    cpu_power_w: float | None
    gpu_power_w: float | None
    cpu_temp_c: float | None
    gpu_temp_c: float | None
    fan_rpm: int | None
    privileged_locked: bool
    raw_hint: str


def run_command(args: list[str], timeout: float = 2.0) -> str:
    try:
        return subprocess.check_output(
            args,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).strip()
    except Exception:
        return ""


def sudo_cached() -> bool:
    return subprocess.run(
        ["sudo", "-n", "-v"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def unlock_privileged_sensors(screen: curses.window) -> bool:
    curses.def_prog_mode()
    curses.endwin()
    print()
    print("PorkyHUD advanced sensor unlock")
    print("macOS requires an administrator password for powermetrics sensor data.")
    print("This may unlock CPU/GPU power data when your Mac exposes it.")
    print()
    result = subprocess.run(["sudo", "-v"])
    print()
    if result.returncode == 0:
        print("Advanced sensor session unlocked. Returning to PorkyHUD...")
    else:
        print("Sensor unlock skipped or failed. Returning to PorkyHUD...")
    time.sleep(1.2)
    curses.reset_prog_mode()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.nodelay(True)
    screen.refresh()
    return result.returncode == 0


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def human_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            return f"{size:3.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024.0
    return f"{size:.1f}PB"


def format_uptime(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def visible_command(command: str, max_width: int) -> str:
    command = command.replace(os.path.expanduser("~"), "~")
    command = re.sub(r"\s+", " ", command).strip()
    if len(command) <= max_width:
        return command
    return "..." + command[-max(0, max_width - 3) :]


def format_temp(value: float | None) -> str:
    if value is None:
        return "locked"
    return f"{value:4.1f}C"


def format_watts(value: float | None) -> str:
    if value is None:
        return "locked"
    if value < 1:
        return f"{value * 1000:4.0f}mW"
    return f"{value:4.1f}W"


def fan_text(sensor: "SensorInfo") -> str:
    if sensor.fan_rpm is not None:
        return f"{sensor.fan_rpm} rpm"
    return "admin locked" if sensor.privileged_locked else "no RPM exposed"


def format_optional_int(value: int | None, suffix: str = "") -> str:
    if value is None:
        return "locked"
    return f"{value}{suffix}"


def parse_ioreg_int(raw: str, key: str) -> int | None:
    matches = re.findall(rf'"{re.escape(key)}"\s*=\s*(-?\d+)', raw)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def parse_ioreg_bool(raw: str, key: str) -> bool:
    matches = re.findall(rf'"{re.escape(key)}"\s*=\s*(Yes|No)', raw)
    return bool(matches and matches[-1] == "Yes")


def parse_ioreg_string(raw: str, key: str) -> str:
    matches = re.findall(rf'"{re.escape(key)}"\s*=\s*"([^"]*)"', raw)
    return matches[-1] if matches else ""


def apple_battery_temp(raw_value: int | None) -> float | None:
    if raw_value is None:
        return None
    if raw_value > 1000:
        return raw_value / 10.0 - 273.15
    return raw_value / 10.0


def parse_power_value(raw: str, label: str) -> float | None:
    pattern = rf"{re.escape(label)}\s*:\s*([\d.]+)\s*(mW|W)"
    match = re.search(pattern, raw, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value / 1000.0 if unit == "mw" else value


def parse_temp_value(raw: str, pattern: str) -> float | None:
    match = re.search(pattern, raw, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


class MacCpuSampler:
    PROCESSOR_CPU_LOAD_INFO = 2
    CPU_STATE_USER = 0
    CPU_STATE_SYSTEM = 1
    CPU_STATE_IDLE = 2
    CPU_STATE_NICE = 3
    CPU_STATE_MAX = 4

    def __init__(self) -> None:
        self.available = False
        self.previous: list[tuple[int, int, int, int]] | None = None
        try:
            self.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            self.lib.mach_host_self.restype = ctypes.c_uint
            self.lib.mach_task_self.restype = ctypes.c_uint
            self.lib.host_processor_info.argtypes = [
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                ctypes.POINTER(ctypes.c_uint),
            ]
            self.lib.host_processor_info.restype = ctypes.c_int
            self.lib.vm_deallocate.argtypes = [
                ctypes.c_uint,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ]
            self.host = self.lib.mach_host_self()
            self.previous = self._read_raw()
            self.available = bool(self.previous)
        except Exception:
            self.available = False

    def _read_raw(self) -> list[tuple[int, int, int, int]]:
        cpu_count = ctypes.c_uint(0)
        info_count = ctypes.c_uint(0)
        info = ctypes.POINTER(ctypes.c_int)()
        result = self.lib.host_processor_info(
            self.host,
            self.PROCESSOR_CPU_LOAD_INFO,
            ctypes.byref(cpu_count),
            ctypes.byref(info),
            ctypes.byref(info_count),
        )
        if result != 0 or not info:
            return []

        rows: list[tuple[int, int, int, int]] = []
        try:
            for index in range(cpu_count.value):
                offset = index * self.CPU_STATE_MAX
                rows.append(
                    (
                        int(info[offset + self.CPU_STATE_USER]),
                        int(info[offset + self.CPU_STATE_SYSTEM]),
                        int(info[offset + self.CPU_STATE_IDLE]),
                        int(info[offset + self.CPU_STATE_NICE]),
                    )
                )
        finally:
            address = ctypes.cast(info, ctypes.c_void_p).value
            if address:
                self.lib.vm_deallocate(
                    self.lib.mach_task_self(),
                    address,
                    int(info_count.value) * ctypes.sizeof(ctypes.c_int),
                )
        return rows

    def sample(self) -> list[float]:
        if not self.available:
            return []
        current = self._read_raw()
        if not current or not self.previous or len(current) != len(self.previous):
            self.previous = current
            return []

        usage: list[float] = []
        for before, after in zip(self.previous, current):
            deltas = [max(0, after[i] - before[i]) for i in range(4)]
            idle = deltas[self.CPU_STATE_IDLE]
            busy = (
                deltas[self.CPU_STATE_USER]
                + deltas[self.CPU_STATE_SYSTEM]
                + deltas[self.CPU_STATE_NICE]
            )
            total = busy + idle
            usage.append((busy / total * 100.0) if total else 0.0)
        self.previous = current
        return usage


def collect_static_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "host": socket.gethostname().split(".")[0],
        "os": f"macOS {run_command(['sw_vers', '-productVersion'])}",
        "build": run_command(["sw_vers", "-buildVersion"]),
        "kernel": platform.release(),
        "model": run_command(["sysctl", "-n", "hw.model"]),
        "chip": run_command(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "physical_cpu": run_command(["sysctl", "-n", "hw.physicalcpu"]),
        "logical_cpu": run_command(["sysctl", "-n", "hw.logicalcpu"]),
        "memory": "",
        "gpus": [],
    }

    raw = run_command(
        ["system_profiler", "SPHardwareDataType", "SPDisplaysDataType", "-json"],
        timeout=12.0,
    )
    if raw:
        try:
            prof = json.loads(raw)
            hardware = (prof.get("SPHardwareDataType") or [{}])[0]
            info["model"] = hardware.get("machine_name") or info["model"]
            info["model_id"] = hardware.get("machine_model") or ""
            info["chip"] = (
                hardware.get("chip_type")
                or hardware.get("cpu_type")
                or info["chip"]
                or "Unknown CPU"
            )
            info["cpu_cores"] = hardware.get("number_cores") or ""
            info["memory"] = hardware.get("physical_memory") or ""

            gpus: list[dict[str, str]] = []
            for gpu in prof.get("SPDisplaysDataType") or []:
                name = (
                    gpu.get("sppci_model")
                    or gpu.get("_name")
                    or gpu.get("spdisplays_device-id")
                    or "GPU"
                )
                cores = (
                    gpu.get("spdisplays_cores")
                    or gpu.get("spdisplays_core_count")
                    or gpu.get("sppci_cores")
                    or ""
                )
                metal = gpu.get("spdisplays_metalfamily") or gpu.get("spdisplays_metal") or ""
                displays = gpu.get("spdisplays_ndrvs") or []
                display_names = []
                for display in displays if isinstance(displays, list) else []:
                    display_names.append(display.get("_name") or display.get("spdisplays_display_type") or "Display")
                gpus.append(
                    {
                        "name": str(name),
                        "cores": str(cores),
                        "metal": str(metal),
                        "displays": ", ".join(display_names[:3]),
                    }
                )
            info["gpus"] = gpus
        except Exception:
            pass

    if not info["chip"]:
        info["chip"] = "Apple Silicon" if platform.machine() == "arm64" else platform.processor()
    if not info["memory"]:
        total = run_command(["sysctl", "-n", "hw.memsize"])
        info["memory"] = human_bytes(float(total or 0))
    return info


def boot_seconds() -> int:
    raw = run_command(["sysctl", "-n", "kern.boottime"])
    match = re.search(r"sec = (\d+)", raw)
    if not match:
        return 0
    return int(time.time()) - int(match.group(1))


def memory_stats() -> dict[str, float]:
    total_raw = run_command(["sysctl", "-n", "hw.memsize"])
    total = float(total_raw or 0)
    raw = run_command(["vm_stat"], timeout=1.5)
    page_match = re.search(r"page size of (\d+) bytes", raw)
    page_size = int(page_match.group(1)) if page_match else 4096

    pages: dict[str, int] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value_match = re.search(r"([\d.]+)", value.replace(".", ""))
        if value_match:
            pages[key.lower()] = int(value_match.group(1))

    free_pages = pages.get("pages free", 0) + pages.get("pages speculative", 0)
    free = free_pages * page_size
    used = max(0.0, total - free)
    compressed = pages.get("pages occupied by compressor", 0) * page_size
    wired = pages.get("pages wired down", 0) * page_size
    active = pages.get("pages active", 0) * page_size
    inactive = pages.get("pages inactive", 0) * page_size

    swap_raw = run_command(["sysctl", "vm.swapusage"])
    swap_total = swap_used = 0.0
    swap_match = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", swap_raw)
    if swap_match:
        swap_total = float(swap_match.group(1)) * 1024 * 1024
        swap_used = float(swap_match.group(2)) * 1024 * 1024

    return {
        "total": total,
        "used": used,
        "free": free,
        "active": active,
        "inactive": inactive,
        "wired": wired,
        "compressed": compressed,
        "swap_total": swap_total,
        "swap_used": swap_used,
    }


def battery_info() -> BatteryInfo:
    pmset_raw = run_command(["pmset", "-g", "batt"])
    ioreg_raw = run_command(["ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"], timeout=1.5)

    source_match = re.search(r"Now drawing from '([^']+)'", pmset_raw)
    battery_match = re.search(r"(\d+)%;\s*([^;]+);\s*([^;]+);?", pmset_raw)
    percent: int | None = None
    state = "unknown"
    remaining = "unknown"
    if battery_match:
        percent = int(battery_match.group(1))
        state = battery_match.group(2).strip()
        remaining = battery_match.group(3).strip()
    elif pmset_raw:
        remaining = pmset_raw.splitlines()[-1].strip()[:32]

    voltage_mv = parse_ioreg_int(ioreg_raw, "Voltage")
    amperage_ma = parse_ioreg_int(ioreg_raw, "InstantAmperage")
    health = parse_ioreg_int(ioreg_raw, "MaxCapacity")
    charger_watts = parse_ioreg_int(ioreg_raw, "Watts")
    charger_name = parse_ioreg_string(ioreg_raw, "Name")

    return BatteryInfo(
        percent=percent,
        source=source_match.group(1) if source_match else "Power",
        state=state,
        remaining=remaining,
        external_connected=parse_ioreg_bool(ioreg_raw, "ExternalConnected"),
        is_charging=parse_ioreg_bool(ioreg_raw, "IsCharging"),
        fully_charged=parse_ioreg_bool(ioreg_raw, "FullyCharged"),
        cycle_count=parse_ioreg_int(ioreg_raw, "CycleCount"),
        design_cycles=parse_ioreg_int(ioreg_raw, "DesignCycleCount9C"),
        health_percent=health,
        temp_c=apple_battery_temp(parse_ioreg_int(ioreg_raw, "Temperature")),
        virtual_temp_c=apple_battery_temp(parse_ioreg_int(ioreg_raw, "VirtualTemperature")),
        voltage_v=(voltage_mv / 1000.0 if voltage_mv is not None else None),
        amperage_a=(amperage_ma / 1000.0 if amperage_ma is not None else None),
        charger_watts=charger_watts,
        charger_name=charger_name,
    )


def battery_summary(battery: BatteryInfo) -> str:
    percent = "--" if battery.percent is None else f"{battery.percent}%"
    if battery.is_charging:
        state = "charging"
    elif battery.external_connected:
        state = battery.state or "AC"
    else:
        state = "battery"
    return f"{battery.source}: {percent} {state} {battery.remaining}".strip()


def sensor_info() -> SensorInfo:
    therm_raw = run_command(["pmset", "-g", "therm"], timeout=1.5)
    thermal_warning = "nominal"
    performance_warning = "nominal"
    if "No thermal warning" not in therm_raw and therm_raw:
        thermal_warning = "active"
    if "No performance warning" not in therm_raw and therm_raw:
        performance_warning = "active"

    power_raw = run_command(
        [
            "sudo",
            "-n",
            "powermetrics",
            "--samplers",
            "thermal,cpu_power,gpu_power,battery",
            "-n",
            "1",
            "-i",
            "1000",
        ],
        timeout=4.0,
    )
    locked = not bool(power_raw)
    cpu_temp = parse_temp_value(power_raw, r"(?:CPU|processor)[^\n:]*temperature[^\d]*([\d.]+)\s*C")
    gpu_temp = parse_temp_value(power_raw, r"GPU[^\n:]*temperature[^\d]*([\d.]+)\s*C")
    fan_match = re.search(r"fan[^\n]*?(\d{3,5})\s*rpm", power_raw, re.IGNORECASE)
    fan_rpm = int(fan_match.group(1)) if fan_match else None

    pressure_match = re.search(r"thermal pressure\s*:\s*([A-Za-z ]+)", power_raw, re.IGNORECASE)
    if pressure_match:
        thermal_warning = pressure_match.group(1).strip().lower()

    return SensorInfo(
        thermal_warning=thermal_warning,
        performance_warning=performance_warning,
        cpu_power_w=parse_power_value(power_raw, "CPU Power"),
        gpu_power_w=parse_power_value(power_raw, "GPU Power"),
        cpu_temp_c=cpu_temp,
        gpu_temp_c=gpu_temp,
        fan_rpm=fan_rpm,
        privileged_locked=locked,
        raw_hint="sudo powermetrics" if locked else "powermetrics",
    )


def network_bytes() -> tuple[int, int]:
    raw = run_command(["netstat", "-ibn"], timeout=1.5)
    iface_totals: dict[str, tuple[int, int]] = {}
    header: list[str] | None = None
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "Name":
            header = parts
            continue
        if not header or len(parts) < len(header):
            continue
        try:
            ibytes_idx = header.index("Ibytes")
            obytes_idx = header.index("Obytes")
        except ValueError:
            continue
        name = parts[0]
        if name == "lo0":
            continue
        try:
            ibytes = int(parts[ibytes_idx])
            obytes = int(parts[obytes_idx])
        except Exception:
            continue
        previous = iface_totals.get(name, (0, 0))
        iface_totals[name] = (max(previous[0], ibytes), max(previous[1], obytes))
    return (
        sum(value[0] for value in iface_totals.values()),
        sum(value[1] for value in iface_totals.values()),
    )


def process_rows(sort_mode: str) -> list[ProcessRow]:
    raw = run_command(
        ["ps", "-axo", "pid=,pcpu=,pmem=,stat=,etime=,command="],
        timeout=2.0,
    )
    rows: list[ProcessRow] = []
    for line in raw.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        try:
            rows.append(
                ProcessRow(
                    pid=int(parts[0]),
                    cpu=float(parts[1]),
                    mem=float(parts[2]),
                    stat=parts[3],
                    etime=parts[4],
                    command=parts[5],
                )
            )
        except ValueError:
            continue
    key = (lambda row: row.mem) if sort_mode == "MEM" else (lambda row: row.cpu)
    rows.sort(key=key, reverse=True)
    return rows


def color_for_percent(percent: float) -> int:
    if percent >= 85:
        return curses.color_pair(4) | curses.A_BOLD
    if percent >= 60:
        return curses.color_pair(3) | curses.A_BOLD
    return curses.color_pair(2)


def safe_add(screen: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width - 1:
        return
    try:
        screen.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


def draw_box(screen: curses.window, y: int, x: int, h: int, w: int, title: str, attr: int) -> None:
    if h < 3 or w < 8:
        return
    top = "+" + "-" * (w - 2) + "+"
    safe_add(screen, y, x, top, attr)
    for row in range(1, h - 1):
        safe_add(screen, y + row, x, "|", attr)
        safe_add(screen, y + row, x + w - 1, "|", attr)
    safe_add(screen, y + h - 1, x, top, attr)
    label = f" {title} "
    safe_add(screen, y, x + 2, label[: max(0, w - 4)], attr | curses.A_BOLD)


def bar(percent: float, width: int, label: str = "") -> str:
    width = max(4, width)
    filled = int(round(width * clamp(percent) / 100.0))
    body = "#" * filled + "." * (width - filled)
    suffix = f" {percent:5.1f}%"
    return f"{label}[{body}]{suffix}"


def pulse_bar(percent: float, width: int, phase: int, label: str = "") -> str:
    width = max(4, width)
    filled = int(round(width * clamp(percent) / 100.0))
    body = ["#"] * filled + ["."] * (width - filled)
    if filled:
        body[phase % filled] = "@"
    return f"{label}[{''.join(body)}] {percent:5.1f}%"


def comet(width: int, phase: int, density: float = 0.65) -> str:
    width = max(4, width)
    trail = ["."] * width
    head = phase % width
    chars = ["@", "#", "*", "+", "-", "."]
    for offset, char in enumerate(chars):
        idx = (head - offset) % width
        trail[idx] = char
    fill = int(width * clamp(density * 100.0) / 100.0)
    for idx in range(fill, width):
        if trail[idx] == ".":
            trail[idx] = " "
    return "".join(trail)


def flow_text(phase: int, charging: bool, external: bool) -> str:
    frames = [">  ", ">> ", ">>>", " >>", "  >"] if charging else ["<  ", "<< ", "<<<", " <<", "  <"]
    if external and not charging:
        frames = ["== ", "===", " ==", "==="]
    return frames[phase % len(frames)]


def temp_attr(value: float | None) -> int:
    if value is None:
        return curses.color_pair(3)
    if value >= 85:
        return curses.color_pair(4) | curses.A_BOLD
    if value >= 65:
        return curses.color_pair(3) | curses.A_BOLD
    return curses.color_pair(2)


def draw_header(screen: curses.window, width: int, phase: int, static: dict[str, Any], config: HudConfig) -> int:
    screen.erase()
    now_text = time.strftime("%Y-%m-%d %H:%M:%S")
    if width >= 118:
        eyes = "--" if (phase // 12) % 14 == 0 else "oo"
        title_lines = [
            " ____            _          _   _ _   _ ____  ",
            "|  _ \\ ___  _ __| | ___   _| | | | | | |  _ \\ ",
            "| |_) / _ \\| '__| |/ / | | | |_| | | | | | | |",
            "|  __/ (_) | |  |   <| |_| |  _  | |_| | |_| |",
            "|_|   \\___/|_|  |_|\\_\\\\__, |_| |_|\\___/|____/ ",
            "                       |___/                  ",
        ]
        mascot_lines = [
            "   /\\ .--. /\\  ",
            "  /  \\____/  \\ ",
            f" |    {eyes}    | ",
            " |    (oo)    | ",
            "  \\  \\____/  / ",
            "   '-.____.-'  ",
        ]
        for idx, line in enumerate(title_lines):
            attr = curses.color_pair(2) | curses.A_BOLD if idx in (0, 2) else curses.color_pair(1)
            safe_add(screen, idx, 2, line, attr)
        pig_x = min(max(58, len(title_lines[0]) + 8), max(2, width - 54))
        for idx, line in enumerate(mascot_lines):
            attr = curses.color_pair(3) | curses.A_BOLD if idx in (2, 3) else curses.color_pair(7)
            safe_add(screen, idx, pig_x, line, attr)
        safe_add(screen, 1, max(58, width - len(now_text) - 3), now_text, curses.color_pair(6) | curses.A_BOLD)
        rig = f"{static.get('chip', 'Mac')} / {static.get('model', '')}".strip()
        safe_add(screen, 3, max(58, width - len(rig) - 3), rig[: max(0, width - 62)], curses.color_pair(1))
        meta = f"{COPYRIGHT_TEXT}  |  theme {config.theme_name}"
        safe_add(screen, 5, max(58, width - len(meta) - 3), meta, curses.color_pair(7) | curses.A_BOLD)
        divider = list("-" * (width - 2))
        head = (phase * 3) % max(1, width - 2)
        for offset, char in enumerate("<*>"):
            idx = head + offset
            if 0 <= idx < len(divider):
                divider[idx] = char
        safe_add(screen, 6, 1, "".join(divider), curses.color_pair(5))
        return 7

    title = " P O R K Y H U D "
    safe_add(screen, 0, 1, title, curses.color_pair(2) | curses.A_BOLD)
    safe_add(screen, 0, max(1, width - len(now_text) - 2), now_text, curses.color_pair(6) | curses.A_BOLD)
    meta = f"{COPYRIGHT_TEXT} | {config.theme_name}"
    safe_add(screen, 1, max(1, width - len(meta) - 2), meta[: max(0, width - 3)], curses.color_pair(7))
    divider = list("-" * (width - 2))
    divider[(phase * 2) % max(1, width - 2)] = "*"
    safe_add(screen, 2, 1, "".join(divider), curses.color_pair(5))
    return 3


def draw_cpu_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    per_core: list[float],
    static: dict[str, Any],
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "CPU WORKER CORES", curses.color_pair(5))
    if h < 5:
        return
    total = sum(per_core) / len(per_core) if per_core else 0.0
    logical = static.get("logical_cpu") or str(len(per_core) or "?")
    physical = static.get("physical_cpu") or "?"
    core_line = static.get("cpu_cores") or f"{physical} physical / {logical} logical"
    safe_add(screen, y + 1, x + 2, f"{static.get('chip', 'CPU')}"[: w - 4], curses.color_pair(1) | curses.A_BOLD)
    safe_add(screen, y + 2, x + 2, f"cores: {core_line}"[: w - 4], curses.color_pair(1))
    safe_add(screen, y + 3, x + 2, pulse_bar(total, max(8, w - 18), phase, "total "), color_for_percent(total))

    lanes_start = y + 5
    lanes_available = max(0, h - 6)
    if not per_core or lanes_available <= 0:
        safe_add(screen, y + 5, x + 2, "per-core stream unavailable", curses.color_pair(3))
        return

    columns = 2 if w >= 64 and lanes_available * 2 >= len(per_core) else 1
    if len(per_core) > lanes_available * columns and lanes_available > 1:
        lanes_available -= 1
        columns = 2 if w >= 64 and lanes_available * 2 >= len(per_core) else 1
    col_width = (w - 4) // columns
    visible_count = min(len(per_core), lanes_available * columns)
    for index in range(visible_count):
        column = index // lanes_available
        row = index % lanes_available
        cx = x + 2 + column * col_width
        cy = lanes_start + row
        pct = per_core[index]
        lane_label = f"W{index:02d} "
        safe_add(screen, cy, cx, pulse_bar(pct, max(5, col_width - 13), phase + index, lane_label)[: col_width - 1], color_for_percent(pct))
    if visible_count < len(per_core):
        safe_add(screen, y + h - 2, x + 2, f"+{len(per_core) - visible_count} more workers hidden", curses.color_pair(3))


def draw_system_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    static: dict[str, Any],
    battery: BatteryInfo,
) -> None:
    draw_box(screen, y, x, h, w, "SYSTEM", curses.color_pair(5))
    lines = [
        f"node: {static.get('host', 'localhost')}",
        f"rig:  {static.get('model', 'Mac')} {static.get('model_id', '')}".strip(),
        f"os:   {static.get('os', 'macOS')} build {static.get('build', '')}".strip(),
        f"kern: Darwin {static.get('kernel', '')}",
        f"up:   {format_uptime(boot_seconds())}",
        f"load: {os.getloadavg()[0]:.2f} {os.getloadavg()[1]:.2f} {os.getloadavg()[2]:.2f}",
        battery_summary(battery),
    ]
    for index, line in enumerate(lines[: h - 2], start=1):
        attr = curses.color_pair(1)
        if index == 1:
            attr |= curses.A_BOLD
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def draw_memory_panel(screen: curses.window, y: int, x: int, h: int, w: int, mem: dict[str, float], phase: int = 0) -> None:
    draw_box(screen, y, x, h, w, "MEMORY", curses.color_pair(5))
    total = mem.get("total", 0.0) or 1.0
    used = mem.get("used", 0.0)
    swap_total = mem.get("swap_total", 0.0)
    swap_used = mem.get("swap_used", 0.0)
    used_pct = used / total * 100.0
    swap_pct = (swap_used / swap_total * 100.0) if swap_total else 0.0
    lines = [
        (pulse_bar(used_pct, max(8, w - 18), phase, "ram  "), color_for_percent(used_pct)),
        (f"used {human_bytes(used)} / {human_bytes(total)}", curses.color_pair(1)),
        (f"wired {human_bytes(mem.get('wired', 0.0))}  comp {human_bytes(mem.get('compressed', 0.0))}", curses.color_pair(1)),
        (f"active {human_bytes(mem.get('active', 0.0))}  inactive {human_bytes(mem.get('inactive', 0.0))}", curses.color_pair(1)),
        (pulse_bar(swap_pct, max(8, w - 18), phase + 4, "swap "), color_for_percent(swap_pct)),
        (f"swap {human_bytes(swap_used)} / {human_bytes(swap_total)}", curses.color_pair(1)),
    ]
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def draw_battery_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryInfo,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "POWER CELL", curses.color_pair(5))
    pct = float(battery.percent if battery.percent is not None else 0)
    attr = curses.color_pair(2) | curses.A_BOLD
    if battery.percent is not None and battery.percent <= 30:
        attr = curses.color_pair(3) | curses.A_BOLD
    if battery.percent is not None and battery.percent <= 20 and not battery.external_connected:
        attr = curses.color_pair(4) | curses.A_BOLD

    state = "charging" if battery.is_charging else battery.state
    if battery.external_connected and not battery.is_charging:
        state = "AC hold" if "not charging" in state.lower() else state
    charger_label = battery.charger_name or format_optional_int(battery.charger_watts, "W")
    if battery.charger_watts is not None and battery.charger_name:
        watts_prefix = f"{battery.charger_watts}W"
        if battery.charger_name.lower().startswith(watts_prefix.lower()):
            charger_label = battery.charger_name
        else:
            charger_label = f"{watts_prefix} {battery.charger_name}"

    lines: list[tuple[str, int]] = [
        (pulse_bar(pct, max(8, w - 18), phase, "cell "), attr),
        (f"state {flow_text(phase, battery.is_charging, battery.external_connected)} {state}", curses.color_pair(6) | curses.A_BOLD),
        (f"source: {battery.source}", curses.color_pair(1)),
        (f"charger: {charger_label}".strip(), curses.color_pair(1)),
        (
            f"cycles: {format_optional_int(battery.cycle_count)} / {format_optional_int(battery.design_cycles)}",
            curses.color_pair(1),
        ),
        (
            f"health: {format_optional_int(battery.health_percent, '%')}  temp: {format_temp(battery.temp_c)}",
            temp_attr(battery.temp_c),
        ),
    ]
    if battery.voltage_v is not None and battery.amperage_a is not None:
        lines.append((f"pack: {battery.voltage_v:4.2f}V  {battery.amperage_a:5.2f}A", curses.color_pair(1)))
    for index, (line, line_attr) in enumerate(lines[: h - 2], start=1):
        safe_add(screen, y + index, x + 2, line[: w - 4], line_attr)


def draw_thermal_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    battery: BatteryInfo,
    sensor: SensorInfo,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "THERMAL", curses.color_pair(5))
    pressure_attr = curses.color_pair(2) | curses.A_BOLD
    if sensor.thermal_warning != "nominal":
        pressure_attr = curses.color_pair(3) | curses.A_BOLD

    lines: list[tuple[str, int]] = [
        (f"pressure: {sensor.thermal_warning}", pressure_attr),
        (f"performance: {sensor.performance_warning}", pressure_attr),
        (f"battery skin: {format_temp(battery.temp_c)}", temp_attr(battery.temp_c)),
        (f"virtual pack: {format_temp(battery.virtual_temp_c)}", temp_attr(battery.virtual_temp_c)),
    ]
    if sensor.privileged_locked:
        lines.append(("advanced: press u to unlock", curses.color_pair(3) | curses.A_BOLD))
    else:
        exposed_count = 0
        if sensor.cpu_temp_c is not None:
            lines.append((f"processor temp: {format_temp(sensor.cpu_temp_c)}", temp_attr(sensor.cpu_temp_c)))
            exposed_count += 1
        if sensor.gpu_temp_c is not None:
            lines.append((f"graphics temp:  {format_temp(sensor.gpu_temp_c)}", temp_attr(sensor.gpu_temp_c)))
            exposed_count += 1
        if sensor.fan_rpm is not None:
            lines.append((f"fan: {fan_text(sensor)}", curses.color_pair(2)))
            exposed_count += 1
        if sensor.cpu_power_w is not None:
            lines.append((f"cpu power: {format_watts(sensor.cpu_power_w)}", curses.color_pair(1)))
            exposed_count += 1
        if sensor.gpu_power_w is not None:
            lines.append((f"gpu power: {format_watts(sensor.gpu_power_w)}", curses.color_pair(1)))
            exposed_count += 1
        if exposed_count == 0:
            lines.append(("advanced: no extra sensors exposed", curses.color_pair(3)))
    if h > 10:
        lines.append((f"sensor bus: {comet(max(8, w - 18), phase, 0.85)}", curses.color_pair(2)))
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def draw_gpu_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    static: dict[str, Any],
    sensor: SensorInfo,
    phase: int,
) -> None:
    draw_box(screen, y, x, h, w, "GPU", curses.color_pair(5))
    gpus = static.get("gpus") or []
    if not gpus:
        safe_add(screen, y + 1, x + 2, "GPU scan unavailable", curses.color_pair(3))
        safe_add(screen, y + 2, x + 2, "try: system_profiler SPDisplaysDataType", curses.color_pair(1))
        return

    line_y = y + 1
    for gpu in gpus:
        if line_y >= y + h - 1:
            break
        name = gpu.get("name", "GPU")
        cores = gpu.get("cores", "")
        core_text = f" [{cores} GPU cores]" if cores else ""
        safe_add(screen, line_y, x + 2, f"{name}{core_text}"[: w - 4], curses.color_pair(2) | curses.A_BOLD)
        line_y += 1
        if cores and line_y < y + h - 1:
            try:
                core_count = int(cores)
            except ValueError:
                core_count = 12
            lane_width = min(max(8, w - 12), max(8, core_count))
            cells = []
            for idx in range(lane_width):
                if idx == (phase % lane_width):
                    cells.append("@")
                elif (idx + phase) % 5 == 0:
                    cells.append("*")
                else:
                    cells.append("#" if idx < min(core_count, lane_width) else ".")
            safe_add(screen, line_y, x + 2, f"cores: [{''.join(cells)}]"[: w - 4], curses.color_pair(2))
            line_y += 1
        if line_y < y + h - 1:
            safe_add(screen, line_y, x + 2, f"shader: {comet(max(8, w - 14), phase * 2, 0.9)}"[: w - 4], curses.color_pair(6))
            line_y += 1
        if line_y < y + h - 1 and (sensor.gpu_temp_c is not None or sensor.gpu_power_w is not None):
            stats = []
            if sensor.gpu_temp_c is not None:
                stats.append(f"temp: {format_temp(sensor.gpu_temp_c)}")
            if sensor.gpu_power_w is not None:
                stats.append(f"power: {format_watts(sensor.gpu_power_w)}")
            safe_add(screen, line_y, x + 2, "  ".join(stats)[: w - 4], temp_attr(sensor.gpu_temp_c))
            line_y += 1
        metal = gpu.get("metal", "")
        if metal and line_y < y + h - 1:
            safe_add(screen, line_y, x + 2, f"metal: {metal}"[: w - 4], curses.color_pair(1))
            line_y += 1
        displays = gpu.get("displays", "")
        if displays and line_y < y + h - 1:
            safe_add(screen, line_y, x + 2, f"driving: {displays}"[: w - 4], curses.color_pair(1))
            line_y += 1
        if line_y < y + h - 1:
            safe_add(screen, line_y, x + 2, "-" * min(w - 4, 28), curses.color_pair(5))
            line_y += 1


def draw_io_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    disk: shutil._ntuple_diskusage,
    net_down: float,
    net_up: float,
    phase: int = 0,
) -> None:
    draw_box(screen, y, x, h, w, "I/O", curses.color_pair(5))
    disk_pct = disk.used / disk.total * 100.0 if disk.total else 0.0
    lines = [
        (pulse_bar(disk_pct, max(8, w - 18), phase, "disk "), color_for_percent(disk_pct)),
        (f"root {human_bytes(disk.used)} / {human_bytes(disk.total)}  free {human_bytes(disk.free)}", curses.color_pair(1)),
        (f"net down {human_bytes(net_down)}/s", curses.color_pair(2) | curses.A_BOLD),
        (f"net up   {human_bytes(net_up)}/s", curses.color_pair(2) | curses.A_BOLD),
        (f"rx {comet(max(8, w - 9), phase, min(1.0, net_down / 1_000_000 + 0.35))}", curses.color_pair(6)),
        (f"tx {comet(max(8, w - 9), phase + 7, min(1.0, net_up / 1_000_000 + 0.35))}", curses.color_pair(2)),
    ]
    for index, (line, attr) in enumerate(lines[: h - 2], start=1):
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def draw_process_panel(
    screen: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    rows: list[ProcessRow],
    scroll: int,
    sort_mode: str,
) -> None:
    draw_box(screen, y, x, h, w, f"PROCESS GRID sort={sort_mode}", curses.color_pair(5))
    if h < 5:
        return
    compact = w < 58
    header = " PID     CPU%  MEM% ST   COMMAND" if compact else " PID      CPU%  MEM%  ST     ELAPSED    COMMAND"
    safe_add(screen, y + 1, x + 1, header[: w - 2], curses.color_pair(6) | curses.A_BOLD)
    safe_add(screen, y + 2, x + 1, "-" * (w - 2), curses.color_pair(5))

    body_height = h - 4
    scroll = max(0, min(scroll, max(0, len(rows) - body_height)))
    for index, row in enumerate(rows[scroll : scroll + body_height]):
        line_y = y + 3 + index
        if compact:
            command_width = max(10, w - 28)
            line = (
                f"{row.pid:>6} {row.cpu:6.1f} {row.mem:5.1f} "
                f"{row.stat:<4.4} {visible_command(row.command, command_width)}"
            )
        else:
            command_width = max(8, w - 43)
            line = (
                f"{row.pid:>6}  {row.cpu:6.1f} {row.mem:5.1f}  "
                f"{row.stat:<5.5} {row.etime:>9.9}  {visible_command(row.command, command_width)}"
            )
        attr = curses.color_pair(1)
        if row.cpu >= 80:
            attr = curses.color_pair(4) | curses.A_BOLD
        elif row.cpu >= 35:
            attr = curses.color_pair(3) | curses.A_BOLD
        elif index % 2:
            attr = curses.color_pair(7)
        safe_add(screen, line_y, x + 1, line[: w - 2], attr)

    if compact:
        footer = f"{len(rows)} procs {scroll + 1}-{min(len(rows), scroll + body_height)} | q quit | m sort | arrows/page"
    else:
        footer = f"{len(rows)} processes  scroll {scroll + 1}-{min(len(rows), scroll + body_height)}  q quit | m CPU/MEM | arrows/page scroll"
    safe_add(screen, y + h - 1, x + 2, footer[: w - 4], curses.color_pair(6) | curses.A_BOLD)


def set_message(config: HudConfig, text: str, ttl: float = 3.0) -> None:
    config.message = text
    config.message_until = time.monotonic() + ttl


def draw_help_overlay(screen: curses.window, config: HudConfig, sensor: SensorInfo) -> None:
    height, width = screen.getmaxyx()
    w = min(72, max(48, width - 8))
    h = 16
    y = max(2, (height - h) // 2)
    x = max(2, (width - w) // 2)
    draw_box(screen, y, x, h, w, "SHORTCUTS", curses.color_pair(6) | curses.A_BOLD)
    unlock_state = "online" if not sensor.privileged_locked else "locked"
    lines = [
        "q / Esc       quit",
        "h / ?         show or hide this panel",
        "t             cycle theme",
        "a             animation: off / calm / vivid",
        "m             sort process grid by CPU or memory",
        "r             rescan system and sensor data",
        "u             unlock advanced macOS sensors with sudo",
        "arrows/j/k    scroll processes",
        "PgUp/PgDn     faster process scrolling",
        "",
        f"theme: {config.theme_name}    sensors: {unlock_state}",
        COPYRIGHT_TEXT,
    ]
    for index, line in enumerate(lines[: h - 2], start=1):
        attr = curses.color_pair(1)
        if line.startswith("theme:") or line == COPYRIGHT_TEXT:
            attr = curses.color_pair(7) | curses.A_BOLD
        safe_add(screen, y + index, x + 2, line[: w - 4], attr)


def init_colors(theme_index: int = 0) -> None:
    try:
        if not curses.has_colors():
            return
        curses.start_color()
    except curses.error:
        return

    background = -1
    try:
        curses.use_default_colors()
    except curses.error:
        background = curses.COLOR_BLACK

    theme = THEMES[theme_index % len(THEMES)]["colors"]
    for pair_id, foreground in theme.items():
        try:
            curses.init_pair(pair_id, foreground, background)
        except curses.error:
            pass


def draw_small_screen(screen: curses.window, height: int, width: int) -> None:
    screen.erase()
    safe_add(screen, 1, 2, "PorkyHUD needs a larger terminal.", curses.color_pair(3) | curses.A_BOLD)
    safe_add(screen, 3, 2, f"Current: {width}x{height}. Resize to at least 76x22.", curses.color_pair(1))
    safe_add(screen, 5, 2, "Press q to quit.", curses.color_pair(1))
    screen.refresh()


def hud(screen: curses.window) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.nodelay(True)
    screen.keypad(True)
    config = HudConfig()
    init_colors(config.theme_index)

    static = collect_static_info()
    cpu_sampler = MacCpuSampler()
    per_core = cpu_sampler.sample()
    rows: list[ProcessRow] = []
    sort_mode = "CPU"
    scroll = 0
    last_stats = 0.0
    last_process = 0.0
    last_sensor = 0.0
    previous_net = network_bytes()
    previous_net_time = time.monotonic()
    net_down = 0.0
    net_up = 0.0
    mem = memory_stats()
    battery = battery_info()
    sensor = sensor_info()
    last_sensor = time.monotonic()
    disk = shutil.disk_usage("/")

    while True:
        now = time.monotonic()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (ord("h"), ord("H"), ord("?")):
            config.show_help = not config.show_help
            set_message(config, "shortcut panel opened" if config.show_help else "shortcut panel closed")
        elif key in (ord("t"), ord("T")):
            config.theme_index = (config.theme_index + 1) % len(THEMES)
            init_colors(config.theme_index)
            set_message(config, f"theme switched to {config.theme_name}")
        elif key in (ord("a"), ord("A")):
            config.animation_mode = (config.animation_mode + 1) % 3
            labels = ["off", "calm", "vivid"]
            set_message(config, f"animation {labels[config.animation_mode]}")
        elif key in (ord("u"), ord("U")):
            if unlock_privileged_sensors(screen):
                sensor = sensor_info()
                last_sensor = time.monotonic()
                set_message(config, "advanced sensor session unlocked")
            else:
                set_message(config, "advanced sensor unlock skipped")
        elif key in (ord("m"), ord("M")):
            sort_mode = "MEM" if sort_mode == "CPU" else "CPU"
            rows = process_rows(sort_mode)
            scroll = 0
            last_process = now
            set_message(config, f"process sort: {sort_mode}")
        elif key in (curses.KEY_DOWN, ord("j")):
            scroll += 1
        elif key in (curses.KEY_UP, ord("k")):
            scroll -= 1
        elif key == curses.KEY_NPAGE:
            scroll += 10
        elif key == curses.KEY_PPAGE:
            scroll -= 10
        elif key == curses.KEY_HOME:
            scroll = 0
        elif key == curses.KEY_END:
            scroll = 10**9
        elif key in (ord("r"), ord("R")):
            last_stats = 0.0
            last_process = 0.0
            last_sensor = 0.0
            set_message(config, "rescanning system state")

        if now - last_stats >= REFRESH_SECONDS:
            sampled = cpu_sampler.sample()
            if sampled:
                per_core = sampled
            mem = memory_stats()
            battery = battery_info()
            disk = shutil.disk_usage("/")
            current_net = network_bytes()
            elapsed = max(0.1, now - previous_net_time)
            net_down = max(0.0, (current_net[0] - previous_net[0]) / elapsed)
            net_up = max(0.0, (current_net[1] - previous_net[1]) / elapsed)
            previous_net = current_net
            previous_net_time = now
            last_stats = now

        if now - last_sensor >= SENSOR_REFRESH_SECONDS:
            sensor = sensor_info()
            last_sensor = now

        if now - last_process >= PROCESS_REFRESH_SECONDS:
            rows = process_rows(sort_mode)
            last_process = now

        height, width = screen.getmaxyx()
        if height < 22 or width < 76:
            draw_small_screen(screen, height, width)
            time.sleep(0.08)
            continue

        if config.animation_mode == 0:
            phase = 0
        elif config.animation_mode == 2:
            phase = int(now * 16)
        else:
            phase = int(now * 8)
        header_h = draw_header(screen, width, phase, static, config)
        body_height = height - header_h - 1

        if width >= 118 and height >= 34:
            left_x = 1
            left_width = 38
            center_x = left_x + left_width + 1
            center_width = 44
            right_x = center_x + center_width + 1
            right_width = width - right_x - 1
            body_y = header_h

            system_h = 8
            power_h = 9
            thermal_h = 10
            io_h = max(7, body_height - system_h - power_h - thermal_h)
            draw_system_panel(screen, body_y, left_x, system_h, left_width, static, battery)
            draw_battery_panel(screen, body_y + system_h, left_x, power_h, left_width, battery, phase)
            draw_thermal_panel(screen, body_y + system_h + power_h, left_x, thermal_h, left_width, battery, sensor, phase)
            draw_io_panel(screen, body_y + system_h + power_h + thermal_h, left_x, io_h, left_width, disk, net_down, net_up, phase)

            cpu_h = 16
            gpu_h = 10
            mem_h = max(8, body_height - cpu_h - gpu_h)
            draw_cpu_panel(screen, body_y, center_x, cpu_h, center_width, per_core, static, phase)
            draw_gpu_panel(screen, body_y + cpu_h, center_x, gpu_h, center_width, static, sensor, phase)
            draw_memory_panel(screen, body_y + cpu_h + gpu_h, center_x, mem_h, center_width, mem, phase)

            proc_h = body_height
            process_scroll_max = max(0, len(rows) - max(0, proc_h - 4))
            scroll = max(0, min(scroll, process_scroll_max))
            draw_process_panel(screen, body_y, right_x, proc_h, right_width, rows, scroll, sort_mode)
        else:
            process_body_height = max(8, body_height // 2)
            top_height = body_height - process_body_height
            left_width = max(34, min(56, width * 42 // 100))
            if width - left_width - 3 < 38:
                left_width = max(32, width - 41)
            right_width = width - left_width - 3
            left_x = 1
            right_x = left_x + left_width + 1

            sys_h = max(5, min(7, top_height // 2))
            power_h = max(5, top_height - sys_h)
            cpu_h = top_height
            lower_h = height - (header_h + top_height) - 1
            thermal_h = min(8, max(5, lower_h // 2))
            gpu_h = max(4, lower_h - thermal_h)

            draw_system_panel(screen, header_h, left_x, sys_h, left_width, static, battery)
            draw_battery_panel(screen, header_h + sys_h, left_x, power_h, left_width, battery, phase)
            draw_cpu_panel(screen, header_h, right_x, cpu_h, right_width, per_core, static, phase)
            draw_thermal_panel(screen, header_h + top_height, left_x, thermal_h, left_width, battery, sensor, phase)
            draw_gpu_panel(screen, header_h + top_height + thermal_h, left_x, gpu_h, left_width, static, sensor, phase)

            proc_y = header_h + top_height
            proc_h = height - proc_y - 1
            process_scroll_max = max(0, len(rows) - max(0, proc_h - 4))
            scroll = max(0, min(scroll, process_scroll_max))
            draw_process_panel(screen, proc_y, right_x, proc_h, right_width, rows, scroll, sort_mode)

        if config.show_help:
            draw_help_overlay(screen, config, sensor)

        status = "h help  t theme  a motion  u sensors  m sort  r rescan  q quit"
        if config.message and now < config.message_until:
            status = f"{config.message} | {status}"
        safe_add(screen, height - 1, 1, status[: width - 2], curses.color_pair(6))
        screen.refresh()
        time.sleep(0.08)


def main() -> int:
    try:
        curses.wrapper(hud)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"PorkyHUD crashed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
