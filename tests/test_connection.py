# ruff: noqa: F811
# Reuse the HA coordinator fixture from the existing coordinator test module.
import copy
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.netgear_wax.connection import apply_report, validate_report
from custom_components.netgear_wax.http import ConnectionView
from test_http import Request
from test_coordinator import coordinator, MAC, AP  # noqa: F401


def report(connected=True, at=None, ap="Test"):
    return {"mac": MAC, "connected": connected, "ap_name": ap,
            "observed_at": datetime.fromtimestamp(at or time.time(), timezone.utc).isoformat()}


@pytest.mark.parametrize("mutation", [
    {"connected": "true"}, {"connected": 1}, {"mac": "bad"}, {"ap_name": "Other"},
    {"observed_at": "2026-01-01"}, {"observed_at": "invalid"}, {"observed_at": None},
])
def test_reject_invalid_reports(mutation):
    with pytest.raises((ValueError, TypeError)):
        validate_report({**report(), **mutation}, [AP], time.time())


@pytest.mark.parametrize("offset", [-86401, 10])
def test_reject_expired_or_future(offset):
    with pytest.raises(ValueError):
        validate_report(report(at=time.time()+offset), [AP], time.time())


def test_report_wins_over_inflight_poll_but_new_collection_can_disconnect():
    now = time.time()
    event = validate_report(report(at=now-10), [AP], now)
    row = {"mac": MAC, "connected": False, "fresh": False, "tx_bytes": 12}
    aps = {"a": {"status": "ok", "collection_started_timestamp": now-20}}
    result = apply_report(row, event, aps, now, 600)
    assert result["connected"] is True and result["connection_source"] == "push"
    assert result["tx_bytes"] == 12 and result["fresh"] is False
    aps["a"]["collection_started_timestamp"] = now-5
    assert apply_report(row, event, aps, now, 600)["connected"] is False
    assert apply_report(row, event, {}, now+601, 600)["connected"] is False


def test_disconnect_old_ap_does_not_override_roaming_or_newer_poll():
    now = time.time()
    event = validate_report(report(False, now-10), [AP], now)
    row = {"connected": True, "ap_id": "b", "observed_timestamp": now-30}
    assert apply_report(row, event, {}, now, 600) == row
    row.update(ap_id="a", observed_timestamp=now-5)
    assert apply_report(row, event, {}, now, 600) == row
    row["observed_timestamp"] = now-20
    assert apply_report(row, event, {}, now, 600)["connected"] is False


async def test_reports_persist_without_mutating_poll_or_scheduling(coordinator):
    c = coordinator
    now = time.time()
    c.data = {"clients": {MAC: {"mac": MAC, "connected": False}}, "aps": {},
              "ap_clients": {}, "updated_timestamp": now-30, "updated_at": "unchanged"}
    before = copy.deepcopy(c.data)
    timer = c._unsub_refresh
    first = report(at=now-5)
    result = await c.report_connection(first)
    assert result["accepted"] and result["handled"]
    assert c.data == before and c._unsub_refresh is timer
    assert c.export()["clients"][MAC]["connected"] is True
    assert (await c.report_connection(first))["reason"] == "duplicate_or_older"
    assert (await c.report_connection(report(False, now-10)))["accepted"] is False
    assert (await c.report_connection(report(False, now-1)))["accepted"] is True
    assert c.export()["clients"][MAC]["connected"] is False
    from custom_components.netgear_wax.coordinator import WaxCoordinator
    restored = WaxCoordinator(c.hass, c.entry)
    restored.lease = c.lease
    restored.connection_reports = await restored.connection_store.async_load()
    assert restored.export()["clients"][MAC]["connected"] is False
    await restored.async_shutdown()


async def test_report_received_during_collection_survives_older_absence(coordinator, monkeypatch):
    c = coordinator
    class Client:
        login_attempted = True
        logout_ok = True
        def __init__(self, config):
            pass
        async def collect(self):
            await c.report_connection(report())
            return {"id": "a", "name": "Test", "status": "ok", "clients": []}
    monkeypatch.setattr("custom_components.netgear_wax.coordinator.WaxClient", Client)
    await c._async_update_data()
    assert c.export()["clients"][MAC]["connected"] is True
    assert c.export()["ap_clients"]["a"] == []


def test_superseded_push_stays_superseded_when_poll_later_becomes_unknown():
    now = time.time()
    event = validate_report(report(at=now-10), [AP], now)
    row = {"connected": None, "connection_fence_timestamp": now-5}
    assert apply_report(row, event, {}, now, 600)["connected"] is None


async def test_report_store_failure_no_ack_no_inmemory_change(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator.connection_store, "async_save", AsyncMock())
    with pytest.raises(HomeAssistantError, match="confirmation failed"):
        await coordinator.report_connection(report())
    assert coordinator.connection_reports == {}


async def test_unknown_mac_does_not_create_entity_and_report_expires(coordinator):
    c = coordinator
    m = "02:00:00:00:00:02"
    await c.report_connection({**report(), "mac": m})
    assert c.export()["clients"][m]["connected"] is True
    assert m not in c.settings["selected_clients"]
    c.connection_reports[m]["observed_timestamp"] = time.time()-100000
    assert m not in c.export()["clients"]


async def test_http_report_auth_validation_unloaded_and_ack():
    c = SimpleNamespace(report_connection=AsyncMock(return_value={"handled": True}))
    view = ConnectionView()
    assert (await view.post(Request(c, report(), admin=False))).status == 403
    assert (await view.post(Request(None, report()))).status == 503
    response = await view.post(Request(c, report()))
    assert json.loads(response.body)["handled"]
    c.report_connection.side_effect = ValueError()
    assert (await view.post(Request(c, report()))).status == 400
    c.report_connection.side_effect = HomeAssistantError()
    assert (await view.post(Request(c, report()))).status == 503
