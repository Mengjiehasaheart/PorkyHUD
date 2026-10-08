"""Composition root for optional tools; no terminal or health-policy imports."""

from dataclasses import dataclass
from typing import Callable, Tuple

from .model import ToolReport


@dataclass(frozen=True)
class ToolSpec:
    key: str
    group: str
    title: str
    hint: str
    prompt: str = ""


TOOLS: Tuple[ToolSpec, ...] = (
    ToolSpec("process", "Inspect", "Process details", "Identity, files and sockets", "Process ID"),
    ToolSpec("workload", "Inspect", "Workload explorer", "Follow parent and child processes", "Process ID"),
    ToolSpec("port", "Inspect", "Port owner", "Local TCP and UDP owners", "Local port (1–65535)"),
    ToolSpec("diagnose", "Inspect", "Network checks", "DNS, route, TCP and verified TLS", "Host or IP, optionally :port"),
    ToolSpec("quality", "Inspect", "Network quality", "Start a traffic-generating test", "Type START to run a network traffic test"),
    ToolSpec("interfaces", "Inspect", "Interface counters", "Local packet errors and traffic"),
    ToolSpec("storage", "Inspect", "Storage map", "Disks, containers and volumes"),
    ToolSpec("folder", "Inspect", "Folder size and growth", "Scan only a folder you choose", "Folder path"),
    ToolSpec("sleep", "Inspect", "Sleep requests", "Processes requesting wakefulness"),
    ToolSpec("power", "Sessions", "Power timeline", "Battery and available power history"),
    ToolSpec("job", "Inspect", "Watch a local job", "Pin a workload; optional progress file", "Process ID, optionally | progress-file path"),
    ToolSpec("timeline", "Sessions", "Session timeline", "Review recent readings and changes"),
    ToolSpec("record", "Sessions", "Record / stop session", "Bounded local recording", "New session file path"),
    ToolSpec("mark", "Sessions", "Add bookmark", "Mark a build, analysis or slowdown", "Bookmark label"),
    ToolSpec("replay", "Sessions", "Replay a session", "Open recorded readings", "Session file path"),
    ToolSpec("compare", "Sessions", "Compare two runs", "Duration, coverage, mean and peak", "First session path | second session path"),
    ToolSpec("profile", "Workspaces", "Save workspace", "Remember views, filters and watches", "Workspace name"),
    ToolSpec("load_profile", "Workspaces", "Load workspace", "Restore saved preferences", "Workspace name"),
    ToolSpec("watch", "Workspaces", "Add personal watch", "A separate sustained-value notice", "Field > value for seconds; e.g. memory_used_bytes > 8000000000 for 30"),
    ToolSpec("reset", "Workspaces", "Reset workspace", "Restore Overview and clear pins/watches"),
    ToolSpec("export", "Share", "Save support report", "Preview aggregate fields; private details omitted", "Report file path"),
)
TOOL_BY_KEY = {spec.key: spec for spec in TOOLS}


def split_target(text: str):
    text = text.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("Use [IPv6]:port or a host name")
        host = text[1:end]
        suffix = text[end + 1:]
        port = int(suffix[1:]) if suffix.startswith(":") else 443
        if suffix and not suffix.startswith(":"):
            raise ValueError("Invalid target")
        return host, port
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        return host, int(port)
    return text, 443


class ToolService:
    """One optional action at a time with an injected cancellation-aware runner."""

    def __init__(self, runner: Callable, cancellation=None):
        self.runner = runner
        self.cancellation = cancellation

    def run(self, key: str, value: str = "", *, rows=(), samples=()) -> ToolReport:
        from .processes import inspect_process, workload_report, watch_job_report
        from .storage import inspect_storage, scan_folder
        from .power import inspect_sleep, power_timeline_report
        from .network import inspect_port, diagnose_network, network_quality, collect_interface_diagnostics
        if key == "process":
            return inspect_process(int(value), self.runner, rows)
        if key == "workload":
            return workload_report(int(value), rows)
        if key == "job":
            pid, _, path = value.partition("|")
            return watch_job_report(int(pid), rows, path.strip() or None)
        if key == "port":
            return inspect_port(int(value), self.runner)
        if key == "diagnose":
            target, port = split_target(value)
            return diagnose_network(target, port, runner=self.runner, cancel=self.cancellation)
        if key == "quality":
            if value.strip() != "START":
                return ToolReport("Network quality", "CANCELLED", "Traffic test was not started")
            return network_quality(self.runner)
        if key == "interfaces":
            return collect_interface_diagnostics(self.runner)
        if key == "storage":
            return inspect_storage(self.runner)
        if key == "folder":
            return scan_folder(value, cancel=self.cancellation)
        if key == "sleep":
            return inspect_sleep(self.runner)
        if key == "power":
            return power_timeline_report(samples)
        raise ValueError("Unknown tool")
