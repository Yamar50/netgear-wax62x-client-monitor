import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from custom_components.netgear_wax.http import SnapshotView, ReservationView
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
