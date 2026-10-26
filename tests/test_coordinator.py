from datetime import datetime
from types import MappingProxyType
import time

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
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
