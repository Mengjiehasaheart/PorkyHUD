"""Read-only selected-process details and pure workload projections."""

from datetime import datetime
import json
import math
import os
import re
import stat
from typing import Mapping

from porkyhud_inventory import InventoryStatus
from .model import ToolReport


ROW_LIMIT = 128
PROCESS_LIMIT = 4096
PROGRESS_LIMIT = 65536
_START = r"[A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}"


def _text(value, limit=160):
    return "".join(c for c in str(value) if c.isprintable())[:limit]


def _status(probe):
    return probe.status.value


def _pid(pid):
    return type(pid) is int and 0 < pid <= 2147483647


def _identity(raw, pid):
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise ValueError("selected identity requires one row")
    match = re.fullmatch(r"(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(" + _START + r")\s+(.+)", lines[0])
    if match is None or int(match[1]) != pid:
        raise ValueError("unrecognized process identity")
    started = " ".join(match[5].split())
    datetime.strptime(started, "%a %b %d %H:%M:%S %Y")
    return {"pid": pid, "ppid": int(match[2]), "state": _text(match[3], 24),
            "rss_bytes": int(match[4]) * 1024, "start_id": started,
            "name": _text(os.path.basename(match[6]), 80)}


def _read_identity(pid, runner):
    probe = runner(("/bin/ps", "-p", str(pid), "-o", "pid=,ppid=,stat=,rss=,lstart=,comm="), 2.0)
    if probe.status is not InventoryStatus.LIVE:
        return None, _status(probe)
    try:
        item = _identity(probe.stdout, pid)
    except (ValueError, OverflowError):
        return None, "ERROR"
    return item, "LIVE" if item else "EXITED"


def _files(raw, pid):
    records = []
    current = None
    seen_pid = False
    fields = raw.split("\0") if "\0" in raw else raw.splitlines()

    def finish():
        if current is None:
            return
        if not current.get("type"):
            raise ValueError("open file lacks a type")
        kind = current["type"]
        name = current.get("name", "")
        protocol = current.get("protocol", "")
        if kind in ("IPv4", "IPv6"):
            shown = _text(name)
            kind = protocol or kind
        else:
            # Detailed paths can contain account names, datasets, or secrets.
            shown = _text(os.path.basename(name.rstrip("/"))) or "/"
        records.append((current["fd"], _text(kind, 24), shown, current.get("state", "")))

    for field in fields:
        field = field.strip("\n")
        if not field:
            continue
        key, value = field[0], field[1:]
        if key == "p":
            finish()
            current = None
            if not value.isdigit() or int(value) != pid:
                raise ValueError("different process in detail response")
            seen_pid = True
        elif key == "f":
            finish()
            if not seen_pid or re.fullmatch(r"(?:\d+[ruw]?|cwd|rtd|txt|mem)", value) is None:
                raise ValueError("unrecognized descriptor")
            current = {"fd": value}
        elif key in ("t", "n", "P", "T") and current is not None:
            if key == "T":
                if value.startswith("ST="):
                    current["state"] = _text(value[3:], 32)
            else:
                current[{"t": "type", "n": "name", "P": "protocol"}[key]] = value
    finish()
    if not seen_pid and raw.strip():
        raise ValueError("missing process boundary")
    return records


def inspect_process(pid, runner, rows=()):
    """Read details only when identity is unchanged before and after lsof."""
    if not _pid(pid):
        return ToolReport("process", "INVALID", "Choose a positive process ID.")
    first, status = _read_identity(pid, runner)
    if first is None:
        return ToolReport("process", status, "Process exited." if status == "EXITED" else "Process details unavailable.")
    for row in rows:
        if isinstance(row, Mapping) and row.get("pid") == pid and row.get("start_id"):
            if " ".join(str(row["start_id"]).split()) != first["start_id"]:
                return ToolReport("process", "PID CHANGED", "This process ID now belongs to a different process.")
    probe = runner(("/usr/sbin/lsof", "-nP", "-p", str(pid), "-F0ftPnT"), 3.0)
    last, status = _read_identity(pid, runner)
    if last is None:
        return ToolReport("process", status, "Process exited while details were collected." if status == "EXITED" else "Process identity could not be rechecked.")
    if last["start_id"] != first["start_id"]:
        return ToolReport("process", "PID CHANGED", "Process changed while details were collected.")
    details = ("Start: " + first["start_id"], "Parent PID: %d · state: %s" % (first["ppid"], first["state"]),
               "Start identity uses the reported start time, with one-second precision.",
               "File names are shortened; socket endpoints may be sensitive.",
               "Protected files may be hidden. No process commands are included.")
    data = {"identity": first, "files": [], "files_status": _status(probe)}
    if probe.status is not InventoryStatus.LIVE:
        return ToolReport("process", "PARTIAL", "%s · PID %d" % (first["name"], pid),
                          details=details + ("Open files: " + _status(probe),), data=data)
    try:
        files = _files(probe.stdout, pid)
    except ValueError:
        return ToolReport("process", "PARTIAL", "%s · PID %d" % (first["name"], pid),
                          details=details + ("Open file response was unrecognized.",), data=data)
    truncated = len(files) > ROW_LIMIT
    data["files"] = [dict(zip(("descriptor", "type", "name", "state"), item)) for item in files[:ROW_LIMIT]]
    return ToolReport("process", "LIVE", "%s · PID %d · %d visible open files" % (first["name"], pid, len(files)),
                      ("FD", "Type", "Name or endpoint", "State"), tuple(files[:ROW_LIMIT]), details,
                      complete=False, truncated=truncated, data=data)


def _normalized(rows):
    items = {}
    conflicts = set()
    invalid = 0
    clipped = False
    for index, row in enumerate(rows):
        if index >= PROCESS_LIMIT:
            clipped = True
            break
        if not isinstance(row, Mapping) or not _pid(row.get("pid")):
            invalid += 1
            continue
        pid = row["pid"]
        ppid = row.get("ppid", 0)
        cpu = row.get("cpu_pct")
        rss = row.get("rss_bytes")
        if type(ppid) is not int or ppid < 0 or (cpu is not None and
                (type(cpu) not in (int, float) or not math.isfinite(cpu) or cpu < 0)) or (rss is not None and
                (type(rss) is not int or rss < 0)):
            invalid += 1
            continue
        normalized = {"pid": pid, "ppid": ppid, "start_id": _text(row.get("start_id") or "", 96),
                      "name": _text(row.get("name", ""), 80), "cpu_pct": cpu,
                      "rss_bytes": rss, "state": _text(row.get("state", ""), 24)}
        if pid in items and items[pid]["start_id"] != normalized["start_id"]:
            conflicts.add(pid)
        else:
            items[pid] = normalized
    for pid in conflicts:
        items.pop(pid, None)
    return items, invalid + len(conflicts), clipped


def workload_report(pid, rows):
    """Group the supplied current process sample without starting probes."""
    if not _pid(pid):
        return ToolReport("workload", "INVALID", "Choose a positive process ID.")
    items, invalid, clipped = _normalized(rows)
    if pid not in items:
        return ToolReport("workload", "UNAVAILABLE", "Process is not present in this sample.",
                          details=("It may have exited or the process sample may be incomplete.",))
    children = {}
    for item in items.values():
        children.setdefault(item["ppid"], []).append(item["pid"])
    selected = []
    visited = set()
    pending = [(pid, 0)]
    while pending:
        current, depth = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        selected.append((items[current], depth))
        pending.extend((child, depth + 1) for child in sorted(children.get(current, ()), reverse=True))
    cpu_missing = sum(item["cpu_pct"] is None for item, _ in selected)
    rss_missing = sum(item["rss_bytes"] is None for item, _ in selected)
    cpu = sum(item["cpu_pct"] or 0 for item, _ in selected)
    rss = sum(item["rss_bytes"] or 0 for item, _ in selected)
    truncated = clipped or len(selected) > ROW_LIMIT
    display = tuple((str(item["pid"]), "  " * min(depth, 8) + item["name"], item["state"],
                     "—" if item["cpu_pct"] is None else "%.1f%%" % item["cpu_pct"],
                     "—" if item["rss_bytes"] is None else "%.1f MiB" % (item["rss_bytes"] / 1048576))
                    for item, depth in selected[:ROW_LIMIT])
    details = ["RSS is summed resident memory; shared pages can be counted more than once.",
               "CPU keeps the source process percentage units; this is not aggregate Mac utilization.",
               "Children are those currently observed; exited or reparented workers may be absent."]
    if cpu_missing or rss_missing:
        details.append("Missing readings: CPU %d · RSS %d" % (cpu_missing, rss_missing))
    if invalid:
        details.append("Skipped invalid or conflicting process rows: %d" % invalid)
    complete = not (truncated or invalid or cpu_missing or rss_missing)
    data = {"pid": pid, "start_id": items[pid]["start_id"], "process_count": len(selected),
            "cpu_pct_sum": cpu if not cpu_missing else None, "rss_bytes_sum": rss if not rss_missing else None,
            "cpu_missing": cpu_missing, "rss_missing": rss_missing,
            "processes": [item for item, _ in selected[:ROW_LIMIT]]}
    return ToolReport("workload", "LIVE" if complete else "PARTIAL", "%s · %d observed processes" % (items[pid]["name"], len(selected)),
                      ("PID", "Process", "State", "CPU", "RSS"), display, tuple(details), complete, truncated, data)


def _progress(path):
    """Read a small regular file without following its final symlink."""
    descriptor = None
    try:
        descriptor = os.open(os.fspath(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return {}, "UNSUPPORTED", "Progress file must be a regular file."
        raw = os.read(descriptor, PROGRESS_LIMIT + 1)
        if len(raw) > PROGRESS_LIMIT:
            return {}, "ERROR", "Progress file exceeds the 64 KiB limit."
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return {}, "ERROR", "Progress file must contain a JSON object."
        fields = {}
        for key in ("step", "current", "total", "percent"):
            value = payload.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0 and (key != "percent" or value <= 100):
                fields[key] = value
        if isinstance(payload.get("status"), str):
            fields["reported_status"] = _text(payload["status"], 80)
        if not fields:
            return {}, "NOT EXPOSED", "No recognized progress counters or status were reported."
        return fields, "LIVE", "Progress comes from the selected JSON file; success and ETA are not inferred."
    except PermissionError:
        return {}, "PERMISSION DENIED", "Progress file access denied."
    except OSError:
        return {}, "UNAVAILABLE", "Progress file is unavailable or cannot be read safely."
    except (ValueError, UnicodeError, TypeError, RecursionError, OverflowError):
        return {}, "ERROR", "Progress file content or path was unrecognized."
    finally:
        if descriptor is not None:
            os.close(descriptor)


def watch_job_report(pid, rows, progress_path=None):
    """Present current workload data and optional explicitly reported progress."""
    base = workload_report(pid, rows)
    details = list(base.details)
    data = dict(base.data)
    status, complete = base.status, base.complete
    details.append("This observation does not establish job completion or an ETA.")
    if progress_path is not None:
        progress, progress_status, detail = _progress(progress_path)
        data["progress"] = progress
        data["progress_status"] = progress_status
        details.append(detail)
        details.extend("Reported %s: %s" % (key.replace("_", " "), value) for key, value in progress.items())
        if progress_status != "LIVE":
            complete = False
            if status == "LIVE":
                status = "PARTIAL"
    return ToolReport("job", status, base.summary, base.columns, base.rows, tuple(details),
                      complete, base.truncated, data)
