"""Fleet cache, refresh scheduling, host names and persistent access arbitration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import WaxClient, WaxError, AuthenticationError, LoginLimitError, LogoutError
from .const import DOMAIN, DEFAULT_INTERVAL, COOLDOWN_SECONDS, MAX_POLL_SECONDS
from .lease import LeaseManager, BusyError
from .model import iso_time, mac_address, reconcile

_LOGGER = logging.getLogger(__name__)


class WaxCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        self.entry = entry
        self.settings = {**entry.data, **entry.options}
        self.interval = self.settings.get("scan_interval", DEFAULT_INTERVAL)
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(seconds=self.interval)
        )
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.lease_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.lease")
        self.names = {}
        self.cooldowns = {}
        self.lease = None
        self.data = {"clients": {}, "aps": {}, "updated_at": None}

    async def initialize(self):
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

    async def save(self):
        await self.store.async_save({"names": self.names, "cooldowns": self.cooldowns, "data": self.data})

    async def set_hostname(self, mac, hostname):
        mac = mac_address(mac)
        if len(hostname) > 255 or any(ord(c) < 32 for c in hostname):
            raise ValueError("Invalid hostname")
        if hostname:
            self.names[mac] = hostname
        else:
            self.names.pop(mac, None)
        await self.save()
        self.async_update_listeners()

    def export(self):
        now = time.time()
        clients = {}
        freshness_limit = max(self.interval * 2, 120)
        for mac, source in self.data["clients"].items():
            row = dict(source)
            row["hostname"] = self.names.get(mac) or row.get("reported_hostname")
            row["custom_hostname"] = self.names.get(mac, "")
            if now - self.data.get("updated_timestamp", 0) > freshness_limit:
                row["connected"] = None
                row["fresh"] = False
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
                try:
                    # Serial AP sessions; timeout still runs the client's logout finally.
                    async with asyncio.timeout(MAX_POLL_SECONDS):
                        result = await client.collect()
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
                "updated_timestamp": now,
                "updated_at": iso_time(now),
            }
            await self.save()
            return self.data
        finally:
            if safe_release:
                await self.lease.release("home_assistant", ticket["token"])
            self.async_update_listeners()
