"""Read-only APFS reports and explicitly scoped, bounded folder walks."""

import math
import hashlib
import os
import plistlib
import re
import stat
import time
from typing import Mapping
from xml.parsers.expat import ExpatError

from porkyhud_inventory import InventoryStatus
from .model import ToolReport


ROW_LIMIT = 128
SNAPSHOT_PROBE_LIMIT = 8
MAX_DEPTH = 32
_DEVICE = re.compile(r"disk\d+(?:s\d+){0,2}\Z")


def _text(value, limit=120):
    return "".join(c for c in str(value) if c.isprintable())[:limit]


def _bytes(value):
    return value if type(value) is int and value >= 0 else None


def _size(value):
    return "—" if value is None else "%.2f GiB" % (value / 1073741824)


def _plist(raw):
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("storage response too large")
    result = plistlib.loads(raw.encode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("storage response must be a dictionary")
    return result


def inspect_storage(runner):
    """Discover APFS container sharing and bounded per-volume snapshots."""
    disks_probe = runner(("/usr/sbin/diskutil", "list", "-plist"), 3.0)
    apfs_probe = runner(("/usr/sbin/diskutil", "apfs", "list", "-plist"), 3.0)
    physical = []
    details = ["APFS volumes share their container's free space; volume capacities must not be added.",
               "Snapshot counts do not measure reclaimable bytes. This tool changes no disks."]
    partial = False
    truncated = False
    if disks_probe.status is InventoryStatus.LIVE:
        try:
            payload = _plist(disks_probe.stdout)
            disks = payload.get("AllDisksAndPartitions")
            if not isinstance(disks, list):
                raise ValueError("missing disk list")
            for disk in disks[:ROW_LIMIT]:
                if not isinstance(disk, dict) or not _DEVICE.fullmatch(str(disk.get("DeviceIdentifier", ""))):
                    raise ValueError("unrecognized disk")
                physical.append({"device": disk["DeviceIdentifier"], "size_bytes": _bytes(disk.get("Size")),
                                 "content": _text(disk.get("Content", "")),
                                 "virtual": disk.get("Virtual") if type(disk.get("Virtual")) is bool else None})
            truncated = len(disks) > ROW_LIMIT
        except (ValueError, TypeError, plistlib.InvalidFileException, OverflowError, ExpatError):
            partial = True
            details.append("Disk list response was unrecognized.")
    else:
        partial = True
        details.append("Disk list: " + disks_probe.status.value)
    data = {"disks": physical, "containers": [], "snapshots": [],
            "disk_list_status": disks_probe.status.value, "apfs_status": apfs_probe.status.value}
    if apfs_probe.status is not InventoryStatus.LIVE:
        return ToolReport("storage", "PARTIAL" if physical else apfs_probe.status.value,
                          "APFS details unavailable.", details=tuple(details), data=data)
    try:
        payload = _plist(apfs_probe.stdout)
        containers = payload.get("Containers")
        if not isinstance(containers, list):
            raise ValueError("missing APFS containers")
        display = [("Disk", disk["device"], disk["content"], _size(disk["size_bytes"]), "—", "—") for disk in physical]
        snapshot_probes = 0
        volume_count = 0
        for container in containers[:ROW_LIMIT]:
            if not isinstance(container, dict):
                raise ValueError("unrecognized container")
            device = container.get("ContainerReference", container.get("APFSContainerReference", ""))
            if not isinstance(device, str) or not _DEVICE.fullmatch(device):
                raise ValueError("unrecognized container device")
            volumes = container.get("Volumes")
            if not isinstance(volumes, list):
                raise ValueError("missing volume list")
            item = {"device": device, "capacity_bytes": _bytes(container.get("CapacityCeiling")),
                    "free_bytes": _bytes(container.get("CapacityFree")), "volumes": [], "physical_stores": []}
            display.append(("Container", device, "Shared capacity", _size(item["capacity_bytes"]), _size(item["free_bytes"]), "—"))
            stores = container.get("PhysicalStores", [])
            if not isinstance(stores, list):
                raise ValueError("unrecognized physical stores")
            for store in stores[:ROW_LIMIT]:
                if not isinstance(store, dict) or not _DEVICE.fullmatch(str(store.get("DeviceIdentifier", ""))):
                    raise ValueError("unrecognized physical store")
                store_item = {"device": store["DeviceIdentifier"], "size_bytes": _bytes(store.get("Size"))}
                item["physical_stores"].append(store_item)
                display.append(("Store", store_item["device"], "Backs " + device, _size(store_item["size_bytes"]), "—", "—"))
            truncated = truncated or len(stores) > ROW_LIMIT
            for volume in volumes[:ROW_LIMIT]:
                if volume_count >= ROW_LIMIT:
                    truncated = True
                    break
                if not isinstance(volume, dict):
                    raise ValueError("unrecognized volume")
                volume_device = volume.get("DeviceIdentifier", "")
                if not isinstance(volume_device, str) or not _DEVICE.fullmatch(volume_device):
                    raise ValueError("unrecognized volume device")
                volume_item = {"device": volume_device, "name": _text(volume.get("Name", "Unnamed")),
                               "used_bytes": _bytes(volume.get("CapacityInUse")),
                               "reserve_bytes": _bytes(volume.get("CapacityReserve")),
                               "quota_bytes": _bytes(volume.get("CapacityQuota")), "snapshot_status": "CHECKING"}
                item["volumes"].append(volume_item)
                volume_count += 1
                display_index = len(display)
                display.append(("Volume", volume_device, volume_item["name"], _size(volume_item["used_bytes"]), "Shared", "CHECKING"))
                if volume_item["reserve_bytes"] or volume_item["quota_bytes"]:
                    details.append("%s reserve: %s · quota: %s" % (volume_device, _size(volume_item["reserve_bytes"]), _size(volume_item["quota_bytes"])))
                if snapshot_probes >= SNAPSHOT_PROBE_LIMIT:
                    volume_item["snapshot_status"] = "NOT COLLECTED"
                    display[display_index] = display[display_index][:-1] + ("NOT COLLECTED",)
                    truncated = True
                    continue
                snapshot_probes += 1
                probe = runner(("/usr/sbin/diskutil", "apfs", "listSnapshots", "-plist", volume_device), 3.0)
                volume_item["snapshot_status"] = probe.status.value
                if probe.status is not InventoryStatus.LIVE:
                    display[display_index] = display[display_index][:-1] + (probe.status.value,)
                    partial = True
                    continue
                try:
                    snapshot_payload = _plist(probe.stdout)
                    snapshots = snapshot_payload.get("Snapshots")
                    if not isinstance(snapshots, list):
                        raise ValueError("missing snapshots")
                    volume_item["snapshot_count"] = len(snapshots)
                    display[display_index] = display[display_index][:-1] + (str(len(snapshots)),)
                    for snapshot in snapshots[:ROW_LIMIT]:
                        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("Name"), str):
                            raise ValueError("unrecognized snapshot")
                        if len(data["snapshots"]) >= ROW_LIMIT:
                            truncated = True
                            break
                        snapshot_item = {"volume": volume_device, "name": _text(snapshot["Name"]),
                                         "purgeable": snapshot.get("Purgeable") if type(snapshot.get("Purgeable")) is bool else None}
                        data["snapshots"].append(snapshot_item)
                        display.append(("Snapshot", volume_device, snapshot_item["name"], "—", "—",
                                        "Purgeable" if snapshot_item["purgeable"] is True else "Not purgeable" if snapshot_item["purgeable"] is False else "Unknown"))
                    truncated = truncated or len(snapshots) > ROW_LIMIT
                except (ValueError, TypeError, plistlib.InvalidFileException, OverflowError, ExpatError):
                    volume_item["snapshot_status"] = "ERROR"
                    display[display_index] = display[display_index][:-1] + ("ERROR",)
                    partial = True
            truncated = truncated or len(volumes) > ROW_LIMIT
            data["containers"].append(item)
        truncated = truncated or len(containers) > ROW_LIMIT or len(display) > ROW_LIMIT
        if truncated:
            details.append("Rows or snapshot probes reached their collection limit.")
        failed = sum(volume["snapshot_status"] not in ("LIVE", "NOT COLLECTED")
                     for item in data["containers"] for volume in item["volumes"])
        if failed:
            details.append("Snapshot details unavailable for %d volumes." % failed)
        complete = not (partial or truncated)
        summary = "%d APFS containers · %d volumes · %d returned snapshots" % (
            len(data["containers"]), sum(len(item["volumes"]) for item in data["containers"]), len(data["snapshots"]))
        return ToolReport("storage", "LIVE" if complete else "PARTIAL", summary,
                          ("Kind", "Device", "Name", "Capacity or used", "Container free", "Snapshots or status"),
                          tuple(display[:ROW_LIMIT]), tuple(details), complete, truncated, data)
    except (ValueError, TypeError, plistlib.InvalidFileException, OverflowError, ExpatError):
        return ToolReport("storage", "ERROR", "APFS response was unrecognized.", details=tuple(details), data=data)


def _cancelled(cancel):
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    return bool(cancel.is_set())


def scan_folder(path, cancel=None, max_entries=100000, timeout_s=10):
    """Walk a selected directory with openat/no-follow and cooperative limits.

    The timeout is checked between entries; a blocking filesystem call cannot
    be interrupted by this in-process walker. A worker keeps it off the UI.
    """
    if type(max_entries) is not int or not 0 < max_entries <= 1000000 or type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
        return ToolReport("folder", "INVALID", "Folder limits must be positive and bounded.")
    try:
        root = os.path.abspath(os.fsdecode(os.fspath(path)))
    except (TypeError, ValueError, OSError):
        return ToolReport("folder", "INVALID", "Choose a folder path.")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    frames = []
    root_fd = None
    started = time.monotonic()
    data = {"root": root, "root_id": "", "logical_bytes": 0, "file_count": 0,
            "directory_count": 0, "visited_entries": 0, "skipped_symlinks": 0,
            "skipped_mounts": 0, "denied_entries": 0, "skipped_special": 0,
            "skipped_depth": 0, "children": [], "stopped_reason": "",
            "limits": {"max_entries": max_entries, "timeout_s": timeout_s, "max_depth": MAX_DEPTH}}
    buckets = {}
    hardlinks = set()
    status = "LIVE"
    stop = ""
    try:
        root_mode = os.lstat(root).st_mode
        if stat.S_ISLNK(root_mode):
            return ToolReport("folder", "INVALID", "Choose a folder directly; symlinks are not followed.")
        if not stat.S_ISDIR(root_mode):
            return ToolReport("folder", "INVALID", "Choose a directory.")
        root_fd = os.open(root, flags)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            return ToolReport("folder", "INVALID", "Choose a directory.")
        root_device = root_stat.st_dev
        data["root_id"] = "%d:%d" % (root_device, root_stat.st_ino)
        frames.append((root_fd, os.scandir(root_fd), 0, None))
        root_fd = None
        while frames:
            if _cancelled(cancel):
                stop, status = "cancelled", "CANCELLED"
                break
            if time.monotonic() - started >= timeout_s:
                stop, status = "time limit", "PARTIAL"
                break
            if data["visited_entries"] >= max_entries:
                stop, status = "entry limit", "PARTIAL"
                break
            current_fd, iterator, depth, bucket_name = frames[-1]
            try:
                entry = next(iterator)
            except StopIteration:
                iterator.close()
                os.close(current_fd)
                frames.pop()
                continue
            except OSError:
                data["denied_entries"] += 1
                iterator.close()
                os.close(current_fd)
                frames.pop()
                continue
            data["visited_entries"] += 1
            if depth == 0:
                bucket_name = entry.name
                buckets.setdefault(bucket_name, {"name": _text(entry.name), "entry_id": hashlib.sha256(os.fsencode(entry.name)).hexdigest()[:24],
                                                  "logical_bytes": 0, "file_count": 0, "directory_count": 0})
            bucket = buckets[bucket_name]
            try:
                item_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(item_stat.st_mode):
                    data["skipped_symlinks"] += 1
                    continue
                if item_stat.st_dev != root_device:
                    data["skipped_mounts"] += 1
                    continue
                if stat.S_ISREG(item_stat.st_mode):
                    identity = (item_stat.st_dev, item_stat.st_ino)
                    if item_stat.st_nlink > 1 and identity in hardlinks:
                        continue
                    if item_stat.st_nlink > 1:
                        hardlinks.add(identity)
                    data["logical_bytes"] += item_stat.st_size
                    data["file_count"] += 1
                    bucket["logical_bytes"] += item_stat.st_size
                    bucket["file_count"] += 1
                elif stat.S_ISDIR(item_stat.st_mode):
                    data["directory_count"] += 1
                    bucket["directory_count"] += 1
                    if depth + 1 > MAX_DEPTH:
                        data["skipped_depth"] += 1
                        continue
                    child_fd = os.open(entry.name, flags, dir_fd=current_fd)
                    try:
                        opened = os.fstat(child_fd)
                        if opened.st_dev != root_device or opened.st_ino != item_stat.st_ino:
                            data["skipped_mounts"] += 1
                            continue
                        child_iterator = os.scandir(child_fd)
                        frames.append((child_fd, child_iterator, depth + 1, bucket_name))
                        child_fd = None
                    finally:
                        if child_fd is not None:
                            os.close(child_fd)
                else:
                    data["skipped_special"] += 1
            except OSError:
                data["denied_entries"] += 1
    except PermissionError:
        return ToolReport("folder", "PERMISSION DENIED", "Folder access denied.")
    except (OSError, ValueError):
        return ToolReport("folder", "UNAVAILABLE", "Folder is unavailable or cannot be read safely.")
    finally:
        if root_fd is not None:
            os.close(root_fd)
        for descriptor, iterator, _, _ in reversed(frames):
            iterator.close()
            os.close(descriptor)
    children = sorted(buckets.values(), key=lambda item: (-item["logical_bytes"], item["name"]))
    truncated = bool(stop or data["skipped_depth"] or len(children) > ROW_LIMIT)
    complete = not (truncated or data["denied_entries"])
    if status == "LIVE" and not complete:
        status = "PARTIAL"
    data["children"] = children[:ROW_LIMIT]
    data["stopped_reason"] = stop
    data["complete"] = complete
    data["truncated"] = truncated
    data["elapsed_s"] = max(0, time.monotonic() - started)
    details = ("Scope: " + _text(root, 240),
               "Logical file bytes; APFS clones and sparse files prevent exact reclaimable-space estimates.",
               "Symlinks and other volumes are excluded; hard-linked files count once.",
               "Skipped: %d symlinks · %d other volumes · %d unreadable entries · %d depth limits" %
               (data["skipped_symlinks"], data["skipped_mounts"], data["denied_entries"], data["skipped_depth"]),
               "Time limit is checked between filesystem calls; changing folders are not an atomic snapshot.")
    if stop:
        details += ("Stopped at " + stop + "; totals describe only visited files.",)
    display = tuple((item["name"], _size(item["logical_bytes"]), str(item["file_count"]), str(item["directory_count"])) for item in children[:ROW_LIMIT])
    return ToolReport("folder", status, "%s logical bytes · %d files · %d visited entries" %
                      (_size(data["logical_bytes"]), data["file_count"], data["visited_entries"]),
                      ("Child", "Logical size", "Files", "Directories"), display, details, complete, truncated, data)


def compare_folder_snapshots(before, after):
    """Compare two measurements of the same root, retaining incomplete state."""
    previous = before.data if isinstance(before, ToolReport) else before
    current = after.data if isinstance(after, ToolReport) else after
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping) or not previous.get("root_id") or previous.get("root_id") != current.get("root_id"):
        return ToolReport("folder-compare", "INVALID", "Choose two scans of the same folder.")
    if _bytes(previous.get("logical_bytes")) is None or _bytes(current.get("logical_bytes")) is None:
        return ToolReport("folder-compare", "INVALID", "Folder scans have no recognized byte totals.")
    complete = (before.complete if isinstance(before, ToolReport) else previous.get("complete") is True) and (after.complete if isinstance(after, ToolReport) else current.get("complete") is True)
    first = {item.get("entry_id", item["name"]): item for item in previous.get("children", ()) if isinstance(item, Mapping) and isinstance(item.get("name"), str) and _bytes(item.get("logical_bytes")) is not None}
    second = {item.get("entry_id", item["name"]): item for item in current.get("children", ()) if isinstance(item, Mapping) and isinstance(item.get("name"), str) and _bytes(item.get("logical_bytes")) is not None}
    changes = sorted((((second.get(key) or first[key])["name"], second.get(key, {}).get("logical_bytes", 0) - first.get(key, {}).get("logical_bytes", 0))
                      for key in first.keys() | second.keys()), key=lambda item: (-abs(item[1]), item[0]))
    delta = current["logical_bytes"] - previous["logical_bytes"]
    details = ("Differences are observed logical bytes; files can change during collection.",)
    if not complete:
        details += ("At least one scan is incomplete; differences are partial observations.",)
    return ToolReport("folder-compare", "LIVE" if complete else "PARTIAL", "%+d logical bytes since the earlier scan" % delta,
                      ("Child", "Change in bytes"), tuple((_text(name), "%+d" % change) for name, change in changes[:ROW_LIMIT]),
                      details, bool(complete), len(changes) > ROW_LIMIT,
                      {"root_id": current["root_id"], "logical_bytes_delta": delta,
                       "changes": [{"name": _text(name), "logical_bytes_delta": change} for name, change in changes[:ROW_LIMIT]]})
