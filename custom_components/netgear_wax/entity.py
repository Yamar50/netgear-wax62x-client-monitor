"""Stable client identity independent of AP roaming and host name edits."""

from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_NETWORK_MAC
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN


class ClientEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, mac, key):
        super().__init__(coordinator)
        self.mac = mac
        self._attr_unique_id = f"{mac}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)}, connections={(CONNECTION_NETWORK_MAC, mac)}, name=f"Wi-Fi {mac}"
        )

    @property
    def row(self):
        return self.coordinator.export()["clients"].get(self.mac, {})

    @property
    def available(self):
        return self.row.get("connected") is not None
