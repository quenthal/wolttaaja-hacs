"""One polling coordinator and durable, bounded notification state."""

from __future__ import annotations

import logging
from datetime import timedelta
from time import monotonic

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    WoltApiError,
    WoltAuthenticationError,
    WoltInvalidPayloadError,
    WoltRateLimitError,
    is_active_order,
)
from .const import DEFAULTS, DOMAIN, EVENT
from .models import choose, groups, normalize_order_status, project

_LOGGER = logging.getLogger(__name__)
RETENTION = 30 * 86400


class WolttaajaCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=entry.options.get("idle_interval", 300)),
            always_update=False,
        )
        self._options = dict(entry.options)
        self.entry = entry
        self.api = api
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.primary = None
        self.notified = {}
        self._saved = None

    async def async_load(self):
        saved = await self.store.async_load()
        if saved:
            self.primary = saved.get("primary")
            self.notified = saved.get("notified", {})
            # A reload/restart must not bypass server-requested cooldown.
            remaining = saved.get("retry_until", 0) - dt_util.utcnow().timestamp()
            self.api.retry_at = monotonic() + max(0, remaining)
            self._saved = saved

    async def _save(self, primary, notified):
        remaining = max(0, self.api.retry_at - monotonic())
        saved = {
            "primary": primary,
            "notified": notified,
            "retry_until": dt_util.utcnow().timestamp() + remaining if remaining else 0,
        }
        if saved != self._saved:
            await self.store.async_save(saved)
            self._saved = saved

    async def _async_update_data(self):
        options = DEFAULTS | dict(self.entry.options)
        try:
            summaries = await self.api.fetch_orders()
            orders = {}
            for summary in summaries:
                if "telemetry" in summary or "order_status_type" in summary:
                    telemetry = summary.get("telemetry")
                    lifecycle = (
                        (
                            telemetry.get("order_status_type")
                            if isinstance(telemetry, dict)
                            else None
                        )
                        if "telemetry" in summary
                        else summary.get("order_status_type")
                    )
                    if str(lifecycle).upper() not in {
                        "IN_PROGRESS",
                        "DELIVERED",
                        "COMPLETED",
                        "CANCELLED",
                        "CANCELED",
                        "FAILED",
                        "REJECTED",
                        "REFUNDED",
                    }:
                        raise WoltInvalidPayloadError("Unknown order lifecycle")
                terminal = normalize_order_status(summary) in {"delivered", "cancelled", "failed"}
                if not is_active_order(summary) or terminal:
                    # Missing lifecycle data is not evidence of an idle account.
                    if not terminal:
                        raise WoltInvalidPayloadError("Missing order lifecycle")
                    continue
                oid = summary.get("purchase_id") or summary.get("order_id") or summary.get("id")
                if not isinstance(oid, (str, int)) or isinstance(oid, bool) or not str(oid):
                    raise WoltInvalidPayloadError("Missing order identity")
                oid = str(oid)
                if oid in orders:
                    raise WoltInvalidPayloadError("Duplicate order identity")
                details = await self.api.fetch_order_details(oid)
                orders[oid] = project(oid, summary, details)
        except WoltAuthenticationError as err:
            raise ConfigEntryAuthFailed("Wolt authentication failed") from err
        except WoltRateLimitError as err:
            self.update_interval = timedelta(
                seconds=max(options["active_interval"], err.retry_after)
            )
            await self._save(self.primary, self.notified)
            raise UpdateFailed("Wolt rate limit reached; waiting before retry") from err
        except WoltApiError as err:
            raise UpdateFailed("Unable to update Wolt orders") from err

        now = dt_util.utcnow()
        primary = choose(orders, self.primary)
        notified = {
            key: stamp
            for key, stamp in self.notified.items()
            if key in orders or stamp > now.timestamp() - RETENTION
        }
        batches = groups(orders, notified, now, options["eta_lead"], options["grouping_window"])
        for batch in batches:
            notified.update({o.order_id: now.timestamp() for o in batch})
        # Persist before firing: ordinary restarts cannot duplicate notifications.
        # A crash between this write and event delivery can lose an event (documented).
        try:
            await self._save(primary, notified)
        except OSError as err:
            raise UpdateFailed("Unable to persist Wolttaaja notification state") from err
        self.primary, self.notified = primary, notified
        for batch in batches:
            self.hass.bus.async_fire(
                EVENT,
                {
                    "order_count": len(batch),
                    "notification_window_minutes": options["eta_lead"],
                    "grouping_window_minutes": options["grouping_window"],
                    "orders": [o.as_dict() for o in batch],
                },
            )
        self.update_interval = timedelta(
            seconds=options["active_interval"] if orders else options["idle_interval"]
        )
        return orders
