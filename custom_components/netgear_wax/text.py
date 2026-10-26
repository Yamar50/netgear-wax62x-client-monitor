"""Optional host names persisted independently of AP-reported names."""

from homeassistant.components.text import TextEntity
from homeassistant.const import EntityCategory
from .entity import ClientEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([Hostname(coordinator, m, "hostname") for m in coordinator.settings.get("selected_clients", [])])


class Hostname(ClientEntity, TextEntity):
    _attr_native_max = 255
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return self.coordinator.names.get(self.mac, "")

    async def async_set_value(self, value):
        await self.coordinator.set_hostname(self.mac, value)
