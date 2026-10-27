"""Behavioral tests using actual Home Assistant infrastructure."""

import re
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wolttaaja.api import WoltConnectionError, WoltRateLimitError, retry_seconds
from custom_components.wolttaaja.coordinator import WolttaajaCoordinator
from custom_components.wolttaaja.models import Order, choose, groups, project

DATA = {"refresh_token": "test-refresh", "bearer_token": "test-access", "client_id": "test-client"}


def order(key, minutes=None, restaurant="Same restaurant"):
    return Order(
        key,
        restaurant,
        "on_the_way",
        dt_util.utcnow() + timedelta(minutes=minutes) if minutes is not None else None,
    )


def summary(key):
    return {
        "purchase_id": key,
        "telemetry": {"order_status_type": "IN_PROGRESS"},
        "venue": {"name": "Same restaurant"},
    }


def api_for(keys=()):
    api = AsyncMock()
    api.retry_at = 0
    api.fetch_orders.return_value = [summary(k) for k in keys]
    api.fetch_order_details.return_value = {"status": "delivery"}
    return api


async def coordinator(hass, api):
    entry = MockConfigEntry(domain="wolttaaja", data=DATA)
    entry.add_to_hass(hass)
    c = WolttaajaCoordinator(hass, entry, api)
    await c.async_load()
    return c


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_counts_polling_and_identity(hass, count):
    api = api_for([str(i) for i in range(count)])
    c = await coordinator(hass, api)
    data = await c._async_update_data()
    assert len(data) == count
    assert c.update_interval.total_seconds() == (30 if count else 300)
    assert api.fetch_orders.await_count == 1
    assert api.fetch_order_details.await_count == count
    if count:
        assert all(o.eta is None for o in data.values())


async def test_next_eta_changes_and_completion(hass):
    api = api_for(["a", "b"])
    now = dt_util.utcnow()
    api.fetch_order_details.side_effect = [
        {"delivery_eta": (now + timedelta(minutes=m)).isoformat()} for m in (20, 30, 40, 10, 9)
    ]
    c = await coordinator(hass, api)
    data = await c._async_update_data()
    assert choose(data) == "a"
    data = await c._async_update_data()
    assert choose(data) == "b"
    api.fetch_orders.return_value = [
        summary("b"),
        {"purchase_id": "a", "telemetry": {"order_status_type": "DELIVERED"}},
    ]
    data = await c._async_update_data()
    assert choose(data) == "b"


@pytest.mark.parametrize(
    "minutes,window,sizes",
    [
        ([4], 3, [1]),
        ([4, 6], 3, [2]),
        ([1, 5], 3, [1, 1]),
        ([4, 8], 3, [1]),
        ([4, 4], 0, [2]),
        ([4, 5], 0, [1, 1]),
    ],
)
def test_grouping(minutes, window, sizes):
    now = dt_util.utcnow()
    orders = {
        str(i): Order(str(i), "Same", "on_the_way", now + timedelta(minutes=m))
        for i, m in enumerate(minutes)
    }
    assert [len(g) for g in groups(orders, {}, now, 5, window)] == sizes
    assert groups(orders, orders, now, 5, window) == []


def test_missing_eta_does_not_notify():
    assert groups({"a": order("a")}, {}, dt_util.utcnow(), 5, 3) == []


async def test_eta_that_passed_between_polls_still_notifies_once(hass):
    """A courier ahead of schedule can jump the ETA past the window between polls."""
    api = api_for(["a"])
    c = await coordinator(hass, api)
    events = []
    hass.bus.async_listen("wolttaaja_delivery_approaching", lambda e: events.append(e.data))
    for minutes in (6, -0.5, -1):
        api.fetch_order_details.return_value = {
            "delivery_eta": (dt_util.utcnow() + timedelta(minutes=minutes)).isoformat()
        }
        await c._async_update_data()
    await hass.async_block_till_done()
    assert [e["orders"][0]["order_id"] for e in events] == ["a"]


async def test_restart_dedup_new_order_and_cleanup(hass):
    api = api_for(["a"])
    api.fetch_order_details.return_value = {
        "delivery_eta": (dt_util.utcnow() + timedelta(minutes=4)).isoformat()
    }
    c = await coordinator(hass, api)
    events = []
    hass.bus.async_listen("wolttaaja_delivery_approaching", lambda e: events.append(e.data))
    await c._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1
    restored = WolttaajaCoordinator(hass, c.entry, api)
    await restored.async_load()
    assert restored.order_sequence == ["a"]
    await restored._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1
    api.fetch_orders.return_value = [summary("a"), summary("b")]
    await restored._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 2
    assert events[-1]["orders"][0]["order_id"] == "b"
    restored.notified["old"] = dt_util.utcnow().timestamp() - 31 * 86400
    await restored._async_update_data()
    assert "old" not in restored.notified


async def test_eta_enters_window(hass):
    api = api_for(["a"])
    c = await coordinator(hass, api)
    events = []
    hass.bus.async_listen("wolttaaja_delivery_approaching", lambda e: events.append(e))
    for minutes in (9, 4, 3):
        api.fetch_order_details.return_value = {
            "delivery_eta": (dt_util.utcnow() + timedelta(minutes=minutes)).isoformat()
        }
        await c._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1


@pytest.mark.parametrize("error", [WoltConnectionError("failed"), WoltRateLimitError(120)])
async def test_api_failure(hass, error):
    api = api_for()
    api.fetch_orders.side_effect = error
    c = await coordinator(hass, api)
    with pytest.raises(UpdateFailed):
        await c._async_update_data()
    if isinstance(error, WoltRateLimitError):
        assert c.update_interval.total_seconds() == 120


async def test_three_real_entities_unavailable_options_and_unload(hass):
    api = api_for(["a", "b"])
    entry = MockConfigEntry(domain="wolttaaja", data=DATA)
    entry.add_to_hass(hass)
    with patch("custom_components.wolttaaja.WoltApi", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.wolttaaja_active_orders").state == "2"
        assert hass.states.get("sensor.wolttaaja_next_delivery").state == "unknown"
        assert hass.states.get("binary_sensor.wolttaaja_delivery_active") is None
        assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == 3
        attrs = hass.states.get("sensor.wolttaaja_active_orders").attributes["orders"]
        assert {o["order_id"] for o in attrs} == {"a", "b"}
        api.fetch_orders.side_effect = WoltConnectionError("failed")
        await entry.runtime_data.async_refresh()
        assert hass.states.get("sensor.wolttaaja_active_orders").state == "unavailable"
        api.fetch_orders.side_effect = None
        api.fetch_orders.return_value = []
        await entry.runtime_data.async_refresh()
        assert hass.states.get("sensor.wolttaaja_active_orders").state == "0"
        hass.config_entries.async_update_entry(entry, options={"idle_interval": 600})
        await hass.async_block_till_done()
        assert entry.runtime_data.update_interval.total_seconds() == 600
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_flow_and_options(hass):
    with patch("custom_components.wolttaaja.config_flow.WoltApi", return_value=api_for()):
        result = await hass.config_entries.flow.async_init("wolttaaja", context={"source": "user"})
        with patch("custom_components.wolttaaja.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={"refresh_token": "test-refresh"}
            )
            await hass.async_block_till_done()
        entry = result["result"]
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "idle_interval": 600,
                "active_interval": 45,
                "eta_lead": 7,
                "grouping_window": 2,
            },
        )
        assert result["data"]["active_interval"] == 45


@pytest.mark.parametrize(
    "value,expected", [("120", 120), (None, 60), ("bad", 60), ("NaN", 60), ("-1", 60)]
)
def test_retry_after(value, expected):
    assert retry_seconds(value) == expected


def test_projection_privacy():
    result = project(
        "a", summary("a"), {"address": "PRIVATE", "items": ["PRIVATE"], "status": "delivery"}
    )
    assert set(result.as_dict()) == {"order_id", "restaurant", "status", "eta"}
    assert "PRIVATE" not in str(result)


async def test_storage_failure_does_not_fire_or_mark(hass):
    api = api_for(["a"])
    api.fetch_order_details.return_value = {
        "eta": (dt_util.utcnow() + timedelta(minutes=4)).isoformat()
    }
    c = await coordinator(hass, api)
    events = []
    hass.bus.async_listen("wolttaaja_delivery_approaching", lambda e: events.append(e))
    with patch.object(c.store, "async_save", side_effect=OSError("disk failure")):
        with pytest.raises(UpdateFailed):
            await c._async_update_data()
    await hass.async_block_till_done()
    assert events == [] and c.notified == {}
    await c._async_update_data()
    await hass.async_block_till_done()
    assert len(events) == 1


UNKNOWN_LIFECYCLES = [
    {"telemetry": {}},
    {"telemetry": None},
    {"order_status_type": "NEW_UNKNOWN"},
    {"telemetry": {"order_status_type": "SCHEDULED"}},
    {},
]


@pytest.mark.parametrize("payload", UNKNOWN_LIFECYCLES)
async def test_unknown_lifecycle_of_untracked_order_is_ignored(hass, payload):
    """An unreadable old or unfamiliar order must not take every sensor down."""
    api = api_for(["a"])
    api.fetch_orders.return_value = [summary("a"), {"purchase_id": "old", **payload}]
    c = await coordinator(hass, api)
    assert list(await c._async_update_data()) == ["a"]


@pytest.mark.parametrize("payload", UNKNOWN_LIFECYCLES)
async def test_unknown_lifecycle_of_tracked_order_is_unavailable(hass, payload):
    """Unreadable state of a tracked order is not evidence that it completed."""
    api = api_for(["a"])
    c = await coordinator(hass, api)
    await c._async_update_data()
    api.fetch_orders.return_value = [{"purchase_id": "a", **payload}]
    with pytest.raises(UpdateFailed):
        await c._async_update_data()
    assert c.order_sequence == ["a"]


async def test_rate_limit_survives_restart(hass):
    from time import monotonic

    api = api_for()
    api.retry_at = monotonic() + 120
    api.fetch_orders.side_effect = WoltRateLimitError(120)
    c = await coordinator(hass, api)
    with pytest.raises(UpdateFailed):
        await c._async_update_data()
    other = api_for()
    restored = WolttaajaCoordinator(hass, c.entry, other)
    await restored.async_load()
    assert 115 < other.retry_at - monotonic() <= 120


async def test_real_reauth_and_token_updates_do_not_repoll(hass):
    api = api_for()
    entry = MockConfigEntry(domain="wolttaaja", data=DATA)
    entry.add_to_hass(hass)
    with patch("custom_components.wolttaaja.WoltApi", return_value=api) as factory:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        initial = api.fetch_orders.await_count
        await factory.call_args.kwargs["token_update_callback"]("rotated-access", "rotated-refresh")
        await hass.async_block_till_done()
        assert entry.data["refresh_token"] == "rotated-refresh"
        assert api.fetch_orders.await_count == initial
        with patch("custom_components.wolttaaja.config_flow.WoltApi", return_value=api_for()):
            result = await hass.config_entries.flow.async_init(
                "wolttaaja",
                context={"source": "reauth", "entry_id": entry.entry_id},
                data=entry.data,
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={"refresh_token": "replacement"}
            )
            await hass.async_block_till_done()
            assert result["reason"] == "reauth_successful"
            assert entry.data["refresh_token"] == "replacement"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_recorder_attributes_declared(hass):
    from custom_components.wolttaaja.sensor import WolttaajaSensor

    c = await coordinator(hass, api_for())
    sensor = WolttaajaSensor(c, "active_orders")
    assert {"orders", "restaurant", "order_id", "status", "eta"} <= sensor._unrecorded_attributes


def test_grouping_is_not_transitive():
    now = dt_util.utcnow()
    orders = {
        str(i): Order(str(i), "Same", "pending", now + timedelta(minutes=m))
        for i, m in enumerate([4, 7, 10])
    }
    assert [[o.order_id for o in batch] for batch in groups(orders, {}, now, 5, 3)] == [["0", "1"]]


def test_restaurant_from_detail_payload():
    assert project("a", {}, {"venue_name": "Known venue"}).restaurant == "Known venue"


async def test_completed_legacy_order_with_stale_tracking_link(hass):
    api = api_for()
    api.fetch_orders.return_value = [
        {"purchase_id": "a", "status": "completed", "call_to_action": {"type": "ORDER_TRACKING"}}
    ]
    c = await coordinator(hass, api)
    assert await c._async_update_data() == {}
    api.fetch_order_details.assert_not_awaited()


async def test_three_order_selection_list_order_restart_and_completion(hass):
    """ETA selection changes together; first-seen list order never follows ETA."""
    api = api_for(["z"])
    now = dt_util.utcnow()
    minutes = {"z": 20, "b": 30, "a": 40}
    statuses = {"z": "preparing", "b": "ready", "a": "delivery"}

    async def details(oid):
        return {
            "delivery_eta": (now + timedelta(minutes=minutes[oid])).isoformat(),
            "status": statuses[oid],
        }

    api.fetch_order_details.side_effect = details
    entry = MockConfigEntry(domain="wolttaaja", data=DATA)
    entry.add_to_hass(hass)

    def check(selected, sequence):
        eta = hass.states.get("sensor.wolttaaja_next_delivery")
        status = hass.states.get("sensor.wolttaaja_order_status")
        count = hass.states.get("sensor.wolttaaja_active_orders")
        assert eta.attributes["order_id"] == status.attributes["order_id"] == selected
        for key in ("order_id", "restaurant", "status", "eta"):
            assert eta.attributes[key] == status.attributes[key]
        assert count.state == str(len(sequence))
        assert [o["order_id"] for o in count.attributes["orders"]] == sequence
        if selected:
            assert (
                eta.state
                == (now + timedelta(minutes=minutes[selected])).replace(microsecond=0).isoformat()
            )
            assert status.state == status.attributes["status"]
        else:
            assert eta.state == status.state == "unknown"

    with patch("custom_components.wolttaaja.WoltApi", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        check("z", ["z"])
        # Newest-first API response must append newcomers after already seen z.
        api.fetch_orders.return_value = [summary("b"), summary("a"), summary("z")]
        await entry.runtime_data.async_refresh()
        check("z", ["z", "b", "a"])
        minutes.update(a=10, b=45)
        api.fetch_orders.return_value = [summary("a"), summary("z"), summary("b")]
        await entry.runtime_data.async_refresh()
        check("a", ["z", "b", "a"])
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        check("a", ["z", "b", "a"])
        api.fetch_orders.return_value = [
            summary("b"),
            summary("z"),
            {"purchase_id": "a", "telemetry": {"order_status_type": "DELIVERED"}},
        ]
        await entry.runtime_data.async_refresh()
        check("z", ["z", "b"])
        api.fetch_orders.return_value = [summary("b")]
        await entry.runtime_data.async_refresh()
        check("b", ["b"])
        api.fetch_orders.return_value = [
            {"purchase_id": "b", "telemetry": {"order_status_type": "CANCELLED"}}
        ]
        await entry.runtime_data.async_refresh()
        check(None, [])
        assert entry.runtime_data.order_sequence == []
        assert entry.runtime_data.update_interval.total_seconds() == 300
        await hass.config_entries.async_unload(entry.entry_id)


async def test_upgrade_removes_only_owned_retired_entity(hass):
    entry = MockConfigEntry(domain="wolttaaja", data=DATA)
    other = MockConfigEntry(domain="wolttaaja", data=DATA, disabled_by=ConfigEntryDisabler.USER)
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "binary_sensor",
        "wolttaaja",
        f"{entry.entry_id}_delivery_active",
        config_entry=entry,
        suggested_object_id="renamed_delivery",
    )
    foreign = registry.async_get_or_create(
        "binary_sensor", "wolttaaja", f"{other.entry_id}_delivery_active", config_entry=other
    )
    with patch("custom_components.wolttaaja.WoltApi", return_value=api_for()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(old.entity_id) is None
        assert registry.async_get(foreign.entity_id) is not None
        assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 3
        assert entry.minor_version == 2
        await hass.config_entries.async_unload(entry.entry_id)


async def test_finnish_names_keep_english_entity_ids(hass):
    hass.config.language = "fi"
    api = api_for(["a"])
    api.fetch_order_details.return_value = {"status": "preparing"}
    entry = MockConfigEntry(domain="wolttaaja", data=DATA, minor_version=2)
    entry.add_to_hass(hass)
    with patch("custom_components.wolttaaja.WoltApi", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        status = hass.states.get("sensor.wolttaaja_order_status")
        assert status.state == "preparing"
        assert status.attributes["friendly_name"] == "Wolttaaja Tilauksen tila"
        assert "ready_for_pickup" in status.attributes["options"]
        assert hass.states.get("sensor.wolttaaja_next_delivery") is not None
        assert hass.states.get("sensor.wolttaaja_active_orders").state == "1"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_old_store_preserves_dedup_and_discards_primary(hass):
    api = api_for(["a"])
    api.fetch_order_details.return_value = {
        "eta": (dt_util.utcnow() + timedelta(minutes=4)).isoformat()
    }
    c = await coordinator(hass, api)
    await c.store.async_save(
        {"primary": "old", "notified": {"a": dt_util.utcnow().timestamp()}, "retry_until": 0}
    )
    await c.async_load()
    events = []
    hass.bus.async_listen("wolttaaja_delivery_approaching", lambda e: events.append(e))
    await c._async_update_data()
    await hass.async_block_till_done()
    assert events == []
    saved = await c.store.async_load()
    assert "primary" not in saved
    assert saved["order_sequence"] == ["a"]
    assert "a" in saved["notified"]


def test_selection_missing_eta_and_ties_is_shared():
    from types import SimpleNamespace

    from custom_components.wolttaaja.sensor import WolttaajaSensor

    orders = {"z": order("z"), "a": order("a")}
    coordinator = SimpleNamespace(data=orders, entry=SimpleNamespace(entry_id="test"))
    eta = WolttaajaSensor(coordinator, "next_delivery")
    status = WolttaajaSensor(coordinator, "order_status")
    assert eta.selected == status.selected == orders["a"]
    assert eta.native_value is None
    orders["z"] = order("z", 20)
    assert eta.selected == status.selected == orders["z"]
    orders["a"] = Order("a", "Same", "pending", orders["z"].eta)
    assert eta.selected == status.selected == orders["a"]


def test_unknown_status_is_not_an_enum_option():
    from types import SimpleNamespace

    from custom_components.wolttaaja.sensor import WolttaajaSensor

    orders = {"a": Order("a", "Same", "unknown", None)}
    coordinator = SimpleNamespace(data=orders, entry=SimpleNamespace(entry_id="test"))
    status = WolttaajaSensor(coordinator, "order_status")
    assert "unknown" not in status.options
    assert status.native_value is None
    assert status.extra_state_attributes["status"] == "unknown"


@pytest.mark.parametrize(
    "shift,threshold,expected",
    [(10, 10, True), (9, 10, False), (-15, 10, False), (30, 0, False)],
)
def test_delay_detection(shift, threshold, expected):
    from custom_components.wolttaaja.models import delays

    now = dt_util.utcnow()
    orders = {"a": Order("a", "Same", "on_the_way", now + timedelta(minutes=shift))}
    found = delays(orders, {"a": now.isoformat()}, threshold)
    assert [o.order_id for o, _ in found] == (["a"] if expected else [])
    assert delays(orders, {}, threshold) == []


async def test_delay_event_after_arrival_event(hass):
    """A slipped ETA after notifying fires one delayed event; small jitter does not."""
    api = api_for(["a"])
    c = await coordinator(hass, api)
    arrivals, delayed = [], []
    hass.bus.async_listen("wolttaaja_delivery_approaching", lambda e: arrivals.append(e.data))
    hass.bus.async_listen("wolttaaja_delivery_delayed", lambda e: delayed.append(e.data))
    base = dt_util.utcnow()

    async def poll(minutes, target=c):
        api.fetch_order_details.return_value = {
            "delivery_eta": (base + timedelta(minutes=minutes)).isoformat()
        }
        await target._async_update_data()
        await hass.async_block_till_done()

    await poll(20)  # Not yet announced: moving it is not a delay.
    await poll(32)
    assert delayed == []
    await poll(4)
    assert len(arrivals) == 1
    await poll(8)
    assert delayed == []
    await poll(16)
    assert len(delayed) == 1
    event = delayed[0]
    assert event["order_count"] == 1 and event["delay_threshold_minutes"] == 10
    assert event["orders"][0]["delay_minutes"] == 12
    assert event["orders"][0]["previous_eta"] == (base + timedelta(minutes=4)).isoformat()
    await poll(20)  # Measured from the last event's ETA (16), so no repeat.
    assert len(delayed) == 1

    restored = WolttaajaCoordinator(hass, c.entry, api)
    await restored.async_load()
    await poll(20, restored)
    assert len(delayed) == 1 and len(arrivals) == 1
    await poll(27, restored)
    assert len(delayed) == 2

    api.fetch_orders.return_value = []
    await restored._async_update_data()
    assert restored.announced == {}


async def test_delay_events_can_be_disabled(hass):
    api = api_for(["a"])
    entry = MockConfigEntry(domain="wolttaaja", data=DATA, options={"delay_threshold": 0})
    entry.add_to_hass(hass)
    c = WolttaajaCoordinator(hass, entry, api)
    await c.async_load()
    delayed = []
    hass.bus.async_listen("wolttaaja_delivery_delayed", lambda e: delayed.append(e))
    for minutes in (4, 40):
        api.fetch_order_details.return_value = {
            "delivery_eta": (dt_util.utcnow() + timedelta(minutes=minutes)).isoformat()
        }
        await c._async_update_data()
    await hass.async_block_till_done()
    assert delayed == []


PRIVATE = {
    "purchase_id": "a",
    "telemetry": {"order_status_type": "IN_PROGRESS"},
    "venue": {"name": "Secret Venue"},
    "delivery_location": {"address": "Secret Street 1", "coordinates": [24.9, 60.1]},
}


def private_details(status, **extra):
    return {
        "status": status,
        "status_text": "Courier Secretname is on the way",
        "payment_method_id": "secret-payment-id",
        "items": {"secret-item-id": {"name": "Secret dish", "state": "ok"}},
        "_drivers": [
            {"delivering_your_order": True, "name": "Secretname", "location": [60.2, 24.8]}
        ],
        **extra,
    }


async def test_diagnostics_timeline_is_private_and_outlives_the_order(hass):
    from custom_components.wolttaaja.diagnostics import async_get_config_entry_diagnostics

    api = api_for()
    api.fetch_orders.return_value = [PRIVATE]
    api.fetch_order_details.return_value = private_details("ready")
    entry = MockConfigEntry(domain="wolttaaja", data=DATA, minor_version=2)
    entry.add_to_hass(hass)
    with patch("custom_components.wolttaaja.WoltApi", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        c = entry.runtime_data
        await c.async_refresh()  # Unchanged signals add no event.
        api.fetch_order_details.return_value = private_details(
            "ready",
            delivery_status="courier_en_route",
            delivery_eta=(dt_util.utcnow() + timedelta(minutes=20)).isoformat(),
            eta_range={"min": 5, "max": 15},
        )
        await c.async_refresh()
        api.fetch_orders.return_value = [
            {"purchase_id": "a", "telemetry": {"order_status_type": "DELIVERED"}}
        ]
        await c.async_refresh()
        result = await async_get_config_entry_diagnostics(hass, entry)
        await hass.config_entries.async_unload(entry.entry_id)

    text = str(result)
    for secret in ("Secret", "secret", "24.9", "60.2", "'a'", "test-refresh", "test-access"):
        assert secret not in text
    assert result["active_orders"] == []
    [timeline] = result["status_timelines"]
    assert timeline["label"] == "order 1"
    events = timeline["events"]
    assert [e["status"] for e in events] == ["ready_for_pickup", "ready_for_pickup"]
    assert events[0]["signals"]["details.status"] == "ready"
    assert events[0]["signals"]["details.status_text"] == "<text 32>"
    assert events[0]["signals"]["drivers.delivering_your_order"] == [True]
    assert "details.payment_method_id" not in events[0]["signals"]
    assert events[1]["signals"]["details.delivery_status"] == "courier_en_route"
    assert events[0]["at"] == "t+0.0 min"
    assert events[1]["eta"] == events[1]["signals"]["eta_source.details"]
    assert events[1]["eta"].startswith(("t+19.", "t+20."))
    assert events[1]["signals"]["details.eta_range.min"] == 5
    assert events[1]["signals"]["details.eta_range.max"] == 15
    assert re.search(r"\d{4}-\d{2}-\d{2}", str(result["status_timelines"])) is None
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", events[0]["clock"])
    assert "details.items.<key>.state" in timeline["paths"]


async def test_unknown_status_is_logged_once(hass, caplog):
    api = api_for(["a"])
    api.fetch_order_details.return_value = {"status": "Brand New Phase"}
    c = await coordinator(hass, api)
    for _ in range(2):
        data = await c._async_update_data()
    assert data["a"].status == "unknown"
    assert caplog.text.count("Unrecognized Wolt order status brand_new_phase") == 1


async def test_trace_survives_restart_and_logs_changes(hass, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.wolttaaja")
    api = api_for(["a"])
    api.fetch_order_details.return_value = {"status": "production"}
    c = await coordinator(hass, api)
    await c._async_update_data()
    await c._async_update_data()  # Unchanged: no new event, no log line.
    assert caplog.text.count("Status trace") == 1
    await c.trace_store.async_save(c.trace.to_storage())

    restored = WolttaajaCoordinator(hass, c.entry, api)
    await restored.async_load()
    api.fetch_order_details.return_value = {"status": "ready"}
    await restored._async_update_data()
    [timeline] = restored.trace.as_dict()
    assert timeline["label"] == "order 1"
    assert [e["status"] for e in timeline["events"]] == ["preparing", "ready_for_pickup"]
