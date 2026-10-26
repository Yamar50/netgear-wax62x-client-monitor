"""NETGEAR WAX client monitor with shared access reservations."""

from datetime import timedelta

import voluptuous as vol
from homeassistant.core import ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN, PLATFORMS
from .coordinator import WaxCoordinator
from .http import ReservationView, SnapshotView

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass, config):
    hass.http.register_view(SnapshotView())
    hass.http.register_view(ReservationView())

    async def set_hostname(call: ServiceCall):
        coordinator = hass.data.get(DOMAIN)
        if coordinator is None:
            raise ServiceValidationError("Integration is not loaded")
        try:
            await coordinator.set_hostname(call.data["mac"], call.data["hostname"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async_register_admin_service(
        hass,
        DOMAIN,
        "set_hostname",
        set_hostname,
        schema=vol.Schema({vol.Required("mac"): str, vol.Required("hostname"): str}),
    )
    return True


async def async_setup_entry(hass, entry):
    coordinator = WaxCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.initialize()
    hass.data[DOMAIN] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    async def update_freshness(_now):
        # Invalidate old states even while external reservations defer collection.
        coordinator.async_update_listeners()

    entry.async_on_unload(async_track_time_interval(hass, update_freshness, timedelta(seconds=30)))
    return True


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data.pop(DOMAIN, None)
        return True
    return False


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)
