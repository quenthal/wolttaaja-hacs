"""Three permanent aggregate sensors."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import WolttaajaEntity
from .models import STATUSES, choose


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            WolttaajaSensor(entry.runtime_data, kind)
            for kind in ("next_delivery", "order_status", "active_orders")
        ]
    )


class WolttaajaSensor(WolttaajaEntity, SensorEntity):
    def __init__(self, coordinator, kind):
        super().__init__(coordinator, kind, "sensor")
        if kind == "next_delivery":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif kind == "order_status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(STATUSES)
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
        if self.kind == "next_delivery":
            return order.eta
        # HA renders a missing enum value as the same "unknown" state.
        return order.status if order.status in STATUSES else None

    @property
    def extra_state_attributes(self):
        if self.kind == "active_orders":
            return {"orders": [order.as_dict() for order in (self.coordinator.data or {}).values()]}
        return (
            self.selected.as_dict()
            if self.selected
            else {"order_id": None, "restaurant": None, "status": None, "eta": None}
        )
