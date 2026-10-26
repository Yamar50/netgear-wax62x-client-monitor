"""Exercise actual aiohttp transport against a synthetic loopback AP."""

import base64
from aiohttp import web
import pytest
from yarl import URL
from custom_components.netgear_wax.api import WaxClient, WaxError


@pytest.mark.parametrize("mode", ["normal", "bad_count", "wrong_detail_mac"])
async def test_full_wire_protocol_and_cleanup(mode):
    calls = []
    authenticated = False

    async def handle(request):
        nonlocal authenticated
        if request.path == "/AP_login":
            raise web.HTTPFound("/")
        if request.method == "GET":
            r = web.Response(text="Test AP")
            r.set_cookie("lhttpdsid", "synthetic-session")
            return r
        payload = await request.json()
        if "adminPasswd" in str(payload):
            assert request.cookies.get("lhttpdsid") == "synthetic-session"
            assert request.headers.get("time")
            authenticated = True
            calls.append("login")
            return web.json_response({"status": 0, "system": {"security_token": "synthetic-token"}})
        assert authenticated
        assert request.headers.get("security") == "synthetic-token"
        assert request.cookies.get("ssid") == base64.b64encode(b"synthetic-token").decode()
        if request.path == "/logout":
            calls.append("logout")
            authenticated = False
            return web.json_response({"status": 0})
        monitor = payload.get("system", {}).get("monitor", {})
        if "productId" in monitor:
            return web.json_response(
                {
                    "status": 0,
                    "system": {
                        "basicSettings": {"apName": "Test AP"},
                        "monitor": {
                            "productId": "WAX625",
                            "sysVersion": "test",
                            "DeviceInfo": {"UpTime": "01:00:00"},
                            "radioApStatus": {
                                "wlan0": {"numberOfStations": "bad" if mode == "bad_count" else 1},
                                "wlan1": {"numberOfStations": 0},
                            },
                        },
                    },
                }
            )
        if "optCliList" in monitor:
            node = monitor["optCliList"]["wlan0"]
            if "3146087" in node:
                data = {"3146087": [{"mac": "02:00:00:00:00:01", "status": "Authenticated", "ssid": "Example"}]}
            else:
                key = next(iter(node["0x1FFFFFFFFFF"]))
                data = {
                    "0x1FFFFFFFFFF": {
                        key: [
                            {
                                "mac": "02:00:00:00:00:02" if mode == "wrong_detail_mac" else "02:00:00:00:00:01",
                                "status": "Authenticated",
                                "txB": 0,
                                "rxB": 1500,
                                "txR": "600 Mbps",
                                "rssi": "-48",
                            }
                        ]
                    }
                }
            return web.json_response({"status": 0, "system": {"monitor": {"optCliList": {"wlan0": data}}}})
        return web.json_response({"status": 0})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = WaxClient(
            {"id": "ap", "name": "Test", "host": "ap.example.test", "username": "test", "password": "test-only"}
        )
        client.base = URL(f"http://127.0.0.1:{port}")
        if mode == "bad_count":
            with pytest.raises(WaxError, match="invalid_station_count"):
                await client.collect()
        else:
            result = await client.collect()
            row = result["clients"][0]
            assert row["mac"] == "02:00:00:00:00:01"
            assert row["connected"]
            assert row["detail_available"] is (mode == "normal")
            if mode == "normal":
                assert row["tx_bytes"] == 0 and row["tx_rate"] == 600000000
        assert calls == ["login", "logout"]
        assert client.logout_ok
        assert not authenticated
    finally:
        await runner.cleanup()
