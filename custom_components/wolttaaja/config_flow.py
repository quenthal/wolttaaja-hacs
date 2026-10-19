"""Credential validation, reauthentication and four polling/event options."""

import uuid

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import WoltApi, WoltApiError, WoltAuthenticationError
from .const import DEFAULTS, DOMAIN, LIMITS

SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
SCHEMA = vol.Schema(
    {
        vol.Required("refresh_token"): vol.All(SECRET, vol.Length(min=1)),
        vol.Optional("bearer_token", default=""): SECRET,
        vol.Optional("session_id", default=""): SECRET,
    }
)


class WolttaajaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await self._credentials("user", user_input)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._credentials("reauth_confirm", user_input)

    async def _credentials(self, step, user_input):
        errors = {}
        if user_input is not None:
            entry = self._get_reauth_entry() if step == "reauth_confirm" else None
            previous = getattr(self, "_submitted", None)
            data = self._validated if previous == user_input else dict(user_input)
            data.setdefault("client_id", entry.data["client_id"] if entry else str(uuid.uuid4()))

            async def persist(access, refresh):
                data.update(bearer_token=access, refresh_token=refresh)
                # Rotation must survive even if subsequent validation fails.
                if entry:
                    self.hass.config_entries.async_update_entry(entry, data={**entry.data, **data})

            api = (
                self._api
                if previous == user_input
                else WoltApi(
                    async_get_clientsession(self.hass),
                    data.get("session_id"),
                    data.get("bearer_token", ""),
                    data["refresh_token"],
                    client_id=data["client_id"],
                    token_update_callback=persist,
                )
            )
            self._submitted = dict(user_input)
            self._validated, self._api = data, api
            try:
                await api.fetch_orders()
            except WoltAuthenticationError:
                errors["base"] = "invalid_auth"
            except WoltApiError:
                errors["base"] = "cannot_connect"
            else:
                if entry:
                    return self.async_update_reload_and_abort(entry, data_updates=data)
                return self.async_create_entry(title="Wolttaaja", data=data)
        return self.async_show_form(step_id=step, data_schema=SCHEMA, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WolttaajaOptionsFlow()


class WolttaajaOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        schema = vol.Schema(
            {
                vol.Required(key, default=self.config_entry.options.get(key, default)): vol.All(
                    vol.Coerce(int), vol.Range(min=LIMITS[key][0], max=LIMITS[key][1])
                )
                for key, default in DEFAULTS.items()
            }
        )
        if user_input is not None:
            return self.async_create_entry(title="", data=schema(user_input))
        return self.async_show_form(step_id="init", data_schema=schema)
