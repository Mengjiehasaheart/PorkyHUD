"""Optional tool state, bounded background execution, and session workflow."""

from concurrent.futures import ThreadPoolExecutor
import re
import threading
from .model import ToolReport, clean_text
from .service import ToolService, TOOL_BY_KEY
from .observations import replay_report


def parse_watch_rule(value):
    from .profiles import WatchRule
    match = re.fullmatch(r"([a-z_]+)\s*(>=|<=|>|<|==)\s*([0-9]+(?:\.[0-9]+)?)\s+for\s+([0-9]+(?:\.[0-9]+)?)", value.strip())
    if not match:
        raise ValueError("Use field > value for seconds")
    field, operator, threshold, duration = match.groups()
    return WatchRule(field, operator, float(threshold), float(duration))


class Workbench:
    def __init__(self, runner_factory, settings, apply_settings, include_processes=False):
        from .sessions import SessionRecorder
        from .profiles import WatchEngine
        self.runner_factory = runner_factory
        self.include_processes = include_processes
        self.settings = settings
        self.apply_settings = apply_settings
        self.history = SessionRecorder(max_samples=600, max_bytes=16 * 1024 * 1024, include_private=True)
        self.recording = None
        self.rows = ()
        self.latest = None
        self.pinned_pid = None
        self.pinned_start_id = ""
        self.job_progress = None
        self.watch_rules = []
        self.watches = WatchEngine(())
        self.notices = ()
        self.visible = False
        self.report = None
        self.pending = ""
        self.text = ""
        self.scroll = 0
        self.replay = None
        self.replay_index = 0
        self.paused = False
        self.revision = 0
        self.profile_name = ""
        self.active_key = ""
        self.active_value = ""
        self.time_mode = "REPLAY"
        self._executor = None
        self._future = None
        self._cancel = threading.Event()
        self._generation = 0
        self._job_generation = 0
        self._folder = None
        self._closed = threading.Event()

    @property
    def editing(self):
        return bool(self.pending)

    def open(self, key, value=None):
        if self._closed.is_set():
            raise RuntimeError("Workbench is closed")
        self.visible = True
        self.paused = False
        self.replay = None
        self.scroll = 0
        self.revision += 1
        self.active_key, self.active_value = key, value or ""
        if key == "record" and self.recording is not None:
            self._submit("stop_record", "")
            return
        spec = TOOL_BY_KEY[key]
        self.report = ToolReport(spec.title, "READY", spec.hint)
        if spec.prompt and value is None:
            self.pending, self.text = key, ""
        else:
            self.pending = ""
            self._submit(key, value or "")

    def _submit(self, key, value, quiet=False):
        if self._closed.is_set():
            return
        if self._future is not None:
            self.report = ToolReport("Tools", "BUSY", "Previous tool is still stopping; try again shortly")
            return
        self.pending = ""
        self.active_key, self.active_value = key, value
        self._cancel = threading.Event()
        self._job_generation = self._generation
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="porkyhud-tool")
        rows = self.rows
        samples = ()
        settings = self.settings()
        if not quiet:
            self.report = ToolReport(TOOL_BY_KEY[key].title if key in TOOL_BY_KEY else
                                     "Process trends" if key == "trends" else "Recording",
                                     "CHECKING", "Working · Esc cancels and returns to LIVE")
        self.revision += 1
        self._future = self._executor.submit(self._execute_guarded, key, value, rows, samples,
                                            settings, self._cancel, self._job_generation)

    def _execute_guarded(self, key, value, rows, samples, settings, cancellation, generation):
        if self._closed.is_set() or cancellation.is_set():
            return ToolReport("Tools", "CANCELLED", "Tool cancelled"), None
        report, effect = self._execute(key, value, rows, samples, settings)
        if (self._closed.is_set() or cancellation.is_set()
                or generation != self._generation):
            if effect and effect[0] == "record":
                # This runs on the tool worker, including after close() timed out.
                effect[1].stop()
                effect = None
            return report, effect
        return report, effect

    @staticmethod
    def _finish_recorder_cleanup(recorder):
        recorder.stop()
        return ToolReport("Recording", "CANCELLED", "Recording cancelled"), None

    def _discard_recorder(self, recorder):
        # Signal shutdown immediately, without joining a writer on the HUD thread.
        recorder.stop(timeout_s=0)
        if self._closed.is_set():
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="porkyhud-tool")
        self._future = self._executor.submit(self._finish_recorder_cleanup, recorder)

    def _execute(self, key, value, rows, samples, settings):
        from .sessions import SessionRecorder, load_session, compare_sessions, export_report
        from .profiles import save_profile, load_profile, WatchRule
        if key == "power":
            samples = self.history.samples
        if key == "timeline":
            return self.history.to_report(), None
        if key == "trends":
            from .sessions import process_trends
            return process_trends(self.history.samples, int(value), self.pinned_start_id), None
        if key == "record":
            recorder = SessionRecorder(value, include_private=self.include_processes)
            try:
                if self.latest is not None:
                    recorder.append(self.latest)
            except BaseException:
                recorder.stop()
                raise
            summary = ("Recording started · selected process details included" if self.include_processes else
                       "Recording started · private fields omitted")
            return ToolReport("Recording", "RECORDING", summary,
                              details=("Open Record / stop session to finish",)), ("record", recorder)
        if key == "stop_record":
            result = self.recording.stop()
            return ToolReport("Recording", "SAVED" if result.get("complete", False) else "STOPPED",
                              "Recording stopped", details=("Writer finished: %s · dropped records: %d" %
                              (result.get("writer_finished"), result.get("dropped_samples", 0)),
                              result.get("error", "") or ""), data=result), ("stop_record", None)
        if key == "mark":
            self.history.mark(value)
            if self.recording is not None:
                self.recording.mark(value)
            return ToolReport("Bookmark", "SAVED", clean_text(value, 120)), None
        if key == "replay":
            session = load_session(value)
            return replay_report(session, 0), ("replay", session)
        if key == "compare":
            left, separator, right = value.partition("|")
            if not separator:
                raise ValueError("Choose two session paths separated by |")
            return compare_sessions(left.strip(), right.strip()), None
        if key == "export":
            exported = export_report(self.history.to_report().data, value)
            return ToolReport("Support report", "SAVED", "Aggregate report saved",
                              details=("Commands, host name, addresses and paths omitted",), data=exported), None
        if key == "profile":
            saved = save_profile(value, settings)
            return ToolReport("Workspace", "SAVED", "Workspace saved: " + clean_text(value)), ("profile_name", value)
        if key == "load_profile":
            saved = load_profile(value)
            return ToolReport("Workspace", "LOADED", "Workspace loaded: " + clean_text(value)), ("profile", (value, saved))
        if key == "reset":
            return ToolReport("Workspace", "RESET", "Overview restored; personal watches and pins cleared"), ("reset", None)
        if key == "watch":
            if len(self.watch_rules) >= 16:
                raise ValueError("Personal watch limit is 16; reset the workspace to clear watches")
            rule = parse_watch_rule(value)
            if self.latest is None or rule.field not in self.latest["metrics"]:
                raise ValueError("Choose a field shown in Session timeline")
            return ToolReport("Personal watch", "SAVED", clean_text(value)), ("watch", rule)
        service = ToolService(self.runner_factory(self._cancel), self._cancel)
        report = service.run(key, value, rows=rows, samples=samples)
        effect = None
        if key in ("process", "workload", "job") and report.status in ("LIVE", "PARTIAL"):
            pid = int(value.partition("|")[0])
            row = next((row for row in rows if row["pid"] == pid), None)
            start_id = (report.data.get("start_id") or report.data.get("identity", {}).get("start_id")
                        or (row.get("start_id", "") if row else ""))
            effect = ("pin", (pid, start_id, value.partition("|")[2].strip() or None))
        if key == "folder":
            effect = ("folder", report)
        return report, effect

    def poll(self):
        if self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        try:
            report, effect = future.result()
        except Exception as exc:
            # Only deliberate argument/validation messages cross the boundary.
            message = clean_text(exc) if isinstance(exc, ValueError) else "Tool could not complete; retry or choose another target"
            report, effect = ToolReport("Tools", "ERROR", message), None
        if self._closed.is_set() or self._job_generation != self._generation:
            if effect and effect[0] == "record":
                self._discard_recorder(effect[1])
            if effect and effect[0] == "stop_record":
                self.recording = None
            return
        self.report = report
        if effect:
            key, value = effect
            if key == "record":
                self.recording = value
            elif key == "stop_record":
                self.recording = None
            elif key == "replay":
                self.replay, self.replay_index = value, 0
                self.time_mode = "REPLAY"
            elif key == "pin":
                self.pinned_pid, self.pinned_start_id, self.job_progress = value
            elif key == "profile_name":
                self.profile_name = value
            elif key == "profile":
                self.profile_name = value[0]
                self.apply_settings(value[1])
                self.restore_watches(value[1].get("settings", value[1]))
            elif key == "watch":
                from .profiles import WatchEngine
                self.watch_rules.append(value)
                self.watch_rules = self.watch_rules[-16:]
                self.watches = WatchEngine(tuple(self.watch_rules))
            elif key == "reset":
                self.watch_rules, self.notices, self.profile_name = [], (), ""
                self.pinned_pid, self.pinned_start_id = None, ""
                from .profiles import WatchEngine
                self.watches = WatchEngine(())
                self.apply_settings({"mode": "balanced", "theme": "graphite", "style": "sculpted",
                                     "motion": "off", "no_intro": True, "searches": {}, "state": "all", "sort": "CPU"})
            elif key == "folder":
                if self._folder and self._folder.data.get("root") == value.data.get("root"):
                    from .storage import compare_folder_snapshots
                    self.report = compare_folder_snapshots(self._folder, value)
                self._folder = value
        self.revision += 1

    def restore_watches(self, settings):
        from .profiles import WatchRule, WatchEngine
        self.watch_rules = [WatchRule(**item) for item in settings.get("watches", ())]
        self.watches = WatchEngine(tuple(self.watch_rules))
        self.pinned_pid = settings.get("pinned_pid")
        self.pinned_start_id = settings.get("pinned_start_id", "")

    def observe(self, sample, rows, observed_mono_s=None):
        self.rows = tuple(rows)
        # A compact process selection keeps the default timeline inexpensive.
        selected = list(rows[:32])
        if self.pinned_pid is not None:
            selected.extend(row for row in rows if row["pid"] == self.pinned_pid and row not in selected)
        sample = {**sample, "processes": selected[:64], "processes_complete": False,
                  "processes_truncated": len(rows) > len(selected[:64])}
        self.latest = sample
        self.history.append(sample, observed_mono_s=observed_mono_s)
        if self.recording is not None:
            self.recording.append(sample, observed_mono_s=observed_mono_s)
        notices = self.watches.update(sample)
        if notices != self.notices:
            self.notices = notices
            self.revision += 1
        if self.visible and not self.editing and self._future is None and not self.paused and self.replay is None:
            if self.report and self.report.tool in ("Session", "Session timeline"):
                self._submit("timeline", "", quiet=True)
            elif self.active_key == "trends":
                self._submit("trends", str(self.pinned_pid), quiet=True)
            elif self.active_key in ("workload", "job", "power"):
                current = next((row for row in self.rows if row["pid"] == self.pinned_pid), None)
                if self.active_key == "power" or current is not None and current.get("start_id") == self.pinned_start_id:
                    self._submit(self.active_key, self.active_value, quiet=True)
                else:
                    self.report = ToolReport("Pinned workload", "PID CHANGED" if current else "UNAVAILABLE",
                                             "The original pinned process is no longer visible")
                    self.revision += 1

    def handle_key(self, key):
        """Return True when an overlay consumes input before dashboard shortcuts."""
        if not self.visible:
            return False
        code = ord(key) if isinstance(key, str) and len(key) == 1 else key
        if code == 27:
            self._cancel.set()
            self._generation += 1
            self.visible, self.pending, self.replay = False, "", None
            self.revision += 1
            return True
        if self.editing:
            if code in (10, 13, 343):
                self._submit(self.pending, self.text.strip())
            elif code in (8, 127, 263):
                self.text = self.text[:-1]
            elif code == 21:
                self.text = ""
            elif isinstance(key, str) and key.isprintable() and len(self.text) < 1024:
                self.text += key
            elif isinstance(code, int) and 32 <= code < 127 and len(self.text) < 1024:
                self.text += chr(code)
        elif self.replay is not None and code in (260, 261, 259, 258, ord("j"), ord("k"), ord(" ")):
            self.replay_index = min(len(self.replay["samples"]) - 1, max(0, self.replay_index + (1 if code in (261, 258, ord("j"), ord(" ")) else -1)))
            self.report = replay_report(self.replay, self.replay_index)
            self.scroll = 0
        elif code == 260 and self.report and self.report.tool == "Session timeline" and self.report.data.get("samples"):
            self.replay = self.report.data
            self.replay_index = max(0, len(self.replay["samples"]) - 2)
            self.time_mode = "PAUSED"
            self.report = replay_report(self.replay, self.replay_index)
            self.scroll = 0
        elif code == ord(" "):
            self.paused = not self.paused
        elif code in (258, ord("j"), 338):
            self.scroll += 10 if code == 338 else 1
        elif code in (259, ord("k"), 339):
            self.scroll = max(0, self.scroll - (10 if code == 339 else 1))
        elif code in (ord("o"), ord("r")) and self.pinned_pid:
            current = next((row for row in self.rows if row["pid"] == self.pinned_pid), None)
            if current is None:
                self.report = ToolReport("Pinned workload", "UNAVAILABLE", "The pinned process is no longer visible")
            elif not self.pinned_start_id or not current.get("start_id"):
                self.report = ToolReport("Pinned workload", "UNAVAILABLE", "Process start identity is unavailable")
            elif current["start_id"] != self.pinned_start_id:
                self.report = ToolReport("Pinned workload", "PID CHANGED", "The pinned process exited; its ID belongs to another process")
            else:
                self.open("workload" if code == ord("o") else "job", str(self.pinned_pid) +
                          (" | " + self.job_progress if code == ord("r") and self.job_progress else ""))
        elif code == ord("v") and self.pinned_pid:
            self.active_key, self.active_value = "trends", str(self.pinned_pid)
            self._submit("trends", self.active_value)
            self.scroll = 0
        elif code in (ord("q"), ord("Q")):
            return False
        if code != -1:
            self.revision += 1
        return True

    def lines(self):
        if self.editing:
            spec = TOOL_BY_KEY[self.pending]
            details = (spec.hint, "", spec.prompt, "> " + self.text + "_", "", "Enter runs this action · Esc returns to LIVE")
            if self.pending == "export":
                details += ("", "Preview: timestamps, units, CPU, memory, storage, traffic, battery,", "available sensors, source status and aggregate summaries.", "Private process details, host name, paths and addresses are omitted.")
            return details
        return self.report.lines() if self.report else ("Choose a tool from All Tools",)

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._generation += 1
        self._cancel.set()
        if self._future:
            try:
                self._future.result(timeout=1.5)
            except Exception:
                pass
            self.poll()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        if self.recording:
            self.recording.stop()
        self.history.stop()
