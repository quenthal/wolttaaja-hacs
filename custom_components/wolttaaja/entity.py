"""Fixed identities with Recorder attribute exclusion."""

from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class WolttaajaEntity(CoordinatorEntity):
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"orders", "order_id", "restaurant", "status", "eta"})

    def __init__(self, coordinator, kind, platform):
        super().__init__(coordinator)
        self.kind = kind
        # HA would derive IDs from translated names in some languages (e.g. Finnish);
        # documented IDs stay language-independent. The registry still resolves clashes.
        self.entity_id = f"{platform}.wolttaaja_{kind}"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{kind}"
        self._attr_translation_key = kind
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Wolttaaja",
            manufacturer="Wolttaaja",
            entry_type=DeviceEntryType.SERVICE,
        )
