"""Conservative 5 GHz recovery using the existing owned-login/logout path."""

import asyncio
from copy import deepcopy
import re

from .api import WaxClient, WaxError

FIELDS = ("radioStatus", "operateMode", "guardInterval", "txPower", "channel", "channelWidth", "beamForming")


def uptime_seconds(value):
    if not isinstance(value, str):
        raise WaxError("unknown_uptime")
    units = {"day": 86400, "hour": 3600, "hr": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
    pattern = r"(\d+)\s*(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)"
    position, previous, total = 0, float("inf"), 0
    for match in re.finditer(pattern, value, re.I):
        unit = units[match[2].lower().rstrip("s")]
        amount = int(match[1])
        if value[position : match.start()].strip() or unit >= previous or amount >= previous / unit:
            raise WaxError("unknown_uptime")
        total += amount * unit
        previous, position = unit, match.end()
    if not position or value[position:].strip() or total > 2**53 - 1:
        raise WaxError("unknown_uptime")
    return total


def radio_payload(table, status):
    """No guessed defaults: retain every existing radio setting verbatim."""
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


class WaxRadioClient(WaxClient):
    strict_page = True
    strict_status = True

    def __init__(self, config, recover=False, inspect=False):
        super().__init__(config)
        self.recover = recover
        self.inspect = inspect
        self.mutation_started = False

    async def readiness(self):
        await self.request("/dashboard")
        system, counts = await self.dashboard()
        monitor = system.get("monitor", {})
        if monitor.get("productId") not in ("WAX625", "WAX628"):
            raise WaxError("unsupported_model")
        uptime = uptime_seconds(monitor.get("DeviceInfo", {}).get("UpTime"))
        if uptime < 900:
            raise WaxError("boot_grace")
        return {"uptime_seconds": uptime, "clients_5ghz": counts["wlan1"]}

    async def details(self):
        reply = await self.socket(
            {"system": {"wlanSettings": {"wlanSettingTable": {"getRadioDetails": ""}}}}, "/WirelessBasic"
        )
        table = reply.get("system", {}).get("wlanSettings", {}).get("wlanSettingTable", {})
        # getRadioDetails omits some fields on real firmware. Request those
        # leaves explicitly; never substitute defaults before applying settings.
        missing = {
            band: {key: "" for key in FIELDS if table.get(band, {}).get(key) in (None, "")}
            for band in ("wlan0", "wlan1")
        }
        missing = {band: fields for band, fields in missing.items() if fields}
        if missing:
            extra = await self.socket({"system": {"wlanSettings": {"wlanSettingTable": missing}}}, "/WirelessBasic")
            extra = extra.get("system", {}).get("wlanSettings", {}).get("wlanSettingTable", {})
            for band, fields in missing.items():
                table.setdefault(band, {}).update({key: extra.get(band, {}).get(key) for key in fields})
        radio_payload(table, "1")
        return table

    async def apply(self, payload):
        self.mutation_started = True
        await self.socket(payload, "/WirelessBasic")

    async def read_clients(self):
        await self.socket({"system": {"basicSettings": {"dayZeroStatus": ""}}}, "/AP_login")
        ready = await self.readiness()
        if not self.recover:
            if self.inspect:
                table = await self.details()
                return {
                    "status": "ready",
                    **ready,
                    "radio_5ghz_on": str(table["wlan1"]["radioStatus"]) == "1",
                    "configured_channel": table["wlan1"]["channel"],
                    "radio_fields_validated": True,
                }
            return {"status": "ready", **ready}
        if ready["clients_5ghz"]:
            return {"status": "clients_active", **ready}
        table = await self.details()
        restore = radio_payload(table, "1")
        off = deepcopy(restore)
        off["system"]["wlanSettings"]["wlanSettingTable"]["wlan1"]["radioStatus"] = "0"
        # Final fresh check immediately before the first write.
        ready = await self.readiness()
        if ready["clients_5ghz"]:
            return {"status": "clients_active", **ready}
        if str(table["wlan1"]["radioStatus"]) == "1":
            try:
                await self.apply(off)
            finally:
                # Even an ambiguous OFF response or cancellation must attempt ON.
                task = asyncio.create_task(self.apply(restore))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
        else:
            await self.apply(restore)
        after = await self.details()
        if str(after["wlan1"]["radioStatus"]) != "1":
            raise WaxError("radio_restore_unconfirmed")
        return {"status": "recovered", **ready}
