import asyncio
import pytest
from custom_components.netgear_wax.api import WaxClient, WaxError, LoginLimitError, LogoutError

CONFIG = {
    "id": "ap",
    "name": "Test AP",
    "host": "ap.example.test",
    "username": "example",
    "password": "test-password",
    "verify_ssl": True,
}
MAC = "02:00:00:00:00:01"


class Simulated(WaxClient):
    def __init__(self, failure=None):
        super().__init__(CONFIG)
        self.calls = []
        self.failure = failure

    async def request(self, path, payload=None, referer="/dashboard", login=False):
        self.calls.append((path, login))
        if login:
            if self.failure == "limit":
                raise LoginLimitError("login_limit")
            return {"status": 0, "system": {"security_token": "synthetic-token"}}
        if path == "/logout":
            if self.failure == "logout":
                raise WaxError("connection_failed")
            return {"status": 0}
        return {}

    async def read_clients(self):
        if self.failure == "read":
            raise WaxError("invalid_station_count")
        if self.failure == "cancel":
            raise asyncio.CancelledError
        return {"status": "ok"}


async def test_success_logs_out_own_session():
    client = Simulated()
    assert (await client.collect())["status"] == "ok"
    assert client.calls == [("/AP_login", False), ("/socketCommunication", True), ("/logout", False)]
    assert client.logout_ok


@pytest.mark.parametrize("failure,exception", [("read", WaxError), ("cancel", asyncio.CancelledError)])
async def test_failure_and_cancellation_still_logout(failure, exception):
    client = Simulated(failure)
    with pytest.raises(exception):
        await client.collect()
    assert client.logout_ok
    assert client.calls[-1] == ("/logout", False)


async def test_limit_no_retry_and_no_foreign_logout():
    client = Simulated("limit")
    with pytest.raises(LoginLimitError):
        await client.collect()
    assert client.calls == [("/AP_login", False), ("/socketCommunication", True)]


async def test_logout_failure_is_not_success():
    client = Simulated("logout")
    with pytest.raises(LogoutError):
        await client.collect()
    assert not client.logout_ok


@pytest.mark.parametrize("value", [None, "", "-", True, -1, "bad"])
async def test_bad_counts_rejected(monkeypatch, value):
    client = WaxClient(CONFIG)

    async def reply(*args):
        return {
            "system": {
                "monitor": {"radioApStatus": {"wlan0": {"numberOfStations": value}, "wlan1": {"numberOfStations": 0}}}
            }
        }

    monkeypatch.setattr(client, "socket", reply)
    with pytest.raises(WaxError, match="invalid_station_count"):
        await client.dashboard()


async def test_short_list_not_treated_as_disconnection(monkeypatch):
    client = WaxClient(CONFIG)

    async def reply(*args):
        return {"system": {"monitor": {"optCliList": {"wlan0": {"3146087": [{"mac": MAC}]}}}}}

    monkeypatch.setattr(client, "socket", reply)
    with pytest.raises(WaxError, match="incomplete_client_list"):
        await client.client_list("wlan0", 2)


async def test_zero_count_requires_no_list_request(monkeypatch):
    client = WaxClient(CONFIG)

    async def unexpected(*args):
        pytest.fail("zero-client AP should not request a list")

    monkeypatch.setattr(client, "socket", unexpected)
    assert await client.client_list("wlan0", 0) == []
