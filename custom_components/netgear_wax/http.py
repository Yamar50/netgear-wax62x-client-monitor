"""Authenticated cache export and admin-only reservations. No AP access on GET."""

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .lease import BusyError, LeaseError


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
