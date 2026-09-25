"""Polite HTTP client: timeouts, bounded retries for transient errors, permanent cache.

Retry policy (a decision of this rebuild; the production system retried every error
except 404, waiting at most 30 s):

* Retried (exponential backoff): 429, any 5xx, connection errors, timeouts and
  truncated bodies (the server closed the connection early, or the payload fails the
  caller's validation, e.g. an XML document that does not parse).
* ``Retry-After`` (seconds or HTTP date) is honoured. If the server asks for a longer
  wait than ``RetryPolicy.retry_after_max`` the client gives up instead of retrying early.
* Never retried: any other 4xx, and TLS certificate failures (retrying cannot fix
  them). A 404 raises :class:`NotFound` (for the daily summary it simply means "no
  gazette that day").

Politeness: every worker thread waits ``min_interval`` seconds between its own requests
and the caller bounds the number of threads (``--workers``, at most 4). One client is
shared by the worker threads; its counters are updated under a lock.

TLS is verified against the operating system trust store (``truststore``), so the
client works behind corporate proxies or antivirus software that install their own
root certificate, without ever disabling verification.
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
    """Non-retryable HTTP failure, or retries exhausted."""


class NotFound(HttpError):
    """The server answered 404."""


class NotCached(HttpError):
    """Offline mode: the resource is not in the cache."""


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
    latencies: list[float] = field(default_factory=list)  # seconds per network request

    @property
    def latency_median(self) -> float | None:
        return statistics.median(self.latencies) if self.latencies else None


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
        """GET ``url``; cached responses are returned without touching the network.

        ``validate`` receives the body and must raise ``ValueError`` if it is incomplete
        or corrupt; such a response is retried and never cached. In offline mode a
        cache miss raises :class:`NotCached`.
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
