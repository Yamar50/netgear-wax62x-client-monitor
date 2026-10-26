from custom_components.netgear_wax.model import mac_address, normalize_client, reconcile
import pytest

MAC = "02:00:00:00:00:01"


def row(ap="a", connected=True, at=100):
    return normalize_client(
        {"mac": MAC, "status": "Authenticated" if connected else "Authentication Failed"},
        {"id": ap, "name": ap},
        "5GHz",
        at,
    )


def result(ap="a", clients=None, status="ok"):
    return {"id": ap, "status": status, "clients": clients or []}


def test_roaming_positive_beats_later_failed_auth():
    value = reconcile({}, [result("a", [row()]), result("b", [row("b", False, 200)])], [], 200)
    assert value[MAC]["connected"] is True
    assert value[MAC]["ap_id"] == "a"


def test_missing_with_failed_ap_is_unknown_not_away():
    value = reconcile({MAC: row()}, [result(), result("b", status="unavailable")], [], 200)
    assert value[MAC]["connected"] is None
    assert value[MAC]["observed_timestamp"] == 100
    assert value[MAC]["fresh"] is False


def test_successful_empty_fleet_proves_disconnection():
    assert reconcile({MAC: row()}, [result(), result("b")], [], 200)[MAC]["connected"] is False


def test_positive_survives_other_ap_failure():
    assert reconcile({}, [result("a", [row()]), result("b", status="cooldown")], [], 200)[MAC]["connected"] is True


def test_selected_unknown_client_kept_and_old_unselected_expires():
    assert MAC in reconcile({MAC: row()}, [result()], [MAC], 90000)
    assert MAC not in reconcile({MAC: row()}, [result()], [], 90000)


def test_mac_identity_and_invalid_values():
    assert mac_address("02-00-00-AB-CD-EF") == "02:00:00:ab:cd:ef"
    for bad in ["abcd", "", "02:00:00:00:00:gg", "02:00:00:00:00:01\nextra"]:
        with pytest.raises(ValueError):
            mac_address(bad)


def test_zero_and_missing_measurements_not_conflated():
    value = normalize_client(
        {"mac": MAC, "rssi": "-48 dBm", "txB": 0, "rxB": "1.5 MB", "txR": "600 Mbps"},
        {"id": "a", "name": "AP"},
        "5GHz",
        100,
    )
    assert value["tx_bytes"] == 0
    assert value["rx_bytes"] == 1500000
    assert value["tx_rate"] == 600000000
    assert value["rssi"] == -48
    assert value["pmf"] is None
    assert value["channel"] is None
    assert "password" not in value
