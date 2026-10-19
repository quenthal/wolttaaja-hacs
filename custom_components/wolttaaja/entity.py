"""Fixed identities with Recorder attribute exclusion."""

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class WolttaajaEntity(CoordinatorEntity):
    _attr_has_entity_name = False
    _unrecorded_attributes = frozenset({"orders", "order_id", "restaurant", "status", "eta"})

    def __init__(self, coordinator, kind):
        super().__init__(coordinator)
        self.kind = kind
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{kind}"
        self._attr_name = "Wolttaaja " + kind.replace("_", " ")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Wolttaaja",
            manufacturer="Wolttaaja",
        )
