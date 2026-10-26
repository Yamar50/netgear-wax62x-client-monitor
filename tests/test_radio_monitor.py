import asyncio
from copy import deepcopy
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
import pytest

from custom_components.netgear_wax.api import WaxError, LogoutError
from custom_components.netgear_wax.lease import LeaseManager
from custom_components.netgear_wax.radio_monitor import RadioMonitor, validate_config, next_missing
from custom_components.netgear_wax.wifi_probe import fingerprint

APS = [{"id": str(i), "name": f"Test{i}"} for i in range(4)]
CONFIG = {"interface": "wlan0", "ssid": "Synthetic", "targets": {str(i): f"02:00:00:00:00:0{i}" for i in range(4)}}


def scan(missing=()):
    return {
        "completed": True,
        "elapsed_seconds": 8,
        "rows": [
            {
                "ssid_sha256": fingerprint(b"Synthetic"),
                "bssid_sha256": fingerprint(bytes.fromhex(mac.replace(":", ""))),
                "frequency_mhz": 5180,
                "signal_dbm": -50,
                "seen_ms_ago": 10,
            }
            for key, mac in CONFIG["targets"].items()
            if key not in missing
        ],
    }


@pytest.fixture
async def monitor(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    hass.data["netgear_wax_scan_lock"] = asyncio.Lock()
    coordinator = SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(entry_id="synthetic"),
        settings={"aps": APS},
        cooldowns={},
        lease=LeaseManager(AsyncMock()),
        save=AsyncMock(),
        async_update_listeners=Mock(),
    )
    radio = RadioMonitor(coordinator)
    radio.state.update(mode="active", config=deepcopy(CONFIG))
    radio.call_ap = AsyncMock(return_value={"status": "ready", "clients_5ghz": 0, "uptime_seconds": 1000})
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan())
    yield radio
    await radio.close()
    await hass.async_stop(force=True)


def test_targets_must_cover_exact_fleet_without_duplicates():
    assert validate_config(CONFIG, APS) == CONFIG
    for bad in [dict(CONFIG, interface="wlan0;down"), dict(CONFIG, ssid=""), dict(CONFIG, targets={})]:
        with pytest.raises(ValueError):
            validate_config(bad, APS)
    bad = deepcopy(CONFIG)
    bad["targets"]["1"] = bad["targets"]["0"]
    with pytest.raises(ValueError):
        validate_config(bad, APS)


def test_streaks_need_partial_visibility_and_full_ready_fleet():
    assert next_missing({"a": 2, "b": 2}, {"a": None, "b": {"seen": True}}, True) == {"a": 3, "b": 0}
    assert next_missing({"a": 2}, {"a": None}, True) == {"a": 0}
    assert next_missing({"a": 2}, {"a": None}, False) == {"a": 0}


async def test_all_visible_does_not_log_into_any_ap(monitor):
    await monitor.run()
    assert monitor.state["status"] == "all_visible"
    monitor.call_ap.assert_not_awaited()
    assert not monitor.coordinator.lease.status()["busy"]


async def test_three_completed_scans_only_one_recovery_and_durable_hold(monitor, monkeypatch):
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan(("2", "3")))
    recoveries = []

    async def call(ap, recover=False):
        if recover:
            assert monitor.state["cooldowns"][ap["id"]] > time.time() + 3500
            recoveries.append(ap["id"])
            return {"status": "recovered"}
        return {"status": "ready", "clients_5ghz": 0}

    monitor.call_ap = call
    for _ in range(3):
        monitor.last_attempt = 0
        await monitor.run()
    assert recoveries == ["2"]
    assert monitor.state["missing"]["3"] == 3
    assert monitor.state["cooldowns"]["2"] > time.time() + 3500


async def test_manual_recheck_cannot_accelerate_streak(monitor, monkeypatch):
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan(("3",)))
    await monitor.run()
    await monitor.run()
    await monitor.run()
    assert monitor.state["missing"]["3"] == 1


async def test_empty_scan_never_toggles_ha_wifi_or_ap(monitor, monkeypatch):
    monkeypatch.setattr(
        "custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan(("0", "1", "2", "3"))
    )
    for _ in range(4):
        monitor.last_attempt = 0
        await monitor.run()
    assert monitor.state["status"] == "all_missing_hold"
    assert not any(monitor.state["missing"].values())
    assert all(not call.kwargs.get("recover", False) for call in monitor.call_ap.await_args_list)


async def test_one_unready_ap_clears_streaks_and_does_not_recover(monitor, monkeypatch):
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan(("3",)))
    monitor.state["missing"] = {"3": 2}
    monitor.call_ap.side_effect = WaxError("boot_grace")
    await monitor.run()
    assert monitor.state["status"] == "check_failed"
    assert monitor.state["missing"] == {}
    assert all(x["visible"] is None for x in monitor.state["observations"].values())


async def test_observe_only_does_not_login_when_missing(monitor, monkeypatch):
    monitor.state["mode"] = "observe"
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan(("3",)))
    await monitor.run()
    monitor.call_ap.assert_not_awaited()
    assert monitor.state["status"] == "observing_missing"


async def test_active_clients_stop_recovery(monitor, monkeypatch):
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan(("3",)))
    monitor.state.update(missing={"3": 2}, checked_timestamp=time.time())
    monitor.call_ap.return_value = {"status": "ready", "clients_5ghz": 1}
    await monitor.run()
    assert monitor.state["status"] == "clients_active_hold"
    assert monitor.state["missing"]["3"] == 0
    assert all(not call.kwargs.get("recover", False) for call in monitor.call_ap.await_args_list)


async def test_external_reservation_blocks_scan_and_ap_calls(monitor, monkeypatch):
    monkeypatch.setattr(
        "custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: pytest.fail("scan during lease")
    )
    await monitor.coordinator.lease.acquire("external")
    await monitor.run()
    assert monitor.last_attempt == 0
    assert monitor.state["status"] == "waiting_for_access"
    monitor.call_ap.assert_not_awaited()


async def test_standby_ack_waits_for_inflight_operation(monitor):
    await monitor.lock.acquire()
    transition = asyncio.create_task(monitor.set_mode("standby"))
    await asyncio.sleep(0)
    assert not transition.done()
    monitor.lock.release()
    ack = await transition
    assert ack["mode"] == "standby" and not ack["running"]


async def test_handoff_keeps_recovery_cooldown(monitor):
    hold = time.time() + 1800
    await monitor.set_mode("active", hold)
    assert all(x >= hold for x in monitor.state["cooldowns"].values())
    await monitor.set_mode("standby")
    assert all(x >= hold for x in monitor.state["cooldowns"].values())


async def test_logout_failure_holds_fleet_lease(monitor, monkeypatch):
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan(("3",)))

    class Client:
        login_attempted = True
        logout_ok = False
        mutation_started = False

        def __init__(self, *args, **kwargs):
            pass

        async def collect(self):
            raise LogoutError("logout_failed")

    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.WaxRadioClient", Client)
    del monitor.call_ap
    await monitor.run()
    assert monitor.coordinator.lease.status()["busy"]
    assert monitor.coordinator.cooldowns["0"] > time.time() + 3500
