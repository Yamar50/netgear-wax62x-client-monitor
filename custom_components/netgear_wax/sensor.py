"""Client metrics and AP collection diagnostics."""

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfDataRate
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .entity import ClientEntity

FIELDS = {
    "ap_name": (None, None),
    "ssid": (None, None),
    "ip": (None, None),
    "channel": (None, None),
    "band": (None, None),
    "mode": (None, None),
    "rssi": (None, None),
    "tx_rate": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.BITS_PER_SECOND),
    "rx_rate": (SensorDeviceClass.DATA_RATE, UnitOfDataRate.BITS_PER_SECOND),
    "tx_bytes": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.BYTES),
    "rx_bytes": (SensorDeviceClass.DATA_SIZE, UnitOfInformation.BYTES),
    "association_time": (None, None),
    "security": (None, None),
    "pmf": (None, None),
    "state": (None, None),
    "status": (None, None),
    "observed_at": (SensorDeviceClass.TIMESTAMP, None),
    "reported_hostname": (None, None),
}
AP_FIELDS = ("uptime", "clients_24ghz", "clients_5ghz", "status", "channel_24ghz", "channel_5ghz")


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    entities = [
        ClientMetric(coordinator, m, key) for m in coordinator.settings.get("selected_clients", []) for key in FIELDS
    ]
    entities.extend(APMetric(coordinator, ap, key) for ap in coordinator.settings["aps"] for key in AP_FIELDS)
    entities.extend(RFMetric(coordinator, ap, key) for ap in coordinator.settings["aps"] for key in ("channel", "rssi"))
    entities.append(RFStatus(coordinator))
    async_add_entities(entities)


class ClientMetric(ClientEntity, SensorEntity):
    def __init__(self, coordinator, mac, key):
        super().__init__(coordinator, mac, key)
        self.key = key
        self._attr_device_class, self._attr_native_unit_of_measurement = FIELDS[key]
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self):
        return self.row.get("fresh", False) and super().available

    @property
    def native_value(self):
        value = self.row.get(self.key)
        if self.key == "observed_at" and value:
            return datetime.fromisoformat(value)
        if self.key == "pmf" and value is not None:
            return "Yes" if value else "No"
        return value


class APMetric(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, ap, key):
        super().__init__(coordinator)
        self.ap_id = ap["id"]
        self.key = key
        self._attr_unique_id = f"ap_{self.ap_id}_{key}"
        self._attr_translation_key = f"ap_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self.ap_id)}, "name": ap["name"], "manufacturer": "NETGEAR"}

    @property
    def native_value(self):
        return self.coordinator.export()["aps"].get(self.ap_id, {}).get(self.key)

    @property
    def available(self):
        if self.key == "status":
            return True
        return self.coordinator.export()["aps"].get(self.ap_id, {}).get("status") == "ok"


class RFMetric(APMetric):
    def __init__(self, coordinator, ap, key):
        super().__init__(coordinator, ap, key)
        self._attr_unique_id = f"ap_{self.ap_id}_rf_{key}"
        self._attr_translation_key = f"rf_{key}"
        if key == "rssi":
            self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
            self._attr_native_unit_of_measurement = "dBm"

    @property
    def native_value(self):
        return self.coordinator.radio.export().get("observations", {}).get(self.ap_id, {}).get(self.key)

    @property
    def available(self):
        return self.coordinator.radio.export().get("observations", {}).get(self.ap_id, {}).get("visible") is True


class RFStatus(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "rf_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["standby", "observe", "active", "starting", "configuration_required", "waiting_for_access",
                     "check_failed", "all_visible", "observing_missing", "all_missing_hold", "missing_wait",
                     "clients_active_hold", "clients_active", "recovered"]

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_rf_status"
        self._attr_device_info = {"identifiers": {(DOMAIN, "collector")}, "name": NAME}

    @property
    def native_value(self):
        return self.coordinator.radio.state["status"]

    @property
    def extra_state_attributes(self):
        state = self.coordinator.radio.export()
        return {k: state.get(k) for k in ("mode", "running", "checked_at", "interval_seconds", "scan_seconds",
                                        "missing", "cooldowns", "last_action", "error")}
