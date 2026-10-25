"""Synthetic offline tests for the Home Assistant-independent Wolt API client."""

import asyncio
from collections import deque
from typing import Any

import aiohttp
import pytest

from custom_components.wolttaaja.api import (
    WoltApi,
    WoltAuthenticationError,
    WoltConnectionError,
    WoltInvalidPayloadError,
    WoltRateLimitError,
)
from custom_components.wolttaaja.const import (
    ACTIVE_ORDERS_URL,
    ORDER_DETAILS_FALLBACK_URL,
    ORDER_DETAILS_URL,
    REFRESH_URL,
)


class FakeResponse:
    """Minimal asynchronous response implementing the API client's contract."""

    def __init__(self, status: int, payload: Any = None) -> None:
        self.headers = {}
        self.status = status
        self._payload = payload

    async def json(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeRequestContext:
    """Minimal aiohttp request context manager."""

    def __init__(self, response: FakeResponse | BaseException) -> None:
        self._response = response

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class FakeSession:
    """Queue deterministic responses and record requests without network access."""

    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        self._responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: dict[str, str] | None = None,
    ) -> FakeRequestContext:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "data": data,
            }
        )
        return FakeRequestContext(self._responses.popleft())

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        data: dict[str, str],
    ) -> FakeRequestContext:
        return self.request("POST", url, headers=headers, data=data)


def make_api(session: FakeSession, session_id: str | None = "test-session") -> WoltApi:
    """Create an API client containing only synthetic credentials."""
    return WoltApi(
        session,  # type: ignore[arg-type]
        session_id,
        "test-access-token",
        "test-refresh-token",
    )


async def test_active_orders_success_uses_current_token_first() -> None:
    """Use a valid access token without refreshing it preemptively."""
    payload = {
        "orders": [
            {
                "purchase_id": "purchase-001",
                "telemetry": {"order_status_type": "IN_PROGRESS"},
            }
        ]
    }
    session = FakeSession(FakeResponse(200, payload))

    assert await make_api(session).fetch_orders() == payload["orders"]
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == ACTIVE_ORDERS_URL
    assert session.calls[0]["headers"]["authorization"] == ("Bearer test-access-token")
    assert session.calls[0]["headers"]["w-wolt-session-id"] == "test-session"


@pytest.mark.parametrize(
    ("method_name", "payload", "args"),
    [
        ("fetch_orders", {"orders": {"unexpected": "shape"}}, ()),
        (
            "fetch_order_details",
            {"order_details": "unexpected-shape"},
            ("purchase-001",),
        ),
    ],
)
async def test_malformed_payloads_raise_typed_exception(
    method_name: str,
    payload: dict[str, Any],
    args: tuple[str, ...],
) -> None:
    """Report incompatible endpoint payloads instead of silently accepting them."""
    api = make_api(FakeSession(FakeResponse(200, payload)))

    with pytest.raises(WoltInvalidPayloadError):
        await getattr(api, method_name)(*args)


async def test_malformed_json_raises_typed_payload_exception() -> None:
    """Translate JSON decoding failures from an otherwise successful response."""
    api = make_api(FakeSession(FakeResponse(200, ValueError("malformed JSON"))))

    with pytest.raises(WoltInvalidPayloadError):
        await api.fetch_orders()


async def test_missing_access_token_refreshes_before_first_account_request() -> None:
    """Bootstrap from one refresh token without sending an empty bearer header."""
    session = FakeSession(
        FakeResponse(
            200,
            {
                "access_token": "bootstrapped-access-token",
                "refresh_token": "rotated-refresh-token",
            },
        ),
        FakeResponse(200, {"orders": []}),
    )
    api = WoltApi(
        session,  # type: ignore[arg-type]
        None,
        "",
        "test-refresh-token",
    )

    assert await api.fetch_orders() == []
    assert [call["url"] for call in session.calls] == [REFRESH_URL, ACTIVE_ORDERS_URL]
    assert "authorization" not in session.calls[0]["headers"]
    assert session.calls[1]["headers"]["authorization"] == ("Bearer bootstrapped-access-token")


async def test_unauthorized_request_refreshes_persists_and_retries_once() -> None:
    """Refresh only after 401, expose rotation, notify persistence, and retry once."""
    rotated = ("next-access-token", "next-refresh-token")
    session = FakeSession(
        FakeResponse(401),
        FakeResponse(
            200,
            {"access_token": rotated[0], "refresh_token": rotated[1]},
        ),
        FakeResponse(
            200,
            {
                "orders": [
                    {
                        "purchase_id": "purchase-001",
                        "telemetry": {"order_status_type": "IN_PROGRESS"},
                    }
                ]
            },
        ),
    )
    persisted: list[tuple[str, str]] = []

    async def persist_tokens(access_token: str, refresh_token: str) -> None:
        persisted.append((access_token, refresh_token))

    api = WoltApi(
        session,  # type: ignore[arg-type]
        "test-session",
        "test-access-token",
        "test-refresh-token",
        token_update_callback=persist_tokens,
    )

    assert await api.fetch_orders() == [
        {
            "purchase_id": "purchase-001",
            "telemetry": {"order_status_type": "IN_PROGRESS"},
        }
    ]
    assert [call["url"] for call in session.calls] == [
        ACTIVE_ORDERS_URL,
        REFRESH_URL,
        ACTIVE_ORDERS_URL,
    ]
    assert session.calls[1]["method"] == "POST"
    assert session.calls[1]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "test-refresh-token",
    }
    assert session.calls[2]["headers"]["authorization"] == ("Bearer next-access-token")
    assert api.access_token == rotated[0]
    assert api.refresh_token == rotated[1]
    assert persisted == [rotated]


async def test_concurrent_unauthorized_requests_share_one_token_refresh() -> None:
    """Serialize concurrent 401 recovery when refresh tokens rotate once."""
    api = make_api(FakeSession())
    both_initial_requests_started = asyncio.Event()
    initial_requests = 0
    refreshes = 0

    async def perform_request(
        _method: str,
        url: str,
        *,
        authenticated: bool,
        data: dict[str, str] | None = None,
    ) -> Any:
        nonlocal initial_requests
        del data
        if url == ACTIVE_ORDERS_URL and authenticated and api.access_token == "test-access-token":
            initial_requests += 1
            if initial_requests == 2:
                both_initial_requests_started.set()
            await both_initial_requests_started.wait()
            raise WoltAuthenticationError("expired", status=401)
        return {"orders": []}

    async def refresh_access_token() -> None:
        nonlocal refreshes
        refreshes += 1
        api._access_token = "next-access-token"

    api._perform_request = perform_request  # type: ignore[method-assign]
    api._refresh_access_token = refresh_access_token  # type: ignore[method-assign]

    first, second = await asyncio.gather(api.fetch_orders(), api.fetch_orders())

    assert first == second == []
    assert initial_requests == 2
    assert refreshes == 1


@pytest.mark.parametrize(
    ("failed_status", "exception_type"),
    [(400, WoltAuthenticationError), (401, WoltAuthenticationError), (403, WoltConnectionError)],
)
async def test_refresh_failure_raises_without_looping(
    failed_status: int, exception_type: type[Exception]
) -> None:
    """Refresh rejection is an auth failure; a forbidden refresh is not proof of bad tokens."""
    session = FakeSession(FakeResponse(401), FakeResponse(failed_status, {"error": "no"}))

    with pytest.raises(exception_type):
        await make_api(session).fetch_orders()

    assert len(session.calls) == 2


async def test_forbidden_is_a_connection_failure_without_refresh() -> None:
    """Bot protection answers 403; it must neither refresh tokens nor force reauth."""
    session = FakeSession(FakeResponse(403))

    with pytest.raises(WoltConnectionError) as exc:
        await make_api(session).fetch_orders()

    assert exc.value.status == 403
    assert [call["url"] for call in session.calls] == [ACTIVE_ORDERS_URL]


async def test_second_unauthorized_response_raises_without_refresh_loop() -> None:
    """Retry the authenticated endpoint only once after successful refresh."""
    session = FakeSession(
        FakeResponse(401),
        FakeResponse(200, {"access_token": "next-access-token"}),
        FakeResponse(401),
    )

    with pytest.raises(WoltAuthenticationError):
        await make_api(session).fetch_orders()

    assert len(session.calls) == 3


@pytest.mark.parametrize("session_id", [None, ""])
async def test_authenticated_request_omits_optional_session_header(
    session_id: str | None,
) -> None:
    """Allow accounts without Wolt's optional analytics session identifier."""
    session = FakeSession(FakeResponse(200, {"orders": []}))

    assert await make_api(session, session_id).fetch_orders() == []
    assert "w-wolt-session-id" not in session.calls[0]["headers"]


async def test_order_details_uses_rich_purchase_tracking_endpoint() -> None:
    """Fetch rich tracking details by purchase ID from restaurant-api."""
    session = FakeSession(FakeResponse(200, {"order_details": {"status": "delivery"}}))

    assert await make_api(session).fetch_order_details("purchase-001") == {"status": "delivery"}
    assert session.calls[0]["url"] == ORDER_DETAILS_URL.format("purchase-001")
    assert session.calls[0]["url"] == (
        "https://restaurant-api.wolt.com/v2/order_details/purchase_tracking/purchase-001"
    )


async def test_order_details_accepts_legacy_list_shape() -> None:
    """Keep compatibility with the earlier sanitized tracking fixture shape."""
    session = FakeSession(FakeResponse(200, {"order_details": [{"status": "delivery"}]}))

    assert await make_api(session).fetch_order_details("purchase-001") == {"status": "delivery"}


@pytest.mark.parametrize("missing_status", [404, 405])
async def test_order_details_falls_back_to_query_form(
    missing_status: int,
) -> None:
    """Support both private purchase-tracking URL forms seen in live clients."""
    session = FakeSession(
        FakeResponse(missing_status),
        FakeResponse(200, {"order_details": {"status": "delivery"}}),
    )

    assert await make_api(session).fetch_order_details("purchase/with space") == {
        "status": "delivery"
    }
    assert [call["url"] for call in session.calls] == [
        ORDER_DETAILS_URL.format("purchase%2Fwith%20space"),
        ORDER_DETAILS_FALLBACK_URL.format("purchase%2Fwith%20space"),
    ]


async def test_order_details_remembers_answering_url_form() -> None:
    """Skip the failing URL form on later polls instead of doubling every request."""
    ok = {"order_details": {"status": "delivery"}}
    session = FakeSession(FakeResponse(404), FakeResponse(200, ok), FakeResponse(200, ok))
    api = make_api(session)

    await api.fetch_order_details("a")
    await api.fetch_order_details("b")

    assert [call["url"] for call in session.calls] == [
        ORDER_DETAILS_URL.format("a"),
        ORDER_DETAILS_FALLBACK_URL.format("a"),
        ORDER_DETAILS_FALLBACK_URL.format("b"),
    ]


async def test_order_details_keeps_url_form_when_both_forms_fail() -> None:
    """An order that vanished from both forms must not flip the preference."""
    ok = {"order_details": {"status": "delivery"}}
    session = FakeSession(FakeResponse(404), FakeResponse(404), FakeResponse(200, ok))
    api = make_api(session)

    with pytest.raises(WoltConnectionError):
        await api.fetch_order_details("gone")
    await api.fetch_order_details("b")

    assert session.calls[-1]["url"] == ORDER_DETAILS_URL.format("b")


async def test_web_client_identifier_is_generated_per_api_instance() -> None:
    """Do not ship one captured browser identifier to every installation."""
    first_session = FakeSession(FakeResponse(200, {"orders": []}))
    second_session = FakeSession(FakeResponse(200, {"orders": []}))

    await make_api(first_session).fetch_orders()
    await make_api(second_session).fetch_orders()

    first_id = first_session.calls[0]["headers"]["x-wolt-web-clientid"]
    second_id = second_session.calls[0]["headers"]["x-wolt-web-clientid"]
    assert first_id != second_id
    assert len(first_id) == len(second_id) == 36


@pytest.mark.parametrize(
    ("response", "exception_type"),
    [
        (TimeoutError(), WoltConnectionError),
        (aiohttp.ClientConnectionError(), WoltConnectionError),
        (FakeResponse(429), WoltRateLimitError),
    ],
)
async def test_transport_and_rate_limit_failures_are_typed(
    response: FakeResponse | BaseException,
    exception_type: type[Exception],
) -> None:
    """Distinguish retryable connectivity and rate-limit failures."""
    with pytest.raises(exception_type):
        await make_api(FakeSession(response)).fetch_orders()


async def test_retry_after_blocks_additional_requests():
    response = FakeResponse(429)
    response.headers = {"Retry-After": "120"}
    session = FakeSession(response, FakeResponse(200, {"orders": []}))
    api = make_api(session)
    for _ in range(2):
        with pytest.raises(WoltRateLimitError) as exc:
            await api.fetch_orders()
        assert 115 < exc.value.retry_after <= 120
    assert len(session.calls) == 1


def test_http_date_retry_after():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from custom_components.wolttaaja.api import retry_seconds

    assert 115 < retry_seconds(format_datetime(datetime.now(UTC) + timedelta(seconds=120))) <= 120
