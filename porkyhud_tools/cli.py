"""CLI adapter for shared inspection and session services."""

import json
from .model import ToolReport, clean_text
from .service import ToolService
from .observations import replay_report


INSPECTION_TOOLS = ("process", "workload", "port", "diagnose", "quality", "interfaces", "storage", "folder", "sleep", "job")


def add_arguments(parser):
    parser.add_argument("--tool", choices=INSPECTION_TOOLS, help="run an optional tool and exit; --json returns a v1 report")
    parser.add_argument("--target", help="process ID, local port, host[:port], or folder for --tool")
    parser.add_argument("--progress-file", help="explicit local progress file for --tool job")
    parser.add_argument("--record", metavar="FILE", help="record a bounded local session; add --duration for headless recording")
    parser.add_argument("--include-processes", action="store_true", help="include selected process IDs/names in a local recording or export")
    parser.add_argument("--duration", type=float, metavar="SECONDS", help="record without the HUD for this duration (0.1–86400 seconds)")
    parser.add_argument("--replay", metavar="FILE", help="replay saved readings; no telemetry collectors are started")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="compare two recorded runs")
    parser.add_argument("--export", metavar="FILE", help="save a v1 aggregate support report; private fields omitted")
    parser.add_argument("--workspace", metavar="NAME", help="restore a saved workspace; explicit flags override it")
    parser.add_argument("--save-workspace", metavar="NAME", help="save the supplied view/style/filter preferences and exit")
    parser.add_argument("--list-workspaces", action="store_true", help="list saved workspace names and exit")
    parser.add_argument("--watch", action="append", default=[], metavar="RULE", help="personal watch: 'memory_used_bytes > 8000000000 for 30'")


def validate_arguments(args, parser):
    import math
    actions = [bool(args.tool), bool(args.replay), bool(args.compare), bool(args.export), bool(args.save_workspace), args.list_workspaces,
               bool(args.record and args.duration is not None)]
    if sum(actions) > 1:
        parser.error("choose one tool, replay, comparison, export, workspace-save or headless recording")
    if args.duration is not None and (not args.record or not math.isfinite(args.duration) or not 0.1 <= args.duration <= 86400):
        parser.error("--duration requires --record and 0.1–86400 seconds")
    if args.target and not args.tool:
        parser.error("--target requires --tool")
    if args.progress_file and args.tool != "job":
        parser.error("--progress-file requires --tool job")
    if args.include_processes and not (args.record or args.export):
        parser.error("--include-processes requires --record or --export")
    if args.tool in ("process", "workload", "port", "diagnose", "folder", "job") and not args.target:
        parser.error("this tool requires --target")
    if args.tool and args.inspect:
        parser.error("choose --tool or --inspect")
    if args.record and (args.replay or args.compare or args.tool or args.export):
        parser.error("--record cannot be combined with another report action")
    if args.record and (args.save_workspace or args.list_workspaces or args.inspect or args.snapshot):
        parser.error("--record cannot be combined with workspace reports, --inspect or --snapshot")
    if any(actions) and args.inspect:
        parser.error("choose an optional tool/report action or --inspect")
    if any(actions) and args.snapshot:
        parser.error("choose an optional tool/report action or --snapshot")
    if (args.tool or args.replay or args.compare or args.export or args.list_workspaces or args.duration is not None) and (args.search or args.state):
        parser.error("--search and --state apply to dashboard/inventory reports, not optional tool actions")


def print_report(report, json_output=False):
    if json_output:
        print(json.dumps(report.payload(), indent=2, sort_keys=True, allow_nan=False))
    else:
        print("PorkyHUD · %s · %s" % (clean_text(report.tool), clean_text(report.status)))
        for line in report.lines():
            print(line)
    return 0 if report.status in ("LIVE", "PARTIAL", "SAVED", "COMPLETE", "REPLAY", "EXITED") else 1


def handle(args, *, runner, process_rows, capture_sample, record_stream, replay_ui, settings):
    """Return None for a live launch, or an exit status for an explicit action."""
    from .sessions import SessionRecorder, load_session, compare_sessions, export_report
    from .profiles import list_profiles, save_profile
    try:
        if args.list_workspaces:
            names = list_profiles()
            if args.json:
                print(json.dumps({"schema_version": 1, "workspaces": names}))
            else:
                print("\n".join(names) or "No saved workspaces")
            return 0
        if args.save_workspace:
            save_profile(args.save_workspace, settings)
            return print_report(ToolReport("Workspace", "SAVED", "Saved " + clean_text(args.save_workspace)), args.json)
        if args.tool:
            rows = process_rows() if args.tool in ("process", "workload", "job") else ()
            target = args.target or ("START" if args.tool == "quality" else "")
            if args.progress_file:
                target += " | " + args.progress_file
            return print_report(ToolService(runner).run(args.tool, target, rows=rows), args.json)
        if args.replay:
            session = load_session(args.replay)
            if not args.json and replay_ui(session):
                return 0
            return print_report(replay_report(session), args.json)
        if args.compare:
            return print_report(compare_sessions(*args.compare), args.json)
        if args.export:
            export_report(capture_sample(), args.export, include_private=args.include_processes)
            summary = "Report saved with selected process details" if args.include_processes else "Aggregate report saved; private fields omitted"
            return print_report(ToolReport("Support report", "SAVED", summary), args.json)
        if args.record and args.duration is not None:
            recorder = SessionRecorder(args.record, include_private=args.include_processes)
            stream = record_stream(args.duration)
            try:
                for sample in stream:
                    if not recorder.append(sample):
                        break
            except KeyboardInterrupt:
                pass
            finally:
                try:
                    if hasattr(stream, "close"):
                        stream.close()
                finally:
                    result = recorder.stop()
            return print_report(ToolReport("Recording", "ERROR" if result.get("error") else "SAVED" if result.get("complete") else "PARTIAL",
                                           "Session recorded", details=("Writer finished: %s · dropped records: %d" %
                                           (result.get("writer_finished"), result.get("dropped_samples", 0)),
                                           result.get("error", "") or ""), data=result), args.json)
    except (ValueError, OSError) as exc:
        message = clean_text(exc) if isinstance(exc, ValueError) else "Could not read or write the selected file"
        return print_report(ToolReport("Tools", "ERROR", message), args.json)
    return None
