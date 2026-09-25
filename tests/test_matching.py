"""Matcher: registry sheet first, exact name or alias next, similar names to review."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from borme_radar.matching import Matcher, WatchIndex
from borme_radar.normalize import normalize_name
from borme_radar.watchlist import load_watchlist


def matcher(*names: str, cutoff: float = 90.0, incorporated: date | None = None) -> Matcher:
    index = WatchIndex()
    for i, name in enumerate(names, start=1):
        index.add_name(normalize_name(name), i)
        index.incorporated[i] = incorporated
    return Matcher(index, fuzzy_cutoff=cutoff, guard_days=31)


def test_exact_normalised_name_matches() -> None:
    m = matcher("Telefónica, S.A.").match("TELEFONICA SOCIEDAD ANONIMA", "MADRID", None)
    assert (m.company_id, m.via) == (1, "name")


def test_sheet_wins_and_survives_a_rename() -> None:
    m = matcher("ACME SA")
    m.learn_sheet("MADRID", "M 1", 1)
    result = m.match("NOMBRE NUEVO TOTALMENTE DISTINTO SL", "MADRID", "M 1")
    assert (result.company_id, result.via) == (1, "sheet")
    # Same sheet number in another province is another company.
    assert m.match("NOMBRE NUEVO SL", "TOLEDO", "M 1").company_id is None


def test_similar_name_is_never_matched_only_suggested() -> None:
    result = matcher("INDRA SISTEMAS SA").match("INDRA SISTEMSA SA", "MADRID", None)
    assert result.company_id is None
    assert result.suggestion == 1
    assert 90 <= result.score < 100


def test_same_name_other_legal_form_goes_to_review() -> None:
    result = matcher("REPSOL SA").match("REPSOL SL", "MADRID", None)
    assert (result.company_id, result.suggestion, result.score) == (None, 1, 100.0)
    assert "legal form SL vs SA" in result.reason


def test_subsidiaries_and_prefixes_are_not_similar() -> None:
    m = matcher("TELEFONICA SA", "ACCIONA SA")
    for name in ("TELEFONICA DE ESPANA SA", "ACCIONA ENERGIA SA"):
        assert m.match(name, "MADRID", None).suggestion is None


@pytest.mark.parametrize(("cutoff", "suggested"), [(90.0, False), (80.0, True)])
def test_cutoff_controls_the_review_queue(cutoff: float, suggested: bool) -> None:
    # "ENAGAS" vs "ENAGAZ": fuzz.ratio 83.3
    result = matcher("ENAGAS SA", cutoff=cutoff).match("ENAGAZ SA", "MADRID", None)
    assert (result.suggestion is not None) is suggested


def test_acts_long_before_incorporation_belong_to_a_homonym() -> None:
    m = matcher("NUEVA EMPRESA SL", incorporated=date(2026, 6, 1))
    old = m.match("NUEVA EMPRESA SL", "MADRID", None, date(2020, 1, 10))
    assert old.company_id is None
    assert "before incorporation" in old.reason
    # Within the guard window the act is attributed.
    assert m.match("NUEVA EMPRESA SL", "MADRID", None, date(2026, 5, 15)).company_id == 1
    m.learn_sheet("MADRID", "M 9", 1)
    assert m.match("X", "MADRID", "M 9", date(2020, 1, 10)).company_id is None


def test_a_sheet_closed_by_the_announcement_is_not_learnable() -> None:
    m = matcher("ACME SA")
    assert m.sheet_learnable("MADRID", "M 1", ["appointment"])
    assert not m.sheet_learnable("MADRID", "M 1", ["dissolution", "extinction"])
    assert not m.sheet_learnable("MADRID", "M 1", ["registry_sheet_closure"])
    m.learn_sheet("MADRID", "M 1", 1)
    assert not m.sheet_learnable("MADRID", "M 1", ["appointment"])  # already known


def test_aliases_register_once() -> None:
    index = WatchIndex()
    assert index.add_name("ACME SA", 1)
    assert not index.add_name("ACME SA", 2)
    assert index.by_name["ACME SA"] == 1


def test_example_watchlist_is_valid() -> None:
    example = Path(__file__).parents[1] / "examples" / "watchlist.csv"
    entries = load_watchlist(example)
    assert len(entries) >= 30
    assert len({e.group for e in entries}) == len(entries)
    assert all(e.norm.endswith(" SA") for e in entries)


def test_watchlist_requires_name_column(tmp_path: Path) -> None:
    path = tmp_path / "watch.csv"
    path.write_text("company\nACME SA\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'name' column"):
        load_watchlist(path)


def test_watchlist_normalises_and_deduplicates(tmp_path: Path) -> None:
    path = tmp_path / "watch.csv"
    path.write_text(
        'group,name,capital\nG,"Telefónica, S.A.",1000\nG,TELEFONICA SA,\nH,"Repsol, S.A.",\n',
        encoding="utf-8",
    )
    entries = load_watchlist(path)
    assert [(e.group, e.norm, e.capital) for e in entries] == [
        ("G", "TELEFONICA SA", 1000.0),
        ("H", "REPSOL SA", None),
    ]
