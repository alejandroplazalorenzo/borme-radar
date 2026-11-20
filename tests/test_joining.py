"""Year each company joined its group: the rule, the evidence and the confirmation.

Each test writes the acts of an invented group (a parent with a board and the companies
around it) and checks the entry read from them.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from borme_radar import joining, store
from borme_radar.joining import Group, Member
from conftest import watched

FIRST = date(2009, 1, 2)
PARENT = "MATRIZ EJEMPLO SA"


def company(name: str, *acts: tuple[date, str, str], origin: str = "watchlist", **kw) -> Member:  # type: ignore[no-untyped-def]
    from borme_radar.normalize import normalize_name

    return Member(
        key=name,
        name=name,
        group_id=1,
        origin=origin,  # type: ignore[arg-type]
        names={normalize_name(name)},
        acts=list(acts),
        **kw,
    )


def parent() -> Member:
    board = (date(2013, 6, 3), "appointment", "Consejero: PERSONA UNO;PERSONA DOS")
    return company(PARENT, board, is_head=True)


def entries(*others: Member) -> dict[str, joining.Dated]:
    return {d.member.name: d for d in joining.analyse(Group(1, [parent(), *others]), FIRST)}


def test_set_up_with_the_group_as_sole_shareholder() -> None:
    sub = company(
        "NUEVA UNO SL",
        (date(2016, 4, 11), "incorporation", "Capital: 3.000,00 Euros"),
        (date(2016, 5, 2), "sole_shareholder_declared", f"Socio único: {PARENT}"),
        origin="candidate",
    )
    d = entries(sub)["NUEVA UNO SL"]
    assert (d.entry.rule, d.entry.confirmed, d.entry.day) == ("founded", True, date(2016, 4, 11))
    assert (d.year, d.evidence, d.confirmed) == (2016, "owner", True)
    assert d.entry.codes == ("founded:2016-04-11", f"owner:2016-05-02:{PARENT}")
    assert d.member.tie == f"owned_by:2016-05-02:{PARENT}"


def test_set_up_and_run_by_the_group_officers_is_not_confirmed() -> None:
    sub = company(
        "NUEVA DOS SL",
        (date(2016, 4, 11), "incorporation", "Capital: 3.000,00 Euros"),
        (date(2016, 4, 11), "appointment", "Adm. Solid.: PERSONA UNO;PERSONA DOS"),
    )
    d = entries(sub)["NUEVA DOS SL"]
    assert (d.entry.rule, d.year, d.evidence, d.confirmed) == ("founded", 2016, "officers", False)


def test_bought_from_an_outside_owner_on_the_inscribed_change() -> None:
    sub = company(
        "ADQUIRIDA SL",
        (date(2011, 9, 1), "incorporation", "Capital: 3.000,00 Euros"),
        (date(2011, 9, 30), "sole_shareholder_declared", "Socio único: OTRO DUEÑO SL"),
        (date(2019, 2, 14), "sole_shareholder_change", PARENT),
        (date(2019, 2, 14), "appointment", f"Adm. Unico: {PARENT}"),
        origin="candidate",
    )
    d = entries(sub)["ADQUIRIDA SL"]
    assert (d.entry.rule, d.entry.confirmed, d.entry.day) == ("acquired", True, date(2019, 2, 14))
    assert "outside_owner:2011-09-30" in d.entry.codes
    assert "founded_before:2011-09-01" in d.entry.codes
    # The officers alone (the group company as sole director) date it as well.
    assert (d.officers_only.rule, d.officers_only.evidence) == ("acquired", "officers")


def test_a_handover_of_the_body_dates_a_purchase_by_the_officers() -> None:
    sub = company(
        "TRASPASADA SL",
        (date(2015, 1, 20), "appointment", "Adm. Unico: PERSONA TRES"),
        (date(2020, 10, 5), "officer_removal", "Adm. Unico: PERSONA TRES"),
        (date(2020, 10, 19), "appointment", "Consejero: PERSONA DOS;PERSONA CUATRO"),
    )
    d = entries(sub)["TRASPASADA SL"]
    assert (d.entry.rule, d.entry.evidence, d.entry.day) == (
        "acquired",
        "officers",
        date(2020, 10, 19),
    )
    assert d.entry.codes[-1] == "handover:14d"


def test_outsiders_leaving_long_before_are_not_a_handover() -> None:
    sub = company(
        "FAMILIAR SL",
        (date(2015, 1, 20), "officer_removal", "Adm. Unico: PERSONA TRES"),
        (date(2020, 10, 19), "appointment", "Consejero: PERSONA DOS"),
    )
    d = entries(sub)["FAMILIAR SL"]
    assert (d.entry.rule, d.entry.day, d.year) == ("already_in", None, None)


def test_already_the_groups_when_first_seen_has_no_year() -> None:
    acts = [(date(2010, 6, 1), "appointment", "Consejero: PERSONA DOS")]
    d = entries(company("VETERANA SL", *acts))["VETERANA SL"]
    assert (d.entry.rule, d.year, d.evidence) == ("already_in", None, None)
    assert d.entry.codes == ("group_from:2010-06-01",)
    # Unless the watchlist gives an incorporation inside the years of the gazette.
    known = company("VETERANA DOS SL", *acts, incorporated=date(2009, 11, 2))
    d = entries(known)["VETERANA DOS SL"]
    assert (d.entry.rule, d.year, d.evidence, d.confirmed) == (
        "listed",
        2009,
        "incorporation",
        False,
    )


def test_no_tie_leaves_the_incorporation_year() -> None:
    sub = company("SIN LAZO SL", (date(2014, 9, 9), "incorporation", "Capital: 3.000,00 Euros"))
    d = entries(sub)["SIN LAZO SL"]
    assert (d.entry.rule, d.entry.day) == ("untied", None)
    assert (d.year, d.evidence, d.confirmed) == (2014, "incorporation", False)


def test_a_candidate_with_the_group_only_on_its_body_is_left_out() -> None:
    related = company(
        "PARTICIPADA SL",
        (date(2020, 1, 1), "appointment", f"Consejero: {PARENT};PERSONA CINCO"),
        origin="candidate",
    )
    assert "PARTICIPADA SL" not in entries(related)


def test_a_candidate_absorbing_a_group_company_is_in() -> None:
    absorbing = company(
        "ABSORBENTE SL",
        (date(2021, 3, 3), "merger", f"Sociedades absorbidas: {PARENT}"),
        origin="candidate",
    )
    assert entries(absorbing)["ABSORBENTE SL"].member.tie == f"absorbs:2021-03-03:{PARENT}"


def test_group_officers_sit_on_the_parent_or_on_three_companies() -> None:
    subs = [
        company(f"FILIAL {n} SL", (date(2015, 1, 1), "appointment", "Adm. Solid.: PERSONA SEIS"))
        for n in ("A", "B", "C")
    ]
    group = Group(1, [parent(), *subs])
    group.index()
    assert joining.group_officers(group) == {"PERSONA UNO", "PERSONA DOS", "PERSONA SEIS"}
    group = Group(1, [parent(), *subs[:2]])
    group.index()
    assert joining.group_officers(group) == {"PERSONA UNO", "PERSONA DOS"}


def test_the_officers_alone_set_against_the_owner_act() -> None:
    sub = company(
        "ADQUIRIDA SL",
        (date(2011, 9, 1), "incorporation", "Capital: 3.000,00 Euros"),
        (date(2019, 2, 14), "sole_shareholder_change", PARENT),
        (date(2020, 6, 1), "appointment", f"Adm. Unico: {PARENT}"),
        origin="candidate",
    )
    result = joining.officers_against_owner(list(entries(sub).values()))
    assert result["by_rule"] == {"acquired": {"officers, later year": 1}}
    assert result["officers_minus_owner_days"] == {
        "n": 1,
        "quantiles": {10: 473, 50: 473, 90: 473},
    }


def test_the_threshold_distributions() -> None:
    sub = company(
        "NUEVA UNO SL",
        (date(2016, 4, 11), "incorporation", "Capital: 3.000,00 Euros"),
        (date(2016, 5, 2), "sole_shareholder_declared", f"Socio único: {PARENT}"),
        (date(2016, 5, 9), "officer_removal", "Adm. Unico: PERSONA TRES"),
        origin="candidate",
    )
    found = joining.window_checks(list(entries(sub).values()))
    assert found == {
        "incorporation_to_first_group_sign_days": {"0-30": 1},
        "first_group_sign_to_nearest_outsider_leaving_days": {"0-7": 1},
    }


def test_apply_never_overwrites_a_year_typed_by_a_person(conn: sqlite3.Connection) -> None:
    store.import_watchlist(
        conn,
        [
            watched("G", PARENT, head=True, incorporated=date(1990, 1, 1)),
            watched("G", "FILIAL INVESTIGADA SL", joined=2003),
        ],
        date(2026, 1, 1),
    )
    conn.execute(
        "INSERT INTO gazette_docs (doc_id, section, province, day, issue_no, xml_url, read_at)"
        " VALUES ('D', 'A', 'MADRID', '2009-01-02', 1, 'u', 'x')"
    )
    first = joining.first_gazette_day(conn)
    assert first == date(2009, 1, 2)

    def dated() -> list[joining.Dated]:
        return [d for g in joining.load_groups(conn) for d in joining.analyse(g, first)]

    assert joining.apply(conn, dated()) == 1
    assert joining.apply(conn, dated()) == 0  # a second run finds nothing to write
    rows = conn.execute(
        "SELECT name, joined_year, joined_evidence, joined_confirmed, joined_manual FROM watched"
        " ORDER BY name"
    )
    assert [tuple(r) for r in rows] == [
        ("FILIAL INVESTIGADA SL", 2003, None, 0, 1),
        (PARENT, 1990, "incorporation", 0, 0),
    ]
