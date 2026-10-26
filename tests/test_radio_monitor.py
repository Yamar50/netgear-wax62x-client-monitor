import asyncio
from copy import deepcopy
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant, CoreState, HassJob
import pytest

from custom_components.netgear_wax.lease import LeaseManager
from custom_components.netgear_wax.radio_monitor import RadioMonitor, validate_config
from custom_components.netgear_wax.wifi_probe import fingerprint

APS = [{"id": str(i), "name": f"Test{i}"} for i in range(4)]
CONFIG = {"interface": "wlan0", "ssid": "Synthetic", "targets": {str(i): f"02:00:00:00:00:0{i}" for i in range(4)},
          "recovery_enabled": False, "missing_minutes": 30, "dfs_enabled": False, "dfs_policies": {}}


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
    co = SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(entry_id="synthetic"),
        settings={"aps": APS},
        lease=LeaseManager(AsyncMock()),
        async_update_listeners=Mock(),
    )
    radio = RadioMonitor(co)
    radio.state.update(mode="observe", config=deepcopy(CONFIG))
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


async def test_all_visible_with_channel_and_no_rssi(monitor):
    await monitor.run()
    assert monitor.state["status"] == "all_visible"
    assert all(row == {"visible": True, "channel": 36} for row in monitor.state["observations"].values())
    assert not monitor.coordinator.lease.status()["busy"]


async def test_completed_partial_scan_only_marks_absent_target_off(monitor, monkeypatch):
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: scan(("3",)))
    await monitor.run()
    assert monitor.state["status"] == "observing_missing"
    assert monitor.state["observations"]["3"] == {"visible": False, "channel": None}
    assert monitor.state["observations"]["2"]["visible"] is True


async def test_failed_scan_is_unknown_not_disconnected(monitor, monkeypatch):
    await monitor.run()

    def fail(interface):
        raise TimeoutError("scan_completion_timeout")

    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", fail)
    monitor.last_attempt = 0
    await monitor.run()
    assert monitor.state["status"] == "check_failed"
    assert all(row["visible"] is None for row in monitor.state["observations"].values())


async def test_manual_recheck_does_not_accelerate_scan(monitor, monkeypatch):
    scanner = Mock(return_value=scan())
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", scanner)
    await monitor.run()
    await monitor.run()
    scanner.assert_called_once()


async def test_external_reservation_blocks_scan(monitor, monkeypatch):
    monkeypatch.setattr(
        "custom_components.netgear_wax.radio_monitor.scan_wifi", lambda interface: pytest.fail("reserved")
    )
    await monitor.coordinator.lease.acquire("external")
    await monitor.run()
    assert monitor.last_attempt == 0 and monitor.state["status"] == "waiting_for_access"


async def test_standby_ack_waits_for_inflight_scan(monitor):
    await monitor.lock.acquire()
    transition = asyncio.create_task(monitor.set_mode("standby"))
    await asyncio.sleep(0)
    assert not transition.done()
    monitor.lock.release()
    ack = await transition
    assert ack["mode"] == "standby" and not ack["running"]


async def test_legacy_cooldown_can_be_carried_back_without_recovery(monitor):
    hold = time.time() + 1800
    await monitor.set_mode("observe", hold)
    assert all(x >= hold for x in monitor.state["cooldowns"].values())
    await monitor.set_mode("standby")
    assert all(x >= hold for x in monitor.state["cooldowns"].values())
    with pytest.raises(ValueError):
        await monitor.set_mode("active")


async def test_old_results_expire_to_unknown(monitor):
    await monitor.run()
    monitor.state["checked_timestamp"] = time.time() - 1201
    assert all(row["visible"] is None for row in monitor.export()["observations"].values())


async def test_interval_callback_dispatch_runs_on_ha_loop(monitor):
    monitor.hass.set_state(CoreState.running)
    job = HassJob(monitor.tick)
    monitor.hass.async_run_hass_job(job, None)
    await monitor.hass.async_block_till_done()
    assert monitor.task is not None
    await monitor.task
    assert monitor.state["status"] == "all_visible"


async def test_continuous_missing_duration_resets_on_reception_or_all_missing(monitor, monkeypatch):
    scanner = Mock(return_value=scan(("3",)))
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", scanner)
    await monitor.check()
    since = monitor.state["missing"]["3"]
    await monitor.check()
    assert monitor.state["missing"]["3"] == since
    monitor.state["config"]["recovery_enabled"] = True
    assert not monitor.recovery_due("3", time.time(), 1)
    monitor.state["missing"]["3"] -= 1801
    assert monitor.recovery_due("3", time.time(), 1)
    scanner.return_value = scan(("0", "1", "2", "3"))
    await monitor.check()
    assert monitor.state["missing"] == {}
    assert not monitor.recovery_due("3", time.time(), 1)


async def test_candidate_needs_fresh_scan_and_respects_cooldown_and_one_per_cycle(monitor, monkeypatch):
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", lambda _: scan(("3",)))
    await monitor.check()
    monitor.state["config"]["recovery_enabled"] = True
    monitor.state["missing"]["3"] -= 1801
    assert monitor.recovery_due("3", time.time(), 1)
    monitor.state["cooldowns"]["3"] = time.time() + 60
    assert not monitor.recovery_due("3", time.time(), 1)
    monitor.state["cooldowns"].clear()
    monitor.recovery_cycle = 1
    assert not monitor.recovery_due("3", time.time(), 1)
    assert monitor.recovery_due("3", time.time(), 2)
    monitor.state["checked_timestamp"] -= 1201
    assert not monitor.recovery_due("3", time.time(), 2)


async def test_failed_scan_resets_long_missing_streak(monitor, monkeypatch):
    monitor.state["missing"] = {"3": time.time() - 3600}
    monkeypatch.setattr("custom_components.netgear_wax.radio_monitor.scan_wifi", Mock(side_effect=TimeoutError))
    await monitor.run()
    assert monitor.state["missing"] == {}
