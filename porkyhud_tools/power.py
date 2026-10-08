"""Read-only sleep assertions and observed battery-state timelines."""

from dataclasses import asdict, is_dataclass
from datetime import datetime
import math
import re
from typing import Mapping

from porkyhud_inventory import InventoryStatus
from .model import ToolReport


ROW_LIMIT = 128
SAMPLE_LIMIT = 4096
_OWNER = re.compile(r'^\s*pid\s+(\d+)\((.*?)\):\s+\[[^]]+\]\s+(\d+:\d{2}:\d{2})\s+(\w+)\s+named:\s*"(.*)"\s*$')


def _text(value, limit=160):
    text = "".join(c for c in str(value) if c.isprintable())
    text = re.sub(r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key|authorization)\b\s*[=:]\s*)[^\s&;,]+", r"\1[redacted]", text)
    return text[:limit]


def _assertions(raw):
    if "Assertion status system-wide:" not in raw or "Listed by owning process:" not in raw:
        raise ValueError("unrecognized assertion response")
    statuses = {}
    owners = []
    section = ""
    malformed = False
    for line in raw.splitlines():
        if "Assertion status system-wide:" in line:
            section = "status"
            continue
        if "Listed by owning process:" in line:
            section = "owners"
            continue
        if section == "status":
            match = re.fullmatch(r"\s+(\w+)\s+(\d+)\s*", line)
            if match:
                statuses[match[1]] = int(match[2])
            elif line.strip():
                malformed = True
        elif section == "owners":
            match = _OWNER.fullmatch(line)
            if match:
                duration = match[3].split(":")
                if int(match[1]) <= 0 or int(duration[1]) >= 60 or int(duration[2]) >= 60:
                    malformed = True
                    continue
                owners.append({"pid": int(match[1]), "name": _text(match[2], 80),
                               "duration": match[3], "duration_s": int(duration[0]) * 3600 + int(duration[1]) * 60 + int(duration[2]),
                               "type": _text(match[4], 64), "reason": _text(match[5])})
            elif line.lstrip().startswith("pid "):
                malformed = True
    if not statuses:
        raise ValueError("missing assertion statuses")
    return statuses, owners, malformed


def inspect_sleep(runner):
    """Report active requests without changing sleep or power settings."""
    probe = runner(("/usr/bin/pmset", "-g", "assertions"), 3.0)
    if probe.status is not InventoryStatus.LIVE:
        return ToolReport("sleep", probe.status.value, "Sleep assertions unavailable.")
    try:
        statuses, owners, malformed = _assertions(probe.stdout)
    except ValueError:
        return ToolReport("sleep", "ERROR", "Sleep assertion response was unrecognized.")
    truncated = len(owners) > ROW_LIMIT
    active = {name: value for name, value in statuses.items() if value}
    details = ("Active requests do not establish every cause of a failed sleep.",
               "Assertion owners and reasons describe this observation; power settings are unchanged.")
    if active:
        details += ("Active system requests: " + ", ".join("%s=%d" % item for item in sorted(active.items())),)
    else:
        details += ("No active system request was reported.",)
    if malformed:
        details += ("Some assertion rows were unrecognized.",)
    complete = not (truncated or malformed)
    return ToolReport("sleep", "LIVE" if complete else "PARTIAL", "%d visible process assertions" % len(owners),
                      ("PID", "Owner", "Duration", "Request", "Reason"),
                      tuple((str(item["pid"]), item["name"], item["duration"], item["type"], item["reason"]) for item in owners[:ROW_LIMIT]),
                      details, complete, truncated,
                      {"system_requests": statuses, "owners": owners[:ROW_LIMIT]})


def _sample(sample):
    if is_dataclass(sample) and not isinstance(sample, type):
        sample = asdict(sample)
    if not isinstance(sample, Mapping):
        return None
    metrics = sample.get("metrics")
    standardized = isinstance(metrics, Mapping)
    if "metrics" in sample and not standardized:
        return None
    if standardized:
        names = ("battery_pct", "battery_present", "battery_charging", "battery_external", "package_power_w", "cpu_temp_c")
        if not any(name in metrics for name in names):
            return None
        values, statuses = {}, {}
        for name in names:
            metric = metrics.get(name, {})
            if not isinstance(metric, Mapping):
                metric = {}
            status = metric.get("status", "UNAVAILABLE")
            statuses[name] = _text(status, 40)
            value = metric.get("value")
            recognized = type(value) in (int, float) and math.isfinite(value) and status == "LIVE"
            if name.startswith("battery_") and name != "battery_pct":
                recognized = recognized and value in (0, 1)
            elif name == "battery_pct":
                recognized = recognized and 0 <= value <= 100
            elif name == "package_power_w":
                recognized = recognized and value >= 0
            values[name] = value if recognized else None
        present = None if values["battery_present"] is None else bool(values["battery_present"])
        if present is None and values["battery_charging"] is not None and values["battery_external"] is not None:
            present = True
        battery = {"present": present, "percent": values["battery_pct"],
                   "external_connected": None if values["battery_external"] is None else bool(values["battery_external"]),
                   "is_charging": None if values["battery_charging"] is None else bool(values["battery_charging"]),
                   "state": "Charging" if values["battery_charging"] == 1 else "Observed"}
        power, temperature = values["package_power_w"], values["cpu_temp_c"]
    else:
        statuses = {}
        power, temperature = None, None
        battery = sample.get("battery", sample)
    if is_dataclass(battery) and not isinstance(battery, type):
        battery = asdict(battery)
    if not isinstance(battery, Mapping) or (not standardized and type(battery.get("present")) is not bool):
        return None
    percent = battery.get("percent")
    if percent is not None and (type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100):
        return None
    for key in ("external_connected", "is_charging"):
        if not standardized and type(battery.get(key)) is not bool:
            return None
    timestamp = sample.get("timestamp", sample.get("completed_at_utc", sample.get("time", "")))
    if isinstance(timestamp, datetime):
        timestamp = timestamp.isoformat()
    if type(timestamp) in (int, float):
        if not math.isfinite(timestamp):
            return None
        timestamp = "%.3f s" % timestamp
    elif not isinstance(timestamp, str):
        timestamp = ""
    return {"time": _text(timestamp, 80), "present": battery["present"], "percent": percent,
            "external_connected": battery["external_connected"], "is_charging": battery["is_charging"],
            "state": _text(battery.get("state", ""), 64), "package_power_w": power,
            "cpu_temp_c": temperature, "source_status": statuses,
            "available": any(value is not None for value in (battery["present"], percent, power, temperature))}


def power_timeline_report(samples):
    """Reuse supplied battery observations; never estimate runtime or energy."""
    readings = []
    invalid = 0
    clipped = False
    for index, sample in enumerate(samples):
        if index >= SAMPLE_LIMIT:
            clipped = True
            break
        reading = _sample(sample)
        if reading is None:
            invalid += 1
            readings.append(None)
        else:
            readings.append(reading)
    valid = [item for item in readings if item is not None]
    if not valid:
        return ToolReport("power-timeline", "UNAVAILABLE", "No recognized battery observations.")
    unavailable = sum(not item["available"] for item in valid)
    events = []
    previous = None
    for item in readings:
        if item is None:
            previous = None
            continue
        identity = (item["present"], item["external_connected"], item["is_charging"], item["state"], tuple(sorted(item["source_status"].items())))
        if previous is None or identity != previous:
            events.append(item)
        previous = identity
    if events[-1] is not valid[-1]:
        events.append(valid[-1])
    truncated = clipped or len(events) > ROW_LIMIT
    complete = not (invalid or unavailable or truncated)
    details = ("Events come from supplied battery observations; collection gaps can hide changes.",
               "Battery percentage is descriptive; runtime, energy, and job completion are not inferred.")
    if invalid:
        details += ("Unrecognized or missing samples: %d" % invalid,)
    if unavailable:
        details += ("Observations without current battery, power, or temperature readings: %d" % unavailable,)
    latest_status = valid[-1]["source_status"]
    if latest_status:
        details += ("Latest source status: battery %s · package power %s · CPU temperature %s" %
                    (latest_status.get("battery_pct", "UNAVAILABLE"), latest_status.get("package_power_w", "UNAVAILABLE"), latest_status.get("cpu_temp_c", "UNAVAILABLE")),)
    ranges = {}
    for key, label, unit in (("package_power_w", "Package power", "W"), ("cpu_temp_c", "CPU temperature", "°C")):
        values = [item[key] for item in valid if item[key] is not None]
        if values:
            ranges[key] = {"minimum": min(values), "maximum": max(values)}
            details += ("Observed %s range: %.1f–%.1f %s" % (label.lower(), min(values), max(values), unit),)
    display = []
    for item in events[:ROW_LIMIT]:
        display.append((item["time"] or "Unknown time", "No battery" if item["present"] is False else item["state"] if item["present"] is True else "Battery unavailable",
                        "—" if item["percent"] is None or item["present"] is not True else "%g%%" % item["percent"],
                        "External" if item["external_connected"] is True else "Battery" if item["present"] is True and item["external_connected"] is False else "Unknown",
                        "Charging" if item["is_charging"] is True else "Not charging" if item["is_charging"] is False else "Unavailable",
                        "—" if item["package_power_w"] is None else "%.1f W" % item["package_power_w"],
                        "—" if item["cpu_temp_c"] is None else "%.1f °C" % item["cpu_temp_c"]))
    first, last = valid[0], valid[-1]
    delta = last["percent"] - first["percent"] if first["present"] and last["present"] and first["percent"] is not None and last["percent"] is not None and not invalid else None
    report_status = "UNAVAILABLE" if unavailable == len(valid) else "LIVE" if complete else "PARTIAL"
    return ToolReport("power-timeline", report_status, "%d power observations · %d displayed events" % (len(valid), min(len(events), ROW_LIMIT)),
                      ("Time", "State", "Charge", "Source", "Charging", "Package power", "CPU temperature"), tuple(display), details, complete, truncated,
                      {"sample_count": len(valid), "missing_count": invalid, "unavailable_count": unavailable, "events": events[:ROW_LIMIT],
                       "percent_change": delta, "observed_ranges": ranges})
