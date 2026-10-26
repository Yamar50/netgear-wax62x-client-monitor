import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ha_client import HAError
from with_reservation import run


class Client:
    def __init__(self, busy=False):
        self.busy = busy
        self.calls = []

    def reservation(self, operation, **data):
        self.calls.append(operation)
        if self.busy:
            raise HAError(409)
        return {"token": "test-only", "duration": 900}


def test_busy_never_spawns_child(tmp_path):
    marker = tmp_path / "started"
    client = Client(busy=True)
    with pytest.raises(HAError):
        run(client, [sys.executable, "-c", f'open({str(marker)!r}, "w").close()'])
    assert not marker.exists()
    assert client.calls == ["acquire"]


def test_success_releases_and_failure_retains():
    client = Client()
    assert run(client, [sys.executable, "-c", "pass"]) == 0
    assert client.calls == ["acquire", "release"]
    client = Client()
    assert run(client, [sys.executable, "-c", "raise SystemExit(3)"]) == 3
    assert client.calls == ["acquire"]


def test_timeout_kills_before_expiry():
    client = Client()
    assert run(client, [sys.executable, "-c", "import time;time.sleep(90)"], timeout=1) == 75
    assert client.calls == ["acquire"]
