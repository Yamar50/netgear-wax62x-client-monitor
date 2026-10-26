import sys
from pathlib import Path
import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from export_tsv import convert


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
