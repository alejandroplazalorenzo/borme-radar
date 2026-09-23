"""Retry policy, Retry-After, politeness and caching, with a fake transport (no network)."""

from __future__ import annotations

import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from borme_radar.cache import DiskCache
from borme_radar.httpclient import (
    CONTACT,
    HttpError,
    NotFound,
    PoliteClient,
    RetryPolicy,
    parse_retry_after,
)

URL = "https://www.boe.es/diario_borme/xml.php?id=BORME-A-2026-183-02"


class FakeTime:
    """Deterministic clock: sleeping advances it, nothing really waits."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


Handler = Callable[[httpx.Request], httpx.Response]


def scripted(*steps: httpx.Response | Exception) -> tuple[Handler, list[httpx.Request]]:
    """Transport handler that plays the given responses/exceptions in order."""
    seen: list[httpx.Request] = []
    queue = list(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        step = queue.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    return handler, seen


def make_client(
    handler: Handler,
    fake: FakeTime,
    *,
    cache: DiskCache | None = None,
    min_interval: float = 0.0,
    policy: RetryPolicy | None = None,
) -> PoliteClient:
    return PoliteClient(
        cache=cache,
        transport=httpx.MockTransport(handler),
        min_interval=min_interval,
        policy=policy or RetryPolicy(max_attempts=4, backoff_base=2.0),
        sleep=fake.sleep,
        clock=fake.clock,
    )


def test_retries_5xx_with_exponential_backoff() -> None:
    fake = FakeTime()
    handler, seen = scripted(
        httpx.Response(503), httpx.Response(502), httpx.Response(200, content=b"ok")
    )
    client = make_client(handler, fake)
    assert client.get(URL) == b"ok"
    assert len(seen) == 3
    assert fake.sleeps == [2.0, 4.0]
    assert client.stats.retries == 2


def test_honours_retry_after_seconds_on_429() -> None:
    fake = FakeTime()
    handler, _ = scripted(
        httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, content=b"ok")
    )
    client = make_client(handler, fake)
    assert client.get(URL) == b"ok"
    assert fake.sleeps == [7.0]


def test_gives_up_when_retry_after_exceeds_the_maximum() -> None:
    fake = FakeTime()
    handler, seen = scripted(httpx.Response(503, headers={"Retry-After": "3600"}))
    client = make_client(handler, fake, policy=RetryPolicy(retry_after_max=300))
    with pytest.raises(HttpError, match="wait 3600s"):
        client.get(URL)
    assert len(seen) == 1
    assert fake.sleeps == []  # never retries earlier than the server asked


@pytest.mark.parametrize("status", [400, 401, 403, 410, 422])
def test_other_4xx_are_not_retried(status: int) -> None:
    fake = FakeTime()
    handler, seen = scripted(httpx.Response(status))
    client = make_client(handler, fake)
    with pytest.raises(HttpError, match=f"HTTP {status}"):
        client.get(URL)
    assert len(seen) == 1
    assert fake.sleeps == []


def test_404_raises_not_found_without_retry() -> None:
    fake = FakeTime()
    handler, seen = scripted(httpx.Response(404))
    client = make_client(handler, fake)
    with pytest.raises(NotFound):
        client.get(URL)
    assert len(seen) == 1
    assert client.stats.not_found == 1


def test_connection_errors_are_retried_until_exhausted() -> None:
    fake = FakeTime()
    request = httpx.Request("GET", URL)
    handler, seen = scripted(*(httpx.ConnectError("refused", request=request) for _ in range(4)))
    client = make_client(handler, fake)
    with pytest.raises(HttpError, match="giving up after 4 attempts"):
        client.get(URL)
    assert len(seen) == 4
    assert fake.sleeps == [2.0, 4.0, 8.0]


def test_truncated_read_is_retried() -> None:
    fake = FakeTime()
    request = httpx.Request("GET", URL)
    handler, seen = scripted(
        httpx.RemoteProtocolError("peer closed connection", request=request),
        httpx.Response(200, content=b"ok"),
    )
    client = make_client(handler, fake)
    assert client.get(URL) == b"ok"
    assert len(seen) == 2


def test_invalid_payload_is_retried_and_never_cached(tmp_path: Path) -> None:
    fake = FakeTime()
    cache = DiskCache(tmp_path)
    handler, seen = scripted(
        httpx.Response(200, content=b"<documento><texto>"),  # cut in the middle
        httpx.Response(200, content=b"<documento/>"),
    )

    def validate(body: bytes) -> None:
        if not body.endswith(b"/>"):
            raise ValueError("truncated")

    client = make_client(handler, fake, cache=cache)
    assert client.get(URL, validate=validate) == b"<documento/>"
    assert len(seen) == 2
    assert cache.get(URL) == b"<documento/>"


def test_certificate_errors_are_not_retried() -> None:
    fake = FakeTime()
    request = httpx.Request("GET", URL)
    cause = ssl.SSLCertVerificationError("certificate verify failed")
    error = httpx.ConnectError("tls", request=request)
    error.__cause__ = cause
    handler, seen = scripted(error)
    client = make_client(handler, fake)
    with pytest.raises(HttpError, match="certificate"):
        client.get(URL)
    assert len(seen) == 1


def test_second_get_is_served_from_cache(tmp_path: Path) -> None:
    fake = FakeTime()
    handler, seen = scripted(httpx.Response(200, content=b"payload"))
    client = make_client(handler, fake, cache=DiskCache(tmp_path))
    assert client.get(URL) == b"payload"
    assert client.get(URL) == b"payload"
    assert len(seen) == 1
    assert (client.stats.network_requests, client.stats.cache_hits) == (1, 1)


def test_polite_delay_between_network_requests() -> None:
    fake = FakeTime()
    handler, _ = scripted(httpx.Response(200), httpx.Response(200))
    client = make_client(handler, fake, min_interval=1.5)
    client.get(URL)
    client.get(URL + "-other")
    assert fake.sleeps == [1.5]


def test_user_agent_identifies_project_and_contact() -> None:
    fake = FakeTime()
    handler, seen = scripted(httpx.Response(200))
    make_client(handler, fake).get(URL)
    user_agent = seen[0].headers["User-Agent"]
    assert user_agent.startswith("borme-radar/")
    assert CONTACT in user_agent


def test_parse_retry_after_http_date() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=30), usegmt=True)
    assert parse_retry_after(header, now=now) == pytest.approx(30.0)
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None
