from __future__ import annotations

from pathlib import Path

import pytest

from borme_radar.matching import WatchEntry, load_watchlist, match_names
from borme_radar.normalize import normalize_name


def entry(name: str) -> WatchEntry:
    return WatchEntry(name, normalize_name(name))


def candidates(*names: str) -> dict[str, str]:
    return {normalize_name(n): n for n in names}


def test_exact_normalised_match_is_confirmed() -> None:
    found = match_names([entry("Telefónica, S.A.")], candidates("TELEFONICA SOCIEDAD ANONIMA"))
    assert [(m.status, m.score, m.company_name) for m in found] == [
        ("confirmed", 100.0, "TELEFONICA SOCIEDAD ANONIMA")
    ]


def test_similar_name_above_threshold_needs_review_and_is_never_confirmed() -> None:
    found = match_names([entry("INDRA SISTEMAS SA")], candidates("INDRA SISTEMSA SA"))
    assert len(found) == 1
    assert found[0].status == "needs_review"
    assert 90 <= found[0].score < 100


def test_same_name_with_other_legal_form_needs_review() -> None:
    found = match_names([entry("REPSOL SA")], candidates("REPSOL SL"))
    assert [(m.status, m.score) for m in found] == [("needs_review", 100.0)]
    assert "legal form SL vs SA" in found[0].reason


def test_subsidiaries_and_prefixes_do_not_match() -> None:
    # A token-set scorer would give 100 here; fuzz.ratio on the base name does not.
    found = match_names(
        [entry("TELEFONICA SA"), entry("ACCIONA SA")],
        candidates("TELEFONICA DE ESPANA SA", "ACCIONA ENERGIA SA", "ACCIONA ENERGIA SL"),
    )
    assert found == []


@pytest.mark.parametrize(("threshold", "expected"), [(90.0, 0), (80.0, 1)])
def test_threshold_controls_review_candidates(threshold: float, expected: int) -> None:
    # "ENAGAS" vs "ENAGAZ": one substitution in six letters, fuzz.ratio = 83.3
    found = match_names([entry("ENAGAS SA")], candidates("ENAGAZ SA"), threshold=threshold)
    assert len(found) == expected


def test_confirmed_come_before_review_candidates() -> None:
    found = match_names(
        [entry("IBERDROLA SA")], candidates("IBERDROLA SL", "IBERDROLA S.A.", "IBERDROLLA SA")
    )
    assert [m.status for m in found] == ["confirmed", "needs_review", "needs_review"]


def test_load_watchlist_normalises_and_deduplicates(tmp_path: Path) -> None:
    path = tmp_path / "watch.csv"
    path.write_text(
        'name,notes\n"Telefónica, S.A.",x\nTELEFONICA SA,dup\n\n"Repsol, S.A.",\n',
        encoding="utf-8",
    )
    entries = load_watchlist(path)
    assert [e.norm for e in entries] == ["TELEFONICA SA", "REPSOL SA"]


def test_load_watchlist_requires_name_column(tmp_path: Path) -> None:
    path = tmp_path / "watch.csv"
    path.write_text("company\nACME SA\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'name' column"):
        load_watchlist(path)


def test_example_watchlist_is_valid() -> None:
    example = Path(__file__).parents[1] / "examples" / "watchlist.csv"
    entries = load_watchlist(example)
    assert len(entries) >= 15
    assert all(e.norm.endswith(" SA") for e in entries)
