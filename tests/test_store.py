from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from borme_radar import store
from borme_radar.models import Document
from borme_radar.parser import parse_document

MADRID = "BORME-A-2026-183-28.xml"
URL = "https://www.boe.es/diario_borme/xml.php?id=BORME-A-2026-183-28"


@pytest.fixture
def conn() -> sqlite3.Connection:
    return store.connect(":memory:")


@pytest.fixture
def doc(fixtures: Path) -> Document:
    return parse_document((fixtures / MADRID).read_bytes())


def counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return tuple(  # type: ignore[return-value]
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("documents", "announcements", "acts")
    )


def test_schema_is_versioned(conn: sqlite3.Connection) -> None:
    assert store.schema_version(conn) == 1
    assert store.migrate(conn) == 1  # re-running migrations is a no-op


def test_loading_the_same_document_twice_does_not_duplicate(
    conn: sqlite3.Connection, doc: Document
) -> None:
    store.load_document(conn, doc, URL)
    first = counts(conn)
    store.load_document(conn, doc, URL)
    assert counts(conn) == first
    assert first == (1, len(doc.announcements), sum(len(a.acts) for a in doc.announcements))


def test_reloading_replaces_stale_rows(conn: sqlite3.Connection, doc: Document) -> None:
    store.load_document(conn, doc, URL)
    smaller = dataclasses.replace(doc, announcements=doc.announcements[:2])
    store.load_document(conn, smaller, URL)
    assert counts(conn)[:2] == (1, 2)


def test_record_day_is_idempotent(conn: sqlite3.Connection) -> None:
    store.record_day(conn, date(2026, 9, 20), published=False, n_documents=0)
    store.record_day(conn, date(2026, 9, 20), published=False, n_documents=0)
    store.record_day(conn, date(2026, 9, 22), published=True, n_documents=2)
    s = store.stats(conn)
    assert (s.days_published, s.days_without_gazette) == (1, 1)


def test_stats_and_lookup(conn: sqlite3.Connection, doc: Document) -> None:
    store.load_document(conn, doc, URL)
    s = store.stats(conn)
    assert s.announcements == len(doc.announcements)
    assert sum(n for _, n in s.acts_by_type) == s.acts
    assert s.lag_median is not None and s.lag_median >= 0

    first = doc.announcements[0]
    names = store.company_names(conn, since=date(2026, 9, 22))
    norm = next(k for k, v in names.items() if v == first.company_name)
    found = store.announcements_for(conn, [norm])
    assert found[0].number == first.number
    assert [a.act_type for a in found[0].acts] == [a.act_type for a in first.acts]
    assert store.company_names(conn, since=date(2026, 9, 23)) == {}
