"""One polling coordinator and durable, bounded notification state."""

from __future__ import annotations

import asyncio
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
)
from .const import DEFAULTS, DELAYED_EVENT, DOMAIN, EVENT
from .models import delays, groups, lifecycle, lifecycle_value, order_identity, project

_LOGGER = logging.getLogger(__name__)
RETENTION = 30 * 86400


class WolttaajaCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api):
        self.options = DEFAULTS | dict(entry.options)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=self.options["idle_interval"]),
            always_update=False,
        )
        self.entry = entry
        self.api = api
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.order_sequence = []
        self.notified = {}
        # ETA last told to the user per active order, as ISO text.
        self.announced = {}
        self._saved = None
        self._ignored_lifecycles = set()

    async def async_apply_options(self):
        """Refresh only when options changed; token rotation also updates the entry."""
        options = DEFAULTS | dict(self.entry.options)
        if options != self.options:
            self.options = options
            await self.async_request_refresh()

    async def async_load(self):
        saved = await self.store.async_load()
        if saved:
            self.order_sequence = saved.get("order_sequence", [])
            self.notified = saved.get("notified", {})
            self.announced = saved.get("announced", {})
            # A reload/restart must not bypass server-requested cooldown.
            remaining = saved.get("retry_until", 0) - dt_util.utcnow().timestamp()
            self.api.retry_at = monotonic() + max(0, remaining)
            self._saved = saved

    async def _save(self, order_sequence, notified, announced):
        remaining = max(0, self.api.retry_at - monotonic())
        saved = {
            "order_sequence": order_sequence,
            "notified": notified,
            "announced": announced,
            "retry_until": dt_util.utcnow().timestamp() + remaining if remaining else 0,
        }
        if saved != self._saved:
            await self.store.async_save(saved)
            self._saved = saved

    def _active_summaries(self, summaries):
        """Select active orders; unreadable lifecycle only fails for tracked orders."""
        active = {}
        for summary in summaries:
            state = lifecycle(summary)
            oid = order_identity(summary)
            if state == "terminal":
                continue
            if state is None:
                # Not being able to read a tracked order is not evidence it ended.
                if oid in self.order_sequence:
                    raise WoltInvalidPayloadError("Unknown lifecycle for a tracked order")
                self._ignore_unknown(summary)
                continue
            if oid is None:
                raise WoltInvalidPayloadError("Missing order identity")
            if oid in active:
                raise WoltInvalidPayloadError("Duplicate order identity")
            active[oid] = summary
        return active

    def _ignore_unknown(self, summary):
        value = str(lifecycle_value(summary)[1])[:40]
        if value not in self._ignored_lifecycles:
            self._ignored_lifecycles.add(value)
            _LOGGER.warning("Ignoring Wolt order with unrecognized lifecycle %s", value)

    async def _async_update_data(self):
        options = self.options
        try:
            active = self._active_summaries(await self.api.fetch_orders())
            details = await asyncio.gather(
                *(self.api.fetch_order_details(oid) for oid in active), return_exceptions=True
            )
            for result in details:
                if isinstance(result, BaseException):
                    raise result
            orders = {
                oid: project(oid, summary, detail)
                for (oid, summary), detail in zip(active.items(), details, strict=True)
            }
        except WoltAuthenticationError as err:
            raise ConfigEntryAuthFailed("Wolt authentication failed") from err
        except WoltRateLimitError as err:
            self.update_interval = timedelta(
                seconds=max(options["active_interval"], err.retry_after)
            )
            await self._save(self.order_sequence, self.notified, self.announced)
            raise UpdateFailed("Wolt rate limit reached; waiting before retry") from err
        except WoltApiError as err:
            raise UpdateFailed("Unable to update Wolt orders") from err

        now = dt_util.utcnow()
        # Keep first-seen order across ETA changes, API response reordering and restarts.
        # Only active IDs persist. New IDs from the same poll use response order.
        sequence = [oid for oid in self.order_sequence if oid in orders]
        known = set(sequence)
        sequence.extend(oid for oid in orders if oid not in known)
        orders = {oid: orders[oid] for oid in sequence}
        notified = {
            key: stamp
            for key, stamp in self.notified.items()
            if key in orders or stamp > now.timestamp() - RETENTION
        }
        announced = {key: eta for key, eta in self.announced.items() if key in orders}
        delayed = delays(orders, announced, options["delay_threshold"])
        batches = groups(orders, notified, now, options["eta_lead"], options["grouping_window"])
        for batch in batches:
            notified.update({o.order_id: now.timestamp() for o in batch})
        # Later delays are measured from the ETA in the most recent event.
        for order in [o for o, _ in delayed] + [o for batch in batches for o in batch]:
            announced[order.order_id] = order.eta.isoformat()
        # Persist before firing: ordinary restarts cannot duplicate notifications.
        # A crash between this write and event delivery can lose an event (documented).
        try:
            await self._save(sequence, notified, announced)
        except OSError as err:
            raise UpdateFailed("Unable to persist Wolttaaja notification state") from err
        self.order_sequence, self.notified, self.announced = sequence, notified, announced
        if delayed:
            self.hass.bus.async_fire(
                DELAYED_EVENT,
                {
                    "order_count": len(delayed),
                    "delay_threshold_minutes": options["delay_threshold"],
                    "orders": [
                        {
                            **o.as_dict(),
                            "previous_eta": previous.isoformat(),
                            "delay_minutes": round((o.eta - previous).total_seconds() / 60),
                        }
                        for o, previous in delayed
                    ],
                },
            )
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
