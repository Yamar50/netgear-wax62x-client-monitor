"""5 GHz recovery within an already authenticated client collection session."""

import asyncio
from copy import deepcopy
import logging
import re

from .api import WaxError

FIELDS = ("radioStatus", "operateMode", "guardInterval", "txPower", "channel", "channelWidth", "beamForming")
_LOGGER = logging.getLogger(__name__)


def channel_number(value):
    if type(value) not in (str, int) or not str(value).isdigit() or not 1 <= int(value) <= 196:
        raise WaxError("unknown_channel")
    return int(value)


def validate_dfs_policies(policies, aps):
    if not isinstance(policies, dict) or not set(policies) <= {ap["id"] for ap in aps}:
        raise ValueError("invalid_dfs_policies")
    result = {}
    for key, policy in policies.items():
        if not isinstance(policy, dict):
            raise ValueError("invalid_dfs_policy")
        allowed, rotation = policy.get("allowed"), policy.get("rotation")
        if any(not isinstance(x, list) or not 1 <= len(x) <= 64 for x in (allowed, rotation)):
            raise ValueError("invalid_dfs_channels")
        if any(type(x) is not int or not 32 <= x <= 177 for x in allowed + rotation):
            raise ValueError("invalid_dfs_channels")
        if not set(rotation) <= set(allowed):
            raise ValueError("rotation_outside_allowed_channels")
        result[key] = {"allowed": list(allowed), "rotation": list(rotation)}
    return result


def validate_channel_choices(table, policy):
    channels = table.get("wlan1", {}).get("channelList")
    if not isinstance(channels, dict) or any(not channels.get(str(ch)) for ch in policy["rotation"]):
        raise WaxError("target_channel_not_available")


def uptime_seconds(value):
    if not isinstance(value, str):
        raise WaxError("unknown_uptime")
    units = {"day": 86400, "hour": 3600, "hr": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
    pattern = r"(\d+)\s*(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)"
    position, previous, total = 0, float("inf"), 0
    for match in re.finditer(pattern, value, re.I):
        unit = units[match[2].lower().rstrip("s")]
        amount = int(match[1])
        if value[position:match.start()].strip() or unit >= previous or amount >= previous / unit:
            raise WaxError("unknown_uptime")
        total += amount * unit
        previous, position = unit, match.end()
    if not position or value[position:].strip() or total > 2**53 - 1:
        raise WaxError("unknown_uptime")
    return total


def radio_payload(table, status):
    """Preserve settings; incomplete reads must never become guessed defaults."""
    clean = {}
    for band in ("wlan0", "wlan1"):
        row = table.get(band, {})
        for key in FIELDS:
            if type(row.get(key)) not in (str, int) or not str(row[key]).isdigit():
                raise WaxError(f"incomplete_radio_settings:{band}:{key}")
        if str(row["radioStatus"]) not in ("0", "1"):
            raise WaxError("invalid_radio_status")
        clean[band] = {key: str(row[key]) for key in FIELDS}
    clean["wlan1"]["radioStatus"] = status
    return {"system": {"wlanSettings": {"wlanSettingTable": clean}}}


class SessionRadio:
    """No login, logout, independent session, scheduler, or retry loop."""

    def __init__(self, client):
        self.client = client

    async def socket(self, payload, timeout=None):
        if timeout is None:
            reply = await self.client.socket(payload, "/WirelessBasic")
        else:
            reply = await self.client.socket(payload, "/WirelessBasic", timeout=timeout)
        if type(reply.get("status")) is not int or reply["status"] != 0:
            raise WaxError("radio_api_status_unconfirmed")
        return reply

    async def details(self):
        reply = await self.socket({"system": {"wlanSettings": {"wlanSettingTable": {"getRadioDetails": ""}}}})
        table = reply.get("system", {}).get("wlanSettings", {}).get("wlanSettingTable", {})
        if not isinstance(table, dict) or any(not isinstance(table.get(b), dict) for b in ("wlan0", "wlan1")):
            raise WaxError("invalid_radio_settings")
        missing = {band: {key: "" for key in FIELDS if table[band].get(key) in (None, "")} for band in ("wlan0", "wlan1")}
        missing = {band: fields for band, fields in missing.items() if fields}
        if missing:
            extra = await self.socket({"system": {"wlanSettings": {"wlanSettingTable": missing}}})
            extra = extra.get("system", {}).get("wlanSettings", {}).get("wlanSettingTable", {})
            for band, fields in missing.items():
                if not isinstance(extra.get(band, {}), dict):
                    raise WaxError("invalid_radio_settings")
                table[band].update({key: extra.get(band, {}).get(key) for key in fields})
        radio_payload(table, "1")
        return table

    async def readiness(self):
        system, counts = await self.client.dashboard()
        monitor = system.get("monitor", {})
        if monitor.get("productId") not in ("WAX625", "WAX628"):
            raise WaxError("unsupported_model")
        if uptime_seconds(monitor.get("DeviceInfo", {}).get("UpTime")) < 900:
            raise WaxError("boot_grace")
        return counts["wlan1"]

    async def recover(self, before_write):
        table = await self.details()
        restore = radio_payload(table, "1")
        off = deepcopy(restore)
        off["system"]["wlanSettings"]["wlanSettingTable"]["wlan1"]["radioStatus"] = "0"
        # Final fresh read in the SAME session. A client arriving while its
        # settings were read prevents OFF; counts are never taken from cache.
        if await self.readiness():
            return "clients_active"
        await before_write()

        async def apply_pair():
            if str(table["wlan1"]["radioStatus"]) == "1":
                try:
                    await self.socket(off)
                finally:
                    # Even an ambiguous OFF response requires an ON attempt.
                    await self.socket(restore)
            else:
                await self.socket(restore)

        task = asyncio.create_task(apply_pair())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            # Keep the collection session, fleet reservation and mode lock
            # until ON has been attempted, then let collect() log out.
            try:
                await task
            except Exception as err:
                _LOGGER.warning("Radio ON not confirmed during cancellation: %s", type(err).__name__)
            raise cancelled
        after = await self.details()
        if str(after["wlan1"]["radioStatus"]) != "1":
            raise WaxError("radio_restore_unconfirmed")
        return "recovered"

    async def restore_channel(self, policy, index, before_write):
        table = await self.details()
        validate_channel_choices(table, policy)
        target = policy["rotation"][index % len(policy["rotation"])]
        payload = radio_payload(table, str(table["wlan1"]["radioStatus"]))
        payload["system"]["wlanSettings"]["wlanSettingTable"]["wlan1"]["channel"] = str(target)
        # Active clients do not prevent DFS restoration (explicit user policy).
        system, _counts = await self.client.dashboard()
        monitor = system.get("monitor", {})
        if monitor.get("productId") not in ("WAX625", "WAX628"):
            raise WaxError("unsupported_model")
        if uptime_seconds(monitor.get("DeviceInfo", {}).get("UpTime")) < 900:
            raise WaxError("boot_grace")
        if channel_number(monitor.get("currentChannel", {}).get("wlan1")) in policy["allowed"]:
            return "channel_allowed"
        await before_write(target, (index + 1) % len(policy["rotation"]))
        # A channel apply may wait for DFS/CAC. Retain the session and reservation
        # through a cancelled request, then let collect() perform logout.
        task = asyncio.create_task(self.socket(payload, timeout=180))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            try:
                await task
            except Exception as err:
                _LOGGER.warning("DFS apply not confirmed during cancellation: %s", type(err).__name__)
            raise cancelled
        after = await self.details()
        if channel_number(after["wlan1"]["channel"]) != target:
            raise WaxError("channel_apply_unconfirmed")
        # This confirms configuration, not over-the-air reception after CAC.
        return "channel_configured"
