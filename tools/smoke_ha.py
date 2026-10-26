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
        finally:
            await hass.async_stop(force=True)


if __name__ == "__main__":
    asyncio.run(main())
