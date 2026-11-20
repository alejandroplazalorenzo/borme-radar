"""Pipeline on synthetic documents: sheets, orphans, aliases, minimisation, candidates."""

from __future__ import annotations

import sqlite3
from datetime import date

from borme_radar import store
from borme_radar.discovery import GroupIndex, mention_keys
from borme_radar.matching import Matcher
from borme_radar.pipeline import Pipeline
from conftest import announcement, document, watched

DAY1, DAY2, DAY3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)


def pipeline(conn: sqlite3.Connection, tokens: dict[str, int] | None = None, **kw) -> Pipeline:  # type: ignore[no-untyped-def]
    store.import_watchlist(
        conn, [watched("Grupo", "ACME, S.A."), watched("Otro", "BETA, S.A.")], date(2026, 1, 1)
    )
    groups = store.groups(conn)
    index = GroupIndex(
        groups=groups,
        mention_names=mention_keys(store.watched_names(conn)),
        hqs=[],
        tokens=tokens or {},
    )
    return Pipeline(conn, Matcher(store.load_watch_index(conn)), index, **kw)


def run(p: Pipeline, day: date, *anns, doc: str | None = None):  # type: ignore[no-untyped-def]
    ref, document_ = document(doc or f"BORME-A-{day:%Y%m%d}-28", day, *anns)
    return p.process_document(ref, document_)


def one(conn: sqlite3.Connection, sql: str) -> object:
    return conn.execute(sql).fetchone()[0]


def test_first_exact_match_learns_the_sheet_and_later_spellings_follow_it(
    conn: sqlite3.Connection,
) -> None:
    p = pipeline(conn)
    c = run(
        p, DAY1, announcement(1, "ACME SA", ("appointment", "Apoderado: PERSONA UNO"), sheet="M 1")
    )
    assert c["watched"] == 1 and c["sheets_learned"] == 1
    c = run(p, DAY2, announcement(2, "ACME IBERICA SA", ("website", "x"), sheet="M 1"))
    assert c["watched"] == 1
    assert one(conn, "SELECT via FROM notices WHERE num = 2") == "sheet"


def test_learning_a_sheet_relinks_stored_orphans(conn: sqlite3.Connection) -> None:
    p = pipeline(conn)
    # Same name, other legal form: in the review queue until the sheet is known.
    run(p, DAY1, announcement(1, "ACME SL", ("website", "x"), sheet="M 1"))
    assert one(conn, "SELECT kept_as FROM notices WHERE num = 1") == "review"
    c = run(
        p,
        DAY2,
        announcement(
            2,
            "ACME SA",
            ("capital_increase", "Capital: 1,00 Euros. Resultante Suscrito: 2,00 Euros"),
            sheet="M 1",
        ),
    )
    assert c["relinked"] == 1
    row = conn.execute("SELECT company_id, via, kept_as FROM notices WHERE num = 1")
    assert tuple(row.fetchone()) == (1, "sheet", "watched")
    assert one(conn, "SELECT COUNT(*) FROM notices WHERE near_company IS NOT NULL") == 0


def test_the_sheet_of_a_closing_announcement_is_not_learned(conn: sqlite3.Connection) -> None:
    p = pipeline(conn)
    c = run(
        p,
        DAY1,
        announcement(1, "ACME SA", ("dissolution", "Fusión"), ("extinction", ""), sheet="M 9"),
    )
    assert c["watched"] == 1 and c["sheets_learned"] == 0
    assert one(conn, "SELECT COUNT(*) FROM sheet_owner") == 0


def test_rename_keeps_both_names_as_aliases(conn: sqlite3.Connection) -> None:
    p = pipeline(conn)
    run(p, DAY1, announcement(1, "ACME SA", ("name_change", "ZETA NUEVA SA")))  # no sheet
    c = run(p, DAY2, announcement(2, "ZETA NUEVA, S.A.", ("website", "x")))
    assert c["watched"] == 1
    aliases = [r[0] for r in conn.execute("SELECT name_key FROM other_names")]
    assert aliases == ["ZETA NUEVA SA"]  # the old name is the watched name itself


def test_reprocessing_never_loses_a_match(conn: sqlite3.Connection) -> None:
    p = pipeline(conn)
    run(p, DAY1, announcement(1, "ACME SL", ("website", "x"), sheet="M 1"), doc="D1")
    run(p, DAY2, announcement(2, "ACME SA", ("website", "x"), sheet="M 1"), doc="D2")
    assert one(conn, "SELECT company_id FROM notices WHERE doc_id = 'D1'") == 1
    # A fresh matcher that has not learned the sheet reprocesses D1: the match stays.
    fresh = Pipeline(conn, Matcher(store.load_watch_index(conn)), p.groups)
    fresh.matcher.index.by_sheet.clear()
    run(fresh, DAY1, announcement(1, "ACME SL", ("website", "x"), sheet="M 1"), doc="D1")
    assert one(conn, "SELECT company_id FROM notices WHERE doc_id = 'D1'") == 1


def test_only_relevant_announcements_are_stored_but_everything_is_counted(
    conn: sqlite3.Connection,
) -> None:
    p = pipeline(conn)
    c = run(
        p,
        DAY1,
        announcement(
            1,
            "ACME SA",
            ("appointment", "Adm. Unico: PERSONA UNO"),
            sheet="M 1",
            inscribed=date(2026, 2, 23),
        ),
        announcement(
            2,
            "AJENA SL",
            ("appointment", "Apoderado: PERSONA DOS"),
            sheet="M 2",
            inscribed=date(2026, 2, 23),
        ),
    )
    assert (c["watched"], c["discarded"]) == (1, 1)
    assert one(conn, "SELECT COUNT(*) FROM notices") == 1
    assert one(conn, "SELECT n FROM doc_tally WHERE what = 'announcements'") == 2
    assert one(conn, "SELECT n FROM doc_tally WHERE what = 'kept'") == 1
    scopes = dict(conn.execute("SELECT scope, n FROM act_tally").fetchall())
    assert scopes == {"board": 1, "attorney": 1}
    assert dict(conn.execute("SELECT days, n FROM lag_tally").fetchall()) == {7: 2}
    # Officer names stay in the local database only.
    assert one(conn, "SELECT holder FROM role_changes") == "PERSONA UNO"


def test_store_all_keeps_everything(conn: sqlite3.Connection) -> None:
    p = pipeline(conn, store_all=True)
    run(p, DAY1, announcement(1, "AJENA SL", ("website", "x")))
    assert one(conn, "SELECT kept_as FROM notices") == "all"
    assert store.prune(conn) == 1


def test_candidates_are_stored_counted_and_rejudged(conn: sqlite3.Connection) -> None:
    p = pipeline(conn, tokens={"ACMETOKEN": 1})
    run(
        p,
        DAY1,
        announcement(1, "FILIAL NUEVA SL", ("sole_shareholder_declared", "Socio único: ACME SA")),
    )
    run(p, DAY2, announcement(2, "FILIAL NUEVA SL", ("website", "x")), doc="D2")
    run(p, DAY3, announcement(3, "ACMETOKEN SERVICIOS SL", ("website", "x")), doc="D3")
    rows = conn.execute(
        "SELECT firm_key, found_by, notice_count, state FROM leads ORDER BY 1"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("ACMETOKEN SERVICIOS SL", "token", 1, "open"),
        ("FILIAL NUEVA SL", "mention", 1, "open"),
    ]
    # Rules change: the token is withdrawn. The candidate found only by it is dismissed.
    p.groups = GroupIndex(p.groups.groups, p.groups.mention_names, [], {})
    verdicts = p.rejudge()
    assert verdicts["gone"] == 1
    status = dict(conn.execute("SELECT firm_key, state FROM leads").fetchall())
    assert status["ACMETOKEN SERVICIOS SL"] == "gone"
    assert status["FILIAL NUEVA SL"] == "open"


def test_a_candidate_that_becomes_watched_is_marked_incorporated(conn: sqlite3.Connection) -> None:
    p = pipeline(conn)
    run(
        p,
        DAY1,
        announcement(1, "FILIAL NUEVA SL", ("sole_shareholder_declared", "Socio único: ACME SA")),
    )
    store.import_watchlist(conn, [watched("Grupo", "FILIAL NUEVA, S.L.")])
    p.matcher = Matcher(store.load_watch_index(conn))
    assert p.rejudge()["watched"] == 1


def test_a_candidate_without_stored_acts_is_still_judged(conn: sqlite3.Connection) -> None:
    p = pipeline(conn, tokens={"ACMETOKEN": 1})
    run(p, DAY1, announcement(1, "ACMETOKEN SERVICIOS SL", ("website", "x")))
    conn.execute("UPDATE notices SET lead_id = NULL")  # e.g. pruned
    assert p.rejudge() == {}  # still pending: judged on its name
    assert one(conn, "SELECT state FROM leads") == "open"
    p.groups = GroupIndex(p.groups.groups, {}, [], {})
    assert p.rejudge()["gone"] == 1
