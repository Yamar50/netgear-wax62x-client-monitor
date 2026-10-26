import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from custom_components.netgear_wax.http import SnapshotView, ReservationView, RadioMonitorView
from custom_components.netgear_wax.lease import LeaseManager


class Request(dict):
    def __init__(self, coordinator, body=None, admin=True, user="example-user"):
        super().__init__(hass_user=SimpleNamespace(is_admin=admin, id=user))
        self.app = {"hass": SimpleNamespace(data={"netgear_wax": coordinator})}
        self.body = body

    async def json(self):
        return self.body


@pytest.fixture
def coordinator():
    c = SimpleNamespace(lease=LeaseManager(AsyncMock()), async_update_listeners=lambda: None)
    c.export = lambda: {"schema_version": 1, "access": c.lease.status(), "clients": {}}
    return c


async def test_http_admin_required(coordinator):
    assert (await SnapshotView().get(Request(coordinator, admin=False))).status == 403
    assert (await ReservationView().post(Request(coordinator, {"operation": "acquire"}, admin=False))).status == 403


async def test_http_lifecycle_and_wrong_token(coordinator):
    view = ReservationView()
    response = await view.post(Request(coordinator, {"operation": "acquire", "duration": 120}))
    assert response.status == 200
    ticket = json.loads(response.body)
    assert (await view.post(Request(coordinator, {"operation": "acquire"}))).status == 409
    assert (await view.post(Request(coordinator, {"operation": "release", "token": "bad"}))).status == 409
    assert (await view.post(Request(coordinator, {"operation": "renew", "token": ticket["token"]}))).status == 200
    assert (
        await view.post(Request(coordinator, {"operation": "release", "token": ticket["token"]}, user="different"))
    ).status == 409
    assert (await view.post(Request(coordinator, {"operation": "release", "token": ticket["token"]}))).status == 200


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"operation": "unknown"},
        {"operation": "renew"},
        {"operation": "acquire", "duration": True},
        {"operation": "acquire", "duration": "120"},
    ],
)
async def test_bad_requests_rejected(coordinator, body):
    assert (await ReservationView().post(Request(coordinator, body))).status == 400


async def test_unloaded_integration_is_unavailable():
    assert (await SnapshotView().get(Request(None))).status == 503
    assert (await ReservationView().post(Request(None, {"operation": "acquire"}))).status == 503


async def test_radio_api_requires_admin_and_loaded_monitor(coordinator):
    view = RadioMonitorView()
    for method in (view.get, view.post):
        assert (await method(Request(coordinator, admin=False))).status == 403
        assert (await method(Request(None))).status == 503
        assert (await method(Request(coordinator))).status == 503


async def test_radio_mode_ack_and_unknown_operation(coordinator):
    radio = SimpleNamespace(
        set_mode=AsyncMock(return_value={"mode": "standby", "running": False}),
        preflight=AsyncMock(return_value={"read_only": True}),
    )
    coordinator.radio = radio
    view = RadioMonitorView()
    response = await view.post(Request(coordinator, {"operation": "set_mode", "mode": "standby"}))
    assert json.loads(response.body) == {"mode": "standby", "running": False}
    radio.set_mode.assert_awaited_once_with("standby", 0)
    assert (await view.post(Request(coordinator, {"operation": "preflight"}))).status == 400
    radio.preflight.assert_not_awaited()
    assert (await view.post(Request(coordinator, {"operation": "invalid"}))).status == 400
