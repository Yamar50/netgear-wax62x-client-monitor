"""Synthetic Linux UAPI replies; no real radio access."""

import errno
import struct

import pytest

from custom_components.netgear_wax import wifi_probe as probe


def test_attribute_alignment_and_validation():
    assert probe.attributes(probe.attribute(1, b"abc") + probe.attribute(2 | 0x8000, b"x")) == {1: b"abc", 2: b"x"}
    with pytest.raises(ValueError):
        probe.attributes(struct.pack("=HH", 20, 1) + b"x")


def test_scan_rows_redact_identifiers_keep_frequency_signal_age():
    mac = bytes.fromhex("020000000001")
    ssid = b"Synthetic-Test"
    bss = b"".join(probe.attribute(k, v) for k, v in [
        (1, mac), (2, struct.pack("=I", 5260)),
        (6, bytes([0, len(ssid)]) + ssid),
        (7, struct.pack("=i", -5432)), (10, struct.pack("=I", 125)),
    ])
    rows = probe.scan_rows([{47: bss}])
    assert rows == [{"bssid_sha256": probe.fingerprint(mac), "ssid_sha256": probe.fingerprint(ssid),
                     "ssid_bytes": len(ssid), "frequency_mhz": 5260, "signal_dbm": -54.32, "seen_ms_ago": 125}]
    assert "Synthetic-Test" not in str(rows)
    assert "020000000001" not in str(rows)
    assert probe.scan_rows([{}, {47: probe.attribute(1, b"bad")}]) == []


def test_permission_failure_does_not_retry_or_change_radio(monkeypatch):
    calls = []

    class Fake:
        def request(self, family, command, payload=b"", dump=False):
            calls.append(command)
            if command == 3:
                return [{1: struct.pack("=H", 40)}]
            if command == 5:
                return [{3: struct.pack("=I", 7), 4: b"wlan0\0"}]
            if command == 32:
                return []
            raise PermissionError(errno.EPERM, "Operation not permitted")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(probe, "GenericNetlink", Fake)
    result = probe.probe_wifi()
    assert calls == [3, 5, 32, 33, "close"]
    assert result["interfaces"][0]["errno"] == errno.EPERM
    assert "passive_scan_accepted" not in result["interfaces"][0]
    assert not result["configuration_changed"]


def test_cache_alone_not_fresh_scan(monkeypatch):
    class Fake:
        def request(self, family, command, payload=b"", dump=False):
            if command == 3:
                return [{1: struct.pack("=H", 40)}]
            if command == 5:
                return [{3: struct.pack("=I", 7), 4: b"wlan0\0"}]
            return []

        def close(self):
            pass

    monkeypatch.setattr(probe, "GenericNetlink", Fake)
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)
    result = probe.probe_wifi()["interfaces"][0]
    assert result["passive_scan_accepted"]
    assert result["fresh_rows"] == 0
