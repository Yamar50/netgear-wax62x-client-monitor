"""Authenticated cache export and admin-only reservations. No AP access on GET."""

import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .api import WaxError
from .lease import BusyError, LeaseError

_LOGGER = logging.getLogger(__name__)


class ConnectionView(HomeAssistantView):
    url = f"/api/{DOMAIN}/connection"
    name = f"api:{DOMAIN}:connection"
    requires_auth = True

    async def post(self, request):
        if not request["hass_user"].is_admin:
            return self.json({"error": "admin_required"}, status_code=403)
        coordinator = request.app["hass"].data.get(DOMAIN)
        if coordinator is None:
            return self.json({"error": "integration_not_loaded"}, status_code=503)
        try:
            return self.json(await coordinator.report_connection(await request.json()))
        except (ValueError, TypeError, OverflowError, web.HTTPBadRequest):
            return self.json({"error": "invalid_connection_report"}, status_code=400)
        except HomeAssistantError:
            return self.json({"error": "report_not_saved"}, status_code=503)


class RadioMonitorView(HomeAssistantView):
    url = f"/api/{DOMAIN}/radio_monitor"
    name = f"api:{DOMAIN}:radio_monitor"
    requires_auth = True

    async def get(self, request):
        if not request["hass_user"].is_admin:
            return self.json({"error": "admin_required"}, status_code=403)
        coordinator = request.app["hass"].data.get(DOMAIN)
        if coordinator is None or not hasattr(coordinator, "radio"):
            return self.json({"error": "integration_not_loaded"}, status_code=503)
        return self.json(coordinator.radio.export())

    async def post(self, request):
        if not request["hass_user"].is_admin:
            return self.json({"error": "admin_required"}, status_code=403)
        coordinator = request.app["hass"].data.get(DOMAIN)
        if coordinator is None or not hasattr(coordinator, "radio"):
            return self.json({"error": "integration_not_loaded"}, status_code=503)
        radio = coordinator.radio
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("invalid_request")
            operation = data.get("operation")
            if operation == "configure":
                result = await radio.configure(data.get("config", {}))
            elif operation == "set_mode":
                result = await radio.set_mode(data.get("mode"), data.get("hold_until", 0))
            elif operation == "check":
                result = await radio.run()
            elif operation == "queue_preflight":
                result = await radio.queue_preflight()
            elif operation == "import_dfs_indexes":
                result = await radio.import_dfs_indexes(data.get("indexes"))
            else:
                raise ValueError("invalid_operation")
        except (ValueError, TypeError, web.HTTPBadRequest):
            return self.json({"error": "invalid_request_or_configuration"}, status_code=400)
        except BusyError:
            return self.json({"error": "busy"}, status_code=409)
        except WaxError as error:
            return self.json({"error": "radio_operation_unconfirmed", "reason": str(error)}, status_code=503)
        except (HomeAssistantError, OSError, TimeoutError) as error:
            _LOGGER.exception("Radio operation failed before confirmation")
            return self.json({"error": "radio_operation_unconfirmed", "reason": type(error).__name__}, status_code=503)
        return self.json(result)


class SnapshotView(HomeAssistantView):
    url = f"/api/{DOMAIN}/snapshot"
    name = f"api:{DOMAIN}:snapshot"
    requires_auth = True

    async def get(self, request):
        if not request["hass_user"].is_admin:
            return self.json({"error": "admin_required"}, status_code=403)
        coordinator = request.app["hass"].data.get(DOMAIN)
        if coordinator is None:
            return self.json({"error": "integration_not_loaded"}, status_code=503)
        return self.json(coordinator.export())


class ReservationView(HomeAssistantView):
    url = f"/api/{DOMAIN}/reservation"
    name = f"api:{DOMAIN}:reservation"
    requires_auth = True

    async def post(self, request):
        user = request["hass_user"]
        if not user.is_admin:
            return self.json({"error": "admin_required"}, status_code=403)
        coordinator = request.app["hass"].data.get(DOMAIN)
        if coordinator is None:
            return self.json({"error": "integration_not_loaded"}, status_code=503)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError
            operation = data.get("operation")
            ttl = data.get("duration", 600)
            if isinstance(ttl, bool) or not isinstance(ttl, int):
                raise ValueError
            owner = user.id
            if operation == "acquire":
                result = await coordinator.lease.acquire(owner, ttl)
            elif operation == "renew":
                token = data.get("token")
                if not isinstance(token, str):
                    raise ValueError
                result = await coordinator.lease.renew(owner, token, ttl)
            elif operation == "release":
                token = data.get("token")
                if not isinstance(token, str):
                    raise ValueError
                await coordinator.lease.release(owner, token)
                result = {"released": True}
            else:
                raise ValueError
        except BusyError:
            return self.json({"error": "busy", "access": coordinator.lease.status()}, status_code=409)
        except LeaseError as err:
            return self.json({"error": str(err)}, status_code=409)
        except ValueError, web.HTTPBadRequest:
            return self.json({"error": "invalid_request"}, status_code=400)
        coordinator.async_update_listeners()
        return self.json(result)
