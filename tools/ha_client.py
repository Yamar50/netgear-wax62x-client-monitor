"""Small standard-library HA client; credentials stay in a private local file."""

import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HAError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Home Assistant request failed (HTTP {status})")


class HAClient:
    def __init__(self, config_file):
        self.config = json.loads(Path(config_file).read_text())
        self.base = self.config["ha_url"].rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
        ):
            raise ValueError("Invalid HA base URL")
        self.token = Path(self.config["token_file"]).read_text().strip()
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, data=None):
        request = urllib.request.Request(
            self.base + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            raise HAError(err.code) from None
        except urllib.error.URLError, TimeoutError:
            raise HAError("unreachable") from None

    def reservation(self, operation, **data):
        return self.request("/api/netgear_wax/reservation", {"operation": operation, **data})
