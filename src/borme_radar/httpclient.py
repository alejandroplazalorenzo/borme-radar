"""HTTP client that is polite to the BOE and keeps no payload until it is valid.

* one User-Agent that names the project and a contact;
* a minimum pause between two requests of the same worker thread, and at most
  ``MAX_WORKERS`` connections;
* retries with exponential backoff only for what a retry can fix: 429, 5xx, network
  errors and payloads that fail validation (truncated, or not what was asked for);
  ``Retry-After`` is honoured, and a wait longer than ``retry_after_max`` gives up;
* TLS verified against the operating-system trust store (``truststore``), never off;
* an optional permanent cache that only receives validated responses; an offline
  client answers from it or raises :class:`NotCached`.
"""

from __future__ import annotations

import ssl
import statistics
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import truststore

from borme_radar import __version__
from borme_radar.cache import DiskCache

CONTACT = "alejandroplaza.dev@gmail.com"
USER_AGENT = f"borme-radar/{__version__} (open-source BORME monitor; contact: {CONTACT})"
MAX_WORKERS = 4

TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


class HttpError(Exception):
    """A request that failed for good: not worth retrying, or out of attempts."""


class NotFound(HttpError):
    """404. For a daily summary it means there was no gazette that day."""


class NotCached(HttpError):
    """Raised when the client works offline and the cache lacks the URL."""


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
        """Seconds before retry ``attempt`` (from 1): doubling from the base, capped."""
        return min(self.backoff_base * 2 ** (attempt - 1), self.backoff_max)


@dataclass(slots=True)
class ClientStats:
    network_requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    not_found: int = 0
    latencies: list[float] = field(default_factory=list)  # seconds per network request

    @property
    def latency_median(self) -> float | None:
        return statistics.median(self.latencies) if self.latencies else None


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` value, given in seconds or as an HTTP date.

    None when missing or unreadable; a date already past gives 0.
    """
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
        offline: bool = False,
    ) -> None:
        self.cache = cache
        self.min_interval = min_interval
        self.policy = policy or RetryPolicy()
        self.stats = ClientStats()
        self.offline = offline
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._local = threading.local()  # last request time of each worker thread
        self._http = httpx.Client(
            transport=transport,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=MAX_WORKERS),
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _count(self, name: str) -> None:
        with self._lock:
            setattr(self.stats, name, getattr(self.stats, name) + 1)

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        validate: Callable[[bytes], object] | None = None,
    ) -> bytes:
        """Body of ``url``: from the cache, or downloaded, validated and cached.

        ``validate`` raises ValueError for a payload that must not be kept, which is retried
        like a network error. Raises NotFound on 404, NotCached offline, and HttpError for any
        other failure or when the server asks to wait more than ``retry_after_max``.
        """
        if self.cache is not None and (cached := self.cache.get(url)) is not None:
            self._count("cache_hits")
            return cached
        if self.offline:
            raise NotCached(f"{url}: not in the cache (offline run)")

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
                self._count("retries")
                self._sleep(delay)
                continue
            if self.cache is not None:
                self.cache.put(url, body)
            return body
        raise AssertionError("unreachable")  # pragma: no cover

    def _wait_politely(self) -> None:
        last: float | None = getattr(self._local, "last_request", None)
        if last is None:
            return
        remaining = self.min_interval - (self._clock() - last)
        if remaining > 0:
            self._sleep(remaining)

    def _attempt(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        validate: Callable[[bytes], object] | None,
    ) -> bytes:
        self._wait_politely()
        self._count("network_requests")
        started = self._clock()
        try:
            response = self._http.get(url, headers=headers)
        except _TRANSIENT_ERRORS as exc:
            if _is_certificate_error(exc):
                raise HttpError(f"{url}: TLS certificate verification failed: {exc}") from exc
            raise _Transient(f"{type(exc).__name__}: {exc}") from exc
        finally:
            finished = self._clock()
            self._local.last_request = finished
            with self._lock:
                self.stats.latencies.append(finished - started)

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
            self._count("not_found")
            raise NotFound(f"{url}: 404 Not Found")
        if status in TRANSIENT_STATUS or status >= 500:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            raise _Transient(f"HTTP {status}", retry_after)
        raise HttpError(f"{url}: HTTP {status} (not retried)")
