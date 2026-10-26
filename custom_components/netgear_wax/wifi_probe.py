"""Bounded, on-demand Linux nl80211 feasibility probe; no radio configuration.

Only family/interface/cache reads and one passive scan request per wireless
interface are sent. No shell, AP management login, link change or connection
operation is available. Names and BSSIDs are hashed before leaving this module.
Linux UAPI: include/uapi/linux/{netlink,genetlink,nl80211}.h.
"""

import errno
import hashlib
import os
from pathlib import Path
import socket
import struct
import time


def attributes(data):
    result = {}
    offset = 0
    while offset + 4 <= len(data):
        size, kind = struct.unpack_from("=HH", data, offset)
        if size < 4 or offset + size > len(data):
            raise ValueError("invalid_netlink_attribute")
        result[kind & 0x3FFF] = data[offset + 4 : offset + size]
        offset += (size + 3) & ~3
    return result


def attribute(kind, value):
    size = len(value) + 4
    return struct.pack("=HH", size, kind) + value + b"\0" * (-size % 4)


class GenericNetlink:
    def __init__(self):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16)
        self.sock.settimeout(3)
        self.sock.bind((0, 0))
        self.seq = 0
        self.events = []

    def close(self):
        self.sock.close()

    def request(self, family, command, payload=b"", dump=False):
        self.seq += 1
        flags = 1 | (0x300 if dump else 4)
        body = struct.pack("=BBH", command, 1, 0) + payload
        self.sock.sendto(struct.pack("=IHHII", 16 + len(body), family, flags, self.seq, 0) + body, (0, 0))
        result = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            packet = self.sock.recv(262144)
            offset = 0
            while offset + 16 <= len(packet):
                size, kind, msg_flags, seq, _pid = struct.unpack_from("=IHHII", packet, offset)
                if size < 16 or offset + size > len(packet):
                    raise ValueError("invalid_netlink_message")
                data = packet[offset + 16 : offset + size]
                offset += (size + 3) & ~3
                if seq != self.seq:
                    if kind == family and len(data) >= 4:
                        self.events.append((data[0], attributes(data[4:])))
                    continue
                if msg_flags & 0x10:
                    raise OSError(errno.EINTR, "interrupted_dump")
                if kind == 2:
                    error = struct.unpack_from("=i", data)[0]
                    if error:
                        raise OSError(-error, os.strerror(-error))
                    return result
                if kind == 3:
                    if len(data) >= 4 and (error := struct.unpack_from("=i", data)[0]):
                        raise OSError(-error, os.strerror(-error))
                    return result
                if kind == family:
                    result.append(attributes(data[4:]))
                    if len(result) > 4096:
                        raise ValueError("netlink_result_limit")
        raise TimeoutError("netlink_timeout")


def fingerprint(value):
    return hashlib.sha256(value).hexdigest()


def scan_rows(messages):
    result = []
    for message in messages:
        if 47 not in message:
            continue
        bss = attributes(message[47])
        if len(bss.get(1, b"")) != 6 or len(bss.get(2, b"")) != 4:
            continue
        row = {"bssid_sha256": fingerprint(bss[1]), "frequency_mhz": struct.unpack("=I", bss[2])[0]}
        ies = bss.get(6, bss.get(11, b""))
        offset = 0
        while offset + 2 <= len(ies):
            tag, size = ies[offset : offset + 2]
            value = ies[offset + 2 : offset + 2 + size]
            if len(value) != size:
                break
            if tag == 0:
                row["ssid_sha256"] = fingerprint(value)
                row["ssid_bytes"] = len(value)
                break
            offset += 2 + size
        for key, name, fmt in [(7, "signal_dbm", "=i"), (10, "seen_ms_ago", "=I")]:
            if len(bss.get(key, b"")) == 4:
                value = struct.unpack(fmt, bss[key])[0]
                row[name] = value / 100 if key == 7 else value
        result.append(row)
    return result


def error_result(error):
    return {"error_type": type(error).__name__, "errno": getattr(error, "errno", None), "error": str(error)}


def probe_wifi():
    """Try the current container's existing permissions; never change them."""
    result = {"uid": os.geteuid(), "interfaces": [], "configuration_changed": False}
    try:
        status = Path("/proc/self/status").read_text()
        cap = next(line.split()[1] for line in status.splitlines() if line.startswith("CapEff:"))
        result["cap_net_admin"] = bool(int(cap, 16) & (1 << 12))
        result["sysfs_wireless_interfaces"] = sorted(p.name for p in Path("/sys/class/net").iterdir() if (p / "wireless").exists() or (p / "phy80211").exists())
    except (OSError, StopIteration) as error:
        result["environment_error"] = error_result(error)
    nl = None
    try:
        nl = GenericNetlink()
        reply = nl.request(16, 3, attribute(2, b"nl80211\0"))
        family = struct.unpack("=H", reply[0][1])[0]
        result["nl80211_family"] = family
        interfaces = nl.request(family, 5, dump=True)
        for interface in interfaces[:4]:
            index = interface.get(3)
            if index is None or len(index) != 4:
                continue
            item = {"name": interface.get(4, b"").rstrip(b"\0").decode(errors="replace")}
            result["interfaces"].append(item)
            payload = attribute(3, index)
            try:
                item["before"] = scan_rows(nl.request(family, 32, payload, dump=True))
                started = time.monotonic()
                # No SCAN_SSIDS attribute means passive scan (no probe request).
                nl.request(family, 33, payload)
                item["passive_scan_accepted"] = True
                time.sleep(15)
                item["after"] = scan_rows(nl.request(family, 32, payload, dump=True))
                item["elapsed_seconds"] = round(time.monotonic() - started, 2)
                # A successful request/cache alone does not prove a fresh scan.
                item["fresh_rows"] = sum(row.get("seen_ms_ago", float("inf")) <= item["elapsed_seconds"] * 1000 for row in item["after"])
            except (OSError, ValueError, IndexError, struct.error) as error:
                item.update(error_result(error))
    except (OSError, ValueError, IndexError, KeyError, struct.error, AttributeError) as error:
        result.update(error_result(error))
    finally:
        if nl is not None:
            nl.close()
    return result
