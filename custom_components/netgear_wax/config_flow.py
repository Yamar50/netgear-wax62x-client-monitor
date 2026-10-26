"""UI configuration: AP credentials, selected clients and collection settings."""

from __future__ import annotations

from copy import deepcopy
import re
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN, NAME, DEFAULT_INTERVAL
from .model import mac_address


def ap_schema(ap=None):
    ap = ap or {}
    return vol.Schema(
        {
            vol.Required("name", default=ap.get("name", "Access point")): str,
            vol.Required("host", default=ap.get("host", "")): str,
            vol.Required("username", default=ap.get("username", "admin")): str,
            vol.Optional("password"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required("verify_ssl", default=ap.get("verify_ssl", True)): bool,
        }
    )


def validate_ap(data, previous=None):
    ap = {**(previous or {}), **data}
    ap["host"] = ap["host"].strip().lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", ap["host"]):
        raise ValueError("invalid_host")
    if not ap.get("password"):
        raise ValueError("missing_password")
    if not ap.get("name", "").strip() or not ap.get("username", "").strip():
        raise ValueError("invalid_input")
    ap.setdefault("id", uuid4().hex)
    return ap


class WaxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            try:
                ap = validate_ap(user_input)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return self.async_create_entry(
                    title=NAME,
                    data={
                        "aps": [ap],
                        "scan_interval": DEFAULT_INTERVAL,
                        "polling_enabled": False,
                        "selected_clients": [],
                    },
                )
        return self.async_show_form(step_id="user", data_schema=ap_schema(user_input), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WaxOptionsFlow()


class WaxOptionsFlow(config_entries.OptionsFlow):
    @property
    def settings(self):
        return deepcopy({**self.config_entry.data, **self.config_entry.options})

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["collection", "add_ap", "choose_ap", "clients"])

    async def async_step_collection(self, user_input=None):
        settings = self.settings
        if user_input is not None:
            settings.update(user_input)
            return self.async_create_entry(title="", data=settings)
        return self.async_show_form(
            step_id="collection",
            data_schema=vol.Schema(
                {
                    vol.Required("polling_enabled", default=settings.get("polling_enabled", False)): bool,
                    vol.Required("scan_interval", default=settings.get("scan_interval", DEFAULT_INTERVAL)): vol.All(
                        vol.Coerce(int), vol.Range(min=300, max=86400)
                    ),
                }
            ),
        )

    async def async_step_add_ap(self, user_input=None):
        errors = {}
        settings = self.settings
        if user_input is not None:
            try:
                ap = validate_ap(user_input)
                if any(a["host"] == ap["host"] for a in settings["aps"]):
                    raise ValueError("duplicate_host")
            except ValueError as err:
                errors["base"] = str(err)
            else:
                settings["aps"].append(ap)
                return self.async_create_entry(title="", data=settings)
        return self.async_show_form(step_id="add_ap", data_schema=ap_schema(user_input), errors=errors)

    async def async_step_choose_ap(self, user_input=None):
        if user_input is not None:
            self.ap_id = user_input["ap_id"]
            return await self.async_step_edit_ap()
        return self.async_show_form(
            step_id="choose_ap",
            data_schema=vol.Schema(
                {
                    vol.Required("ap_id"): vol.In({ap["id"]: ap["name"] for ap in self.settings["aps"]}),
                }
            ),
        )

    async def async_step_edit_ap(self, user_input=None):
        settings = self.settings
        ap = next(a for a in settings["aps"] if a["id"] == self.ap_id)
        errors = {}
        if user_input is not None:
            if user_input.get("remove"):
                if len(settings["aps"]) == 1:
                    errors["base"] = "last_ap"
                else:
                    settings["aps"] = [a for a in settings["aps"] if a["id"] != self.ap_id]
                    return self.async_create_entry(title="", data=settings)
            else:
                try:
                    fields = {k: v for k, v in user_input.items() if k != "remove" and not (k == "password" and not v)}
                    replacement = validate_ap(fields, ap)
                    if any(a["host"] == replacement["host"] and a["id"] != self.ap_id for a in settings["aps"]):
                        raise ValueError("duplicate_host")
                except ValueError as err:
                    errors["base"] = str(err)
                else:
                    settings["aps"] = [replacement if a["id"] == self.ap_id else a for a in settings["aps"]]
                    return self.async_create_entry(title="", data=settings)
        return self.async_show_form(
            step_id="edit_ap",
            data_schema=ap_schema(ap).extend({vol.Optional("remove", default=False): bool}),
            errors=errors,
        )

    async def async_step_clients(self, user_input=None):
        settings = self.settings
        errors = {}
        if user_input is not None:
            try:
                chosen = [mac_address(m) for m in user_input.get("selected_clients", [])]
                chosen.extend(
                    mac_address(m.strip()) for m in user_input.get("manual_macs", "").splitlines() if m.strip()
                )
            except ValueError:
                errors["base"] = "invalid_mac"
            else:
                settings["selected_clients"] = sorted(set(chosen))
                return self.async_create_entry(title="", data=settings)
        coordinator = self.config_entry.runtime_data
        clients = coordinator.export()["clients"]
        known = sorted(set(clients) | set(settings.get("selected_clients", [])))
        options = [{"value": m, "label": f"{clients.get(m, {}).get('hostname') or 'Client'} ({m})"} for m in known]
        return self.async_show_form(
            step_id="clients",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "selected_clients", default=settings.get("selected_clients", [])
                    ): selector.SelectSelector(selector.SelectSelectorConfig(options=options, multiple=True)),
                    vol.Optional("manual_macs", default=""): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                }
            ),
            errors=errors,
        )
