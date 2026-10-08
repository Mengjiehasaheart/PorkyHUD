"""Validated local presentation profiles and independent custom watch notices."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Optional


MAX_PROFILE_BYTES = 64 * 1024
MAX_PROFILES = 128
MAX_WATCHES = 16
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_FIELD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_MODES = frozenset(("balanced", "compute", "thermals", "io", "processes", "insights",
                    "devices", "network", "connections", "system", "cinema", "tools"))
_THEMES = frozenset(("graphite", "neon-grid", "catppuccin", "solar-circuit"))
_STYLES = frozenset(("sculpted", "outline", "ascii"))
_MOTIONS = frozenset(("off", "calm", "vivid"))
_STATES = frozenset(("all", "listeners", "established", "other"))
_SORTS = frozenset(("cpu", "memory", "gpu", "CPU", "MEM", "GPU"))
_KEYS = frozenset(("mode", "theme", "style", "motion", "no_intro", "sort", "search",
                   "state", "watches", "searches", "pinned_pid", "pinned_start_id"))
_OPERATORS = frozenset((">", ">=", "<", "<=", "==", "!="))


def _finite(value: Any, label: str, *, minimum: Optional[float] = None,
            maximum: Optional[float] = None) -> float:
    if type(value) not in (int, float):
        raise ValueError("Invalid %s" % label)
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Invalid %s" % label) from None
    if (not math.isfinite(result) or (minimum is not None and result < minimum)
            or (maximum is not None and result > maximum)):
        raise ValueError("Invalid %s" % label)
    return result


def _text(value: Any, limit: int, label: str) -> str:
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Invalid %s" % label)
    return value


@dataclass(frozen=True)
class WatchRule:
    field: str
    operator: str
    threshold: float
    duration_s: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or _FIELD_NAME.fullmatch(self.field) is None:
            raise ValueError("Invalid watch field")
        if not isinstance(self.operator, str) or self.operator not in _OPERATORS:
            raise ValueError("Invalid watch operator")
        object.__setattr__(self, "threshold", _finite(self.threshold, "watch threshold"))
        object.__setattr__(self, "duration_s", _finite(
            self.duration_s, "watch duration", minimum=0, maximum=3600))


def _watch(value: Any) -> WatchRule:
    if isinstance(value, WatchRule):
        return value
    if not isinstance(value, Mapping) or set(value) - {"field", "operator", "threshold", "duration_s"}:
        raise ValueError("Invalid watch rule")
    try:
        return WatchRule(value["field"], value["operator"], value["threshold"],
                         value.get("duration_s", 0))
    except KeyError:
        raise ValueError("Incomplete watch rule") from None


def _settings(value: Any) -> dict:
    if not isinstance(value, Mapping) or set(value) - _KEYS:
        raise ValueError("Unsupported profile setting")
    normalized = {}
    value = dict(value)
    if value.get("mode") == "overview":
        value["mode"] = "balanced"
    enums = {"mode": _MODES, "theme": _THEMES, "style": _STYLES, "motion": _MOTIONS,
             "state": _STATES, "sort": _SORTS}
    for key, allowed in enums.items():
        if key in value:
            if not isinstance(value[key], str) or value[key] not in allowed:
                raise ValueError("Invalid profile %s" % key)
            normalized[key] = value[key]
    if "no_intro" in value:
        if type(value["no_intro"]) is not bool:
            raise ValueError("Invalid profile intro setting")
        normalized["no_intro"] = value["no_intro"]
    if "search" in value:
        normalized["search"] = _text(value["search"], 160, "profile search")
    if "searches" in value:
        searches = value["searches"]
        if not isinstance(searches, Mapping) or len(searches) > len(_MODES):
            raise ValueError("Invalid profile searches")
        if any(key not in _MODES for key in searches):
            raise ValueError("Invalid profile search view")
        normalized["searches"] = {
            key: _text(query, 160, "profile search") for key, query in searches.items()}
    if "watches" in value:
        watches = value["watches"]
        if not isinstance(watches, (list, tuple)) or len(watches) > MAX_WATCHES:
            raise ValueError("Too many profile watches")
        normalized["watches"] = [asdict(_watch(watch)) for watch in watches]
    if "pinned_pid" in value or "pinned_start_id" in value:
        pid, start_id = value.get("pinned_pid"), value.get("pinned_start_id")
        if pid is None and start_id is None:
            normalized.update(pinned_pid=None, pinned_start_id=None)
        else:
            if type(pid) is not int or not 1 <= pid < 2**31:
                raise ValueError("Invalid pinned process ID")
            identifier = _text(start_id, 128, "pinned process start identifier")
            if not identifier:
                raise ValueError("A pinned process requires a start identifier")
            normalized.update(pinned_pid=pid, pinned_start_id=identifier)
    return normalized


def _directory(root=None) -> Path:
    return (Path(root).expanduser() if root is not None else
            Path.home() / ".config" / "porkyhud" / "profiles")


def _name(name: Any) -> str:
    if not isinstance(name, str) or _PROFILE_NAME.fullmatch(name) is None:
        raise ValueError("Profile names use letters, numbers, hyphens, and underscores")
    return name


def save_profile(name, settings, root=None) -> dict:
    """Atomically save allowlisted settings with owner-only file permissions."""

    name = _name(name)
    normalized = _settings(settings)
    directory = _directory(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / (name + ".json")
    if target.is_symlink():
        raise ValueError("Profile files cannot be symlinks")
    if not target.exists() and len(list_profiles(root)) >= MAX_PROFILES:
        raise ValueError("Profile limit reached")
    encoded = (json.dumps({"schema_version": 1, "name": name, "settings": normalized},
                          allow_nan=False, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_PROFILE_BYTES:
        raise ValueError("Profile is too large")
    descriptor, temporary = tempfile.mkstemp(prefix=".porkyhud-", suffix=".json", dir=str(directory))
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
        os.replace(temporary, str(target))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return normalized


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate profile field")
        result[key] = value
    return result


def load_profile(name, root=None) -> dict:
    """Load only the requested safe-name v1 profile and revalidate all settings."""

    name = _name(name)
    selected = _directory(root) / (name + ".json")
    if selected.is_symlink() or not selected.is_file():
        raise ValueError("Profile is missing or is not a regular file")
    with selected.open("rb") as stream:
        raw = stream.read(MAX_PROFILE_BYTES + 1)
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("Profile is too large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(
                               ValueError("Invalid profile number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Invalid profile JSON") from None
    if (not isinstance(value, Mapping) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("name") != name
            or set(value) != {"schema_version", "name", "settings"}):
        raise ValueError("Unsupported profile format")
    return _settings(value["settings"])


def list_profiles(root=None) -> tuple:
    """List safe local profile names without reading their contents."""

    directory = _directory(root)
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ValueError("Profile location is not a directory")
    names = []
    # Bound directory traversal as well as the returned list.
    for index, entry in enumerate(directory.iterdir()):
        if index >= 4096:
            raise ValueError("Profile directory contains too many entries")
        if (entry.suffix == ".json" and _PROFILE_NAME.fullmatch(entry.stem)
                and entry.is_file() and not entry.is_symlink()):
            names.append(entry.stem)
            if len(names) > MAX_PROFILES:
                raise ValueError("Profile limit exceeded")
    return tuple(sorted(names))


class WatchEngine:
    """Evaluate bounded custom rules; notices never modify the core health result."""

    def __init__(self, rules: Iterable[WatchRule], max_gap_s: float = 5.0) -> None:
        collected = []
        for value in rules:
            if len(collected) == MAX_WATCHES:
                raise ValueError("Too many watches")
            collected.append(_watch(value))
        self.rules = tuple(collected)
        self.max_gap_s = _finite(max_gap_s, "watch gap", minimum=0.001, maximum=300)
        self._started = [None] * len(self.rules)
        self._last_elapsed = None
        self._active = ()
        self._last_revisions = [None] * len(self.rules)
        self._last_reading_times = [None] * len(self.rules)
        self._rule_notices = [None] * len(self.rules)

    def reset(self) -> None:
        self._started = [None] * len(self.rules)
        self._last_elapsed = None
        self._active = ()
        self._last_revisions = [None] * len(self.rules)
        self._last_reading_times = [None] * len(self.rules)
        self._rule_notices = [None] * len(self.rules)

    @staticmethod
    def _matches(value: float, operator: str, threshold: float) -> bool:
        if operator == ">":
            return value > threshold
        if operator == ">=":
            return value >= threshold
        if operator == "<":
            return value < threshold
        if operator == "<=":
            return value <= threshold
        if operator == "==":
            return value == threshold
        return value != threshold

    def update(self, sample: dict) -> tuple:
        if not isinstance(sample, Mapping):
            self.reset()
            return ()
        try:
            elapsed = _finite(sample.get("elapsed_s"), "watch sample time", minimum=0)
        except ValueError:
            self.reset()
            return ()
        if self._last_elapsed is not None:
            gap = elapsed - self._last_elapsed
            if gap < 0 or gap > self.max_gap_s:
                self.reset()
            elif gap == 0:
                return tuple(dict(notice) for notice in self._active)
        self._last_elapsed = elapsed
        metrics = sample.get("metrics")
        if not isinstance(metrics, Mapping):
            self.reset()
            self._last_elapsed = elapsed
            return ()
        notices = []
        for index, rule in enumerate(self.rules):
            reading = metrics.get(rule.field)
            if not isinstance(reading, Mapping) or reading.get("status") not in ("LIVE", "OK"):
                self._started[index] = None
                self._last_revisions[index] = None
                self._last_reading_times[index] = None
                self._rule_notices[index] = None
                continue
            try:
                value = _finite(reading.get("value"), "watch reading")
                age = _finite(reading.get("age_s", 0), "watch reading age", minimum=0)
                source_gap = _finite(reading.get("max_gap_s", self.max_gap_s),
                                     "watch source gap", minimum=0, maximum=300)
                if source_gap == 0 or age > source_gap:
                    raise ValueError("Watch source is too old")
            except ValueError:
                self._started[index] = None
                self._rule_notices[index] = None
                continue
            revision = reading.get("revision")
            if revision is not None and (type(revision) is not int or revision < 0):
                self._started[index] = None
                self._rule_notices[index] = None
                continue
            reading_time = max(0.0, elapsed - age)
            if (revision is not None and self._last_revisions[index] is not None
                    and revision < self._last_revisions[index]):
                self._started[index] = None
                self._rule_notices[index] = None
            if revision is not None and revision == self._last_revisions[index]:
                if self._rule_notices[index] is not None:
                    notices.append(dict(self._rule_notices[index]))
                continue
            previous_time = self._last_reading_times[index]
            if (previous_time is not None and (reading_time <= previous_time
                    or reading_time - previous_time > source_gap)):
                self._started[index] = None
                self._rule_notices[index] = None
            self._last_revisions[index] = revision
            self._last_reading_times[index] = reading_time
            if not self._matches(value, rule.operator, rule.threshold):
                self._started[index] = None
                self._rule_notices[index] = None
                continue
            if self._started[index] is None:
                self._started[index] = reading_time
            duration = reading_time - self._started[index]
            if duration < rule.duration_s:
                continue
            notice = {"field": rule.field, "operator": rule.operator,
                            "threshold": rule.threshold, "value": value,
                            "duration_s": duration,
                            "message": "%s %s %g for %.1f s" % (
                                rule.field, rule.operator, rule.threshold, duration)}
            self._rule_notices[index] = notice
            notices.append(notice)
        self._active = tuple(notices)
        return tuple(dict(notice) for notice in self._active)
