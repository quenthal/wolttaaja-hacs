"""Small, privacy-filtered order model; parsers adapted from selfish (MIT)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


def _contains_non_negated_status(value: str, *fragments: str) -> bool:
    """Match fragments without treating negative status phrases as that state."""
    return any(
        fragment in value
        and re.search(
            rf"(?:^|_)(?:(?:not(?:_(?:yet|currently))?|non)_|un){re.escape(fragment)}",
            value,
        )
        is None
        for fragment in fragments
    )


def _raw_status(order: dict[str, Any]) -> str | None:
    """Extract a scalar status while respecting authoritative telemetry."""
    status_type: Any = None
    if "telemetry" in order:
        telemetry = order["telemetry"]
        status_type = telemetry.get("order_status_type") if isinstance(telemetry, dict) else None
        if str(status_type).upper() != "IN_PROGRESS":
            return str(status_type) if status_type is not None else None
    elif "order_status_type" in order:
        status_type = order["order_status_type"]
        if str(status_type).upper() != "IN_PROGRESS":
            return str(status_type) if status_type is not None else None

    status = order.get("status")
    if isinstance(status, dict):
        status = status.get("value") or status.get("text") or status.get("label")
    if status is None:
        status = status_type
    if str(status_type).upper() == "IN_PROGRESS" and status is not None:
        display_status = re.sub(r"[^a-z0-9]+", "_", str(status).strip().lower()).strip("_")
        if _contains_non_negated_status(
            display_status,
            "delivered",
            "completed",
            "finished",
            "cancel",
            "fail",
            "reject",
            "refund",
        ):
            return str(status_type)
    return str(status) if status is not None else None


def normalize_order_status(order: dict[str, Any]) -> str:
    """Map unstable Wolt status text to a fixed Home Assistant enum."""
    raw = _raw_status(order)
    if not raw:
        return "unknown"
    value = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    if _contains_non_negated_status(value, "cancel", "refunded"):
        return "cancelled"
    if _contains_non_negated_status(value, "fail", "reject", "declin"):
        return "failed"
    if _contains_non_negated_status(value, "delivered", "completed", "finished"):
        return "delivered"
    if _contains_non_negated_status(value, "arriv", "nearby", "almost_there"):
        return "arriving"
    if _contains_non_negated_status(value, "picked_up", "courier_pickup", "delivery_pickup"):
        return "picked_up"
    if _contains_non_negated_status(value, "ready", "awaiting_pickup"):
        return "ready_for_pickup"
    if _contains_non_negated_status(value, "prepar", "production", "restaurant"):
        return "preparing"
    if _contains_non_negated_status(
        value, "pending", "received", "created", "in_progress", "accepted"
    ):
        return "pending"
    if _contains_non_negated_status(
        value, "on_the_way", "en_route", "courier_delivery", "delivery"
    ):
        return "on_the_way"
    return "unknown"


def _parse_eta(value: Any) -> datetime | None:
    """Parse an ETA without guessing from human-readable duration text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("$date", "date", "value", "timestamp", "max", "end"):
            if key in value and (parsed := _parse_eta(value[key])) is not None:
                return parsed
        return None
    if isinstance(value, int | float):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        # Explicit ETAs must be plausible wall-clock timestamps. Small values
        # are durations/range bounds, not Unix timestamps.
        if not 1_577_836_800 <= timestamp <= 4_102_444_800:
            return None
        try:
            return datetime.fromtimestamp(timestamp, UTC)
        except OSError, OverflowError, ValueError:
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def extract_order_eta(order: dict[str, Any]) -> datetime | None:
    """Extract the first explicit timestamp-shaped ETA."""
    for key in ("delivery_eta", "estimated_delivery_time", "eta"):
        if (parsed := _parse_eta(order.get(key))) is not None:
            return parsed
    estimate = order.get("client_pre_estimate")
    if isinstance(estimate, dict):
        return _parse_eta(estimate.get("delivery_eta"))
    return None


def venue_name(order: dict[str, Any]) -> str | None:
    """Read only the order summary's canonical venue label, never customer text."""
    venue = order.get("venue")
    name = venue.get("name") if isinstance(venue, dict) else None
    if not isinstance(name, str) or any(ord(c) < 32 for c in name):
        return None
    name = name.strip()
    return name if name and len(name) <= 255 else None


@dataclass(frozen=True)
class Order:
    order_id: str
    restaurant: str | None
    status: str
    eta: datetime | None

    def as_dict(self):
        return {
            "order_id": self.order_id,
            "restaurant": self.restaurant,
            "status": self.status,
            "eta": self.eta.isoformat() if self.eta else None,
        }


def project(order_id, summary, details):
    """Project only allowlisted fields and prefer assigned courier ETA."""
    drivers = [d for d in details.get("_drivers", []) if isinstance(d, dict)]
    driver = next((d for d in drivers if d.get("delivering_your_order") is True), {})
    if not driver and len(drivers) == 1 and drivers[0].get("delivering_your_order") is not False:
        driver = drivers[0]
    merged = {**summary, **details}
    # The orders endpoint remains authoritative for lifecycle.
    for key in ("telemetry", "order_status_type"):
        if key in summary:
            merged[key] = summary[key]
    return Order(
        order_id,
        venue_name(summary) or venue_name(details),
        normalize_order_status(merged),
        extract_order_eta(driver) or extract_order_eta(details) or extract_order_eta(summary),
    )


def choose(orders, primary):
    """Keep primary sticky; known ETA first, stable ID breaks ties."""
    if primary in orders:
        return primary
    return min(
        orders,
        key=lambda key: (
            orders[key].eta is None,
            orders[key].eta or datetime.max.replace(tzinfo=UTC),
            key,
        ),
        default=None,
    )


def groups(orders, notified, now, lead, window):
    """Emit immediately, grouping by anchor ETA without transitive chaining."""
    pending = sorted(
        (o for o in orders.values() if o.order_id not in notified and o.eta and o.eta >= now),
        key=lambda o: (o.eta, o.order_id),
    )
    result = []
    while pending and pending[0].eta <= now + timedelta(minutes=lead):
        anchor = pending[0].eta
        group = [o for o in pending if o.eta - anchor <= timedelta(minutes=window)]
        result.append(group)
        pending = [o for o in pending if o not in group]
    return result
