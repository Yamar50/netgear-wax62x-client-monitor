"""Connection and collector activity entities."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .entity import ClientEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities(
        [Connected(coordinator, m, "connected") for m in coordinator.settings.get("selected_clients", [])]
        + [Busy(coordinator)]
        + [RFVisible(coordinator, ap) for ap in coordinator.settings["aps"]]
    )


class Connected(ClientEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self):
        return self.row.get("connected")

    @property
    def extra_state_attributes(self):
        row = self.row
        return {k: row.get(k) for k in ("mac", "ap_name", "ssid", "connection_checked_at", "observed_at",
                                      "connection_source", "connection_observed_at")}


class Busy(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "collector_busy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_busy"
        self._attr_device_info = {"identifiers": {(DOMAIN, "collector")}, "name": NAME}

    @property
    def is_on(self):
        return self.coordinator.lease.status()["busy"]

    @property
    def extra_state_attributes(self):
        return self.coordinator.lease.status()


class RFVisible(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "rf_visible"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, ap):
        super().__init__(coordinator)
        self.ap_id = ap["id"]
        self._attr_unique_id = f"ap_{self.ap_id}_rf_visible"
        self._attr_device_info = {"identifiers": {(DOMAIN, self.ap_id)}, "name": ap["name"], "manufacturer": "NETGEAR"}

    @property
    def is_on(self):
        return self.coordinator.radio.export().get("observations", {}).get(self.ap_id, {}).get("visible")

    @property
    def available(self):
        return self.is_on is not None
