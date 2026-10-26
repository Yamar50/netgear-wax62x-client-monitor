"""Timestamped connection reports, independent of AP measurement snapshots."""

from datetime import datetime
import math

from .model import iso_time, mac_address


def validate_report(data, aps, now):
    if not isinstance(data, dict) or type(data.get("connected")) is not bool:
        raise ValueError("invalid_connection_report")
    mac = mac_address(data.get("mac", ""))
    stamp = data.get("observed_at")
    if not isinstance(stamp, str):
        raise ValueError("observed_at_required")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    timestamp = parsed.timestamp()
    if not math.isfinite(timestamp) or not now - 86400 <= timestamp <= now + 5:
        raise ValueError("invalid_report_age")
    name = data.get("ap_name")
    if not isinstance(name, str):
        raise ValueError("ap_name_required")
    matches = [ap for ap in aps if ap["name"].casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError("unknown_or_ambiguous_ap")
    ap = matches[0]
    return {"mac": mac, "connected": data["connected"], "ap_id": ap["id"], "ap_name": ap["name"],
            "observed_timestamp": timestamp, "observed_at": iso_time(timestamp)}


def apply_report(row, event, aps, now, freshness_limit):
    """Overlay state only. Never fabricate fresh signal/counters or AP rows."""
    if not event or now - event["observed_timestamp"] > freshness_limit:
        return row
    stamp = event["observed_timestamp"]
    if row.get("connection_fence_timestamp", 0) >= stamp:
        return row
    # A completed collection may contain an observation older than an event
    # received during that collection. Use observation/start times, not completion.
    positive = row.get("connected") is True
    if positive and row.get("observed_timestamp", 0) >= stamp:
        return row
    if not positive and aps and all(
        ap.get("status") == "ok" and ap.get("collection_started_timestamp", 0) >= stamp
        for ap in aps.values()
    ):
        return row
    # A disassociation from a previous AP must not disconnect a roaming client.
    if not event["connected"] and positive and row.get("ap_id") != event["ap_id"]:
        return row
    return {**row, "mac": event["mac"], "connected": event["connected"],
            "ap_id": event["ap_id"], "ap_name": event["ap_name"],
            "connection_checked_at": event["observed_at"],
            "connection_source": "push", "connection_observed_at": event["observed_at"]}
