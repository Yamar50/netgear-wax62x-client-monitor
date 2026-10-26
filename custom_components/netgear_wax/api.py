"""Read-only WAX625/WAX628 management API client with an isolated session."""

from __future__ import annotations

import asyncio
import base64
from email.utils import formatdate
import json
import time

import aiohttp
from yarl import URL

from .model import mac_address, normalize_client

LIST_ID = "3146087"
DETAIL_ID = "0x1FFFFFFFFFF"


class WaxError(Exception):
    """Sanitized error: never include response bodies, headers or credentials."""


class AuthenticationError(WaxError):
    pass


class LoginLimitError(WaxError):
    pass


class LogoutError(WaxError):
    pass


class WaxClient:
    """One login per collection, with guaranteed attempted logout of our own session."""

    def __init__(self, config: dict):
        self.config = config
        self.base = URL.build(scheme="https", host=config["host"])
        self.token = None
        self.owned = False
        self.session = None
        self.logout_ok = False
        self.login_attempted = False

    async def request(self, path, payload=None, referer="/dashboard", login=False):
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": str(self.base),
            "Referer": str(self.base) + referer,
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.token:
            headers["security"] = self.token
        if login:
            headers["time"] = formatdate(usegmt=True)
        try:
            # Never send credentials/cookies to a redirect target. Only the initial
            # unauthenticated GET may redirect, and only within this AP origin.
            async with self.session.request(
                "GET" if payload is None else "POST",
                self.base.with_path(path),
                json=payload,
                headers=headers,
                allow_redirects=False,
                ssl=None if self.config.get("verify_ssl", True) else False,
            ) as response:
                if response.status in (301, 302, 303, 307, 308) and payload is None:
                    target = self.base.join(URL(response.headers.get("Location", "/")))
                    if path == "/" or target.origin() != self.base.origin() or target.path != "/":
                        raise WaxError("unexpected_redirect")
                    return await self.request("/")
                if response.content_length and response.content_length > 4 * 1024 * 1024:
                    raise WaxError("response_too_large")
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > 4 * 1024 * 1024:
                        raise WaxError("response_too_large")
                body = bytes(chunks)
                if b"Maximum users to login limit exceeded" in body:
                    raise LoginLimitError("login_limit")
                if response.status in (401, 403):
                    raise AuthenticationError("authentication_failed")
                if response.status != 200:
                    raise WaxError("http_error")
                if payload is None:
                    return {}
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError) as err:
                    raise WaxError("invalid_json") from err
                if not isinstance(result, dict):
                    raise WaxError("invalid_response")
                if result.get("status") not in (0, "0"):
                    if login or result.get("status") in (100, 401, "100", "401"):
                        raise AuthenticationError("authentication_failed")
                    raise WaxError("api_error")
                return result
        except (aiohttp.ClientError, TimeoutError) as err:
            raise WaxError("connection_failed") from err

    async def socket(self, payload, referer="/dashboard"):
        return await self.request("/socketCommunication", payload, referer)

    async def dashboard(self):
        # Empty leaves are the observed read selectors, not configuration writes.
        data = await self.socket(
            {
                "system": {
                    "monitor": {
                        "productId": "",
                        "sysVersion": "",
                        "ethernetMacAddress": "",
                        "DeviceInfo": {"UpTime": ""},
                        "currentChannel": {"wlan0": "", "wlan1": ""},
                        "radioApStatus": {
                            "wlan0": {"numberOfStations": ""},
                            "wlan1": {"numberOfStations": ""},
                        },
                    },
                    "basicSettings": {"apName": ""},
                }
            }
        )
        system = data.get("system", {})
        counts = {}
        for band in ("wlan0", "wlan1"):
            value = system.get("monitor", {}).get("radioApStatus", {}).get(band, {}).get("numberOfStations")
            if isinstance(value, bool) or not str(value).isdigit():
                raise WaxError("invalid_station_count")
            counts[band] = int(value)
        return system, counts

    async def client_list(self, wlan, expected):
        if expected == 0:
            return []
        data = await self.socket({"system": {"monitor": {"optCliList": {wlan: {LIST_ID: ""}}}}}, "/connectedClients")
        node = data.get("system", {}).get("monitor", {}).get("optCliList", {}).get(wlan, {})
        rows = node.get(LIST_ID)
        if not rows:
            rows = next((v for v in node.values() if isinstance(v, list) and v), rows)
        if not isinstance(rows, list) or not rows:
            raise WaxError("incomplete_client_list")
        if len(rows) > 1024:
            raise WaxError("client_limit_exceeded")
        for row in rows:
            if not isinstance(row, dict):
                raise WaxError("invalid_client")
            try:
                mac_address(row.get("mac", ""))
            except ValueError as err:
                raise WaxError("invalid_client_mac") from err
        if len(rows) < expected:
            raise WaxError("incomplete_client_list")
        return rows

    async def read_clients(self):
        await self.socket({"system": {"basicSettings": {"dayZeroStatus": ""}}}, "/AP_login")
        system, counts = await self.dashboard()
        monitor = system["monitor"]
        model = monitor.get("productId")
        if model not in ("WAX625", "WAX628"):
            raise WaxError("unsupported_model")
        ap = {"id": self.config["id"], "name": system.get("basicSettings", {}).get("apName") or self.config["name"]}
        # Dashboard counts and lists are separate, non-atomic AP reads. Refresh
        # both in this same owned session when a list is incomplete; never retry
        # authentication or open another AP session here.
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(1)
                system, counts = await self.dashboard()
                monitor = system["monitor"]
            await self.request("/connectedClients")
            await self.socket({"system": {"monitor": {"radioApStatus": {"totalNumberOfStations": ""}, "clientList": ""}}})
            await self.socket({"system": {"monitor": {"recentCliList": {"5": ""}}}})
            try:
                # Capture both lists before the slower per-client detail reads.
                lists = {wlan: await self.client_list(wlan, counts[wlan]) for wlan in ("wlan0", "wlan1")}
                break
            except WaxError as err:
                if str(err) != "incomplete_client_list" or attempt == 2:
                    raise
        now = time.time()
        rows = []
        for wlan, band in (("wlan0", "2.4GHz"), ("wlan1", "5GHz")):
            for basic in lists[wlan]:
                mac = mac_address(basic["mac"])
                # WAX detail selectors are case-sensitive, unlike stored MAC identity.
                key = mac.upper().replace(":", "-")
                detail = basic
                detail_ok = False
                try:
                    reply = await self.socket(
                        {"system": {"monitor": {"optCliList": {wlan: {DETAIL_ID: {key: ""}}}}}}, "/connectedClients"
                    )
                    candidate = reply["system"]["monitor"]["optCliList"][wlan][DETAIL_ID][key][0]
                    if mac_address(candidate.get("mac", "")) == mac:
                        detail = {**basic, **candidate}
                        detail_ok = True
                except KeyError, IndexError, TypeError, ValueError:
                    pass
                except AuthenticationError, LoginLimitError:
                    raise
                except WaxError:
                    pass
                row = normalize_client(detail, ap, band, now)
                if row["connected"] is None:
                    raise WaxError("unknown_client_status")
                row["detail_available"] = detail_ok
                rows.append(row)
        return {
            **ap,
            "model": model,
            "firmware": monitor.get("sysVersion"),
            "uptime": monitor.get("DeviceInfo", {}).get("UpTime"),
            # Consumers validate these counts against the actual captured rows.
            # AP counters can lag and are retained separately for diagnostics.
            "clients_24ghz": len(lists["wlan0"]),
            "clients_5ghz": len(lists["wlan1"]),
            "reported_clients_24ghz": counts["wlan0"],
            "reported_clients_5ghz": counts["wlan1"],
            "channel_24ghz": monitor.get("currentChannel", {}).get("wlan0"),
            "channel_5ghz": monitor.get("currentChannel", {}).get("wlan1"),
            "observed_timestamp": now,
            "clients": rows,
            "status": "ok",
        }

    async def collect(self):
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=20)) as session:
            self.session = session
            try:
                await self.request("/AP_login")
                self.login_attempted = True
                data = await self.request(
                    "/socketCommunication",
                    {
                        "system": {
                            "basicSettings": {
                                "adminName": self.config["username"],
                                "adminPasswd": self.config["password"],
                            }
                        }
                    },
                    "/AP_login",
                    login=True,
                )
                self.owned = True
                self.token = data.get("system", {}).get("security_token")
                if not self.token:
                    raise AuthenticationError("missing_session_token")
                jar.update_cookies({"ssid": base64.b64encode(self.token.encode()).decode()}, response_url=self.base)
                return await self.read_clients()
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as err:
                raise WaxError("invalid_response") from err
            finally:
                if self.owned and self.token:
                    task = asyncio.create_task(self.request("/logout", {"admin": self.config["username"]}))
                    try:
                        await asyncio.shield(task)
                        self.logout_ok = True
                    except asyncio.CancelledError:
                        try:
                            await task
                            self.logout_ok = True
                        finally:
                            raise
                    except WaxError as err:
                        raise LogoutError("logout_failed") from err
