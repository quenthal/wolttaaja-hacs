"""Three permanent aggregate sensors."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import WolttaajaEntity
from .models import choose


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            WolttaajaSensor(entry.runtime_data, kind)
            for kind in ("next_delivery", "order_status", "active_orders")
        ]
    )


class WolttaajaSensor(WolttaajaEntity, SensorEntity):
    def __init__(self, coordinator, kind):
        super().__init__(coordinator, kind)
        if kind == "next_delivery":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:moped"

    @property
    def selected(self):
        orders = self.coordinator.data or {}
        return orders.get(choose(orders))

    @property
    def native_value(self):
        if self.kind == "active_orders":
            return len(self.coordinator.data or {})
        order = self.selected
        if not order:
            return None
        return order.eta if self.kind == "next_delivery" else order.status

    @property
    def extra_state_attributes(self):
        if self.kind == "active_orders":
            return {"orders": [order.as_dict() for order in (self.coordinator.data or {}).values()]}
        return (
            self.selected.as_dict()
            if self.selected
            else {"order_id": None, "restaurant": None, "status": None, "eta": None}
        )
