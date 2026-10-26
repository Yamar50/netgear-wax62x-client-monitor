import sys
from pathlib import Path
import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from export_tsv import convert, export, atomic_write
from ha_client import HAError
import ast
import json


def snapshot():
    return {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "updated_timestamp": 100,
        "freshness_limit": 1200,
        "aps": {"a": {"name": "Test AP", "status": "ok", "uptime": "01:00:00"}},
        "clients": {
            "02:00:00:00:00:01": {
                "ap_id": "a",
                "ap_name": "Test AP",
                "connected": True,
                "observed_at": "2026-01-01T00:00:00+00:00",
                "hostname": "Example\tPhone\n",
                "tx_bytes": 0,
                "tx_rate": 600000000,
                "pmf": None,
            }
        },
    }


def test_export_hash_units_and_sanitized_cells():
    tsv, status = convert(snapshot(), now=110)
    assert hashlib.sha256(tsv.encode()).hexdigest() == status["tsvSha256"]
    assert "Authenticated" in tsv and "600000000 bps" in tsv and "0 B" in tsv
    assert len(tsv.splitlines()) == 2
    assert status["aps"][0]["status"] == "ok"


def test_stale_export_never_claims_active():
    tsv, status = convert(snapshot(), now=1400)
    assert "Authenticated" not in tsv
    assert "Disconnect" in tsv
    assert status["aps"][0]["status"] == "unavailable"
    assert status["generatedAt"] == "2026-01-01T00:00:00+00:00"


def test_ha_outage_invalidates_old_active_rows_without_refreshing_observation(tmp_path):
    class OfflineHA:
        def request(self, path):
            raise HAError("unreachable")

    target = tmp_path / "WAX625_ClientDetail.tsv"
    tsv, status = convert(snapshot(), now=110)
    target.write_text(tsv)
    sidecar = Path(str(target) + ".status.json")
    sidecar.write_text(json.dumps(status))
    export(OfflineHA(), target)
    result = target.read_text()
    metadata = json.loads(sidecar.read_text())
    assert "Authenticated" not in result and "Disconnect" in result
    assert "2026-01-01T00:00:00+00:00" in result
    assert metadata["generatedAt"] == status["generatedAt"]
    assert metadata["aps"][0]["status"] == "unavailable"
    assert metadata["tsvSha256"] == hashlib.sha256(result.encode()).hexdigest()
    assert (tmp_path / "WAX625_OLD_ClientDetails.tsv").read_text() == tsv


def test_external_helpers_support_python39_syntax():
    for name in ("ha_client.py", "export_tsv.py", "export_snapshot.py", "with_reservation.py"):
        source = Path(__file__).resolve().parents[1] / "tools" / name
        ast.parse(source.read_text(), filename=name, feature_version=(3, 9))


def test_atomic_export_preserves_existing_viewer_read_access(tmp_path):
    target = tmp_path / "view.tsv"
    atomic_write(target, "first")
    assert target.stat().st_mode & 0o777 == 0o600
    target.chmod(0o644)
    atomic_write(target, "second")
    assert target.read_text() == "second"
    assert target.stat().st_mode & 0o777 == 0o644
