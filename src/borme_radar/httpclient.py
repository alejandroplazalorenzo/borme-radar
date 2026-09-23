"""Polite HTTP client: timeouts, bounded retries for transient errors, permanent cache.

Retry policy:

* Retried (exponential backoff): 429, any 5xx, connection errors, timeouts and
  truncated bodies (the server closed the connection early, or the payload fails the
  caller's validation, e.g. an XML document that does not parse).
* ``Retry-After`` (seconds or HTTP date) is honoured. If the server asks for a longer
  wait than ``RetryPolicy.retry_after_max`` the client gives up instead of retrying early.
* Never retried: any other 4xx, and TLS certificate failures (retrying cannot fix
  them). A 404 raises :class:`NotFound` (for the daily summary it simply means "no
  gazette that day").

TLS is verified against the operating system trust store (``truststore``), so the
client works behind corporate proxies or antivirus software that install their own
root certificate, without ever disabling verification.
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import truststore

from borme_radar import __version__
from borme_radar.cache import DiskCache

CONTACT = "alejandroplaza.dev@gmail.com"
USER_AGENT = f"borme-radar/{__version__} (open-source BORME monitor; contact: {CONTACT})"

TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


class HttpError(Exception):
    """Non-retryable HTTP failure, or retries exhausted."""


class NotFound(HttpError):
    """The server answered 404."""


def _is_certificate_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    return False


class _Transient(Exception):
    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    backoff_base: float = 2.0  # seconds before the first retry; doubles each time
    backoff_max: float = 60.0
    retry_after_max: float = 300.0

    def backoff(self, attempt: int) -> float:
        """Delay after the given failed attempt (1-based)."""
        return min(self.backoff_base * 2 ** (attempt - 1), self.backoff_max)


@dataclass(slots=True)
class ClientStats:
    network_requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    not_found: int = 0


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait according to a Retry-After header (delta-seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return max(0.0, (when - now).total_seconds())


class PoliteClient:
    def __init__(
        self,
        *,
        cache: DiskCache | None = None,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = 1.0,
        timeout: float = 30.0,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache = cache
        self.min_interval = min_interval
        self.policy = policy or RetryPolicy()
        self.stats = ClientStats()
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self._http = httpx.Client(
            transport=transport,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        validate: Callable[[bytes], object] | None = None,
    ) -> bytes:
        """GET ``url``; cached responses are returned without touching the network.

        ``validate`` receives the body and must raise ``ValueError`` if it is incomplete
        or corrupt; such a response is retried and never cached.
        """
        if self.cache is not None and (cached := self.cache.get(url)) is not None:
            self.stats.cache_hits += 1
            return cached

        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                body = self._attempt(url, headers, validate)
            except _Transient as exc:
                if attempt == self.policy.max_attempts:
                    raise HttpError(f"{url}: giving up after {attempt} attempts ({exc})") from exc
                delay = self.policy.backoff(attempt)
                if exc.retry_after is not None:
                    if exc.retry_after > self.policy.retry_after_max:
                        raise HttpError(
                            f"{url}: server asked to wait {exc.retry_after:.0f}s ({exc})"
                        ) from exc
                    delay = exc.retry_after
                self.stats.retries += 1
                self._sleep(delay)
                continue
            if self.cache is not None:
                self.cache.put(url, body)
            return body
        raise AssertionError("unreachable")  # pragma: no cover

    def _wait_politely(self) -> None:
        if self._last_request is None:
            return
        remaining = self.min_interval - (self._clock() - self._last_request)
        if remaining > 0:
            self._sleep(remaining)

    def _attempt(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        validate: Callable[[bytes], object] | None,
    ) -> bytes:
        self._wait_politely()
        self.stats.network_requests += 1
        try:
            response = self._http.get(url, headers=headers)
        except _TRANSIENT_ERRORS as exc:
            if _is_certificate_error(exc):
                raise HttpError(f"{url}: TLS certificate verification failed: {exc}") from exc
            raise _Transient(f"{type(exc).__name__}: {exc}") from exc
        finally:
            self._last_request = self._clock()

        status = response.status_code
        if status == 200:
            body = response.content
            if validate is not None:
                try:
                    validate(body)
                except ValueError as exc:
                    raise _Transient(f"invalid or truncated payload: {exc}") from exc
            return body
        if status == 404:
            self.stats.not_found += 1
            raise NotFound(f"{url}: 404 Not Found")
        if status in TRANSIENT_STATUS or status >= 500:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            raise _Transient(f"HTTP {status}", retry_after)
        raise HttpError(f"{url}: HTTP {status} (not retried)")
