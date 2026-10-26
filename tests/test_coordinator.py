from datetime import datetime
from types import MappingProxyType
import time

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import device_registry as dr
import pytest

from custom_components.netgear_wax.coordinator import WaxCoordinator
from custom_components.netgear_wax.lease import LeaseManager, BusyError
from custom_components.netgear_wax.api import LogoutError
from custom_components.netgear_wax.binary_sensor import Connected
from custom_components.netgear_wax.sensor import ClientMetric, FIELDS
from custom_components.netgear_wax.model import normalize_client

MAC = "02:00:00:00:00:01"
AP = {"id": "a", "name": "Test", "host": "ap.example.test", "username": "sample", "password": "sample-secret"}


@pytest.fixture
async def coordinator(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    dr.async_setup(hass)
    await dr.async_load(hass)
    entry = ConfigEntry(
        domain="netgear_wax",
        title="Test",
        data={"aps": [AP], "polling_enabled": True, "selected_clients": [MAC]},
        options={},
        version=1,
        minor_version=1,
        source="user",
        unique_id="netgear_wax",
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        state=ConfigEntryState.SETUP_IN_PROGRESS,
    )
    coordinator = WaxCoordinator(hass, entry)
    coordinator.device_names_ready = True

    async def save(data):
        pass

    coordinator.lease = LeaseManager(save)
    coordinator.save = lambda: save(None)
    yield coordinator
    await coordinator.async_shutdown()
    await hass.async_stop(force=True)


async def test_external_reservation_prevents_any_ap_call(coordinator, monkeypatch):
    def forbidden(*args):
        pytest.fail("AP access during external reservation")

    monkeypatch.setattr("custom_components.netgear_wax.coordinator.WaxClient", forbidden)
    ticket = await coordinator.lease.acquire("consumer")
    await coordinator._async_update_data()
    assert coordinator.lease.status()["owner"] == "consumer"
    await coordinator.lease.release("consumer", ticket["token"])


async def test_poll_success_and_entity_values(coordinator, monkeypatch):
    class Client:
        login_attempted = True
        logout_ok = True

        def __init__(self, config):
            pass

        async def collect(self):
            with pytest.raises(BusyError):
                await coordinator.lease.acquire("consumer")
            return {
                "id": "a",
                "name": "Test",
                "status": "ok",
                "clients": [
                    normalize_client({"mac": MAC, "status": "Authenticated", "txB": 0}, AP, "5GHz", time.time())
                ],
            }

    monkeypatch.setattr("custom_components.netgear_wax.coordinator.WaxClient", Client)
    await coordinator._async_update_data()
    assert not coordinator.lease.status()["busy"]
    assert Connected(coordinator, MAC, "connected").is_on is True
    assert ClientMetric(coordinator, MAC, "tx_bytes").native_value == 0
    assert isinstance(ClientMetric(coordinator, MAC, "observed_at").native_value, datetime)
    assert "sample-secret" not in str(coordinator.export())
    exported = coordinator.export()
    assert exported["ap_clients"]["a"][0]["legacy_row"]["txB"] == 0
    assert exported["ap_clients"]["a"][0]["mac"] == MAC


async def test_logout_failure_holds_lease_and_unknown(coordinator, monkeypatch):
    class Client:
        login_attempted = True
        logout_ok = False

        def __init__(self, config):
            pass

        async def collect(self):
            raise LogoutError("logout_failed")

    monkeypatch.setattr("custom_components.netgear_wax.coordinator.WaxClient", Client)
    await coordinator._async_update_data()
    assert coordinator.lease.status()["busy"]
    assert coordinator.export()["clients"][MAC]["connected"] is None
    assert coordinator.cooldowns["a"] > time.time() + 3500


async def test_names_do_not_change_identity_or_get_overwritten(coordinator):
    before = Connected(coordinator, MAC, "connected").unique_id
    await coordinator.set_hostname(MAC, "Family iPad")
    assert coordinator.names[MAC] == "Family iPad"
    assert Connected(coordinator, MAC, "connected").unique_id == before
    await coordinator.set_hostname(MAC, "")
    assert MAC not in coordinator.names


async def test_unselected_hostname_is_persisted_and_survives_reload(coordinator):
    from custom_components.netgear_wax.text import Hostname
    unknown = "02:00:00:00:00:02"
    result = await coordinator.set_hostname(unknown, "Future phone")
    assert result == {"mac": unknown, "hostname": "Future phone", "stored": True}
    assert unknown not in coordinator.settings["selected_clients"]
    restored = WaxCoordinator(coordinator.hass, coordinator.config_entry)
    saved = await restored.store.async_load()
    assert saved["names"][unknown] == "Future phone"
    assert Hostname(coordinator, unknown, "hostname").native_value == "Future phone"
    await restored.async_shutdown()


async def test_swallowed_storage_failure_has_no_false_ack(coordinator, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError
    await coordinator.set_hostname(MAC, "Original")
    async def silent_failure(data):
        pass
    monkeypatch.setattr(coordinator.store, "async_save", silent_failure)
    with pytest.raises(HomeAssistantError, match="confirmation failed"):
        await coordinator.set_hostname(MAC, "Not saved")
    assert coordinator.names[MAC] == "Original"


async def test_concurrent_names_are_not_lost(coordinator):
    import asyncio
    other = "02:00:00:00:00:02"
    await asyncio.gather(coordinator.set_hostname(MAC, "One"), coordinator.set_hostname(other, "Two"))
    assert coordinator._read_saved_names() == {MAC: "One", other: "Two"}


async def test_no_ack_for_deferred_save_while_stopping(coordinator):
    from homeassistant.core import CoreState
    from homeassistant.exceptions import HomeAssistantError
    old = coordinator.hass.state
    coordinator.hass.state = CoreState.stopping
    try:
        with pytest.raises(HomeAssistantError, match="stopping"):
            await coordinator.set_hostname(MAC, "Deferred")
        assert coordinator.names == {}
    finally:
        coordinator.hass.state = old


async def test_display_name_sync_only_changes_owned_selected_device(coordinator, monkeypatch):
    from unittest.mock import Mock
    from types import SimpleNamespace
    other = "02:00:00:00:00:02"
    registry = Mock()
    device = SimpleNamespace(id="owned", name="Old name", name_by_user="Manual")
    registry.async_get_device_by_identifier.return_value = device
    monkeypatch.setattr(dr, "async_get", lambda hass: registry)
    coordinator.names = {MAC: "New name", other: "Unregistered"}
    coordinator.async_sync_device_names()
    registry.async_get_device_by_identifier.assert_called_once_with(("netgear_wax", MAC), coordinator.entry.entry_id)
    registry.async_update_device.assert_called_once_with("owned", name="New name")
    registry.reset_mock()
    coordinator.async_sync_device_names([other])
    registry.async_get_device_by_identifier.assert_not_called()


async def test_display_name_sync_does_not_create_or_rewrite_unchanged_devices(coordinator, monkeypatch):
    from unittest.mock import Mock
    from types import SimpleNamespace
    registry = Mock()
    monkeypatch.setattr(dr, "async_get", lambda hass: registry)
    registry.async_get_device_by_identifier.return_value = None
    coordinator.async_sync_device_names()
    registry.async_update_device.assert_not_called()
    registry.async_get_or_create.assert_not_called()
    registry.async_get_device_by_identifier.return_value = SimpleNamespace(name=f"Wi-Fi {MAC}")
    coordinator.async_sync_device_names()
    registry.async_update_device.assert_not_called()


async def test_hostname_push_during_platform_setup_defers_display_change(coordinator, monkeypatch):
    from unittest.mock import Mock
    from types import SimpleNamespace
    registry = Mock()
    registry.async_get_device_by_identifier.return_value = SimpleNamespace(id="owned", name=f"Wi-Fi {MAC}")
    monkeypatch.setattr(dr, "async_get", lambda hass: registry)
    coordinator.device_names_ready = False
    await coordinator.set_hostname(MAC, "Pushed during setup")
    registry.async_update_device.assert_not_called()
    assert coordinator.names[MAC] == "Pushed during setup"
    coordinator.device_names_ready = True
    coordinator.async_sync_device_names()
    registry.async_update_device.assert_called_once_with("owned", name="Pushed during setup")


async def test_all_metric_entities_construct_and_rssi_keeps_raw_value(coordinator):
    coordinator.data = {
        "clients": {MAC: {"mac": MAC, "connected": True, "fresh": True, "rssi": 48}},
        "aps": {},
        "updated_timestamp": time.time(),
    }
    entities = {key: ClientMetric(coordinator, MAC, key) for key in FIELDS}
    rssi = entities["rssi"]
    assert rssi.native_value == 48
    assert rssi.device_class is None
    assert rssi.native_unit_of_measurement is None


async def test_stale_data_unavailable_even_without_poll(coordinator):
    coordinator.data = {
        "clients": {MAC: {"mac": MAC, "connected": True, "fresh": True}},
        "aps": {},
        "updated_timestamp": time.time() - 3600,
        "updated_at": "old",
    }
    entity = Connected(coordinator, MAC, "connected")
    assert entity.is_on is None
    assert entity.available is False


@pytest.mark.parametrize("status,age,expected", [("ok", 10, False), ("unavailable", 10, None), ("ok", 3600, None)])
async def test_select_absent_client_uses_original_cache_age(coordinator, status, age, expected):
    observed = time.time() - age
    coordinator.data = {
        "clients": {},
        "aps": {"a": {"id": "a", "status": status}},
        "ap_clients": {"a": []},
        "updated_timestamp": observed,
        "updated_at": "unchanged",
    }
    coordinator.select_clients([MAC])
    assert coordinator.export()["clients"][MAC]["connected"] is expected
    assert coordinator.data["updated_timestamp"] == observed
    assert coordinator.data["updated_at"] == "unchanged"


async def test_select_client_does_not_override_active_lease(coordinator):
    ticket = await coordinator.lease.acquire("consumer")
    before = coordinator.lease.status()
    coordinator.select_clients([MAC])
    assert coordinator.lease.status() == before
    assert coordinator.export()["clients"][MAC]["connected"] is None
    await coordinator.lease.release("consumer", ticket["token"])


async def test_disabled_polling_does_not_login(coordinator, monkeypatch):
    coordinator.settings["polling_enabled"] = False

    def forbidden(*args):
        pytest.fail("Polling disabled")

    monkeypatch.setattr("custom_components.netgear_wax.coordinator.WaxClient", forbidden)
    await coordinator._async_update_data()
    assert not coordinator.lease.status()["busy"]
