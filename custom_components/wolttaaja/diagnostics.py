"""Diagnostics without credentials, order IDs, venue names or locations."""

from homeassistant.loader import async_get_integration

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)
    return {
        "version": str(integration.version),
        "options": coordinator.options,
        "last_update_success": coordinator.last_update_success,
        "active_orders": [
            {"status": order.status, "has_eta": order.eta is not None}
            for order in (coordinator.data or {}).values()
        ],
        # Recent orders, including ones that already completed.
        "status_timelines": coordinator.trace.as_dict(),
    }
