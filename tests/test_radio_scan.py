import socket
import struct

import pytest

from custom_components.netgear_wax import radio_scan as scan
from custom_components.netgear_wax.wifi_probe import attribute


def event(command, index):
    data = struct.pack("=BBH", command, 1, 0) + attribute(3, index)
    return struct.pack("=IHHII", 16 + len(data), 21, 0, 0, 0) + data


class Socket:
    def __init__(self, packets):
        self.packets = iter(packets)

    def settimeout(self, value):
        pass

    def recv(self, limit):
        try:
            return next(self.packets)
        except StopIteration:
            raise TimeoutError


def test_wait_ignores_other_interface_and_requires_completion():
    index = struct.pack("=I", 2)
    nl = type("NL", (), {"events": [], "sock": Socket([event(34, struct.pack("=I", 3)), event(34, index)])})()
    scan.wait_scan(nl, 21, index)


def test_aborted_scan_not_success():
    index = struct.pack("=I", 2)
    nl = type("NL", (), {"events": [], "sock": Socket([event(35, index)])})()
    with pytest.raises(ValueError, match="scan_aborted"):
        scan.wait_scan(nl, 21, index)


def test_timeout_not_success():
    nl = type("NL", (), {"events": [], "sock": Socket([])})()
    with pytest.raises(TimeoutError):
        scan.wait_scan(nl, 21, struct.pack("=I", 2))


def test_multicast_group_decoding():
    data = attribute(1, b"scan\0") + attribute(2, struct.pack("=I", 17))
    assert scan.scan_group({7: attribute(1 | 0x8000, data)}) == 17


def test_fresh_scan_filters_previous_cached_entries(monkeypatch):
    class Fake:
        events = []
        sock = type("S", (), {"setsockopt": lambda *a: None})()

        def request(self, family, command, payload=b"", dump=False):
            if command == 3:
                return [
                    {
                        1: struct.pack("=H", 21),
                        7: attribute(1, attribute(1, b"scan\0") + attribute(2, struct.pack("=I", 17))),
                    }
                ]
            if command == 5:
                return [{3: struct.pack("=I", 2)}]
            return []

        def close(self):
            pass

    monkeypatch.setattr(scan, "GenericNetlink", Fake)
    monkeypatch.setattr(socket, "if_nametoindex", lambda name: 2)
    monkeypatch.setattr(scan, "wait_scan", lambda *args: None)
    monkeypatch.setattr(scan, "scan_rows", lambda messages: [{"seen_ms_ago": 50}, {"seen_ms_ago": 120000}, {}])
    result = scan.scan_wifi("wlan0")
    assert result["completed"]
    assert result["rows"] == [{"seen_ms_ago": 50}]
