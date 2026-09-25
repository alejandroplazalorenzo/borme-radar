from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from borme_radar import store
from conftest import watched


def test_schema_is_versioned_and_v1_upgrades(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "x.sqlite")
    assert store.schema_version(conn) == 2
    assert store.migrate(conn) == 2  # re-running migrations is a no-op

    # A version 1 database (derived data only) upgrades in place.
    old = sqlite3.connect(tmp_path / "v1.sqlite")
    sql = (Path(store.__file__).parent / "sql" / "001_initial.sql").read_text(encoding="utf-8")
    old.executescript(f"BEGIN;{sql};PRAGMA user_version = 1;COMMIT;")
    old.close()
    upgraded = store.connect(tmp_path / "v1.sqlite")
    assert store.schema_version(upgraded) == 2
    assert upgraded.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_import_fills_only_what_the_registro_has_not_said(conn: sqlite3.Connection) -> None:
    store.import_watchlist(conn, [watched("G", "ACME, S.A.", city="MADRID")], date(2026, 1, 1))
    assert conn.execute("SELECT city FROM companies").fetchone()[0] == "MADRID"
    conn.execute(
        "INSERT INTO observations (company_id, field, value, obs_date, source, created_at)"
        " VALUES (1, 'city', 'TOLEDO', '2026-02-01', 'registry', 'x')"
    )
    result = store.import_watchlist(
        conn, [watched("G", "ACME SA", city="SEVILLA")], date(2026, 3, 1)
    )
    assert result.new_companies == 0 and result.fields_filled == 0
    assert conn.execute("SELECT city FROM companies").fetchone()[0] == "MADRID"
    sources = conn.execute("SELECT source, value FROM observations ORDER BY obs_id").fetchall()
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
        "INSERT INTO record_changes (company_id, field, new_value, status, applied_at)"
        " VALUES (1, 'city', 'X', 'applied', 'x')"
    )
    store.record_day(conn, date(2026, 9, 20), "no_gazette")
    store.record_day(conn, date(2026, 9, 21), "published", 3)
    store.clear_derived(conn)
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM record_changes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1  # watchlist
    assert [r[0] for r in conn.execute("SELECT status FROM days")] == ["no_gazette"]
