"""HTTP client for NFL.com fantasy pages.

Wraps ``httpx.Client`` with:

* Cookie-string auth loaded from ``settings.nfl_cookie``
* Polite 2s rate limit between requests (configurable via
  ``settings.nfl_com_delay_seconds``)
* Tenacity retry on transient failures (HTTP 5xx, connection errors,
  read timeouts) with exponential backoff
* Auth-failure detection: any response containing ``id="signin-link"``
  or that 302's to ``id.nfl.com`` raises ``AuthFailureError`` — we'd
  rather fail fast and prompt for a cookie refresh than parse a login
  page as if it were league content.

The client is a context manager so callers can ``with NflComClient(...)
as c:`` and let httpx close its connection pool deterministically.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from types import TracebackType

log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SIGNIN_MARKER = 'id="signin-link"'
LOGIN_HOST = "id.nfl.com"


class NflComClientError(RuntimeError):
    """Base class for crawler-side failures."""


class AuthFailureError(NflComClientError):
    """The cookie is rejected / expired. The user must refresh it."""


class TransientHTTPError(NflComClientError):
    """A 5xx or network error that survived the retry budget."""


class NflComClient:
    """Cookie-authenticated reader for fantasy.nfl.com pages.

    Threadsafety: not threadsafe (one shared ``httpx.Client``; one
    ``_last_request`` timestamp). Crawls are single-threaded by design.
    """

    def __init__(
        self,
        cookie: str,
        *,
        delay_seconds: float = 2.0,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not cookie or not cookie.strip():
            raise NflComClientError("NflComClient requires a non-empty cookie")
        self._delay = max(0.0, float(delay_seconds))
        self._last_request: float = 0.0
        self._client = httpx.Client(
            headers={
                "Cookie": cookie.strip(),
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    # ----- context manager -----

    def __enter__(self) -> NflComClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ----- public API -----

    def get_html(self, url: str) -> str:
        """Fetch ``url`` and return decoded HTML.

        Raises ``AuthFailureError`` if the response looks like a login
        page or redirects to NFL.com SSO; ``TransientHTTPError`` if the
        retry budget is exhausted; ``NflComClientError`` for any other
        non-2xx.
        """
        self._sleep_for_rate_limit()
        try:
            response = self._fetch(url)
        except RetryError as exc:
            raise TransientHTTPError(f"GET {url} failed after retries: {exc}") from exc

        self._raise_if_auth_failure(url, response)

        if response.status_code >= 400:
            raise NflComClientError(f"GET {url} returned HTTP {response.status_code}")

        return response.text

    def test_auth(self, probe_url: str) -> bool:
        """One-shot "does this cookie work?" check.

        Returns True on success, False on auth failure. Other errors
        propagate so the caller distinguishes "cookie expired" from
        "NFL.com is down".
        """
        try:
            self.get_html(probe_url)
        except AuthFailureError:
            return False
        return True

    # ----- internals -----

    def _sleep_for_rate_limit(self) -> None:
        if self._delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, TransientHTTPError)),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        stop=stop_after_attempt(3),
        reraise=False,
    )
    def _fetch(self, url: str) -> httpx.Response:
        log.debug("GET", url=url)
        response = self._client.get(url)
        if 500 <= response.status_code < 600:
            # Convert 5xx into a retryable exception; tenacity handles backoff.
            raise TransientHTTPError(f"NFL.com returned HTTP {response.status_code} for {url}")
        return response

    def _raise_if_auth_failure(self, url: str, response: httpx.Response) -> None:
        # 30x to id.nfl.com → SSO bounce → cookie no longer valid.
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            if LOGIN_HOST in location or "/login" in location.lower():
                log.error("Auth failure (redirect to login)", url=url, location=location)
                raise AuthFailureError(
                    f"Request to {url} redirected to login ({location}); "
                    f"refresh NFL_COOKIE via `ff-pipeline cookie set`."
                )
            # An unexpected redirect we don't know how to follow.
            raise NflComClientError(
                f"GET {url} returned unfollowed redirect {response.status_code} → {location}"
            )

        if response.status_code == 401 or SIGNIN_MARKER in response.text:
            log.error("Auth failure (signin marker in response)", url=url)
            raise AuthFailureError(
                f"NFL.com login page returned for {url}; "
                f"refresh NFL_COOKIE via `ff-pipeline cookie set`."
            )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_USER_AGENT",
    "LOGIN_HOST",
    "SIGNIN_MARKER",
    "AuthFailureError",
    "NflComClient",
    "NflComClientError",
    "TransientHTTPError",
]
