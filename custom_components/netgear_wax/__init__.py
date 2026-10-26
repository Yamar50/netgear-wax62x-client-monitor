"""NETGEAR WAX client monitor with shared access reservations."""

from datetime import timedelta

import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.exceptions import ServiceValidationError
from homeassistant.config_entries import ConfigEntryState

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
            result = await coordinator.set_hostname(call.data["mac"], call.data["hostname"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        entity_id = er.async_get(hass).async_get_entity_id("text", DOMAIN, f"{result['mac']}_hostname")
        selected = result["mac"] in coordinator.settings.get("selected_clients", [])
        return {**result, "entity_registered": bool(selected and entity_id), "entity_id": entity_id if selected else None}

    async_register_admin_service(
        hass,
        DOMAIN,
        "set_hostname",
        set_hostname,
        schema=vol.Schema({vol.Required("mac"): str, vol.Required("hostname"): str}),
        supports_response=SupportsResponse.OPTIONAL,
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
    coordinator = entry.runtime_data
    settings = {**entry.data, **entry.options}
    previous = {k: v for k, v in coordinator.settings.items() if k != "selected_clients"}
    current = {k: v for k, v in settings.items() if k != "selected_clients"}
    if previous == current:
        # Rebuild entity platforms only. Keep the collector, cache, reservation
        # and scheduled refresh alive; a client selection needs no AP login.
        async with entry.setup_lock:
            if entry.state is not ConfigEntryState.LOADED:
                return
            keep_polling = coordinator.async_add_listener(lambda: None)
            try:
                if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
                    raise ServiceValidationError("Unable to update client entities")
                coordinator.select_clients(settings.get("selected_clients", []))
                await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
            finally:
                keep_polling()
        return
    await hass.config_entries.async_reload(entry.entry_id)
