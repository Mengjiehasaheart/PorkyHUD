"""Pure projection of cached application readings into session frames."""

from .observations import observation
from .model import clean_text


def system_projection(system, memory=None):
    result = {key: clean_text(system[key], 128) for key in ("model", "chip", "os", "build")
              if isinstance(system.get(key), str) and system[key] not in ("checking", "unavailable", "")}
    total = (memory or {}).get("total", (memory or {}).get("total_bytes"))
    if type(total) in (int, float) and 0 < total <= 2**63 - 1:
        result["ram_bytes"] = int(total)
    return result


def process_projection(rows, identity):
    return tuple({"pid": row.pid, "start_id": row.start_id or None, "ppid": row.ppid,
                  "name": clean_text(identity(row.command), 80), "cpu_pct": row.cpu,
                  "rss_bytes": row.rss_bytes, "state": row.stat}
                 for row in rows)


def live_sample(frame, memory, battery, sensors, activity, net_down, net_up,
                elapsed_s, *, statuses=None, revisions=None, ages=None):
    statuses, revisions, ages = statuses or {}, revisions or {}, ages or {}
    sources = {key.value: view.status.value for key, view in frame.source_views.items()}
    metrics = {}
    def add(name, value, unit, source):
        metrics[name] = (value, unit, statuses.get(source, sources.get(source, "UNAVAILABLE")))
    add("cpu_pct", frame.cpu_percent, "%", "cpu.usage")
    add("memory_pressure", int(frame.memory_pressure.level) if frame.memory_pressure else None, "level", "memory.pressure")
    for key in ("used", "total", "compressed", "swap_used", "cached"):
        add("memory_" + key + "_bytes", memory.get(key), "bytes", "memory.stats")
    add("disk_free_bytes", frame.disk_capacity.free_bytes if frame.disk_capacity else None, "bytes", "disk.capacity")
    add("disk_bytes_per_s", activity.bytes_per_sec if activity.source != "unavailable" else None, "bytes/s", "disk.activity")
    add("network_down_bytes_per_s", net_down if net_down is not None and net_down >= 0 else None, "bytes/s", "network.totals")
    add("network_up_bytes_per_s", net_up if net_up is not None and net_up >= 0 else None, "bytes/s", "network.totals")
    add("battery_pct", battery.percent, "%", "battery")
    battery_known = battery.present is not None and battery.state not in ("checking", "unknown")
    add("battery_present", int(battery.present) if battery_known else None, "flag", "battery")
    add("battery_charging", int(battery.is_charging) if battery.present else None, "flag", "battery")
    add("battery_external", int(battery.external_connected) if battery.present else None, "flag", "battery")
    add("cpu_temp_c", sensors.cpu_temp_c, "°C", "sensors")
    add("gpu_temp_c", sensors.gpu_temp_c, "°C", "sensors")
    add("package_power_w", sensors.package_power_w, "W", "sensors")
    sample = observation(metrics, elapsed_s,
                         health={"severity": frame.assessment.severity.value.upper(),
                                 "readiness": frame.assessment.evidence_state.value.upper()})
    for name in metrics:
        source = _source_for(name)
        if source in revisions:
            sample["metrics"][name]["revision"] = revisions[source]
        if source in ages:
            sample["metrics"][name]["age_s"] = max(0.0, ages[source])
        sample["metrics"][name]["max_gap_s"] = {"cpu.usage": 2.5, "memory.pressure": 2.5,
            "memory.stats": 6, "disk.capacity": 75, "disk.activity": 12,
            "network.totals": 5, "battery": 45, "sensors": 45}.get(source, 5)
    return sample


def _source_for(name):
    if name == "memory_pressure":
        return "memory.pressure"
    if name.startswith("memory_"):
        return "memory.stats"
    if name == "disk_free_bytes":
        return "disk.capacity"
    if name.startswith("disk_"):
        return "disk.activity"
    if name.startswith("network_"):
        return "network.totals"
    if name.startswith("battery_"):
        return "battery"
    if name == "cpu_pct":
        return "cpu.usage"
    return "sensors"


def snapshot_sample(snapshot):
    """New aggregate projection, separate from the legacy snapshot API."""
    metrics = {}
    cpu = snapshot.get("cpu", {})
    metrics["cpu_pct"] = (cpu.get("average_percent"), "%", "LIVE")
    for key in ("used", "total", "compressed", "swap_used", "cached"):
        metrics["memory_" + key + "_bytes"] = (snapshot.get("memory", {}).get(key + "_bytes"), "bytes", "LIVE")
    metrics["disk_free_bytes"] = (snapshot.get("disk", {}).get("capacity", {}).get("free"), "bytes", "LIVE")
    metrics["battery_pct"] = (snapshot.get("battery", {}).get("percent"), "%", "LIVE")
    result = observation(metrics, 0.0, health={"severity": "UNKNOWN", "readiness": "INCOMPLETE"})
    result["system"] = system_projection(snapshot.get("system", {}), snapshot.get("memory", {}))
    return result
