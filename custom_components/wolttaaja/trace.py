"""In-memory, privacy-filtered status timelines for diagnostics.

Wolt's tracking payload is undocumented. To learn which fields drive the
lifecycle without storing personal data, only status-like scalar values are
kept (by key name), timestamps under time-like keys as minutes since the
order was first seen, plus the key layout without values. Each event also
carries the local time of day (no date) for comparison with Wolt's app.
Order IDs, names, addresses, items and coordinates never enter a trace.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .models import _parse_eta, eta_candidates

# Keys are matched by whole words ("details" must not match "eta").
SIGNAL_WORDS = frozenset({"status", "state", "stage", "phase", "step", "type", "method", "mode"})
TIME_WORDS = frozenset(
    {"eta", "estimate", "estimated", "time", "at", "deadline", "window", "range", "min", "max"}
)
SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
# Enum-like tokens only: free text could name the courier.
TOKEN = re.compile(r"^[A-Za-z0-9_.:+-]{0,60}$")
MAX_DEPTH = 5
MAX_PATHS = 300
MAX_EVENTS = 200
MAX_ORDERS = 5


def _words(key: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", key)}


def _key(key: Any) -> str:
    # Dict keys can themselves be identifiers (maps keyed by ID).
    return key if isinstance(key, str) and SAFE_KEY.match(key) else "<key>"


def _minutes(when: datetime | None, origin: datetime) -> float | None:
    return None if when is None else round((when - origin).total_seconds() / 60, 1)


def _walk(value, path, depth, timed, origin, paths, signals) -> None:
    if depth > MAX_DEPTH or len(paths) >= MAX_PATHS:
        return
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = _key(raw_key)
            child_path = f"{path}.{key}" if path else key
            paths.add(child_path)
            words = _words(key) if key != "<key>" else set()
            named = bool(words) and "id" not in words and not key.lower().endswith("id")
            child_timed = timed or (named and bool(words & TIME_WORDS))
            when = None if isinstance(child, dict) else _parse_eta(child)
            if child_timed and when is not None:
                signals[child_path] = f"t{_minutes(when, origin):+} min"
            elif named and (child_timed or words & SIGNAL_WORDS):
                # Under time-like keys, plain numbers are durations such as 5-15 min.
                if child is None or isinstance(child, bool | int | float):
                    signals[child_path] = child
                elif isinstance(child, str):
                    signals[child_path] = child if TOKEN.match(child) else f"<text {len(child)}>"
            _walk(child, child_path, depth + 1, child_timed, origin, paths, signals)
    elif isinstance(value, list) and value:
        # Lists are summarized by their first element only.
        _walk(value[0], f"{path}[]", depth + 1, timed, origin, paths, signals)


def snapshot(summary, details, origin) -> tuple[dict, list[str]]:
    """Return status-like and time signals, and the value-free key layout of one order."""
    paths: set[str] = set()
    signals: dict[str, Any] = {}
    _walk({"summary": summary, "details": details}, "", 0, False, origin, paths, signals)
    drivers = [d for d in details.get("_drivers") or [] if isinstance(d, dict)]
    signals["drivers.count"] = len(drivers)
    signals["drivers.delivering_your_order"] = [d.get("delivering_your_order") for d in drivers]
    for source, eta in eta_candidates(summary, details).items():
        signals[f"eta_source.{source}"] = None if eta is None else f"t{_minutes(eta, origin):+} min"
    return signals, sorted(paths)


class StatusTrace:
    """Bounded per-order timelines; the integration persists them locally."""

    def __init__(self) -> None:
        self._orders: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._labels = 0

    def load(self, data) -> None:
        if not isinstance(data, dict):
            return
        self._labels = data.get("labels", 0)
        for oid, entry in data.get("orders", {}).items():
            self._orders[oid] = {**entry, "origin": datetime.fromisoformat(entry["origin"])}

    def to_storage(self) -> dict[str, Any]:
        return {
            "labels": self._labels,
            "orders": {
                oid: {**entry, "origin": entry["origin"].isoformat()}
                for oid, entry in self._orders.items()
            },
        }

    def record(self, oid, now, order, summary, details) -> dict[str, Any] | None:
        """Append an event when anything changed; return it for debug logging."""
        entry = self._orders.get(oid)
        if entry is None:
            self._labels += 1
            entry = {"label": f"order {self._labels}", "origin": now, "events": [], "paths": []}
            self._orders[oid] = entry
            while len(self._orders) > MAX_ORDERS:
                self._orders.popitem(last=False)
        origin = entry["origin"]
        signals, paths = snapshot(summary, details, origin)
        entry["paths"] = paths
        last = entry["events"][-1] if entry["events"] else None
        eta = _minutes(order.eta, origin)
        state = {
            "status": order.status,
            "eta": None if eta is None else f"t{eta:+} min",
            "signals": signals,
        }
        if last is not None and {k: last[k] for k in state} == state:
            return None
        event = {
            "at": f"t{_minutes(now, origin):+} min",
            "clock": dt_util.as_local(now).strftime("%H:%M:%S"),
            **state,
        }
        entry["events"].append(event)
        del entry["events"][:-MAX_EVENTS]
        return {"label": entry["label"], **event}

    def as_dict(self) -> list[dict[str, Any]]:
        """Times are minutes since the order was first seen (t+0.0 min)."""
        return [
            {key: value for key, value in entry.items() if key != "origin"}
            for entry in self._orders.values()
        ]
