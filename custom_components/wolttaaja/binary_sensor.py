"""One permanent delivery activity sensor."""

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import WolttaajaEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DeliveryActive(entry.runtime_data, "delivery_active")])


class DeliveryActive(WolttaajaEntity, BinarySensorEntity):
    _attr_icon = "mdi:moped"

    @property
    def is_on(self):
        return bool(self.coordinator.data)
