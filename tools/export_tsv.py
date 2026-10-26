#!/usr/bin/env python3
"""Export HA cache to the legacy WAX monitor TSV format without AP logins."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from ha_client import HAClient, HAError

COLUMNS = [
    "MAC Address",
    "IP Address",
    "Host Name",
    "BSSID",
    "SSID",
    "Channel",
    "Channel Width",
    "Tx Rate",
    "Rx Rate",
    "RSSI",
    "Tx Bytes",
    "Rx Bytes",
    "State",
    "Type",
    "Mode",
    "Status",
    "Idle Time",
    "Assoc Time Stamp",
    "VLAN ID",
    "PMF Support",
    "AP Name",
    "Band",
    "AP Uptime",
    "LAST UPDATE TIME",
]


def cell(value):
    if value is None or value == "":
        return "-"
    return str(value).replace("\t", " ").replace("\n", " ").replace("\r", " ")


def unit(value, suffix):
    return None if value is None else f"{format(value, '.6f').rstrip('0').rstrip('.')} {suffix}"


def convert(snapshot, now=None):
    if snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("clients"), dict):
        raise ValueError("Unsupported snapshot")
    now = time.time() if now is None else now
    stale = now - (snapshot.get("updated_timestamp") or 0) > snapshot["freshness_limit"]
    aps = snapshot["aps"]
    rows = []
    counts = Counter()
    for mac, client in sorted(snapshot["clients"].items()):
        if not client.get("observed_at"):
            continue
        ap = aps.get(client.get("ap_id"), {})
        connected = client.get("connected") if not stale else None
        status = "Authenticated" if connected is True else "Disconnect"
        # Existing viewer uses Disconnect for unconfirmed rows; HA retains the
        # stronger distinction (unknown vs confirmed disconnected) in its API.
        pmf = client.get("pmf")
        name = client.get("ap_name") or ap.get("name") or "-"
        values = [
            mac,
            client.get("ip"),
            client.get("hostname"),
            client.get("bssid"),
            client.get("ssid"),
            client.get("channel"),
            client.get("channel_width"),
            unit(client.get("tx_rate"), "bps"),
            unit(client.get("rx_rate"), "bps"),
            client.get("rssi"),
            unit(client.get("tx_bytes"), "B"),
            unit(client.get("rx_bytes"), "B"),
            client.get("state") if connected else "Disconnect",
            client.get("security"),
            client.get("mode"),
            status,
            client.get("idle_time"),
            client.get("association_time"),
            client.get("vlan_id"),
            None if pmf is None else "Yes" if pmf else "No",
            name.upper(),
            client.get("band"),
            ap.get("uptime"),
            client.get("observed_at"),
        ]
        rows.append("\t".join(map(cell, values)))
        counts[name.upper()] += 1
    tsv = "\t".join(COLUMNS) + "\n" + "".join(row + "\n" for row in rows)
    status = {
        "version": 1,
        "generatedAt": snapshot.get("updated_at") or snapshot["generated_at"],
        "source": "home_assistant",
        "stale": stale,
        "tsvSha256": hashlib.sha256(tsv.encode()).hexdigest(),
        "aps": [
            {
                "name": ap["name"].upper(),
                "status": "ok" if ap.get("status") == "ok" and not stale else "unavailable",
                "rows": counts[ap["name"].upper()],
            }
            for ap in aps.values()
        ],
    }
    return tsv, status


def atomic_write(path, value):
    path = Path(path)
    # Keep existing viewer access when a scheduled service uses a different UID.
    # New files remain private by default.
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, mode)
            handle.write(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def invalidate(tsv, previous_status):
    """Keep historical rows/times, but never display them as current after HA failure."""
    lines = tsv.splitlines()
    header = lines[0].split("\t")
    status_index = header.index("Status")
    ap_index = header.index("AP Name")
    state_index = header.index("State") if "State" in header else None
    counts = Counter()
    output = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        row = line.split("\t")
        if len(row) != len(header):
            raise ValueError("Invalid existing TSV")
        row[status_index] = "Disconnect"
        if state_index is not None:
            row[state_index] = "Disconnect"
        counts[row[ap_index]] += 1
        output.append("\t".join(row))
    for ap in previous_status.get("aps", []):
        counts.setdefault(ap["name"], 0)
    result = "\n".join(output) + "\n"
    status = {
        "version": 1,
        "generatedAt": previous_status["generatedAt"],
        "source": "home_assistant",
        "stale": True,
        "source_error": "ha_snapshot_unavailable",
        "tsvSha256": hashlib.sha256(result.encode()).hexdigest(),
        "aps": [{"name": name, "status": "unavailable", "rows": count} for name, count in counts.items()],
    }
    return result, status


def export(client, target):
    target = Path(target)
    try:
        snapshot = client.request("/api/netgear_wax/snapshot")
        if not snapshot.get("updated_at"):
            raise HAError("not_yet_collected")
        tsv, status = convert(snapshot)
    except (HAError, ValueError, KeyError, TypeError):
        if not target.exists():
            raise
        previous = json.loads(Path(str(target) + ".status.json").read_text())
        tsv, status = invalidate(target.read_text(), previous)
        print("HA snapshot unavailable; historical rows marked unconfirmed.", file=sys.stderr)
    if target.exists():
        atomic_write(target.parent / "WAX625_OLD_ClientDetails.tsv", target.read_text())
    atomic_write(target, tsv)
    atomic_write(str(target) + ".status.json", json.dumps(status, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    target = Path(args.output)
    lock = target.with_name(target.name + ".ha-lock")
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit("Another cache export is active; inspect stale lock before removal")
    try:
        export(HAClient(args.config), target)
    finally:
        lock.rmdir()


if __name__ == "__main__":
    main()
