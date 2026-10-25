"""Order-list lifecycle classification shared by the coordinator."""

from typing import Any

import pytest

from custom_components.wolttaaja.models import lifecycle


@pytest.mark.parametrize(
    ("status_type", "expected"),
    [
        ("IN_PROGRESS", "active"),
        ("in_progress", "active"),
        ("DELIVERED", "terminal"),
        ("COMPLETED", "terminal"),
        ("CANCELED", "terminal"),
        ("REFUNDED", "terminal"),
        ("PENDING", None),
        ("SCHEDULED", None),
        ("", None),
    ],
)
def test_telemetry_is_authoritative(status_type: str, expected: str | None) -> None:
    """Legacy hints never override a present telemetry value."""
    order = {
        "telemetry": {"order_status_type": status_type},
        "call_to_action": {"type": "ORDER_TRACKING"},
        "status": {"value": "Preparing"},
    }
    assert lifecycle(order) == expected


@pytest.mark.parametrize("telemetry", [{}, None, [], {"order_status_type": None}])
def test_present_incomplete_telemetry_is_unknown(telemetry: Any) -> None:
    """A malformed telemetry object must not reach the legacy heuristics."""
    order = {"telemetry": telemetry, "call_to_action": {"type": "ORDER_TRACKING"}}
    assert lifecycle(order) is None


@pytest.mark.parametrize(
    ("status_type", "expected"),
    [("IN_PROGRESS", "active"), ("COMPLETED", "terminal"), (None, None)],
)
def test_legacy_top_level_status_is_authoritative(
    status_type: str | None, expected: str | None
) -> None:
    order = {"order_status_type": status_type, "call_to_action": {"type": "ORDER_TRACKING"}}
    assert lifecycle(order) == expected


def test_legacy_tracking_link_is_active_unless_status_is_final() -> None:
    link = {"type": "ORDER_TRACKING"}
    assert lifecycle({"call_to_action": link}) == "active"
    assert lifecycle({"call_to_action": link, "status": "completed"}) == "terminal"
    assert lifecycle({}) is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Delivered", "terminal"),
        ("Completed", "terminal"),
        ("Finished", "terminal"),
        ("Declined", "terminal"),
        ("Refunded", "terminal"),
        ("not delivered", "active"),
        ("not yet delivered", "active"),
        ("undelivered", "active"),
        ("delivery not yet completed", "active"),
        ("cancelled", "terminal"),
        ("not currently cancelled", "active"),
    ],
)
def test_legacy_status_text_respects_negation(status: str, expected: str) -> None:
    """Do not drop telemetry-less active orders because a final word is negated."""
    assert lifecycle({"status": {"value": status}}) == expected
