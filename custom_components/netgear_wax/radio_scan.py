"""Fresh, completed passive scans for the local Linux Wi-Fi interface."""

import socket
import struct
import time

from .wifi_probe import GenericNetlink, attribute, attributes, scan_rows


def scan_group(reply):
    for group in attributes(reply[7]).values():
        values = attributes(group)
        if values.get(1) == b"scan\0":
            return struct.unpack("=I", values[2])[0]
    raise ValueError("scan_multicast_group_missing")


def wait_scan(nl, family, index, timeout=35):
    """Require the completion event for this interface, never infer from cache."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while nl.events:
            command, data = nl.events.pop(0)
            if data.get(3) != index:
                continue
            if command == 35:
                raise ValueError("scan_aborted")
            if command == 34:
                return
        nl.sock.settimeout(max(0.01, deadline - time.monotonic()))
        packet = nl.sock.recv(262144)
        offset = 0
        while offset + 16 <= len(packet):
            size, kind, _flags, _seq, _pid = struct.unpack_from("=IHHII", packet, offset)
            if size < 20 or offset + size > len(packet):
                raise ValueError("invalid_scan_event")
            if kind == family:
                nl.events.append((packet[offset + 16], attributes(packet[offset + 20 : offset + size])))
            offset += (size + 3) & ~3
    raise TimeoutError("scan_completion_timeout")


def scan_wifi(interface):
    nl = GenericNetlink()
    try:
        reply = nl.request(16, 3, attribute(2, b"nl80211\0"))[0]
        family = struct.unpack("=H", reply[1])[0]
        nl.sock.setsockopt(270, 1, scan_group(reply))  # SOL_NETLINK, ADD_MEMBERSHIP
        index = struct.pack("=I", socket.if_nametoindex(interface))
        interfaces = nl.request(family, 5, dump=True)
        if not any(item.get(3) == index for item in interfaces):
            raise ValueError("wireless_interface_missing")
        nl.events.clear()
        started = time.monotonic()
        nl.request(family, 33, attribute(3, index))
        wait_scan(nl, family, index)
        rows = scan_rows(nl.request(family, 32, attribute(3, index), dump=True))
        elapsed = time.monotonic() - started
        # Old BSS cache entries must not hide a stopped AP. A small allowance
        # covers kernel timestamp granularity, not a previous monitor cycle.
        fresh = [row for row in rows if row.get("seen_ms_ago", float("inf")) <= (elapsed + 1) * 1000]
        return {"completed": True, "rows": fresh, "elapsed_seconds": round(elapsed, 3), "cached_rows": len(rows)}
    finally:
        nl.close()
