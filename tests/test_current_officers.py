"""Current officers worked out from the events (positions.py, view ``officers_now``).

Names are invented. Each test states a sequence of acts as the Registro publishes them
and the positions that should be left once they are all applied.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date
from itertools import groupby
from operator import attrgetter

import pytest

from borme_radar import positions, store
from borme_radar.officers import holder_key
from borme_radar.pipeline import derive_rows
from conftest import watched


@pytest.fixture
def company(conn: sqlite3.Connection) -> sqlite3.Connection:
    store.import_watchlist(conn, [watched("G", "ACME, S.A.")], date(2026, 1, 1))
    return conn


def publish(conn: sqlite3.Connection, *acts: tuple[str, str, str]) -> list[tuple[str, str]]:
    """Store the officer events of (day, act type, detail) and return who holds what."""
    for number, (day, act_type, detail) in enumerate(acts, 1):
        rows = derive_rows(1, "D", number, day, [(1, act_type, detail)])[1]
        store.insert_officer_events(conn, rows)
    positions.refresh(conn)
    held = conn.execute("SELECT role, holder FROM officers_now ORDER BY role, holder")
    return [(r["role"], r["holder"]) for r in held]


def test_a_removal_in_another_word_order_ends_the_position(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2019-02-11", "appointment", "Consejero: APELLIDOA APELLIDOB NOMBRE"),
        ("2023-07-20", "officer_removal", "Consejero: NOMBRE APELLIDOA APELLIDOB"),
    )
    assert held == []


def test_different_words_are_a_different_person(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2019-02-11", "appointment", "Consejero: APELLIDOA APELLIDOB NOMBRE"),
        ("2023-07-20", "officer_removal", "Consejero: APELLIDOA NOMBRE"),
    )
    assert held == [("board_member", "APELLIDOA APELLIDOB NOMBRE")]


def test_the_word_order_is_only_ignored_inside_one_role(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2019-02-11", "appointment", "Apoderado: APELLIDOA APELLIDOB NOMBRE"),
        ("2023-07-20", "officer_removal", "Consejero: NOMBRE APELLIDOA APELLIDOB"),
    )
    assert held == [("attorney", "APELLIDOA APELLIDOB NOMBRE")]


def test_a_new_auditor_replaces_the_one_of_its_class(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2017-05-30", "appointment", "Auditor: REVISORES UNO SL. Aud.Supl: REVISORES DOS SL"),
        ("2020-05-30", "appointment", "Auditor: REVISORES TRES SL"),
        ("2021-05-30", "reelection", "Aud.C.Con: REVISORES CUATRO SL"),
    )
    assert held == [
        ("auditor", "REVISORES CUATRO SL"),
        ("auditor", "REVISORES DOS SL"),
        ("auditor", "REVISORES TRES SL"),
    ]


def test_a_replaced_auditor_stays_replaced(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2017-05-30", "appointment", "Auditor: REVISORES UNO SL"),
        ("2020-05-30", "appointment", "Auditor: REVISORES TRES SL"),
        ("2022-01-15", "officer_removal", "Auditor: REVISORES TRES SL"),
    )
    assert held == []


def test_only_one_sole_director(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2014-01-08", "appointment", "Adm. Unico: PERSONA UNO"),
        ("2018-01-08", "appointment", "Adm. Unico: PERSONA DOS"),
    )
    assert held == [("sole_director", "PERSONA DOS")]


def test_a_new_form_of_the_body_ends_the_old_one(company: sqlite3.Connection) -> None:
    board = "Consejero: PERSONA UNO;PERSONA DOS. Presidente: PERSONA UNO"
    held = publish(
        company,
        ("2012-09-03", "appointment", board),
        ("2016-09-03", "appointment", "Adm. Solid.: PERSONA TRES;PERSONA CUATRO"),
    )
    assert held == [
        ("joint_several_director", "PERSONA CUATRO"),
        ("joint_several_director", "PERSONA TRES"),
    ]


def test_an_earlier_board_does_not_come_back_with_the_board(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2012-09-03", "appointment", "Consejero: PERSONA UNO;PERSONA DOS"),
        ("2016-09-03", "appointment", "Adm. Unico: PERSONA TRES"),
        ("2024-09-03", "appointment", "Consejero: PERSONA CINCO. Secretario: PERSONA SEIS"),
    )
    assert held == [("board_member", "PERSONA CINCO"), ("secretary", "PERSONA SEIS")]


def test_positions_appointed_the_same_day_stay(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2016-09-03", "appointment", "Adm. Unico: PERSONA TRES. Consejero: PERSONA CINCO"),
    )
    assert held == [("board_member", "PERSONA CINCO"), ("sole_director", "PERSONA TRES")]


def test_attorneys_do_not_depend_on_the_body(company: sqlite3.Connection) -> None:
    held = publish(
        company,
        ("2011-03-14", "appointment", "Apoderado: PERSONA UNO"),
        ("2019-03-14", "appointment", "Adm. Unico: PERSONA DOS"),
    )
    assert held == [("attorney", "PERSONA UNO"), ("sole_director", "PERSONA DOS")]


def test_the_person_key_counts_the_names_it_joins(company: sqlite3.Connection) -> None:
    publish(
        company,
        ("2019-02-11", "appointment", "Consejero: APELLIDOA APELLIDOB NOMBRE"),
        ("2023-07-20", "officer_removal", "Consejero: NOMBRE APELLIDOA APELLIDOB"),
        ("2023-07-20", "appointment", "Apoderado: PERSONA UNO"),
    )
    assert positions.reordered_names(company) == (3, 1, 2)


def replayed(events: list[positions.Event]) -> list[int]:
    """Reference: apply each company's events in date order, one day at a time."""
    held: dict[tuple[int, str, str], positions.Event] = {}
    for (company, day), batch in groupby(events, attrgetter("company_id", "day")):
        forms: set[str] = set()
        for ev in batch:
            slot = (company, holder_key(ev.holder), ev.role)
            held.pop(slot, None)
            if not ev.opens:
                continue
            for other, opened in list(held.items()):
                if other[0] == company and ev.seat and opened.seat == ev.seat and opened.day < day:
                    del held[other]
            held[slot] = ev
            if ev.role in positions.BODY_FORM:
                forms.add(positions.BODY_FORM[ev.role])
        for other, opened in list(held.items()):
            form = positions.BODY_FORM.get(opened.role)
            if other[0] == company and form and forms and form not in forms and opened.day < day:
                del held[other]
    return sorted(e.event_id for e in held.values())


def test_the_rule_per_position_is_what_a_replay_leaves() -> None:
    labels = [
        ("sole_director", "Adm. Unico"),
        ("joint_director", "Adm. Mancom."),
        ("joint_several_director", "Adm. Solid."),
        ("board_member", "Consejero"),
        ("secretary", "Secretario"),
        ("attorney", "Apoderado"),
        ("auditor", "Auditor"),
        ("auditor", "Aud.Supl"),
        ("auditor", "Aud.C.Con"),
        ("liquidator", "Liquidador"),
    ]
    holders = ["UNO DOS TRES", "TRES DOS UNO", "CUATRO CINCO", "CINCO CUATRO", "EJEMPLO SL"]
    kinds = ["appointment", "appointment", "reelection", "revocation", "officer_removal"]
    rng = random.Random(2026)
    for _ in range(2000):
        events = sorted(
            (
                positions.Event(
                    company_id=rng.randint(1, 2),
                    day=f"2020-01-{rng.randint(1, 6):02d}",
                    event_id=n,
                    holder=rng.choice(holders),
                    role=role,
                    role_raw=label,
                    kind=rng.choice(kinds),
                )
                for n, (role, label) in enumerate(rng.choices(labels, k=rng.randint(1, 20)), 1)
            ),
            key=attrgetter("company_id", "day", "event_id"),
        )
        assert positions.current_event_ids(events) == replayed(events)
