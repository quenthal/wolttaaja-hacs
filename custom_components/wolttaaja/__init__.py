"""Wolttaaja integration setup."""

from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import WoltApi
from .const import DOMAIN
from .coordinator import WolttaajaCoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass, entry):
    async def persist(access, refresh):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "bearer_token": access, "refresh_token": refresh}
        )

    api = WoltApi(
        async_get_clientsession(hass),
        entry.data.get("session_id"),
        entry.data.get("bearer_token", ""),
        entry.data["refresh_token"],
        client_id=entry.data["client_id"],
        token_update_callback=persist,
    )
    coordinator = WolttaajaCoordinator(hass, entry, api)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_entry_updated))
    return True


async def async_entry_updated(hass, entry):
    await entry.runtime_data.async_apply_options()


async def async_migrate_entry(hass, entry):
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        # v1.0.0 shipped a binary sensor; remove it once, even if the user renamed it.
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{entry.entry_id}_delivery_active"
        )
        if entity_id:
            registry.async_remove(entity_id)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass, entry):
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.trace").async_remove()
