"""Behavioral tests using actual Home Assistant infrastructure."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
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


async def test_primary_eta_changes_and_completion(hass):
    api = api_for(["a", "b"])
    now = dt_util.utcnow()
    api.fetch_order_details.side_effect = [
        {"delivery_eta": (now + timedelta(minutes=m)).isoformat()} for m in (20, 30, 40, 10, 9)
    ]
    c = await coordinator(hass, api)
    await c._async_update_data()
    assert c.primary == "a"
    data = await c._async_update_data()
    assert c.primary == "a"
    assert choose(data, None) == "b"
    api.fetch_orders.return_value = [
        summary("b"),
        {"purchase_id": "a", "telemetry": {"order_status_type": "DELIVERED"}},
    ]
    await c._async_update_data()
    assert c.primary == "b"


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


def test_missing_and_past_eta_do_not_notify():
    orders = {"a": order("a"), "b": order("b", -20)}
    assert groups(orders, {}, dt_util.utcnow(), 5, 3) == []


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
    assert restored.primary == "a"
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


async def test_four_real_entities_unavailable_options_and_unload(hass):
    api = api_for(["a", "b"])
    entry = MockConfigEntry(domain="wolttaaja", data=DATA)
    entry.add_to_hass(hass)
    with patch("custom_components.wolttaaja.WoltApi", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.wolttaaja_active_orders").state == "2"
        assert hass.states.get("sensor.wolttaaja_next_delivery").state == "unknown"
        assert hass.states.get("binary_sensor.wolttaaja_delivery_active").state == "on"
        assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == 4
        attrs = hass.states.get("sensor.wolttaaja_active_orders").attributes["orders"]
        assert {o["order_id"] for o in attrs} == {"a", "b"}
        api.fetch_orders.side_effect = WoltConnectionError("failed")
        await entry.runtime_data.async_refresh()
        assert hass.states.get("binary_sensor.wolttaaja_delivery_active").state == "unavailable"
        api.fetch_orders.side_effect = None
        api.fetch_orders.return_value = []
        await entry.runtime_data.async_refresh()
        assert hass.states.get("binary_sensor.wolttaaja_delivery_active").state == "off"
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


@pytest.mark.parametrize(
    "payload", [{"telemetry": {}}, {"telemetry": None}, {"order_status_type": "NEW_UNKNOWN"}, {}]
)
async def test_unknown_lifecycle_is_unavailable(hass, payload):
    api = api_for()
    api.fetch_orders.return_value = [payload]
    c = await coordinator(hass, api)
    with pytest.raises(UpdateFailed):
        await c._async_update_data()


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
