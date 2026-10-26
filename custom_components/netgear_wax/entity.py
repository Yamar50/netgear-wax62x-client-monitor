"""Stable client identity independent of AP roaming and host name edits."""

from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_NETWORK_MAC
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN


class ClientEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, mac, key):
        super().__init__(coordinator)
        self.mac = mac
        self._attr_unique_id = f"{mac}_{key}"
        self._attr_translation_key = key

    @property
    def device_info(self):
        # Keep the registry's current name while registering entities. New
        # clients start with the MAC name so their generated entity IDs retain
        # the existing format; saved HostNames are applied after platform setup.
        device = dr.async_get(self.coordinator.hass).async_get_device_by_identifier(
            (DOMAIN, self.mac), self.coordinator.entry.entry_id
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self.mac)},
            connections={(CONNECTION_NETWORK_MAC, self.mac)},
            name=device.name if device else f"Wi-Fi {self.mac}",
        )

    @property
    def row(self):
        return self.coordinator.export()["clients"].get(self.mac, {})

    @property
    def available(self):
        return self.row.get("connected") is not None
