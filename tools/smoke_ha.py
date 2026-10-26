"""Load this integration in an isolated HA instance, with AP polling disabled."""

import asyncio
import logging
import tempfile
import time
from unittest.mock import patch
from homeassistant.core import HomeAssistant
from homeassistant import bootstrap, loader


async def main():
    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="wax-ha-smoke-") as root:
        hass = HomeAssistant(root)
        hass.config.skip_pip = True
        loader.async_setup(hass)
        try:
            await bootstrap.async_from_config_dict(
                {
                    "homeassistant": {
                        "name": "Test",
                        "latitude": 0,
                        "longitude": 0,
                        "elevation": 0,
                        "unit_system": "metric",
                        "time_zone": "UTC",
                    },
                    "http": {"server_host": "127.0.0.1", "server_port": 18123},
                },
                hass,
            )
            await hass.async_block_till_done()
            print("components", sorted(hass.config.components))
            flow = await hass.config_entries.flow.async_init("netgear_wax", context={"source": "user"})
            result = await hass.config_entries.flow.async_configure(
                flow["flow_id"],
                {
                    "name": "Test AP",
                    "host": "ap.example.test",
                    "username": "test",
                    "password": "test-only",
                    "verify_ssl": True,
                },
            )
            await hass.async_block_till_done()
            entry = result["result"]
            print("state", entry.state)
            print("entities", [(s.entity_id, s.state) for s in hass.states.async_all()])
            assert entry.state.value == "loaded"
            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(options["flow_id"], {"next_step_id": "clients"})
            await hass.config_entries.options.async_configure(
                options["flow_id"], {"selected_clients": [], "manual_macs": "02:00:00:00:00:01"}
            )
            await hass.async_block_till_done()
            assert entry.state.value == "loaded"
            states = hass.states.async_all()
            assert any(state.entity_id.startswith("text.wi_fi_") for state in states)
            assert len(states) == 28, len(states)
            print("selected_client_entities", len(states) - 8)
            # Changing selection must keep the running coordinator/lease and
            # timer, even when polling is enabled. No AP request is needed.
            from custom_components.netgear_wax.coordinator import WaxCoordinator
            from custom_components.netgear_wax.api import WaxClient

            class Client:
                login_attempted = True
                logout_ok = True

                def __init__(self, config):
                    self.config = config

                async def collect(self):
                    return {"id": self.config["id"], "status": "ok", "clients": []}

            with patch("custom_components.netgear_wax.coordinator.WaxClient", Client):
                options = await hass.config_entries.options.async_init(entry.entry_id)
                options = await hass.config_entries.options.async_configure(
                    options["flow_id"], {"next_step_id": "collection"}
                )
                await hass.config_entries.options.async_configure(
                    options["flow_id"], {"polling_enabled": True, "scan_interval": 600}
                )
                await hass.async_block_till_done()
            coordinator = entry.runtime_data
            from homeassistant.helpers import device_registry as dr, entity_registry as er, area_registry as ar
            registry = dr.async_get(hass)
            client1 = registry.async_get_device_by_identifier(("netgear_wax", "02:00:00:00:00:01"), entry.entry_id)
            assert client1.name == "Wi-Fi 02:00:00:00:00:01"
            original_timestamp = coordinator.data["updated_timestamp"]
            original_timer = coordinator._unsub_refresh
            ticket = await coordinator.lease.acquire("smoke-external")
            lease_state = coordinator.lease.status()
            await coordinator.set_hostname("02:00:00:00:00:01", "Saved name")
            await hass.async_block_till_done()
            assert registry.async_get(client1.id).name == "Saved name"
            assert hass.states.get("binary_sensor.wi_fi_02_00_00_00_00_01_connected").attributes["friendly_name"].startswith("Saved name")
            pending_mac = "02:00:00:00:00:02"
            response = await hass.services.async_call(
                "netgear_wax", "set_hostname", {"mac": pending_mac, "hostname": "Pre-saved name"},
                blocking=True, return_response=True,
            )
            assert response == {"mac": pending_mac, "hostname": "Pre-saved name", "stored": True,
                                "entity_registered": False, "entity_id": None}
            assert registry.async_get_device_by_identifier(("netgear_wax", pending_mac), entry.entry_id) is None
            client1_ids = {s.entity_id for s in hass.states.async_all() if "02_00_00_00_00_01" in s.entity_id}
            assert len(client1_ids) == 20
            started = time.monotonic()
            with (
                patch.object(WaxClient, "collect", side_effect=AssertionError("Unexpected AP request")),
                patch.object(WaxCoordinator, "initialize", side_effect=AssertionError("Unexpected reload")),
            ):
                for selected in (
                    ["02:00:00:00:00:01", "02:00:00:00:00:02"],
                    ["02:00:00:00:00:01"],
                    ["02:00:00:00:00:01", "02:00:00:00:00:02"],
                ):
                    options = await hass.config_entries.options.async_init(entry.entry_id)
                    options = await hass.config_entries.options.async_configure(
                        options["flow_id"], {"next_step_id": "clients"}
                    )
                    known = coordinator.export()["clients"]
                    await hass.config_entries.options.async_configure(
                        options["flow_id"],
                        {
                            "selected_clients": [mac for mac in selected if mac in known],
                            "manual_macs": "\n".join(mac for mac in selected if mac not in known),
                        },
                    )
                    await hass.async_block_till_done()
                    assert entry.state.value == "loaded"
                    assert entry.runtime_data is coordinator
                    assert coordinator._unsub_refresh is original_timer
                    assert coordinator.lease.status() == lease_state
                    assert coordinator.data["updated_timestamp"] == original_timestamp
                    assert coordinator.names["02:00:00:00:00:01"] == "Saved name"
                    if pending_mac in selected:
                        client2 = registry.async_get_device_by_identifier(("netgear_wax", pending_mac), entry.entry_id)
                        assert client2.name == "Pre-saved name"
                        assert len([s for s in hass.states.async_all() if "02_00_00_00_00_02" in s.entity_id]) == 20
                        response = await hass.services.async_call(
                            "netgear_wax", "set_hostname", {"mac": pending_mac, "hostname": "Pre-saved name"},
                            blocking=True, return_response=True,
                        )
                        assert response["entity_registered"] is True
                        assert hass.states.get(response["entity_id"]).state == "Pre-saved name"
                    assert all(hass.states.get(entity_id) is not None for entity_id in client1_ids)
                    for mac in selected:
                        assert coordinator.export()["clients"][mac]["connected"] is False
            await coordinator.lease.release("smoke-external", ticket["token"])
            print("selection_without_reload_or_AP_login", "PASS", round(time.monotonic() - started, 3))
            print("hostname_response_and_later_registration", "PASS")
            # A manual device name wins, while default name keeps following
            # HostName. Custom entity IDs/names/areas survive update + reload.
            area = ar.async_get(hass).async_create("Test area")
            registry.async_update_device(client1.id, name_by_user="Manual device name", area_id=area.id)
            entities = er.async_get(hass)
            entities.async_update_entity("binary_sensor.wi_fi_02_00_00_00_00_01_connected",
                                         new_entity_id="binary_sensor.family_presence", name="Custom entity", area_id=area.id)
            await hass.async_block_till_done()
            await coordinator.set_hostname("02:00:00:00:00:01", "Updated HostName")
            await hass.async_block_till_done()
            device = registry.async_get(client1.id)
            assert (device.name, device.name_by_user, device.area_id) == ("Updated HostName", "Manual device name", area.id)
            # Enabling/registering another entity must not revert the device name.
            from custom_components.netgear_wax.sensor import ClientMetric
            info = ClientMetric(coordinator, "02:00:00:00:00:01", "ip").device_info
            assert registry.async_get_or_create(config_entry_id=entry.entry_id, **info).name == "Updated HostName"
            original_entities = {
                e.unique_id: (e.entity_id, e.device_id, e.name, e.area_id, e.disabled_by)
                for e in er.async_entries_for_config_entry(entities, entry.entry_id)
            }
            ticket = await coordinator.lease.acquire("name-reload-test")
            with patch.object(WaxClient, "collect", side_effect=AssertionError("Unexpected AP request")):
                assert await hass.config_entries.async_reload(entry.entry_id)
                await hass.async_block_till_done()
            coordinator = entry.runtime_data
            await coordinator.lease.release("name-reload-test", ticket["token"])
            assert original_entities == {
                e.unique_id: (e.entity_id, e.device_id, e.name, e.area_id, e.disabled_by)
                for e in er.async_entries_for_config_entry(entities, entry.entry_id)
            }
            assert registry.async_get(client1.id).name_by_user == "Manual device name"
            assert registry.async_get(client1.id).name == "Updated HostName"
            registry.async_update_device(client1.id, name_by_user=None)
            await hass.async_block_till_done()
            assert hass.states.get("text.wi_fi_02_00_00_00_00_01_hostname").attributes["friendly_name"].startswith("Updated HostName")
            await coordinator.set_hostname("02:00:00:00:00:01", "")
            assert registry.async_get(client1.id).name == "Wi-Fi 02:00:00:00:00:01"
            print("device_names_manual_override_identity_areas_reload", "PASS")
            from custom_components.netgear_wax.http import SnapshotView, ReservationView
            from types import SimpleNamespace

            class Request(dict):
                app = {"hass": hass}
                body = None

                async def json(self):
                    return self.body

            request = Request(hass_user=SimpleNamespace(is_admin=True, id="smoke-user"))
            assert (await SnapshotView().get(request)).status == 200
            request.body = {"operation": "acquire", "duration": 120}
            response = await ReservationView().post(request)
            assert response.status == 200
            import json

            ticket = json.loads(response.body)
            request.body = {"operation": "release", "token": ticket["token"]}
            assert (await ReservationView().post(request)).status == 200
            print("runtime_cache_reservation_views", "PASS")
        finally:
            await hass.async_stop(force=True)


if __name__ == "__main__":
    asyncio.run(main())
