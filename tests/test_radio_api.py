import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from custom_components.netgear_wax.api import WaxError
from custom_components.netgear_wax.radio_api import WaxRadioClient, radio_payload, uptime_seconds

TABLE = {
    band: {
        "radioStatus": "1",
        "operateMode": "10",
        "guardInterval": "1",
        "txPower": "2",
        "channel": "48",
        "channelWidth": "2",
        "beamForming": "1",
    }
    for band in ("wlan0", "wlan1")
}


@pytest.mark.parametrize(
    "text,expected", [("15 minutes", 900), ("1 day 2 hours 3 minutes 4 seconds", 93784), ("1 hr 5 min", 3900)]
)
def test_uptime(text, expected):
    assert uptime_seconds(text) == expected


@pytest.mark.parametrize("text", [900, "", "01:00:00", "1hour garbage", "1hour 65 minutes", "1min 1hour", None])
def test_unknown_uptime_rejected(text):
    with pytest.raises(WaxError):
        uptime_seconds(text)


def test_radio_payload_preserves_both_bands_without_defaults():
    before = deepcopy(TABLE)
    payload = radio_payload(TABLE, "0")["system"]["wlanSettings"]["wlanSettingTable"]
    assert payload["wlan0"] == TABLE["wlan0"]
    assert {k: v for k, v in payload["wlan1"].items() if k != "radioStatus"} == {
        k: v for k, v in TABLE["wlan1"].items() if k != "radioStatus"
    }
    assert TABLE == before
    del before["wlan0"]["channelWidth"]
    with pytest.raises(WaxError):
        radio_payload(before, "1")


def client():
    c = WaxRadioClient({"host": "test.invalid"}, recover=True)
    c.socket = AsyncMock()
    c.readiness = AsyncMock(return_value={"clients_5ghz": 0})
    c.details = AsyncMock(return_value=deepcopy(TABLE))
    return c


async def test_client_joining_at_final_check_prevents_every_write():
    c = client()
    c.readiness.side_effect = [{"clients_5ghz": 0}, {"clients_5ghz": 1}]
    c.apply = AsyncMock()
    assert (await c.read_clients())["status"] == "clients_active"
    c.apply.assert_not_awaited()


async def test_ambiguous_off_response_still_restores_on():
    c = client()
    calls = []

    async def apply(payload):
        value = payload["system"]["wlanSettings"]["wlanSettingTable"]["wlan1"]["radioStatus"]
        calls.append(value)
        if value == "0":
            raise WaxError("connection_failed")

    c.apply = apply
    with pytest.raises(WaxError):
        await c.read_clients()
    assert calls == ["0", "1"]


async def test_cancellation_after_off_still_restores_on():
    c = client()
    calls = []

    async def apply(payload):
        value = payload["system"]["wlanSettings"]["wlanSettingTable"]["wlan1"]["radioStatus"]
        calls.append(value)
        if value == "0":
            raise asyncio.CancelledError

    c.apply = apply
    with pytest.raises(asyncio.CancelledError):
        await c.read_clients()
    assert calls == ["0", "1"]


async def test_off_radio_only_gets_on():
    c = client()
    before = deepcopy(TABLE)
    before["wlan1"]["radioStatus"] = "0"
    c.details.side_effect = [before, deepcopy(TABLE)]
    c.apply = AsyncMock()
    assert (await c.read_clients())["status"] == "recovered"
    assert c.apply.await_count == 1
    assert c.apply.await_args.args[0]["system"]["wlanSettings"]["wlanSettingTable"]["wlan1"]["radioStatus"] == "1"


@pytest.mark.parametrize("extra,valid", [({"wlan0": {"beamForming": "0"}}, True), ({}, False)])
async def test_missing_radio_field_is_read_explicitly_without_default(extra, valid):
    c = WaxRadioClient({"host": "test.invalid"})
    table = deepcopy(TABLE)
    del table["wlan0"]["beamForming"]
    c.socket = AsyncMock(
        side_effect=[
            {"system": {"wlanSettings": {"wlanSettingTable": table}}},
            {"system": {"wlanSettings": {"wlanSettingTable": extra}}},
        ]
    )
    if valid:
        assert (await c.details())["wlan0"]["beamForming"] == "0"
    else:
        with pytest.raises(WaxError, match="incomplete_radio_settings:wlan0:beamForming"):
            await c.details()
    assert c.socket.await_args.args[0] == {
        "system": {"wlanSettings": {"wlanSettingTable": {"wlan0": {"beamForming": ""}}}}
    }
    assert not c.mutation_started
