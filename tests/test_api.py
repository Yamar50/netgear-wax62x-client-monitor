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


class ClientListsFixture(Simulated):
    read_clients = WaxClient.read_clients

    def __init__(self, counts, lists):
        super().__init__()
        self.counts = counts
        self.lists = lists
        self.attempt = -1
        self.read_order = []

    async def dashboard(self):
        self.attempt += 1
        counts = self.counts[min(self.attempt, len(self.counts) - 1)]
        return {"monitor": {"productId": "WAX625"}}, counts

    async def client_list(self, wlan, expected):
        return await WaxClient.client_list(self, wlan, expected)

    async def socket(self, payload, referer="/dashboard"):
        node = payload.get("system", {}).get("monitor", {}).get("optCliList")
        if node:
            wlan = next(iter(node))
            if "3146087" in node[wlan]:
                self.read_order.append(("list", wlan))
                rows = self.lists[min(self.attempt, len(self.lists) - 1)][wlan]
                if isinstance(rows, Exception):
                    raise rows
                return {"system": {"monitor": {"optCliList": {wlan: {"3146087": rows}}}}}
            self.read_order.append(("detail", wlan))
        return {}


def basic(mac=MAC):
    return {"mac": mac, "status": "Authenticated"}


async def test_lists_retry_same_session_refresh_counts_and_precede_details(monkeypatch):
    async def no_delay(seconds):
        assert seconds == 1
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    client = ClientListsFixture(
        [{"wlan0": 2, "wlan1": 1}, {"wlan0": 1, "wlan1": 1}],
        [{"wlan0": [basic()], "wlan1": [basic("02:00:00:00:00:02")]}],
    )
    result = await client.collect()
    assert client.attempt == 1
    assert client.logout_ok
    assert sum(login for _, login in client.calls) == 1
    assert len(result["clients"]) == 2
    assert client.read_order == [("list", "wlan0"), ("list", "wlan0"), ("list", "wlan1"),
                                 ("detail", "wlan0"), ("detail", "wlan1")]


async def test_export_counts_match_captured_lists_not_lagging_dashboard():
    client = ClientListsFixture(
        [{"wlan0": 1, "wlan1": 0}],
        [{"wlan0": [basic(), basic("02:00:00:00:00:02")], "wlan1": []}],
    )
    result = await client.collect()
    assert result["clients_24ghz"] == len(result["clients"]) == 2
    assert result["reported_clients_24ghz"] == 1
    assert result["clients_5ghz"] == 0


async def test_incomplete_list_stops_after_three_reads_and_logs_out(monkeypatch):
    async def no_delay(seconds):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    client = ClientListsFixture([{"wlan0": 2, "wlan1": 0}], [{"wlan0": [], "wlan1": []}])
    with pytest.raises(WaxError, match="incomplete_client_list"):
        await client.collect()
    assert client.attempt == 2
    assert client.logout_ok
    assert sum(login for _, login in client.calls) == 1
    assert all(kind == "list" for kind, _ in client.read_order)


async def test_retry_can_confirm_genuine_empty_ap(monkeypatch):
    async def no_delay(seconds):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    client = ClientListsFixture([{"wlan0": 1, "wlan1": 0}, {"wlan0": 0, "wlan1": 0}],
                               [{"wlan0": [], "wlan1": []}])
    result = await client.collect()
    assert result["clients"] == []
    assert result["clients_24ghz"] == 0
    assert client.attempt == 1


async def test_list_login_limit_is_not_retried():
    client = ClientListsFixture([{"wlan0": 1, "wlan1": 0}],
                               [{"wlan0": LoginLimitError("login_limit"), "wlan1": []}])
    with pytest.raises(LoginLimitError):
        await client.collect()
    assert client.attempt == 0
    assert client.logout_ok
