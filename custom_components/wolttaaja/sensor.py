"""Three permanent aggregate sensors."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import WolttaajaEntity
from .models import STATUSES

NO_ORDER = "no_order"
# Only vehicle values seen in Wolt data get their own icon.
VEHICLE_ICONS = {"bicycle": "mdi:bicycle", "car": "mdi:car"}


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
            self._attr_options = [NO_ORDER, *STATUSES]

    @property
    def selected(self):
        return (self.coordinator.data or {}).get(self.coordinator.selected)

    @property
    def shown(self):
        """The order this sensor describes; Status also lingers on a just-delivered one."""
        order = self.selected
        if order is None and self.kind == "order_status":
            order = self.coordinator.recently_delivered
        return order

    @property
    def icon(self):
        if self.kind == "active_orders":
            return "mdi:food"
        order = self.shown
        return VEHICLE_ICONS.get(order.vehicle if order else None, "mdi:moped")

    @property
    def native_value(self):
        if self.kind == "active_orders":
            return len(self.coordinator.data or {})
        order = self.shown
        if self.kind == "next_delivery":
            return order.eta if order else None
        if not order:
            # A successful update with nothing to track; failures are unavailable.
            return NO_ORDER
        # HA renders a missing enum value as the same "unknown" state.
        return order.status if order.status in STATUSES else None

    @property
    def extra_state_attributes(self):
        if self.kind == "active_orders":
            return {"orders": [order.as_dict() for order in (self.coordinator.data or {}).values()]}
        return (
            self.shown.as_dict()
            if self.shown
            else dict.fromkeys(
                ("order_id", "restaurant", "status", "eta", "vehicle", "eta_min", "eta_max")
            )
        )
