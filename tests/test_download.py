"""Downloader and runner: parallel documents, day-level failure isolation, offline runs."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date
from pathlib import Path

import httpx
import pytest

from borme_radar import store
from borme_radar.cache import DiskCache
from borme_radar.discovery import GroupIndex
from borme_radar.download import Downloader
from borme_radar.httpclient import PoliteClient, RetryPolicy
from borme_radar.matching import Matcher
from borme_radar.pipeline import Pipeline
from borme_radar.runner import run_range
from borme_radar.source import BormeSource

DAY = date(2026, 9, 22)
XML = "https://www.boe.es/diario_borme/xml.php?id={}"


def summary_with(*doc_ids: str) -> bytes:
    items = [{"identificador": d, "titulo": "MADRID", "url_xml": XML.format(d)} for d in doc_ids]
    return json.dumps(
        {"data": {"sumario": {"diario": {"seccion": {"codigo": "A", "item": items}}}}}
    ).encode()


class Server:
    """Fake BOE: summaries and documents from the fixtures, some broken on purpose."""

    def __init__(self, fixtures: Path, broken: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.fixtures = fixtures
        self.broken = broken
        self.threads: set[str] = set()
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.threads.add(threading.current_thread().name)
        url = str(request.url)
        if "sumario/20260922" in url:
            return httpx.Response(
                200, content=summary_with("BORME-A-2026-183-07", "BORME-A-2026-183-28")
            )
        if "sumario/20260923" in url:
            return httpx.Response(200, content=summary_with("BORME-A-2026-183-07"))
        if "sumario" in url:
            return httpx.Response(404)
        doc_id = url.rsplit("=", 1)[-1]
        if doc_id in self.broken:
            return httpx.Response(503)
        return httpx.Response(200, content=(self.fixtures / f"{doc_id}.xml").read_bytes())


def client(server: Server, tmp_path: Path, offline: bool = False) -> PoliteClient:
    return PoliteClient(
        cache=DiskCache(tmp_path / "cache"),
        transport=httpx.MockTransport(server),
        min_interval=0,
        policy=RetryPolicy(max_attempts=2, backoff_base=0),
        offline=offline,
    )


def pipeline(conn: sqlite3.Connection) -> Pipeline:
    return Pipeline(conn, Matcher(store.load_watch_index(conn)), GroupIndex({}, {}, [], {}))


def test_documents_of_a_day_download_in_parallel(fixtures: Path, tmp_path: Path) -> None:
    server = Server(fixtures)
    with client(server, tmp_path) as c, Downloader(BormeSource(c), workers=4) as d:
        result = d.day(DAY)
    assert result.status == "published"
    assert sorted(result.payloads) == ["BORME-A-2026-183-07", "BORME-A-2026-183-28"]
    assert any(name.startswith("ThreadPoolExecutor") for name in server.threads)


def test_workers_are_bounded() -> None:
    with pytest.raises(ValueError, match="between 1 and 4"):
        Downloader(BormeSource(PoliteClient(min_interval=0)), workers=5)


def test_a_failed_day_is_skipped_listed_and_recovered_later(
    fixtures: Path, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    broken = Server(fixtures, broken={"BORME-A-2026-183-28"})
    with client(broken, tmp_path) as c, Downloader(BormeSource(c), workers=2) as d:
        report = run_range(conn, d, pipeline(conn), date(2026, 9, 21), date(2026, 9, 23))
    assert [day for day, _ in report.failed] == [DAY]
    assert report.counters["days_published"] == 1  # the 23rd still went through
    assert report.counters["days_no_gazette"] == 1
    assert store.days_with_status(conn, "failed") == [DAY]

    # Next run: only what is missing goes to the network; the day is recovered.
    fixed = Server(fixtures)
    with client(fixed, tmp_path) as c, Downloader(BormeSource(c), workers=2) as d:
        report = run_range(conn, d, pipeline(conn), date(2026, 9, 21), date(2026, 9, 23))
        # The 21st asks again (a 404 is never cached), the 22nd only its missing document,
        # the 23rd nothing (already processed).
        assert c.stats.network_requests == 2
    assert report.failed == []
    assert store.days_with_status(conn, "failed") == []


def test_offline_run_reads_only_the_cache(
    fixtures: Path, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    server = Server(fixtures)
    with client(server, tmp_path) as c, Downloader(BormeSource(c), workers=1) as d:
        run_range(conn, d, pipeline(conn), DAY, DAY, download_only=True)
    with client(server, tmp_path, offline=True) as c, Downloader(BormeSource(c), workers=1) as d:
        report = run_range(conn, d, pipeline(conn), date(2026, 9, 21), DAY, reprocess=True)
        assert c.stats.network_requests == 0
    assert report.counters["days_not_cached"] == 1  # the 21st: 404s are never cached
    assert report.counters["documents"] == 2
