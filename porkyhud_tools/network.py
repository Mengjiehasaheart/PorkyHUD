"""Explicit network tools over an injected bounded, no-shell command runner.

Owner and interface reports are passive. Target checks and networkQuality run
only when their caller explicitly starts them. DNS and TLS stay in children so
the application runner can cancel, terminate and reap every potentially slow
operation. Nothing in this module changes the dashboard's health checks.
"""

from dataclasses import asdict, dataclass
import ipaddress
import json
import math
import re
import socket
import sys
import time
from typing import Callable, Optional, Sequence, Tuple

from porkyhud_inventory import InventoryStatus, ProbeResult
from .model import ToolReport


Runner = Callable[[Sequence[str], float], ProbeResult]
ROW_LIMIT = 128
OUTPUT_LIMIT = 1024 * 1024
MAX_RATE_GAP_S = 60.0


def _clean(value: str, limit: int = 160) -> str:
    return "".join(c for c in value if c.isprintable())[:limit]


def _port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    return port


def _probe(runner: Runner, argv: Sequence[str], timeout: float) -> ProbeResult:
    try:
        result = runner(tuple(argv), timeout)
    except Exception:
        return ProbeResult(status=InventoryStatus.ERROR)
    if not isinstance(result, ProbeResult) or len(result.stdout) > OUTPUT_LIMIT:
        return ProbeResult(status=InventoryStatus.ERROR)
    return result


def _failure(tool: str, result: ProbeResult, summary: str) -> ToolReport:
    return ToolReport(tool, result.status.value, summary,
                      details=("The bounded probe did not return usable data.",))


def _endpoint(value: str) -> Optional[Tuple[str, int]]:
    """Validate numeric lsof endpoints without a hostname/service lookup."""
    match = re.fullmatch(r"(\[[0-9A-Fa-f:.%a-zA-Z0-9_-]+\]|[^:]+):(\d{1,5})", value)
    if not match:
        return None
    address, port_text = match.groups()
    address = address.strip("[]")
    try:
        if address != "*":
            ipaddress.ip_address(address)
        port = int(port_text)
        _port(port)
    except ValueError:
        return None
    return value, port


def parse_port_owners(text: str, port: int, protocol: str = "both") -> ToolReport:
    """Parse selected NUL/newline machine fields, scoped to the local port."""
    _port(port)
    if protocol not in ("both", "tcp", "udp"):
        raise ValueError("protocol must be tcp, udp or both")
    if type(text) is not str or len(text) > OUTPUT_LIMIT:
        raise ValueError("socket output exceeds the text limit")
    rows = []
    pid, command, file = "", "", None
    malformed = False
    ignored = 0
    truncated = False

    def finish() -> None:
        nonlocal malformed, ignored, truncated
        if file is None:
            return
        proto, name = file.get("P", ""), file.get("n", "")
        if not re.fullmatch(r"[0-9]{1,10}", pid) or int(pid) <= 0 or not command or proto not in ("TCP", "UDP") or not name:
            malformed = True
            return
        parts = name.split("->")
        local = _endpoint(parts[0])
        peer = _endpoint(parts[1]) if len(parts) == 2 else None
        if len(parts) > 2 or not local or (len(parts) == 2 and not peer):
            malformed = True
            return
        if local[1] != port or (protocol != "both" and proto.lower() != protocol):
            ignored += 1
            return
        state = file.get("state", "NOT EXPOSED") if proto == "TCP" else "NOT APPLICABLE"
        row = (pid, _clean(command, 80), proto, _clean(local[0]),
               _clean(peer[0]) if peer else "—", _clean(state, 40))
        if row in rows:
            return
        if len(rows) < ROW_LIMIT:
            rows.append(row)
        else:
            truncated = True

    for token in re.split(r"\x00|\n", text):
        if not token:
            continue
        key, value = token[0], token[1:]
        if key == "p":
            finish()
            pid, command, file = value, "", None
            if not value.isdecimal():
                malformed = True
        elif key == "c":
            command = value
        elif key == "f":
            finish()
            file = {}
        elif key in ("P", "n") and file is not None:
            if key in file:
                malformed = True
            file[key] = value
        elif key == "T" and file is not None:
            if value.startswith("ST="):
                file["state"] = value[3:]
        else:
            malformed = True
    finish()
    if text.strip() and not rows and not ignored:
        malformed = True
    status = "PARTIAL" if malformed or truncated else "LIVE"
    if malformed and not rows:
        status = "ERROR"
    summary = ("%d visible local-port owner%s on %d" %
               (len(rows), "" if len(rows) == 1 else "s", port)) if rows else (
                   "No visible local-port owner returned for %d" % port)
    details = ["Protected sockets may be hidden; an empty report does not mean the port is free."]
    if ignored:
        details.append("Excluded %d socket%s whose local port or protocol did not match." %
                       (ignored, "" if ignored == 1 else "s"))
    if malformed:
        details.append("Some socket records could not be decoded.")
    return ToolReport("port-owner", status, summary,
                      ("PID", "Process", "Protocol", "Local bind", "Peer", "State"),
                      tuple(rows), tuple(details), complete=False, truncated=truncated,
                      data={"port": port, "protocol": protocol, "visible_owners": len(rows)})


def inspect_port(port: int, runner: Runner, protocol: str = "both") -> ToolReport:
    _port(port)
    if protocol not in ("both", "tcp", "udp"):
        raise ValueError("protocol must be tcp, udp or both")
    argv = ["/usr/sbin/lsof", "-nP", "-F0pcfPnT"]
    for proto in ("TCP", "UDP"):
        if protocol in ("both", proto.lower()):
            argv.append("-i%s:%d" % (proto, port))
    result = _probe(runner, argv, 3.0)
    if result.status is not InventoryStatus.LIVE:
        return ToolReport("port-owner", result.status.value,
                          "No visible local-port owner returned for %d" % port,
                          details=("Socket visibility may be restricted; this does not mean the port is free.",),
                          data={"port": port, "protocol": protocol})
    return parse_port_owners(result.stdout, port, protocol)


def validate_target(target: str) -> str:
    """Accept a single hostname or IP, including a bounded IPv6 interface zone."""
    if type(target) is not str or not target or len(target) > 253:
        raise ValueError("target must be a hostname or IP address")
    if target != target.strip() or any(not c.isprintable() for c in target):
        raise ValueError("target must be a hostname or IP address")
    if target.startswith("[") and target.endswith("]"):
        target = target[1:-1]
    try:
        ipaddress.ip_address(target)
        if "%" in target and not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", target.split("%", 1)[1]):
            raise ValueError("invalid IPv6 interface zone")
        return target
    except ValueError:
        if ":" in target or "%" in target:
            raise ValueError("invalid IP address")
    try:
        hostname = target.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("invalid hostname") from None
    labels = hostname.rstrip(".").split(".")
    if len(hostname) > 253 or not all(re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        raise ValueError("target must be a hostname or IP address, without a URL or port")
    return hostname


_DNS_SCRIPT = r'''
import json, socket, sys, time
start = time.monotonic()
try:
    values = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), type=socket.SOCK_STREAM)
    rows = []
    for family, kind, proto, canon, addr in values:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        row = {"family": int(family), "address": list(addr)}
        if row not in rows:
            rows.append(row)
        if len(rows) == 9:
            break
    result = {"status": "LIVE" if rows else "ERROR", "addresses": rows[:8], "truncated": len(rows) > 8}
except socket.gaierror:
    result = {"status": "ERROR", "addresses": []}
except OSError:
    result = {"status": "ERROR", "addresses": []}
result["elapsed_ms"] = (time.monotonic() - start) * 1000
print(json.dumps(result, allow_nan=False))
'''


_CONNECT_SCRIPT = r'''
import datetime, errno, json, socket, ssl, sys, time
target, values, budget = sys.argv[1], json.loads(sys.argv[2]), float(sys.argv[3])
deadline = time.monotonic() + budget
tcp = {"status": "ERROR"}
tls = {"status": "NOT RUN"}
connected = None
start = time.monotonic()
for index, row in enumerate(values):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        tcp["status"] = "TIMEOUT"
        break
    sock = socket.socket(row["family"], socket.SOCK_STREAM)
    sock.settimeout(max(0.001, remaining / (len(values) - index)))
    try:
        sock.connect(tuple(row["address"]))
        connected = sock
        tcp = {"status": "LIVE", "address": row["address"][0]}
        break
    except (socket.timeout, TimeoutError):
        tcp = {"status": "TIMEOUT"}
    except OSError as error:
        tcp = {"status": "REFUSED" if error.errno == errno.ECONNREFUSED else "ERROR"}
    finally:
        if connected is not sock:
            sock.close()
tcp["elapsed_ms"] = (time.monotonic() - start) * 1000
if connected is not None:
    start = time.monotonic()
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connected.settimeout(remaining)
        context = ssl.create_default_context()
        with context.wrap_socket(connected, server_hostname=target.split("%", 1)[0],
                                 do_handshake_on_connect=False) as secure:
            secure.settimeout(max(0.001, deadline - time.monotonic()))
            secure.do_handshake()
            cert = secure.getpeercert()
            tls = {"status": "LIVE", "version": secure.version()}
            for key, label in (("notBefore", "not_before"), ("notAfter", "not_after")):
                if key in cert:
                    moment = ssl.cert_time_to_seconds(cert[key])
                    tls[label] = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).isoformat()
    except ssl.SSLCertVerificationError:
        tls = {"status": "CERTIFICATE FAILED"}
    except (socket.timeout, TimeoutError):
        tls = {"status": "TIMEOUT"}
    except (ssl.SSLError, OSError, ValueError):
        tls = {"status": "ERROR"}
    finally:
        connected.close()
    tls["elapsed_ms"] = (time.monotonic() - start) * 1000
print(json.dumps({"tcp": tcp, "tls": tls}, allow_nan=False))
'''


def _cancelled(cancel) -> bool:
    return bool(cancel.is_set()) if hasattr(cancel, "is_set") else bool(cancel()) if callable(cancel) else False


def _json_object(text: str) -> dict:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _milliseconds(value) -> str:
    if _nonnegative_number(value) is not None:
        return "%.1f ms" % value
    return "—"


def _nonnegative_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _dns_rows(value: dict, port: int) -> list:
    rows = value.get("addresses")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError("DNS returned no addresses")
    for row in rows:
        if not isinstance(row, dict) or row.get("family") not in (int(socket.AF_INET), int(socket.AF_INET6)):
            raise ValueError("invalid DNS address family")
        address = row.get("address")
        if not isinstance(address, list) or len(address) not in (2, 4):
            raise ValueError("invalid DNS address")
        ip = ipaddress.ip_address(address[0])
        if (type(address[1]) is not int or address[1] != port
                or (ip.version == 4) != (len(address) == 2)
                or (row["family"] == int(socket.AF_INET)) != (ip.version == 4)):
            raise ValueError("invalid DNS endpoint")
        if len(address) == 4 and any(type(x) is not int or x < 0 or x > 2**32 - 1 for x in address[2:]):
            raise ValueError("invalid IPv6 scope")
    return rows


def diagnose_network(target: str, port: int = 443, timeout_s: float = 5,
                     runner: Optional[Runner] = None, cancel=None) -> ToolReport:
    target, port = validate_target(target), _port(port)
    if _nonnegative_number(timeout_s) is None or not 0.2 <= timeout_s <= 60:
        raise ValueError("timeout must be from 0.2 to 60 seconds")
    if runner is None:
        raise ValueError("a bounded command runner is required")
    deadline = time.monotonic() + timeout_s
    stages = {name: {"status": "NOT RUN"} for name in ("dns", "route", "tcp", "tls")}
    data = {"target": target, "port": port, "budget_s": timeout_s, "stages": stages}

    def report() -> ToolReport:
        statuses = [stage["status"] for stage in stages.values()]
        cancelled = _cancelled(cancel)
        status = "CANCELLED" if cancelled else "LIVE" if all(s == "LIVE" for s in statuses) else "PARTIAL" if "LIVE" in statuses else "TIMEOUT" if "TIMEOUT" in statuses else "ERROR"
        rows = tuple((name.upper(), stage["status"], _milliseconds(stage.get("elapsed_ms")),
                      _clean(stage.get("detail", ""))) for name, stage in stages.items())
        dates = stages["tls"]
        details = ["Explicit target check; DNS, TCP and TLS may contact the named target.",
                   "TLS checks the certificate and hostname; a completed handshake does not test the application."]
        for key, label in (("not_before", "Certificate valid from"), ("not_after", "Certificate expires")):
            if key in dates:
                details.append("%s: %s" % (label, _clean(dates[key], 60)))
        if cancelled:
            details.append("Cancelled; no further stages were started.")
        truncated = bool(stages["dns"].get("truncated", False))
        if truncated:
            details.append("Address lookup returned more than eight unique addresses; tests used the first eight.")
        return ToolReport("network-check", status, "%s:%d target check" % (target, port),
                          ("Stage", "Status", "Duration", "Details"), rows, tuple(details),
                          complete=all(s == "LIVE" for s in statuses) and not cancelled and not truncated,
                          truncated=truncated, data=data)

    if _cancelled(cancel):
        return report()
    dns = _probe(runner, (sys.executable, "-I", "-c", _DNS_SCRIPT, target, str(port)),
                 min(timeout_s * 0.4, 3.0))
    if _cancelled(cancel):
        return report()
    if dns.status is not InventoryStatus.LIVE:
        stages["dns"] = {"status": dns.status.value, "detail": "Address lookup unavailable"}
        return report()
    try:
        value = _json_object(dns.stdout)
        if value.get("status") != "LIVE":
            raise ValueError("address lookup failed")
        addresses = _dns_rows(value, port)
        stages["dns"] = {"status": "LIVE", "elapsed_ms": _nonnegative_number(value.get("elapsed_ms")),
                         "truncated": bool(value.get("truncated", False)),
                         "detail": ", ".join(row["address"][0] for row in addresses)}
    except (ValueError, TypeError, KeyError):
        stages["dns"] = {"status": "ERROR", "detail": "No usable address lookup result"}
        return report()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        stages["tcp"] = {"status": "TIMEOUT", "detail": "Total time budget exhausted"}
        return report()
    # Reserve a bounded slice for the local route lookup after choosing a peer.
    route_budget = min(1.0, remaining * 0.2)
    connect_budget = remaining - route_budget
    connection = _probe(runner, (sys.executable, "-I", "-c", _CONNECT_SCRIPT, target,
                                json.dumps(addresses), str(connect_budget)), connect_budget)
    if _cancelled(cancel):
        return report()
    if connection.status is not InventoryStatus.LIVE:
        stages["tcp"] = {"status": connection.status.value, "detail": "Connection check unavailable"}
    else:
        try:
            value = _json_object(connection.stdout)
            for name in ("tcp", "tls"):
                stage = value[name]
                if not isinstance(stage, dict) or stage.get("status") not in (
                        "LIVE", "ERROR", "TIMEOUT", "REFUSED", "NOT RUN", "CERTIFICATE FAILED"):
                    raise ValueError("invalid connection result")
                stages[name] = {"status": stage["status"], "elapsed_ms": _nonnegative_number(stage.get("elapsed_ms"))}
            if value["tcp"].get("address"):
                peer = str(ipaddress.ip_address(value["tcp"]["address"]))
                if peer not in [str(ipaddress.ip_address(row["address"][0])) for row in addresses]:
                    raise ValueError("unexpected connected address")
                stages["tcp"].update(address=peer, detail=peer)
            for key in ("not_before", "not_after", "version"):
                field = value["tls"].get(key)
                if isinstance(field, str) and len(field) <= 80 and value["tls"]["status"] == "LIVE":
                    stages["tls"][key] = _clean(field)
            stages["tls"]["detail"] = stages["tls"].get("version", "")
        except (ValueError, KeyError, TypeError):
            stages["tcp"] = {"status": "ERROR", "detail": "Could not decode connection result"}
            stages["tls"] = {"status": "NOT RUN"}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        stages["route"] = {"status": "TIMEOUT", "detail": "Total time budget exhausted"}
        return report()
    peer = stages["tcp"].get("address", addresses[0]["address"][0])
    if "%" in target and ipaddress.ip_address(peer).version == 6 and "%" not in peer:
        peer += "%" + target.split("%", 1)[1]
    family = "-inet6" if ipaddress.ip_address(peer).version == 6 else "-inet"
    started = time.monotonic()
    route = _probe(runner, ("/sbin/route", "-n", "get", family, peer), min(route_budget, remaining))
    if _cancelled(cancel):
        return report()
    fields = {}
    if route.status is InventoryStatus.LIVE:
        for line in route.stdout.splitlines():
            key, separator, value = line.strip().partition(":")
            if separator and key in ("gateway", "interface"):
                fields[key] = _clean(value.strip(), 80)
    stages["route"] = {"status": "LIVE" if fields.get("interface") else route.status.value if route.status is not InventoryStatus.LIVE else "ERROR",
                       "elapsed_ms": (time.monotonic() - started) * 1000,
                       "detail": " · ".join(fields.get(key, "") for key in ("interface", "gateway") if fields.get(key)),
                       "address": peer}
    return report()


def parse_network_quality(text: str) -> ToolReport:
    try:
        value = _json_object(text)
    except (ValueError, TypeError):
        return ToolReport("network-quality", "ERROR", "Could not decode network quality results")
    metrics = (("dl_throughput", "Download", "Mbit/s", 1e6),
               ("ul_throughput", "Upload", "Mbit/s", 1e6),
               ("base_rtt", "Idle latency", "ms", 1),
               ("responsiveness", "Responsiveness", "RPM", 1),
               ("dl_responsiveness", "Download responsiveness", "RPM", 1),
               ("ul_responsiveness", "Upload responsiveness", "RPM", 1))
    rows, data = [], {}
    malformed = False
    for key, label, unit, divisor in metrics:
        metric = value.get(key)
        if metric is None:
            continue
        if _nonnegative_number(metric) is None:
            malformed = True
            continue
        rows.append((label, "%.2f" % (metric / divisor), unit))
        data[key] = metric
    interface = value.get("interface_name")
    details = ["This explicit test sends upload/download traffic and uses Internet data.",
               "Responsiveness is measured in round trips per minute (RPM)."]
    if isinstance(interface, str):
        details.append("Interface: %s" % _clean(interface, 80))
        data["interface"] = _clean(interface, 80)
    failed = bool(value.get("error_domain") or value.get("error_code"))
    complete = not failed and not malformed and all(key in data for key in ("dl_throughput", "ul_throughput", "responsiveness"))
    status = "LIVE" if complete else "PARTIAL" if rows else "ERROR"
    if failed:
        details.append("The test reported a failure; retained readings may be partial.")
    return ToolReport("network-quality", status, "Network quality test results",
                      ("Measurement", "Value", "Unit"), tuple(rows), tuple(details),
                      complete=complete, data=data)


def network_quality(runner: Runner) -> ToolReport:
    result = _probe(runner, ("/usr/bin/networkQuality", "-c", "-M", "20"), 25.0)
    if result.status is not InventoryStatus.LIVE:
        return _failure("network-quality", result, "Network quality test unavailable")
    return parse_network_quality(result.stdout)


@dataclass(frozen=True)
class InterfaceCounters:
    name: str
    received_bytes: int
    sent_bytes: int
    input_errors: Optional[int] = None
    output_errors: Optional[int] = None
    drops: Optional[int] = None


def parse_interface_counters(text: str) -> Tuple[InterfaceCounters, ...]:
    """Read only link rows; address rows duplicate the same interface totals."""
    lines = text.splitlines()
    if not lines:
        raise ValueError("interface counter table is empty")
    headers = lines[0].split()
    if any(key not in headers for key in ("Name", "Network", "Ibytes", "Obytes")):
        raise ValueError("interface counter columns are missing")
    tail = headers.index("Ipkts") if "Ipkts" in headers else headers.index("Ibytes")
    tail_headers = headers[tail:]
    result = {}
    for line in lines[1:]:
        values = line.split()
        if not values:
            continue
        if len(values) < 3 or not values[2].startswith("<Link#"):
            continue
        if len(values) < 3 + len(tail_headers):
            raise ValueError("interface counter row is incomplete")
        counters = dict(zip(tail_headers, values[-len(tail_headers):]))

        def number(key: str, required: bool = False) -> Optional[int]:
            value = counters.get(key)
            if value is None or value == "-":
                if required:
                    raise ValueError("byte counter is unavailable")
                return None
            if not value.isdecimal():
                raise ValueError("counter is not a nonnegative integer")
            parsed = int(value)
            if parsed > 2**64 - 1:
                raise ValueError("counter exceeds an unsigned 64-bit value")
            return parsed

        name = values[0].rstrip("*")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", name) or name in result:
            raise ValueError("interface identity is invalid or duplicated")
        drops_key = next((key for key in ("Drop", "Drops", "Idrop", "Idrops") if key in counters), "Drop")
        result[name] = InterfaceCounters(name, number("Ibytes", True), number("Obytes", True),
                                         number("Ierrs"), number("Oerrs"), number(drops_key))
        if len(result) > ROW_LIMIT:
            raise ValueError("interface table exceeds the record limit")
    if not result:
        raise ValueError("no link counter rows were returned")
    return tuple(result.values())


def interface_diagnostics(current: Sequence[InterfaceCounters],
                          previous: Optional[Sequence[InterfaceCounters]] = None,
                          elapsed_s: Optional[float] = None) -> ToolReport:
    previous_by_name = {item.name: item for item in previous or ()}
    valid_span = (_nonnegative_number(elapsed_s) is not None
                  and 0 < elapsed_s <= MAX_RATE_GAP_S)
    rows, records = [], []
    for item in current[:ROW_LIMIT]:
        before = previous_by_name.get(item.name)
        rates = None
        reset = bool(before and (item.received_bytes < before.received_bytes or item.sent_bytes < before.sent_bytes))
        if before and valid_span and not reset:
            rates = ((item.received_bytes - before.received_bytes) / elapsed_s,
                     (item.sent_bytes - before.sent_bytes) / elapsed_s)
        rate_status = "LIVE" if rates is not None else "RESET" if reset else "BASELINE"
        rows.append((item.name, str(item.received_bytes), str(item.sent_bytes),
                     str(item.input_errors) if item.input_errors is not None else "—",
                     str(item.output_errors) if item.output_errors is not None else "—",
                     str(item.drops) if item.drops is not None else "NOT EXPOSED",
                     "%.1f" % rates[0] if rates else "—", "%.1f" % rates[1] if rates else "—", rate_status))
        record = asdict(item)
        record.update(receive_bytes_per_s=rates[0] if rates else None,
                      send_bytes_per_s=rates[1] if rates else None, rate_status=rate_status)
        records.append(record)
    truncated = len(current) > ROW_LIMIT
    return ToolReport("interface-counters", "LIVE" if current else "ERROR",
                      "%d interface counter rows" % len(rows),
                      ("Interface", "Received B", "Sent B", "Input errors", "Output errors", "Drops", "Receive B/s", "Send B/s", "Rate status"),
                      tuple(rows), ("Byte, error and drop totals are cumulative counters.",
                                    "Rates require two samples within 60 seconds; resets and gaps start a new baseline.",
                                    "Drops remain NOT EXPOSED when the command provides no drop column."),
                      complete=bool(current) and not truncated, truncated=truncated,
                      data={"interfaces": records, "elapsed_s": elapsed_s if valid_span else None})


def collect_interface_diagnostics(runner: Runner,
                                  previous: Optional[Sequence[InterfaceCounters]] = None,
                                  elapsed_s: Optional[float] = None) -> ToolReport:
    result = _probe(runner, ("/usr/sbin/netstat", "-ibn"), 2.0)
    if result.status is not InventoryStatus.LIVE:
        return _failure("interface-counters", result, "Interface counters unavailable")
    try:
        current = parse_interface_counters(result.stdout)
    except ValueError:
        return ToolReport("interface-counters", "ERROR", "Could not decode interface counters")
    return interface_diagnostics(current, previous, elapsed_s)
