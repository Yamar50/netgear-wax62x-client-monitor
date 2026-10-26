"""Fleet cache, refresh scheduling, host names and persistent access arbitration."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
import logging
from pathlib import Path
import time

from homeassistant.core import CoreState, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import WaxClient, WaxError, AuthenticationError, LoginLimitError, LogoutError
from .const import DOMAIN, DEFAULT_INTERVAL, COOLDOWN_SECONDS, MAX_POLL_SECONDS
from .lease import LeaseManager, BusyError
from .model import iso_time, mac_address, reconcile
from .connection import apply_report, validate_report

_LOGGER = logging.getLogger(__name__)


class WaxCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        self.entry = entry
        self.settings = {**entry.data, **entry.options}
        self.interval = self.settings.get("scan_interval", DEFAULT_INTERVAL)
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(seconds=self.interval)
        )
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        self._save_lock = asyncio.Lock()
        self.lease_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.lease")
        self.names = {}
        self.device_names_ready = False
        self.connection_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.connections", atomic_writes=True)
        self._connection_lock = asyncio.Lock()
        self.connection_reports = {}
        self.cooldowns = {}
        self.lease = None
        self.data = {"clients": {}, "aps": {}, "updated_at": None}

    async def initialize(self):
        self.connection_reports = await self.connection_store.async_load() or {}
        saved = await self.store.async_load() or {}
        self.names = saved.get("names", {})
        self.cooldowns = saved.get("cooldowns", {})
        self.data = saved.get("data", self.data)
        # Never restore a historical connection as a current positive/negative.
        for client in self.data.get("clients", {}).values():
            client.update(connected=None, fresh=False)
        for ap in self.data.get("aps", {}).values():
            ap["status"] = "not_yet_collected"
        self.lease = LeaseManager(self.lease_store.async_save, await self.lease_store.async_load())
        # No initial login when polling is disabled: safe HACS setup/cutover.
        await self.async_config_entry_first_refresh()

    async def report_connection(self, data):
        event = validate_report(data, self.settings["aps"], time.time())
        ack = {k: event[k] for k in ("mac", "connected", "observed_at")}
        async with self._connection_lock:
            if self.hass.state is CoreState.stopping:
                raise HomeAssistantError("Home Assistant is stopping; retry later")
            previous = self.connection_reports.get(event["mac"])
            if previous and previous["observed_timestamp"] >= event["observed_timestamp"]:
                return {**ack, "handled": True, "accepted": False, "reason": "duplicate_or_older"}
            current = self.export()["clients"].get(event["mac"], {})
            if not event["connected"] and current.get("connected") is True and current.get("ap_id") != event["ap_id"]:
                return {**ack, "handled": True, "accepted": False, "reason": "different_ap"}
            reports = {mac: report for mac, report in self.connection_reports.items()
                       if report["observed_timestamp"] >= time.time() - 86400}
            if len(reports) >= 8192 and event["mac"] not in reports:
                raise HomeAssistantError("Connection report capacity reached")
            reports[event["mac"]] = event
            await self.connection_store.async_save(reports)
            try:
                saved = await self.hass.async_add_executor_job(
                    lambda: json.loads(Path(self.connection_store.path).read_text())["data"]
                )
            except (OSError, ValueError, KeyError) as err:
                raise HomeAssistantError("Connection report storage confirmation failed") from err
            if saved != reports:
                raise HomeAssistantError("Connection report storage confirmation failed")
            self.connection_reports = reports
        # Preserve the scheduled poll and raw AP snapshot age.
        self.async_update_listeners()
        return {**ack, "handled": True, "accepted": True, "reason": "stored"}

    async def save(self):
        async with self._save_lock:
            await self.store.async_save({"names": dict(self.names), "cooldowns": self.cooldowns, "data": self.data})

    async def set_hostname(self, mac, hostname):
        mac = mac_address(mac)
        if len(hostname) > 255 or any(ord(c) < 32 for c in hostname):
            raise ValueError("Invalid hostname")
        async with self._save_lock:
            if self.hass.state is CoreState.stopping:
                raise HomeAssistantError("Home Assistant is stopping; retry later")
            names = dict(self.names)
            if hostname:
                names[mac] = hostname
            else:
                names.pop(mac, None)
            await self.store.async_save({"names": names, "cooldowns": self.cooldowns, "data": self.data})
            # Store logs some write failures instead of raising. Confirm the
            # file, not just the HTTP call or an in-memory entity update.
            try:
                saved = await self.hass.async_add_executor_job(self._read_saved_names)
            except (OSError, ValueError, KeyError) as err:
                raise HomeAssistantError("HostName storage confirmation failed") from err
            if saved != names:
                raise HomeAssistantError("HostName storage confirmation failed")
            self.names = names
        self.async_sync_device_names([mac])
        self.async_update_listeners()
        return {"mac": mac, "hostname": hostname, "stored": True}

    def _read_saved_names(self):
        return json.loads(Path(self.store.path).read_text())["data"]["names"]

    @callback
    def async_sync_device_names(self, macs=None):
        """Update only our registered clients' default names, never identity."""
        if not self.device_names_ready:
            # A push can arrive while platforms are registering entities.
            # Defer the display change until every new entity has its MAC ID.
            return
        registry = dr.async_get(self.hass)
        selected = set(self.settings.get("selected_clients", []))
        for mac in selected if macs is None else selected.intersection(macs):
            device = registry.async_get_device_by_identifier((DOMAIN, mac), self.entry.entry_id)
            if device is None:
                continue  # Pre-saved names must not create devices.
            name = self.names.get(mac) or f"Wi-Fi {mac}"
            if device.name != name:
                # name_by_user is separate: HA keeps a manual name preferred,
                # and removing that override reveals the latest HostName.
                registry.async_update_device(device.id, name=name)

    def select_clients(self, selected):
        """Apply a selection to the existing observation, without advancing its age."""
        self.settings["selected_clients"] = list(selected)
        results = [
            {
                **self.data.get("aps", {}).get(ap["id"], {"status": "not_yet_collected"}),
                "clients": self.data.get("ap_clients", {}).get(ap["id"], []),
            }
            for ap in self.settings["aps"]
        ]
        self.data["clients"] = reconcile(
            self.data["clients"], results, selected, self.data.get("updated_timestamp") or 0
        )

    def export(self):
        now = time.time()
        clients = {}
        freshness_limit = max(self.interval * 2, 120)
        candidates = dict(self.data["clients"])
        for mac, report in self.connection_reports.items():
            if now - report["observed_timestamp"] <= freshness_limit:
                candidates.setdefault(mac, {"mac": mac, "connected": None, "fresh": False})
        for mac, source in candidates.items():
            row = dict(source)
            row["connection_source"] = "poll"
            row["connection_observed_at"] = row.get("connection_checked_at")
            row["hostname"] = self.names.get(mac) or row.get("reported_hostname")
            row["custom_hostname"] = self.names.get(mac, "")
            if now - self.data.get("updated_timestamp", 0) > freshness_limit:
                row["connected"] = None
                row["fresh"] = False
            row = apply_report(row, self.connection_reports.get(mac), self.data["aps"], now, freshness_limit)
            clients[mac] = row
        return {
            "schema_version": 1,
            "generated_at": iso_time(now),
            "updated_at": self.data.get("updated_at"),
            "updated_timestamp": self.data.get("updated_timestamp"),
            "polling_enabled": self.settings.get("polling_enabled", False),
            "scan_interval": self.interval,
            "freshness_limit": freshness_limit,
            "access": self.lease.status(),
            "aps": {
                key: {**ap, "status": "stale"}
                if now - (ap.get("observed_timestamp") or 0) > freshness_limit and ap.get("status") == "ok"
                else dict(ap)
                for key, ap in self.data["aps"].items()
            },
            "clients": clients,
            # Per-AP observations before fleet deduplication/history retention.
            # Consumers must check AP status/freshness before using these rows.
            "ap_clients": self.data.get("ap_clients", {}),
        }

    async def _async_update_data(self):
        if not self.settings.get("polling_enabled", False):
            return self.data
        try:
            ticket = await self.lease.acquire("home_assistant", 3600, "collector")
        except BusyError:
            return self.data
        self.async_update_listeners()
        safe_release = True
        try:
            results = []
            for config in self.settings["aps"]:
                ap_id = config["id"]
                result = {"id": ap_id, "name": config["name"], "status": "unavailable"}
                if self.cooldowns.get(ap_id, 0) > time.time():
                    result.update(status="cooldown", retry_after=self.cooldowns[ap_id])
                    results.append(result)
                    continue
                client = WaxClient(config)
                collection_started = time.time()
                try:
                    # Serial AP sessions; timeout still runs the client's logout finally.
                    async with asyncio.timeout(MAX_POLL_SECONDS):
                        result = await client.collect()
                        result["collection_started_timestamp"] = collection_started
                except (AuthenticationError, LoginLimitError, LogoutError) as err:
                    result["error"] = str(err)
                    self.cooldowns[ap_id] = time.time() + COOLDOWN_SECONDS
                except (WaxError, TimeoutError) as err:
                    result["error"] = str(err) if isinstance(err, WaxError) else "collection_timeout"
                finally:
                    if client.login_attempted and not client.logout_ok:
                        self.cooldowns[ap_id] = time.time() + COOLDOWN_SECONDS
                        # Conservatively retain fleet exclusion after ambiguous logout,
                        # including across an HA process restart.
                        safe_release = False
                    await self.save()
                results.append(result)
                if not safe_release:
                    # Do not start further logins after an unresolved session.
                    break
            seen = {r["id"] for r in results}
            results.extend(
                {"id": c["id"], "name": c["name"], "status": "not_collected"}
                for c in self.settings["aps"]
                if c["id"] not in seen
            )
            now = time.time()
            self.data = {
                "clients": reconcile(self.data["clients"], results, self.settings.get("selected_clients", []), now),
                "aps": {r["id"]: {k: v for k, v in r.items() if k != "clients"} for r in results},
                "ap_clients": {r["id"]: r.get("clients", []) for r in results if r["status"] == "ok"},
                "updated_timestamp": now,
                "updated_at": iso_time(now),
            }
            await self.save()
            return self.data
        finally:
            if safe_release:
                await self.lease.release("home_assistant", ticket["token"])
            self.async_update_listeners()
