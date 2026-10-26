"""Persistent passive RF monitoring without AP management requests."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import logging
import math
from pathlib import Path
import re
import struct
import time

from homeassistant.core import CoreState
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .api import WaxError
from .const import DOMAIN
from .lease import BusyError
from .model import mac_address, iso_time
from .radio_scan import scan_wifi
from .wifi_probe import fingerprint

_LOGGER = logging.getLogger(__name__)
INTERVAL = 600
MODES = ("standby", "observe")


def validate_config(config, aps):
    interface, ssid, targets = config.get("interface", ""), config.get("ssid", ""), config.get("targets", {})
    if not isinstance(interface, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", interface):
        raise ValueError("invalid_interface")
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("invalid_ssid")
    if not isinstance(targets, dict) or set(targets) != {ap["id"] for ap in aps}:
        raise ValueError("all_ap_targets_required")
    targets = {key: mac_address(value) for key, value in targets.items()}
    if len(set(targets.values())) != len(targets):
        raise ValueError("duplicate_bssid")
    return {"interface": interface, "ssid": ssid, "targets": targets}


def visibility(config, scan):
    if scan.get("completed") is not True:
        raise ValueError("scan_not_completed")
    ssid = fingerprint(config["ssid"].encode())
    rows = {
        row["bssid_sha256"]: row
        for row in scan["rows"]
        if row.get("ssid_sha256") == ssid and 5000 <= row.get("frequency_mhz", 0) < 6000
    }
    return {key: rows.get(fingerprint(bytes.fromhex(mac.replace(":", "")))) for key, mac in config["targets"].items()}


class RadioMonitor:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.store = Store(self.hass, 1, f"{DOMAIN}.{coordinator.entry.entry_id}.radio_monitor", atomic_writes=True)
        self.lock = asyncio.Lock()
        self.state = {"mode": "standby", "config": {}, "missing": {}, "cooldowns": {}, "status": "standby"}
        self.last_attempt = 0
        self.task = None
        self.unsubscribe = None

    async def save(self):
        saved = deepcopy(self.state)
        await self.store.async_save(saved)
        actual = await self.hass.async_add_executor_job(lambda: json.loads(Path(self.store.path).read_text())["data"])
        if actual != saved:
            raise WaxError("radio_state_not_saved")

    async def initialize(self):
        self.state.update(await self.store.async_load() or {})
        if self.state["mode"] not in MODES:
            self.state["mode"] = "standby"
        # Preserve recovery cooldowns, never carry old missing streaks across boot.
        self.state.update(
            missing={}, observations={}, status="starting" if self.state["mode"] != "standby" else "standby"
        )
        if self.state["config"]:
            try:
                validate_config(self.state["config"], self.coordinator.settings["aps"])
            except ValueError:
                self.state.update(mode="standby", status="configuration_required")
        await self.save()
        self.unsubscribe = async_track_time_interval(self.hass, self.tick, timedelta(seconds=30))

    def tick(self, _now=None):
        if self.state["mode"] == "standby" or self.hass.state is not CoreState.running:
            return
        if self.task is None or self.task.done():
            self.task = self.hass.async_create_task(self.run(), "WAX local RF monitor")

    async def close(self):
        if self.unsubscribe:
            self.unsubscribe()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def export(self):
        result = deepcopy(self.state)
        result["running"] = self.lock.locked()
        result["interval_seconds"] = INTERVAL
        if self.state["mode"] == "standby" or time.time() - self.state.get("checked_timestamp", 0) > 2 * INTERVAL:
            for value in result.get("observations", {}).values():
                value["visible"] = None
        return result

    async def configure(self, config):
        config = validate_config(config, self.coordinator.settings["aps"])
        async with self.lock:
            if self.state["mode"] != "standby":
                raise ValueError("stop_monitor_before_configuring")
            self.state.update(config=config, missing={}, observations={}, status="standby")
            await self.save()
        self.coordinator.async_update_listeners()
        return self.export()

    async def set_mode(self, mode, hold_until=0):
        if mode not in MODES:
            raise ValueError("invalid_mode")
        if (
            type(hold_until) not in (int, float)
            or not math.isfinite(hold_until)
            or not 0 <= hold_until <= time.time() + 86400
        ):
            raise ValueError("invalid_recovery_hold")
        # Wait for a complete in-flight scan before acknowledging.
        async with self.lock:
            if mode != "standby":
                validate_config(self.state["config"], self.coordinator.settings["aps"])
            previous = deepcopy(self.state)
            self.state.update(mode=mode, status=mode, missing={}, observations={})
            if hold_until > time.time():
                for ap in self.coordinator.settings["aps"]:
                    key = ap["id"]
                    self.state["cooldowns"][key] = max(self.state["cooldowns"].get(key, 0), hold_until)
            try:
                await self.save()
            except Exception:
                self.state = previous
                raise
            self.last_attempt = 0
        self.coordinator.async_update_listeners()
        return self.export()

    async def run(self):
        if self.lock.locked() or self.state["mode"] == "standby" or time.time() - self.last_attempt < INTERVAL:
            return self.export()
        async with self.lock:
            if self.state["mode"] == "standby":
                return self.export()
            try:
                ticket = await self.coordinator.lease.acquire("radio_monitor", 3600, "collector")
            except BusyError:
                # A deferred attempt is not a missing observation; retry later.
                self.state["status"] = "waiting_for_access"
                self.coordinator.async_update_listeners()
                return self.export()
            self.last_attempt = time.time()
            if self.last_attempt - self.state.get("checked_timestamp", 0) > 2 * INTERVAL:
                self.state["missing"] = {}
            self.coordinator.async_update_listeners()
            try:
                async with asyncio.timeout(300):
                    await self.check()
            except (WaxError, OSError, ValueError, KeyError, struct.error, TimeoutError) as error:
                self.state.update(status="check_failed", missing={}, error=str(error))
                for observation in self.state.get("observations", {}).values():
                    observation["visible"] = None
                _LOGGER.warning("5 GHz monitoring deferred: %s", error)
            finally:
                try:
                    await self.save()
                finally:
                    await self.coordinator.lease.release("radio_monitor", ticket["token"])
                    self.coordinator.async_update_listeners()
        return self.export()

    async def check(self):
        config = validate_config(self.state["config"], self.coordinator.settings["aps"])
        async with self.hass.data[f"{DOMAIN}_scan_lock"]:
            scan_task = asyncio.ensure_future(self.hass.async_add_executor_job(scan_wifi, config["interface"]))
            try:
                scan = await asyncio.shield(scan_task)
            except asyncio.CancelledError:
                await scan_task
                raise
        visible = visibility(config, scan)
        now = time.time()
        self.state.update(
            checked_timestamp=now,
            checked_at=iso_time(now),
            error=None,
            scan_seconds=scan["elapsed_seconds"],
            observations={
                key: {
                    "visible": row is not None,
                    "channel": (row["frequency_mhz"] - 5000) // 5 if row else None,
                }
                for key, row in visible.items()
            },
        )
        self.state["status"] = "all_visible" if all(visible.values()) else "observing_missing"
