"""Company record: observations, resolution rules and sync (show / apply / revert)."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from borme_radar import record, store
from borme_radar.discovery import GroupIndex
from borme_radar.matching import Matcher
from borme_radar.pipeline import Pipeline
from borme_radar.record import amount_after, observations_of_act, street_and_city
from conftest import announcement, document, watched


def obs(act_type: str, detail: str) -> list[tuple[str, str]]:
    return [(o.field, o.value) for o in observations_of_act(act_type, detail)]


def test_capital_is_the_resulting_subscribed_capital_not_the_increment() -> None:
    detail = "Capital: 194.483,60 Euros. Resultante Suscrito: 200.493,60 Euros"
    assert obs("capital_increase", detail) == [("capital", "200493.60")]
    assert amount_after("Importe reducción: 5.000,00 Euros", "Resultante Suscrito") is None
    # Amounts in pesetas, in older acts, are not read.
    assert amount_after("Resultante Suscrito: 6.000.000 Ptas", "Resultante Suscrito") is None


def test_incorporation_is_split_into_purpose_address_and_capital() -> None:
    detail = (
        "Comienzo de operaciones: 1.09.26. Objeto social: La venta de bicicletas. "
        "Domicilio: C/ ROMERO, 38 06240 (FUENTE DE CANTOS). Capital: 3.000,00 Euros"
    )
    assert obs("incorporation", detail) == [
        ("purpose", "La venta de bicicletas"),
        ("address", "C/ ROMERO, 38 06240"),
        ("city", "FUENTE DE CANTOS"),
        ("capital", "3000.00"),
    ]


@pytest.mark.parametrize(
    ("text", "street", "city"),
    [
        ("C/ ROMERO, 38 06240 (FUENTE DE CANTOS)", "C/ ROMERO, 38 06240", "FUENTE DE CANTOS"),
        # Municipalities as the 2026 documents of the cache write them (streets invented):
        # "(EJIDO (EL))" in BORME-A-2026-91-04, "(PORRIÑO (O))" in BORME-A-2026-28-36.
        ("CL MAYOR 5 (EJIDO (EL)", "CL MAYOR 5", "EL EJIDO"),
        ("AV DO PORTO 7 (PORRIÑO (O))", "AV DO PORTO 7", "O PORRIÑO"),
        ("CL MAYOR 1(R.M. MADRID) (MADRID)", "CL MAYOR 1", "MADRID"),
        ("SIN MUNICIPIO 3", "SIN MUNICIPIO 3", None),
    ],
)
def test_street_and_city(text: str, street: str, city: str | None) -> None:
    assert street_and_city(text) == (street, city)


def test_status_and_name_observations() -> None:
    assert obs("extinction", "") == [("status", "extinct")]
    assert obs("name_change", "NUEVO NOMBRE SL(R.M. MADRID)") == [("name", "NUEVO NOMBRE SL")]


def test_each_field_is_compared_in_its_own_terms() -> None:
    assert not record.differs("capital", "4500.5", "4500.50")
    assert record.differs("capital", "4500.5", "4500.51")
    assert not record.differs("name", "ACME, S.L.", "ACME SOCIEDAD LIMITADA")
    assert not record.differs("purpose", "la venta de pan", "LA VENTA DE PAN.")
    assert record.differs("status", None, "dissolved")


# ---------------------------------------------------------------- with the database


def setup(conn: sqlite3.Connection) -> Pipeline:
    store.import_watchlist(conn, [watched("Grupo", "ACME, S.A.", city="MADRID")], date(2026, 1, 1))
    return Pipeline(
        conn,
        Matcher(store.load_watch_index(conn)),
        GroupIndex(groups={}, mention_names={}, hqs=[], tokens={}),
    )


def load(pipeline: Pipeline, day: date, *anns) -> None:  # type: ignore[no-untyped-def]
    ref, doc = document(f"BORME-A-{day:%Y%m%d}-28", day, *anns)
    pipeline.process_document(ref, doc)


def current(conn: sqlite3.Connection, field: str) -> str | None:
    row = conn.execute("SELECT value FROM record_now WHERE field = ?", (field,)).fetchone()
    return None if row is None else row[0]


def test_registry_beats_watchlist_and_latest_fact_wins(conn: sqlite3.Connection) -> None:
    p = setup(conn)
    assert current(conn, "city") == "MADRID"  # watchlist observation
    load(
        p,
        date(2026, 3, 2),
        announcement(
            1, "ACME SA", ("address_change", "CL X 1 (TOLEDO)"), inscribed=date(2026, 2, 20)
        ),
    )
    load(
        p,
        date(2026, 5, 4),
        announcement(
            2, "ACME SA", ("address_change", "CL Y 2 (SORIA)"), inscribed=date(2026, 4, 28)
        ),
    )
    assert current(conn, "city") == "SORIA"


def test_same_day_statuses_resolve_by_lifecycle(conn: sqlite3.Connection) -> None:
    p = setup(conn)
    day = date(2026, 3, 2)
    load(
        p,
        day,
        announcement(
            1,
            "ACME SA",
            ("extinction", ""),
            ("insolvency", "Auto"),
            ("dissolution", "Voluntaria"),
            inscribed=day,
        ),
    )
    assert current(conn, "status") == "extinct"


def test_extinction_is_ignored_if_the_company_keeps_publishing(conn: sqlite3.Connection) -> None:
    p = setup(conn)
    load(
        p,
        date(2026, 3, 2),
        announcement(1, "ACME SA", ("extinction", ""), inscribed=date(2026, 3, 1)),
    )
    assert current(conn, "status") == "extinct"
    load(
        p,
        date(2026, 6, 1),
        announcement(
            2,
            "ACME SA",
            ("capital_increase", "Capital: 1,00 Euros. Resultante Suscrito: 61.000,00 Euros"),
            inscribed=date(2026, 5, 20),
        ),
    )
    assert current(conn, "status") is None


def test_sync_shows_then_applies_logs_and_reverts(conn: sqlite3.Connection) -> None:
    p = setup(conn)
    load(
        p,
        date(2026, 3, 2),
        announcement(
            1,
            "ACME SA",
            ("capital_increase", "Capital: 1,00 Euros. Resultante Suscrito: 61.000,00 Euros"),
            inscribed=date(2026, 3, 1),
        ),
    )

    shown = record.sync(conn)
    assert [(p.field, p.held, p.proposed, p.why) for p in shown] == [
        ("capital", None, "61000.00", "registry")
    ]
    assert conn.execute("SELECT capital FROM watched").fetchone()[0] is None  # not written

    record.sync(conn, apply=True)
    assert conn.execute("SELECT capital FROM watched").fetchone()[0] == 61000.0
    log = conn.execute("SELECT replaced, written, state FROM sync_log").fetchall()
    assert [tuple(r) for r in log] == [(None, "61000.00", "applied")]
    assert record.sync(conn) == []  # in sync now

    # The evidence disappears (e.g. a matching fix and a rebuild): revert.
    conn.execute("DELETE FROM seen_values WHERE source = 'registry'")
    reverted = record.sync(conn, apply=True)
    assert [(p.why, p.proposed) for p in reverted] == [("unbacked", "")]
    assert conn.execute("SELECT capital FROM watched").fetchone()[0] is None
    assert conn.execute("SELECT state FROM sync_log").fetchone()[0] == "reverted"


def test_a_value_changed_by_hand_after_apply_is_not_reverted(conn: sqlite3.Connection) -> None:
    p = setup(conn)
    load(
        p,
        date(2026, 3, 2),
        announcement(
            1,
            "ACME SA",
            ("capital_increase", "Capital: 1,00 Euros. Resultante Suscrito: 61.000,00 Euros"),
            inscribed=date(2026, 3, 1),
        ),
    )
    record.sync(conn, apply=True)
    conn.execute("UPDATE watched SET capital = 99")
    conn.execute("DELETE FROM seen_values WHERE source = 'registry'")
    assert record.proposals(conn) == []
