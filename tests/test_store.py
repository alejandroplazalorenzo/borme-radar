from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from borme_radar import record, store
from conftest import watched


def test_the_schema_files_run_once_and_in_order(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "x.sqlite")
    files = list((Path(store.__file__).parent / "sql").glob("*.sql"))
    assert store.schema_version(conn) == len(files) == 4
    assert store.migrate(conn) == 4  # nothing left to run


def test_a_database_of_an_earlier_version_is_refused(tmp_path: Path) -> None:
    old = sqlite3.connect(tmp_path / "old.sqlite")
    old.executescript("CREATE TABLE announcements (x); PRAGMA user_version = 3;")
    old.close()
    with pytest.raises(RuntimeError, match="earlier version"):
        store.connect(tmp_path / "old.sqlite")


def test_import_fills_only_what_the_registro_has_not_said(conn: sqlite3.Connection) -> None:
    store.import_watchlist(conn, [watched("G", "ACME, S.A.", city="MADRID")], date(2026, 1, 1))
    assert conn.execute("SELECT city FROM watched").fetchone()[0] == "MADRID"
    conn.execute(
        "INSERT INTO seen_values (company_id, field, value, day, source, added_at)"
        " VALUES (1, 'city', 'TOLEDO', '2026-02-01', 'registry', 'x')"
    )
    result = store.import_watchlist(
        conn, [watched("G", "ACME SA", city="SEVILLA")], date(2026, 3, 1)
    )
    assert result.new_companies == 0 and result.fields_filled == 0
    assert conn.execute("SELECT city FROM watched").fetchone()[0] == "MADRID"
    sources = conn.execute("SELECT source, value FROM seen_values ORDER BY seen_id").fetchall()
    assert [tuple(r) for r in sources] == [
        ("watchlist", "MADRID"),
        ("registry", "TOLEDO"),
        ("watchlist", "SEVILLA"),
    ]


def test_watchlist_sheets_are_loaded(conn: sqlite3.Connection) -> None:
    store.import_watchlist(conn, [watched("G", "ACME SA", sheet="M 5", province="MADRID")])
    index = store.load_watch_index(conn)
    assert index.by_sheet == {("MADRID", "M 5"): 1}


def test_record_day_is_idempotent(conn: sqlite3.Connection) -> None:
    store.record_day(conn, date(2026, 9, 20), "no_gazette")
    store.record_day(conn, date(2026, 9, 20), "no_gazette")
    store.record_day(conn, date(2026, 9, 22), "failed", 2, "boom")
    store.record_day(conn, date(2026, 9, 22), "published", 2)
    s = store.stats(conn)
    assert (s.days_published, s.days_without_gazette, s.days_failed) == (1, 1, 0)


def test_clear_derived_keeps_the_watchlist_and_the_change_log(conn: sqlite3.Connection) -> None:
    store.import_watchlist(conn, [watched("G", "ACME SA", city="MADRID")])
    conn.execute(
        "INSERT INTO sync_log (company_id, field, written, state, applied_at)"
        " VALUES (1, 'city', 'X', 'applied', 'x')"
    )
    store.record_day(conn, date(2026, 9, 20), "no_gazette")
    store.record_day(conn, date(2026, 9, 21), "published", 3)
    store.clear_derived(conn)
    assert conn.execute("SELECT COUNT(*) FROM watched").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM seen_values").fetchone()[0] == 1  # watchlist
    assert [r[0] for r in conn.execute("SELECT outcome FROM issue_days")] == ["no_gazette"]


def test_activity_is_imported_and_never_synced(conn: sqlite3.Connection) -> None:
    store.import_watchlist(
        conn,
        [watched("G", "ACME, S.A.", activity="Tienda de bicicletas", purpose="Comercio")],
        date(2026, 1, 1),
    )
    conn.execute(
        "INSERT INTO seen_values (company_id, field, value, day, source, added_at)"
        " VALUES (1, 'purpose', 'La compraventa de bicicletas', '2026-02-01', 'registry', 'x')"
    )
    found = record.sync(conn, apply=True)
    assert [(p.field, p.proposed) for p in found] == [("purpose", "La compraventa de bicicletas")]
    row = conn.execute("SELECT purpose, activity FROM watched").fetchone()
    assert tuple(row) == ("La compraventa de bicicletas", "Tienda de bicicletas")


def _status_after(conn: sqlite3.Connection, rows: list[tuple[str, str]], later: str) -> str | None:
    store.import_watchlist(conn, [watched("G", "ACME, S.A.")], date(2026, 1, 1))
    for value, day in rows:
        conn.execute(
            "INSERT INTO seen_values (company_id, field, value, day, source, added_at)"
            " VALUES (1, 'status', ?, ?, 'registry', 'x')",
            (value, day),
        )
    conn.execute(
        "INSERT INTO gazette_docs (doc_id, section, province, day, issue_no, xml_url, read_at)"
        " VALUES ('D', 'A', 'MADRID', ?, 1, 'u', 'x')",
        (later,),
    )
    conn.execute(
        "INSERT INTO notices (doc_id, num, province, day, fact_day, firm, firm_key, company_id,"
        " kept_as) VALUES ('D', 1, 'MADRID', ?, ?, 'ACME SA', 'ACME SA', 1, 'watched')",
        (later, later),
    )
    row = conn.execute(
        "SELECT value FROM record_now WHERE company_id = 1 AND field = 'status'"
    ).fetchone()
    return None if row is None else row[0]


def test_a_merger_end_is_ignored_when_the_company_keeps_publishing(
    conn: sqlite3.Connection,
) -> None:
    # Dissolved and extinct the same day (absorbed), yet acts follow: not this company's end.
    rows = [("dissolved", "2016-08-02"), ("extinct", "2016-08-02")]
    assert _status_after(conn, rows, "2020-01-01") is None


def test_a_dissolution_alone_stands_while_liquidators_publish(conn: sqlite3.Connection) -> None:
    assert _status_after(conn, [("dissolved", "2016-08-02")], "2016-12-01") == "dissolved"
