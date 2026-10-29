"""RF monitoring with optional recovery inside ordinary collection sessions."""

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

from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .api import WaxError
from .const import DOMAIN
from .lease import BusyError
from .model import mac_address, iso_time
from .radio_scan import scan_wifi
from .radio_recovery import SessionRadio, uptime_seconds, channel_number, validate_dfs_policies, validate_channel_choices
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
    enabled, minutes = config.get("recovery_enabled", False), config.get("missing_minutes", 30)
    if type(enabled) is not bool or type(minutes) is not int or not 10 <= minutes <= 1440:
        raise ValueError("invalid_recovery_configuration")
    dfs_enabled = config.get("dfs_enabled", False)
    policies = validate_dfs_policies(config.get("dfs_policies", {}), aps)
    if type(dfs_enabled) is not bool or (dfs_enabled and not policies):
        raise ValueError("invalid_dfs_configuration")
    return {"interface": interface, "ssid": ssid, "targets": targets,
            "recovery_enabled": enabled, "missing_minutes": minutes,
            "dfs_enabled": dfs_enabled, "dfs_policies": policies}


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
        self.recovery_cycle = None
        self.closing = False

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
                self.state["config"] = validate_config(self.state["config"], self.coordinator.settings["aps"])
            except ValueError:
                self.state.update(mode="standby", status="configuration_required")
        await self.save()
        self.unsubscribe = async_track_time_interval(self.hass, self.tick, timedelta(seconds=30))

    @callback
    def tick(self, _now=None):
        if self.closing or self.state["mode"] == "standby" or self.hass.state is not CoreState.running:
            return
        if self.task is None or self.task.done():
            self.task = self.hass.async_create_task(self.run(), "WAX local RF monitor")

    async def close(self):
        self.closing = True
        if self.unsubscribe:
            self.unsubscribe()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        # Unload must also drain an owned collection/recovery/logout session.
        async with self.lock:
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
        if self.closing or self.lock.locked() or self.state["mode"] == "standby" or time.time() - self.last_attempt < INTERVAL:
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

    async def queue_preflight(self):
        """Queue read-only settings validation for the next ordinary poll."""
        async with self.lock:
            self.state["preflight_pending"] = [ap["id"] for ap in self.coordinator.settings["aps"]]
            self.state["preflight"] = {}
            await self.save()
        return self.export()

    async def import_dfs_indexes(self, indexes):
        async with self.lock:
            policies = self.state["config"].get("dfs_policies", {})
            if self.state["mode"] != "standby" or not isinstance(indexes, dict) or set(indexes) != set(policies):
                raise ValueError("stop_monitor_and_supply_all_dfs_indexes")
            if any(type(v) is not int or not 0 <= v < len(policies[k]["rotation"]) for k, v in indexes.items()):
                raise ValueError("invalid_dfs_index")
            self.state["dfs_indexes"] = dict(indexes)
            await self.save()
        return self.export()

    def recovery_due(self, ap_id, now, cycle):
        config = self.state["config"]
        if self.closing or self.state["mode"] != "observe" or not config.get("recovery_enabled", False):
            return False
        observed = self.state.get("observations", {})
        since = self.state["missing"].get(ap_id)
        return (
            since is not None
            and 0 <= now - self.state.get("checked_timestamp", 0) <= 2 * INTERVAL
            and now - since >= config.get("missing_minutes", 30) * 60
            and observed.get(ap_id, {}).get("visible") is False
            and any(row.get("visible") is True for row in observed.values())
            and self.state["cooldowns"].get(ap_id, 0) <= now
            and self.recovery_cycle != cycle
        )

    async def after_collection(self, client, result, cycle):
        """Called under our mode lock and collector's lease before logout."""
        if self.closing or result.get("status") != "ok":
            return
        ap_id = client.config["id"]
        if self.state["mode"] == "standby":
            return
        session = SessionRadio(client)
        if ap_id in self.state.get("preflight_pending", []):
            try:
                table = await session.details()
                policy = self.state["config"].get("dfs_policies", {}).get(ap_id)
                if policy:
                    validate_channel_choices(table, policy)
                outcome = "settings_validated"
            except WaxError as err:
                outcome = str(err)
            self.state.setdefault("preflight", {})[ap_id] = {"status": outcome, "checked_at": iso_time(time.time())}
            self.state["preflight_pending"].remove(ap_id)
            await self.save()
        await self.radio_after_collection(client, result, cycle)
        # Do not combine OFF/ON and a channel apply for the same AP in one poll.
        if "radio_recovery" not in result or self.recovery_cycle != cycle:
            await self.dfs_after_collection(client, result)

    async def radio_after_collection(self, client, result, cycle):
        ap_id = client.config["id"]
        session = SessionRadio(client)
        if not self.recovery_due(ap_id, time.time(), cycle):
            return
        # Both a complete captured list and the AP's own counter must say zero.
        # Missing/failed reads never imply zero. No additional request in this case.
        if type(result.get("clients_5ghz")) is not int or type(result.get("reported_clients_5ghz")) is not int:
            return
        if result["clients_5ghz"] != 0 or result["reported_clients_5ghz"] != 0:
            return
        if not isinstance(result.get("clients"), list) or any(row.get("band") == "5GHz" for row in result["clients"]):
            return

        async def before_write():
            if not self.recovery_due(ap_id, time.time(), cycle):
                raise WaxError("recovery_conditions_changed")
            # Persist BEFORE touching Radio; failed/ambiguous writes keep the hold.
            self.state["cooldowns"][ap_id] = time.time() + 3600
            self.state.setdefault("last_recovery", {})[ap_id] = {"status": "started", "at": iso_time(time.time())}
            await self.save()
            self.recovery_cycle = cycle
            # Publish the persisted start before the first Radio write.
            self.coordinator.async_update_listeners()

        try:
            if uptime_seconds(result.get("uptime")) < 900:
                return
            outcome = await session.recover(before_write)
        except (WaxError, OSError, ValueError, TimeoutError) as err:
            outcome = str(err) if isinstance(err, WaxError) else type(err).__name__
            _LOGGER.warning("5 GHz recovery deferred for %s: %s", client.config["name"], outcome)
        self.state.setdefault("last_recovery", {})[ap_id] = {"status": outcome, "at": iso_time(time.time())}
        await self.save()
        result["radio_recovery"] = outcome
        self.coordinator.async_update_listeners()

    async def dfs_after_collection(self, client, result):
        config = self.state["config"]
        ap_id = client.config["id"]
        policy = config.get("dfs_policies", {}).get(ap_id)
        if self.closing or self.state["mode"] != "observe" or not config.get("dfs_enabled", False) or not policy:
            return

        async def before_write(target, next_index):
            if self.closing or self.state["mode"] != "observe":
                raise WaxError("maintenance_stopping")
            # Match the legacy next-index-before-apply ordering, including an
            # ambiguous write. Import/export this index during owner handoff.
            self.state.setdefault("dfs_indexes", {})[ap_id] = next_index
            self.state.setdefault("last_dfs", {})[ap_id] = {
                "status": "started", "target_channel": target, "at": iso_time(time.time())}
            self.state["missing"].pop(ap_id, None)
            await self.save()
            # Publish before channel Apply; completion can take several minutes.
            self.coordinator.async_update_listeners()

        try:
            current = channel_number(result.get("channel_5ghz"))
            if current in policy["allowed"]:
                result["dfs_restoration"] = "channel_allowed"
                return
            if uptime_seconds(result.get("uptime")) < 900:
                return
            index = self.state.get("dfs_indexes", {}).get(ap_id, 0)
            outcome = await SessionRadio(client).restore_channel(policy, index, before_write)
        except (WaxError, OSError, ValueError, TimeoutError) as err:
            outcome = str(err) if isinstance(err, WaxError) else type(err).__name__
            _LOGGER.warning("DFS restoration deferred for %s: %s", client.config["name"], outcome)
        previous = self.state.get("last_dfs", {}).get(ap_id, {})
        self.state.setdefault("last_dfs", {})[ap_id] = {**previous, "status": outcome, "at": iso_time(time.time())}
        await self.save()
        result["dfs_restoration"] = outcome
        self.coordinator.async_update_listeners()

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
        # Missing is a continuous series of successful, fresh partial scans.
        # All targets absent may be a receiver problem, never restart all APs.
        self.state["missing"] = {
            key: self.state["missing"].get(key, now)
            for key, row in visible.items() if row is None
        } if any(visible.values()) else {}
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
