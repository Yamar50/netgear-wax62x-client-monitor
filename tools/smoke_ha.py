"""Load this integration in an isolated HA instance, with AP polling disabled."""

import asyncio
import logging
import tempfile
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
