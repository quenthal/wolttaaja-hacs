"""Home Assistant-independent asynchronous client for Wolt endpoints."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import (
    ACTIVE_ORDERS_URL,
    HEADERS,
    ORDER_DETAILS_FALLBACK_URL,
    ORDER_DETAILS_URL,
    REFRESH_URL,
)

REQUEST_TIMEOUT = 10

TokenUpdateCallback = Callable[[str, str], Awaitable[None] | None]


class WoltApiError(Exception):
    """Base class for Wolt API failures."""


class WoltAuthenticationError(WoltApiError):
    """The access or refresh credentials were rejected."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WoltConnectionError(WoltApiError):
    """Wolt could not be reached or returned an unsuccessful response."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WoltRateLimitError(WoltConnectionError):
    """Wolt rejected a request because its rate limit was reached."""

    def __init__(self, retry_after: float = 60) -> None:
        super().__init__("Wolt rate limit reached", status=429)
        self.retry_after = retry_after


def retry_seconds(value: str | None) -> float:
    """Accept both Retry-After forms; malformed values use a safe default."""
    try:
        seconds = float(value)
        if not 0 <= seconds < float("inf"):
            return 60
        return max(1, seconds)
    except TypeError, ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(1, (date - datetime.now(UTC)).total_seconds())
        except TypeError, ValueError, OverflowError:
            return 60


class WoltInvalidPayloadError(WoltApiError):
    """Wolt returned JSON with an incompatible shape."""


class WoltApi:
    """Asynchronous client for the Wolt endpoints used by this integration."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        session_id: str | None,
        access_token: str,
        refresh_token: str,
        *,
        client_id: str | None = None,
        token_update_callback: TokenUpdateCallback | None = None,
    ) -> None:
        self.retry_at = 0.0
        self._session = session
        self._session_id = session_id
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_update_callback = token_update_callback
        self._refresh_lock = asyncio.Lock()
        self._web_client_id = client_id or str(uuid.uuid4())
        self._details_urls = (ORDER_DETAILS_URL, ORDER_DETAILS_FALLBACK_URL)

    @property
    def access_token(self) -> str:
        """Return the currently active access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str:
        """Return the currently active refresh token."""
        return self._refresh_token

    def _headers(self, *, authenticated: bool) -> dict[str, str]:
        """Build fresh request headers without mutating shared constants."""
        headers = dict(HEADERS)
        headers["x-wolt-web-clientid"] = self._web_client_id
        if authenticated:
            headers["authorization"] = f"Bearer {self._access_token}"
            if self._session_id:
                headers["w-wolt-session-id"] = self._session_id
        return headers

    async def _perform_request(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool,
        data: dict[str, str] | None = None,
    ) -> Any:
        """Perform one request and translate transport/status/payload failures."""
        if monotonic() < self.retry_at:
            raise WoltRateLimitError(self.retry_at - monotonic())
        headers = self._headers(authenticated=authenticated)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                if method == "POST":
                    request = self._session.post(url, headers=headers, data=data)
                else:
                    request = self._session.request(method, url, headers=headers)
                async with request as response:
                    if response.status == 401:
                        raise WoltAuthenticationError(
                            "Wolt rejected the supplied credentials",
                            status=response.status,
                        )
                    # 403 is not treated as bad credentials: bot protection in
                    # front of Wolt answers 403 too, and must not force reauth.
                    if response.status == 429:
                        delay = retry_seconds(response.headers.get("Retry-After"))
                        self.retry_at = monotonic() + delay
                        raise WoltRateLimitError(delay)
                    if response.status >= 400:
                        raise WoltConnectionError(
                            f"Wolt request failed with status {response.status}",
                            status=response.status,
                        )
                    try:
                        return await response.json()
                    except (aiohttp.ContentTypeError, TypeError, ValueError) as err:
                        raise WoltInvalidPayloadError("Wolt returned invalid JSON") from err
        except WoltApiError, asyncio.CancelledError:
            raise
        except (TimeoutError, aiohttp.ClientError) as err:
            raise WoltConnectionError("Unable to connect to Wolt") from err

    async def _refresh_access_token(self) -> None:
        """Refresh credentials with Wolt's form-encoded web authentication flow."""
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        try:
            data = await self._perform_request(
                "POST",
                REFRESH_URL,
                authenticated=False,
                data=payload,
            )
        except WoltConnectionError as err:
            if err.status == 400:
                raise WoltAuthenticationError(
                    "Wolt rejected the supplied refresh token", status=err.status
                ) from err
            raise
        if not isinstance(data, dict):
            raise WoltInvalidPayloadError("Wolt token refresh returned a non-object")

        access_token = data.get("access_token") or data.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise WoltInvalidPayloadError("Wolt token refresh did not return an access token")

        refresh_token = data.get("refresh_token") or data.get("refreshToken")
        if refresh_token is None:
            refresh_token = self._refresh_token
        if not isinstance(refresh_token, str) or not refresh_token:
            raise WoltInvalidPayloadError("Wolt token refresh returned an invalid refresh token")

        self._access_token = access_token
        self._refresh_token = refresh_token

        if self._token_update_callback is not None:
            callback_result = self._token_update_callback(access_token, refresh_token)
            if inspect.isawaitable(callback_result):
                await callback_result

    async def _request(self, method: str, url: str, *, auth: bool = True) -> Any:
        """Request JSON, refreshing and retrying once only after an initial 401."""
        if auth and not self._access_token:
            async with self._refresh_lock:
                if not self._access_token:
                    await self._refresh_access_token()
            return await self._perform_request(method, url, authenticated=True)

        rejected_access_token = self._access_token
        try:
            return await self._perform_request(
                method,
                url,
                authenticated=auth,
            )
        except WoltAuthenticationError:
            if not auth:
                raise

        async with self._refresh_lock:
            # Another concurrent request may already have rotated the token while
            # this request was waiting for the lock. Reuse that token rather than
            # submitting the same single-use refresh token twice.
            if self._access_token == rejected_access_token:
                await self._refresh_access_token()
        return await self._perform_request(method, url, authenticated=True)

    async def fetch_orders(self) -> list[dict[str, Any]]:
        """Fetch the account's order page, including recent completed orders."""
        data = await self._request("GET", ACTIVE_ORDERS_URL)
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            raise WoltInvalidPayloadError("Wolt orders payload is invalid")
        if any(not isinstance(order, dict) for order in data["orders"]):
            raise WoltInvalidPayloadError("Wolt order item is invalid")
        return data["orders"]

    async def fetch_order_details(self, purchase_id: str) -> dict[str, Any]:
        """Fetch rich purchase-tracking details for an order."""
        quoted_id = quote(purchase_id, safe="")
        preferred, alternative = self._details_urls
        try:
            data = await self._request("GET", preferred.format(quoted_id))
        except WoltConnectionError as err:
            if err.status not in (404, 405):
                raise
            data = await self._request("GET", alternative.format(quoted_id))
            # Later polls start from the URL form that answered.
            self._details_urls = (alternative, preferred)
        if not isinstance(data, dict):
            raise WoltInvalidPayloadError("Wolt order details payload is invalid")
        details = data.get("order_details")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            details = details[0]
        if not isinstance(details, dict):
            raise WoltInvalidPayloadError("Wolt order details payload is invalid")
        # Driver locations belong to the envelope, not order_details. Keep them
        # in memory only; entity publication is separately opt-in and allowlisted.
        drivers = data.get("drivers")
        if isinstance(drivers, list):
            return {**details, "_drivers": drivers}
        return details
