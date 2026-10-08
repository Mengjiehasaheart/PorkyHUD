"""On-demand, read-only macOS inventory through an injected bounded runner.

This module owns parsers and minimized immutable records, never curses, health
policy, privileges, or subprocess lifecycle.  The application supplies its
existing no-shell command adapter.  Raw responses are not retained in results.
"""

from dataclasses import dataclass, replace
from enum import Enum
import ipaddress
import json
import math
import re
from typing import Callable, Generic, Mapping, Optional, Sequence, Tuple, TypeVar, Union
import unicodedata


RECORD_LIMIT = 128
NODE_LIMIT = 2048
T = TypeVar("T")


class InventoryStatus(str, Enum):
    CHECKING = "CHECKING"
    LIVE = "LIVE"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    PERMISSION_DENIED = "PERMISSION DENIED"
    MISSING = "MISSING"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class ProbeResult:
    stdout: str = ""
    status: InventoryStatus = InventoryStatus.LIVE
    detail: str = ""

    def __post_init__(self) -> None:
        if type(self.stdout) is not str or not isinstance(self.status, InventoryStatus):
            raise ValueError("probe requires text and a typed status")


@dataclass(frozen=True)
class DeviceRecord:
    kind: str
    name: str
    vendor: str = ""
    product_id: str = ""
    speed: str = ""
    parent: str = ""
    port: str = ""


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    state: str
    ipv4: Tuple[str, ...] = ()
    ipv6: Tuple[str, ...] = ()
    mac: str = ""
    medium: str = ""
    mtu: Optional[int] = None
    hardware_port: str = ""

    @property
    def addresses(self) -> Tuple[str, ...]:
        return self.ipv4 + self.ipv6


@dataclass(frozen=True)
class ConnectionRecord:
    protocol: str
    local: str
    remote: str
    state: str
    recv_queue: int = 0
    send_queue: int = 0


@dataclass(frozen=True)
class InventoryResult(Generic[T]):
    status: InventoryStatus = InventoryStatus.CHECKING
    items: Tuple[T, ...] = ()
    detail: str = "awaiting inventory"
    truncated: bool = False
    complete: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, InventoryStatus) or type(self.items) is not tuple:
            raise ValueError("inventory requires typed status and immutable items")
        if type(self.truncated) is not bool or type(self.complete) is not bool:
            raise ValueError("inventory completeness must be explicit")
        if self.complete and (self.truncated or self.status is not InventoryStatus.LIVE):
            raise ValueError("only an untruncated live result is complete")


@dataclass(frozen=True)
class NetworkInventory:
    status: InventoryStatus = InventoryStatus.CHECKING
    items: Tuple[NetworkInterface, ...] = ()
    detail: str = "awaiting network inventory"
    truncated: bool = False
    complete: bool = False
    gateway: str = ""
    default_interface: str = ""
    dns_servers: Tuple[str, ...] = ()
    dns_status: InventoryStatus = InventoryStatus.CHECKING
    route_status: InventoryStatus = InventoryStatus.CHECKING
    hardware_status: InventoryStatus = InventoryStatus.CHECKING

    def __post_init__(self) -> None:
        InventoryResult(self.status, self.items, self.detail, self.truncated, self.complete)
        if any(not isinstance(item, NetworkInterface) for item in self.items):
            raise ValueError("network inventory must contain interface records")
        if type(self.dns_servers) is not tuple:
            raise ValueError("DNS servers must be immutable")
        if any(not isinstance(status, InventoryStatus) for status in
               (self.dns_status, self.route_status, self.hardware_status)):
            raise ValueError("network supplements require typed statuses")


InventoryValue = Union[InventoryResult, NetworkInventory]


def _valid_completion(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


@dataclass(frozen=True)
class InventoryObservation:
    """Latest typed attempt plus independently retained last-good display data."""

    latest: Optional[InventoryValue] = None
    last_valid: Optional[InventoryValue] = None
    last_valid_completed_mono_s: Optional[float] = None
    latest_completed_mono_s: Optional[float] = None

    def __post_init__(self) -> None:
        for result, timestamp, label in (
            (self.latest, self.latest_completed_mono_s, "latest"),
            (self.last_valid, self.last_valid_completed_mono_s, "last_valid"),
        ):
            if result is None:
                if timestamp is not None:
                    raise ValueError("%s completion requires a matching result" % label)
            else:
                if not isinstance(result, (InventoryResult, NetworkInventory)):
                    raise ValueError("%s must be a typed inventory result" % label)
                result.__post_init__()
                if not _valid_completion(timestamp):
                    raise ValueError("%s completion must be finite and nonnegative" % label)
        if self.latest is None and self.last_valid is not None:
            raise ValueError("retained inventory requires a latest attempt")
        if self.last_valid is not None:
            if self.last_valid.status is not InventoryStatus.LIVE:
                raise ValueError("retained inventory must have a successful LIVE status")
            if type(self.last_valid) is not type(self.latest):
                raise ValueError("latest and retained inventory must use the same result model")
            if self.last_valid_completed_mono_s > self.latest_completed_mono_s:
                raise ValueError("retained inventory cannot come from a future completion")
        if self.latest is not None and self.latest.status is InventoryStatus.LIVE:
            if (self.last_valid != self.latest
                    or self.last_valid_completed_mono_s != self.latest_completed_mono_s):
                raise ValueError("a LIVE latest attempt must retain its own matching sample")


def observe_inventory(
    previous: InventoryObservation,
    result: InventoryValue,
    completed_mono_s: float,
) -> InventoryObservation:
    """Publish one completed attempt without turning retained rows fresh.

    LIVE partial results are valid observations with explicit coverage limits.
    Failure advances the latest attempt while preserving the prior successful
    result and its original completion time.  Supplied monotonic completion
    times must advance; this pure reducer does not consult or infer a clock.
    """

    if not isinstance(previous, InventoryObservation):
        raise ValueError("previous must be InventoryObservation")
    previous.__post_init__()
    if not isinstance(result, (InventoryResult, NetworkInventory)):
        raise ValueError("result must be a typed inventory result")
    result.__post_init__()
    if not _valid_completion(completed_mono_s):
        raise ValueError("inventory completion must be finite and nonnegative")
    if (previous.latest_completed_mono_s is not None
            and completed_mono_s <= previous.latest_completed_mono_s):
        raise ValueError("inventory completions must strictly advance")
    if previous.latest is not None and type(result) is not type(previous.latest):
        raise ValueError("an inventory observation cannot change result models")
    completed = float(completed_mono_s)
    if result.status is InventoryStatus.LIVE:
        return InventoryObservation(result, result, completed, completed)
    return InventoryObservation(result, previous.last_valid,
                                previous.last_valid_completed_mono_s, completed)


Runner = Callable[..., ProbeResult]


def _text(value: object, limit: int = 96) -> str:
    if type(value) not in (str, int, float):
        return ""
    text = "".join(character for character in str(value)
                   if unicodedata.category(character) not in {"Cc", "Cf"})
    text = " ".join(text.split())
    if text.startswith(("/", "~/")) or "/Users/" in text:
        return "[private path]"
    return text[:limit]


def _field(item: Mapping[str, object], *keys: str) -> str:
    return next((_text(item[key]) for key in keys if key in item and _text(item[key])), "")


def parse_devices(raw: str) -> InventoryResult[DeviceRecord]:
    """Parse old and macOS 26 USB schemas plus Thunderbolt and display devices."""

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise ValueError("device response is not valid JSON") from error
    sections = ("SPUSBDataType", "SPUSBHostDataType", "SPThunderboltDataType", "SPDisplaysDataType")
    if type(payload) is not dict or not any(key in payload for key in sections):
        raise ValueError("device response has no recognized section")
    items = []
    truncated = False
    nodes = 0
    seen = set()
    for section in sections:
        if section not in payload:
            continue
        roots = payload[section]
        if type(roots) is not list:
            raise ValueError("device section must be a list")
        stack = [(item, "", 0) for item in reversed(roots)]
        while stack:
            item, parent, depth = stack.pop()
            nodes += 1
            if nodes > NODE_LIMIT or depth > 16:
                truncated = True
                break
            if type(item) is not dict:
                raise ValueError("device tree contains a malformed node")
            name = _field(item, "_name", "device_name_key") or "Unnamed device"
            children = item.get("_items", [])
            if type(children) is not list:
                raise ValueError("device children must be a list")
            stack.extend((child, name, depth + 1) for child in reversed(children))
            record = None
            if section in ("SPUSBDataType", "SPUSBHostDataType"):
                product = _field(item, "product_id", "USBDeviceKeyProductID")
                vendor = _field(item, "manufacturer", "vendor_id", "USBDeviceKeyVendorName", "USBDeviceKeyVendorID")
                if depth > 0 or product:
                    record = DeviceRecord("USB" if depth else "USB controller", name, vendor, product,
                                          _field(item, "speed", "USBDeviceKeyLinkSpeed"), parent,
                                          _field(item, "location_id", "USBKeyLocationID"))
            elif section == "SPThunderboltDataType":
                if depth > 0:
                    port_data = item.get("receptacle_upstream_ambiguous_tag", {})
                    speed = _field(item, "link_speed_key", "mode_key")
                    port = _field(item, "route_string_key")
                    if isinstance(port_data, Mapping):
                        speed = _field(port_data, "link_speed_key", "current_speed_key") or speed
                        port = _field(port_data, "receptacle_id_key") or port
                    record = DeviceRecord("Thunderbolt", name, _field(item, "vendor_name_key"),
                                          _field(item, "device_id_key"), speed, parent, port)
            else:
                displays = item.get("spdisplays_ndrvs", [])
                if type(displays) is not list:
                    raise ValueError("display list must be a list")
                for display in displays:
                    if type(display) is not dict:
                        raise ValueError("display list contains a malformed record")
                    display_record = DeviceRecord(
                        "Display", _field(display, "_name") or "Unnamed display",
                        _field(display, "_spdisplays_display-vendor-id"),
                        _field(display, "_spdisplays_display-product-id"),
                        _field(display, "spdisplays_resolution", "_spdisplays_resolution", "spdisplays_pixelresolution"),
                        name, _field(display, "spdisplays_connection_type"),
                    )
                    if len(items) < RECORD_LIMIT:
                        items.append(display_record)
                    else:
                        truncated = True
            if record is not None:
                identity = (record.kind, record.name, record.parent, record.port, record.product_id)
                if identity not in seen:
                    seen.add(identity)
                    if len(items) < RECORD_LIMIT:
                        items.append(record)
                    else:
                        truncated = True
    required = (("SPUSBDataType" in payload or "SPUSBHostDataType" in payload)
                and "SPThunderboltDataType" in payload and "SPDisplaysDataType" in payload)
    detail = "reported USB, Thunderbolt and display topology"
    if not required:
        detail += "; some categories were not exposed"
    return InventoryResult(InventoryStatus.LIVE, tuple(items), detail, truncated, required and not truncated)


_INTERFACE = re.compile(r"^([A-Za-z][A-Za-z0-9_.:-]{0,31}): flags=[0-9a-f]+<([^>]*)>.*\bmtu ([0-9]+)")


def _ip(value: str) -> str:
    address, separator, scope = value.partition("%")
    normalized = str(ipaddress.ip_address(address))
    if separator:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", scope) is None:
            raise ValueError("invalid address scope")
        normalized += "%" + scope
    return normalized


def parse_interfaces(raw: str) -> InventoryResult[NetworkInterface]:
    records = []
    current = None
    seen = set()
    truncated_addresses = False
    for line in raw.splitlines():
        if line and not line[0].isspace():
            match = _INTERFACE.match(line)
            if match is None:
                raise ValueError("unrecognized interface header")
            if current is not None:
                records.append(NetworkInterface(**current))
            name, flags, mtu = match.groups()
            if name in seen or int(mtu) <= 0:
                raise ValueError("duplicate or malformed interface")
            seen.add(name)
            # UP means administratively enabled, not proof of carrier/link.
            state = "UP / link unknown" if "UP" in flags.split(",") else "DOWN"
            current = dict(name=name, state=state, ipv4=(), ipv6=(), mac="", medium="", mtu=int(mtu), hardware_port="")
        elif line.strip() and current is not None:
            text = line.strip()
            if text.startswith(("inet ", "inet6 ")):
                parts = text.split()
                if len(parts) < 2:
                    raise ValueError("interface address is missing")
                address = _ip(parts[1])
                key = "ipv6" if parts[0] == "inet6" else "ipv4"
                if (":" in address) != (key == "ipv6"):
                    raise ValueError("interface address family mismatch")
                if address not in current[key]:
                    if len(current[key]) < 16:
                        current[key] += (address,)
                    else:
                        truncated_addresses = True
            elif text.startswith("ether "):
                parts = text.split()
                if len(parts) != 2:
                    raise ValueError("malformed interface hardware address")
                value = parts[1]
                if re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value) is None:
                    raise ValueError("malformed interface hardware address")
                current["mac"] = value.lower()
            elif text.startswith("media:"):
                current["medium"] = _text(text.partition(":")[2])
            elif text.startswith("status:"):
                status = text.partition(":")[2].strip()
                if status not in ("active", "inactive"):
                    current["state"] = "link unknown"
                else:
                    current["state"] = status.upper()
        elif line.strip():
            raise ValueError("interface body precedes header")
    if current is not None:
        records.append(NetworkInterface(**current))
    if not records:
        raise ValueError("interface response has no recognized evidence")
    truncated = len(records) > RECORD_LIMIT or truncated_addresses
    return InventoryResult(InventoryStatus.LIVE, tuple(records[:RECORD_LIMIT]),
                           "observed interfaces; UP alone does not prove link", truncated, not truncated)


def parse_dns(raw: str) -> Tuple[str, ...]:
    if raw.strip() in ("No DNS configuration available", "No DNS configuration available."):
        return ()
    if not raw.lstrip().startswith("DNS configuration"):
        raise ValueError("unrecognized DNS response")
    servers = []
    for line in raw.splitlines():
        if "nameserver[" not in line:
            continue
        match = re.fullmatch(r"\s*nameserver\[[0-9]+\]\s*:\s*(\S+)\s*", line)
        if match is None:
            raise ValueError("malformed DNS server")
        address = _ip(match.group(1))
        if address not in servers:
            servers.append(address)
    return tuple(servers)


def parse_route(raw: str) -> Tuple[str, str]:
    if "route to:" not in raw or "destination:" not in raw:
        raise ValueError("unrecognized route response")
    fields = {}
    for line in raw.splitlines():
        key, separator, value = line.strip().partition(":")
        if separator and key in ("gateway", "interface"):
            if key in fields:
                raise ValueError("duplicate route field")
            fields[key] = value.strip()
    if "interface" not in fields or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,31}", fields["interface"]) is None:
        raise ValueError("route has no recognized interface")
    gateway = fields.get("gateway", "")
    if gateway:
        if re.fullmatch(r"link#[0-9]+", gateway) is None:
            gateway = _ip(gateway)
    return gateway, fields["interface"]


def parse_hardware_ports(raw: str) -> Mapping[str, str]:
    result = {}
    port = None
    recognized = False
    for line in raw.splitlines():
        text = line.strip()
        if text.startswith("Hardware Port:"):
            port = _text(text.partition(":")[2])
            recognized = True
        elif text.startswith("Device:"):
            name = text.partition(":")[2].strip()
            if port is None or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,31}", name) is None or name in result:
                raise ValueError("malformed hardware port mapping")
            result[name] = port
            port = None
        elif text.startswith("VLAN Configurations"):
            recognized = True
    if not recognized or port is not None:
        raise ValueError("unrecognized hardware port response")
    return result


_TCP_STATES = frozenset(("CLOSED", "LISTEN", "SYN_SENT", "SYN_RECEIVED", "SYN_RCVD", "ESTABLISHED",
                         "CLOSE_WAIT", "FIN_WAIT_1", "CLOSING", "LAST_ACK", "FIN_WAIT_2", "TIME_WAIT"))


def _endpoint(value: str) -> str:
    host, separator, port = value.rpartition(".")
    if not separator or not host or (port != "*" and (not port.isdigit() or not 0 <= int(port) <= 65535)):
        raise ValueError("unrecognized numeric endpoint")
    if host != "*":
        host = _ip(host)
    return ("[%s]:%s" if ":" in host else "%s:%s") % (host, port)


def parse_connections(raw: str) -> InventoryResult[ConnectionRecord]:
    """Parse numeric TCP endpoints; all process columns are discarded."""

    header = False
    items = []
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("Active Internet connections"):
            continue
        if text.startswith("Proto "):
            if "Local Address" not in text or "Foreign Address" not in text or "(state)" not in text:
                raise ValueError("unrecognized connection header")
            header = True
            continue
        parts = text.split()
        if not header or len(parts) < 6 or parts[0] not in ("tcp4", "tcp6", "tcp46", "tcp"):
            raise ValueError("unrecognized TCP connection row")
        if not parts[1].isdigit() or not parts[2].isdigit() or parts[5] not in _TCP_STATES:
            raise ValueError("malformed TCP queue or state")
        items.append(ConnectionRecord(parts[0], _endpoint(parts[3]), _endpoint(parts[4]),
                                      parts[5], int(parts[1]), int(parts[2])))
    if not header:
        raise ValueError("TCP response has no recognized table")
    # Surface listeners and active connections before short-lived closing rows.
    priority = {"LISTEN": 0, "ESTABLISHED": 1}
    items.sort(key=lambda item: (priority.get(item.state, 2), item.local, item.remote))
    truncated = len(items) > RECORD_LIMIT
    return InventoryResult(InventoryStatus.LIVE, tuple(items[:RECORD_LIMIT]),
                           "numeric TCP sockets; endpoints are sensitive; no process data", truncated, not truncated)


def _lsof_endpoint(value: str) -> str:
    if value.startswith("["):
        match = re.fullmatch(r"\[([^]]+)\]:([0-9]+|\*)", value)
        if match is None:
            raise ValueError("malformed IPv6 socket endpoint")
        host, port = match.groups()
        host = _ip(host)
        if ":" not in host:
            raise ValueError("bracketed socket endpoint must be IPv6")
    else:
        host, separator, port = value.rpartition(":")
        if not separator or not host or ":" in host:
            raise ValueError("malformed socket endpoint")
        if host != "*":
            host = _ip(host)
    if port != "*" and (not port.isdigit() or not 0 <= int(port) <= 65535):
        raise ValueError("malformed socket port")
    return ("[%s]:%s" if ":" in host else "%s:%s") % (host, port)


def parse_lsof_connections(raw: str) -> InventoryResult[ConnectionRecord]:
    """Parse only selected lsof file/type/name/TCP fields, discarding PIDs.

    lsof can observe socket metadata when netstat's socket table is unavailable
    to the parent process.  Its visibility can be restricted by process access;
    therefore a successful projection never claims complete host coverage.
    """

    current = None
    process_seen = False
    items = []
    seen = set()

    def finish() -> None:
        if current is None:
            return
        if not {"type", "name", "ST", "QR", "QS"}.issubset(current):
            raise ValueError("incomplete lsof TCP record")
        endpoints = current["name"].split("->")
        if len(endpoints) not in (1, 2):
            raise ValueError("malformed lsof TCP endpoint pair")
        local = _lsof_endpoint(endpoints[0])
        remote = _lsof_endpoint(endpoints[1]) if len(endpoints) == 2 else "*:*"
        record = ConnectionRecord("tcp6" if current["type"] == "IPv6" else "tcp4",
                                  local, remote, current["ST"], current["QR"], current["QS"])
        # Shared descriptors can repeat the same observable endpoint pair.
        identity = (record.protocol, record.local, record.remote, record.state)
        if identity not in seen:
            seen.add(identity)
            items.append(record)

    for line in raw.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "p":
            finish()
            current = None
            if not value.isdigit() or int(value) <= 0:
                raise ValueError("malformed lsof process boundary")
            process_seen = True
        elif field == "f":
            finish()
            if not process_seen or re.fullmatch(r"[0-9]+[ruw]?", value) is None:
                raise ValueError("malformed lsof file boundary")
            current = {}
        elif field in ("t", "n", "T"):
            if current is None:
                raise ValueError("orphan lsof socket field")
            if field == "t":
                if "type" in current or value not in ("IPv4", "IPv6"):
                    raise ValueError("malformed lsof socket type")
                current["type"] = value
            elif field == "n":
                if "name" in current:
                    raise ValueError("duplicate lsof socket endpoint")
                current["name"] = value
            else:
                key, separator, data = value.partition("=")
                if not separator:
                    raise ValueError("malformed lsof TCP field")
                if key in ("ST", "QR", "QS"):
                    if key in current:
                        raise ValueError("duplicate lsof TCP field")
                    if key == "ST":
                        if data not in _TCP_STATES:
                            raise ValueError("unrecognized lsof TCP state")
                        current[key] = data
                    elif data.isdigit():
                        current[key] = int(data)
                    else:
                        raise ValueError("malformed lsof TCP queue")
        else:
            raise ValueError("unselected lsof field")
    finish()
    if not items:
        raise ValueError("lsof supplied no recognized TCP socket evidence")
    priority = {"LISTEN": 0, "ESTABLISHED": 1}
    items.sort(key=lambda item: (priority.get(item.state, 2), item.local, item.remote))
    return InventoryResult(InventoryStatus.LIVE, tuple(items[:RECORD_LIMIT]),
                           "visible TCP endpoints [lsof]; deduplicated; protected processes may be hidden",
                           len(items) > RECORD_LIMIT, False)


def _probe(runner: Runner, argv: Sequence[str], timeout_s: float, max_output_bytes: int) -> ProbeResult:
    try:
        result = runner(tuple(argv), timeout_s=timeout_s, max_output_bytes=max_output_bytes)
        if not isinstance(result, ProbeResult):
            return ProbeResult(status=InventoryStatus.ERROR)
        return result
    except Exception:
        return ProbeResult(status=InventoryStatus.ERROR)


def collect_devices(runner: Runner) -> InventoryResult[DeviceRecord]:
    probe = _probe(runner, ("/usr/sbin/system_profiler", "SPUSBDataType", "SPUSBHostDataType",
                            "SPThunderboltDataType", "SPDisplaysDataType", "-json"), 5.0, 4 * 1024**2)
    if probe.status is not InventoryStatus.LIVE:
        return InventoryResult(probe.status, detail="device discovery " + probe.status.value.lower())
    try:
        return parse_devices(probe.stdout)
    except (ValueError, TypeError, KeyError, RecursionError):
        return InventoryResult(InventoryStatus.ERROR, detail="device response could not be recognized")


def collect_connections(runner: Runner) -> InventoryResult[ConnectionRecord]:
    probe = _probe(runner, ("/usr/sbin/netstat", "-anW", "-p", "tcp"), 2.0, 2 * 1024**2)
    if probe.status is InventoryStatus.LIVE:
        try:
            return parse_connections(probe.stdout)
        except (ValueError, TypeError, IndexError):
            pass
    fallback = _probe(runner, ("/usr/sbin/lsof", "-nP", "-iTCP", "-F", "ftnT"), 2.0, 2 * 1024**2)
    if fallback.status is not InventoryStatus.LIVE:
        return InventoryResult(fallback.status, detail="TCP discovery " + fallback.status.value.lower())
    try:
        return parse_lsof_connections(fallback.stdout)
    except (ValueError, TypeError, IndexError):
        return InventoryResult(InventoryStatus.ERROR, detail="TCP responses could not be recognized")


def collect_network(runner: Runner) -> NetworkInventory:
    interface_probe = _probe(runner, ("/sbin/ifconfig", "-a"), 2.0, 512 * 1024)
    if interface_probe.status is not InventoryStatus.LIVE:
        return NetworkInventory(interface_probe.status, detail="interface discovery " + interface_probe.status.value.lower())
    try:
        interfaces = parse_interfaces(interface_probe.stdout)
    except (ValueError, TypeError, IndexError):
        return NetworkInventory(InventoryStatus.ERROR, detail="interface response could not be recognized")
    dns_probe = _probe(runner, ("/usr/sbin/scutil", "--dns"), 1.5, 256 * 1024)
    route_probe = _probe(runner, ("/sbin/route", "-n", "get", "default"), 1.5, 64 * 1024)
    hardware_probe = _probe(runner, ("/usr/sbin/networksetup", "-listallhardwareports"), 1.5, 128 * 1024)
    statuses = [dns_probe.status, route_probe.status, hardware_probe.status]
    servers = ()
    gateway = default_interface = ""
    ports = {}
    if dns_probe.status is InventoryStatus.LIVE:
        try:
            servers = parse_dns(dns_probe.stdout)
        except (ValueError, TypeError):
            statuses[0] = InventoryStatus.ERROR
    if route_probe.status is InventoryStatus.LIVE:
        try:
            gateway, default_interface = parse_route(route_probe.stdout)
        except (ValueError, TypeError):
            statuses[1] = InventoryStatus.ERROR
    if hardware_probe.status is InventoryStatus.LIVE:
        try:
            ports = parse_hardware_ports(hardware_probe.stdout)
        except (ValueError, TypeError):
            statuses[2] = InventoryStatus.ERROR
    items = tuple(replace(item, hardware_port=ports.get(item.name, "")) for item in interfaces.items)
    truncated = interfaces.truncated or len(servers) > RECORD_LIMIT
    servers = servers[:RECORD_LIMIT]
    complete = interfaces.complete and not truncated and all(status is InventoryStatus.LIVE for status in statuses)
    detail = "observed interface configuration; no connectivity test"
    if not complete:
        detail += "; supplementary evidence incomplete"
    return NetworkInventory(InventoryStatus.LIVE, items, detail, truncated, complete,
                            gateway, default_interface, servers, *statuses)


def collect_inventory(runner: Runner) -> Mapping[str, object]:
    return {"devices": collect_devices(runner), "network": collect_network(runner),
            "connections": collect_connections(runner)}
