"""Pure data validation and fleet reconciliation; no I/O."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import math


def mac_address(value: str) -> str:
    value = str(value).strip().replace("-", ":").lower()
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", value):
        raise ValueError("Invalid MAC address")
    return value


def iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except ValueError, TypeError:
        return None


def quantity(value, units=None):
    if value is None:
        return None
    match = re.fullmatch(r"\s*(-?[\d.]+)\s*([a-z/]*)\s*", str(value), re.I)
    if not match:
        return None
    numeric = number(match[1])
    if numeric is None:
        return None
    multiplier = (units or {"": 1}).get(match[2].lower())
    return None if multiplier is None else numeric * multiplier


def clean(value):
    if value in (None, "", "-", "--"):
        return None
    return str(value)


def normalize_client(row: dict, ap: dict, band: str, timestamp: float) -> dict:
    """Keep only fields needed by entities/consumers; never retain raw responses."""
    rates = {"": 1, "bps": 1, "kbps": 1000, "mbps": 1000000, "gbps": 1000000000}
    sizes = {"": 1, "b": 1, "kb": 1000, "mb": 1000000, "gb": 1000000000}
    pmf = clean(row.get("pmf"))
    return {
        "mac": mac_address(row.get("mac", "")),
        "ap_id": ap["id"],
        "ap_name": ap["name"],
        "band": band,
        "ip": clean(row.get("ip")),
        "reported_hostname": clean(row.get("hname")),
        "ssid": clean(row.get("ssid")),
        "bssid": clean(row.get("bssid")),
        "channel": number(row.get("chnl")),
        "channel_width": clean(row.get("chWidth")),
        "mode": clean(row.get("mode")),
        "rssi": quantity(row.get("rssi"), {"": 1, "dbm": 1}),
        "tx_rate": quantity(row.get("txR"), rates),
        "rx_rate": quantity(row.get("rxR"), rates),
        "tx_bytes": quantity(row.get("txB"), sizes),
        "rx_bytes": quantity(row.get("rxB"), sizes),
        "association_time": clean(row.get("time")),
        "security": clean(row.get("type")),
        "pmf": None if pmf is None else pmf.lower() in ("yes", "1", "true"),
        "state": clean(row.get("state")),
        "status": clean(row.get("status")),
        "vlan_id": clean(row.get("vlanID")),
        "idle_time": clean(row.get("idle")),
        "observed_at": iso_time(timestamp),
        "observed_timestamp": timestamp,
        "connected": True
        if row.get("status") == "Authenticated"
        else False
        if row.get("status") in ("Disconnect", "Authentication Failed")
        else None,
    }


def reconcile(previous: dict, results: list[dict], selected: list[str], now: float) -> dict:
    """A positive observation wins across roaming; missing is false only if all APs succeeded."""
    complete = bool(results) and all(result["status"] == "ok" for result in results)
    observations = {}
    for result in results:
        if result["status"] != "ok":
            continue
        for row in result.get("clients", []):
            key = row["mac"]
            old = observations.get(key)
            if old is None or (row["connected"] is True, row["observed_timestamp"]) > (
                old["connected"] is True,
                old["observed_timestamp"],
            ):
                observations[key] = dict(row)
    output = {}
    for key in set(previous) | set(observations) | set(selected):
        current = observations.get(key)
        historical = previous.get(key, {"mac": key, "observed_timestamp": 0})
        row = dict(current or historical)
        if current and current["connected"]:
            row["connected"] = True
        elif current and current["connected"] is None:
            row["connected"] = None
        else:
            row["connected"] = False if complete else None
        row["connection_checked_at"] = iso_time(now) if row["connected"] is not None else None
        row["fresh"] = current is not None
        # Keep selected clients indefinitely; unseen unselected clients expire after a day.
        if current or key in selected or now - row.get("observed_timestamp", 0) < 86400:
            output[key] = row
    return output
