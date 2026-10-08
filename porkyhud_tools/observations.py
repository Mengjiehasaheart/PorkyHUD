"""Pure projections of existing readings and complete inventory changes."""

from datetime import datetime, timezone
from .model import finite_number, clean_text, ToolReport


def observation(metrics, elapsed_s, *, processes=(), health=None, events=(), timestamp=None):
    projected = {}
    for name, (value, unit, status) in metrics.items():
        number = finite_number(value)
        projected[name] = {"value": number, "unit": unit,
                           "status": status if number is not None else (status if status != "LIVE" else "UNAVAILABLE")}
    return {"schema_version": 1,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "elapsed_s": elapsed_s, "metrics": projected,
            "processes": list(processes), "events": list(events),
            "health": health or {}}


class ChangeTracker:
    """Compare only successful, complete inventories, bounded to current IDs."""

    def __init__(self):
        self._previous = {}

    def update(self, kind, records, *, complete, status, timestamp):
        if not complete or status != "LIVE":
            self._previous.pop(kind, None)
            return ()
        current = {str(key): clean_text(label, 120) for key, label in records}
        if len(current) != len(records):
            self._previous.pop(kind, None)
            return ()
        before = self._previous.get(kind)
        self._previous[kind] = current
        if before is None:
            return ()
        events = []
        for key in sorted(current.keys() & before.keys()):
            if current[key] != before[key]:
                events.append({"timestamp": timestamp, "kind": kind, "label": kind + " changed"})
        for key in sorted(current.keys() - before.keys()):
            events.append({"timestamp": timestamp, "kind": kind, "label": kind + " added"})
        for key in sorted(before.keys() - current.keys()):
            events.append({"timestamp": timestamp, "kind": kind, "label": kind + " removed"})
        return tuple(events[:128])


def inventory_keys(kind, items):
    from dataclasses import asdict
    import json
    records = []
    for item in items:
        values = asdict(item)
        if kind == "network":
            key = values.get("name", "")
        elif kind == "connections":
            key = (values.get("protocol"), values.get("local"), values.get("remote"))
        else:
            key = tuple(values.get(field, "") for field in ("kind", "vendor", "product_id", "parent", "port", "name"))
        records.append((str(key), json.dumps(values, sort_keys=True)))
    return tuple(records)


def replay_report(session, index=-1):
    samples = session.get("samples", ())
    if not samples:
        return ToolReport("Replay", "EMPTY", "Session has no readings")
    index = min(len(samples) - 1, max(0, index if index >= 0 else len(samples) - 1))
    sample = samples[index]
    rows = tuple((name, "—" if value.get("value") is None else "%.3g" % value["value"],
                  value.get("unit", ""), value.get("status", "UNAVAILABLE"))
                 for name, value in sample.get("metrics", {}).items())
    return ToolReport("Replay", "REPLAY", "%s · frame %d/%d · %.1fs" %
                      (sample["timestamp"], index + 1, len(samples), sample["elapsed_s"]),
                      ("Reading", "Value", "Unit", "Status"), rows,
                      ("Historical readings · arrows move through time · Esc returns to LIVE",),
                      complete=bool(session.get("complete")), data=sample)
