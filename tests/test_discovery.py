"""Discovery signals: sites, mention slots, brand tokens, their order and crowded sites."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from borme_radar import discovery
from borme_radar.discovery import (
    GroupIndex,
    detect,
    is_valid_token_shape,
    load_tokens,
    mention_keys,
    registered_site,
    same_site,
    signals,
    site,
)


@pytest.fixture(autouse=True)
def _generic(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tested with a known list; the shipped one is measured (formats).
    monkeypatch.setattr(discovery, "GENERIC_STREET_WORDS", frozenset({"GENERAL", "MAYOR"}))


def at(street: str, city: str = "VILLAEJEMPLO"):  # type: ignore[no-untyped-def]
    found = site(street, city)
    assert found is not None
    return found


def test_a_site_is_the_number_the_words_and_the_town() -> None:
    a = at("CL TALAVERO PIMIENTA NUM.44 BAJO 28045")
    assert (a.street.portal, a.street.words) == ("44", frozenset({"TALAVERO", "PIMIENTA"}))
    assert a.label == "PIMIENTA TALAVERO 44 (VILLAEJEMPLO)"
    assert site("RONDA DE LA COMUNICACION S/N", "VILLAEJEMPLO") is None  # no number
    assert site("C/ GENERAL 9", "VILLAEJEMPLO") is None  # generic words only
    assert site("CL TALAVERO 44", None) is None


def test_on_a_road_the_kilometre_point_places_it() -> None:
    a = at("CTRA NACIONAL 120 KM 17,4")
    assert a.label == "NACIONAL KM 17.4 (VILLAEJEMPLO)"
    assert same_site(a, at("CARRETERA NACIONAL 120, KM 17,400"))
    assert not same_site(a, at("CTRA NACIONAL 120 KM 17,9"))
    assert same_site(at("CTRA N-120 KM 17,4"), at("CARRETERA N-120 KM 17,400"))


def test_generic_words_alone_never_make_the_same_site() -> None:
    assert not same_site(at("C/ GENERAL TALAVERO 9"), at("C/ GENERAL PIMIENTA 9"))
    assert same_site(at("C/ GENERAL TALAVERO 9"), at("CALLE GENERAL TALAVERO, 9"))
    assert not same_site(at("C/ GENERAL TALAVERO 9"), at("C/ GENERAL TALAVERO 9", "OTRAVILLA"))
    assert same_site(
        at("CL TALAVERO 9", "DONOSTIA/SAN SEBASTIAN"), at("CL TALAVERO 9", "SAN SEBASTIAN")
    )


def test_registered_site_from_incorporation_or_change() -> None:
    inc = "Objeto social: X. Domicilio: CL TALAVERO 5 (SORIA). Capital: 3.000,00 Euros"
    assert registered_site("incorporation", inc) == site("CL TALAVERO 5", "SORIA")
    change = registered_site("address_change", "CL TALAVERO 5 (SORIA)")
    assert change == site("CL TALAVERO 5", "SORIA")
    assert registered_site("capital_increase", "Capital: 1,00 Euros") is None


def index(**kwargs: object) -> GroupIndex:
    base: dict[str, object] = {"groups": {1: "G"}, "mention_names": {}, "hqs": [], "tokens": {}}
    base.update(kwargs)
    return GroupIndex(**base)  # type: ignore[arg-type]


def test_mention_is_a_watched_company_in_a_relation_slot() -> None:
    idx = index(mention_names=mention_keys([("ACS, S.A.", 1)]))
    owned = detect("X SL", [("sole_shareholder_declared", "Socio único: ACS SA")], idx)
    assert owned is not None and owned.evidence == 'owned_by "ACS SA"'
    directed = detect("X SL", [("appointment", "Adm. Unico: ACS, S.A.")], idx)
    assert directed is not None and directed.evidence == 'directed_by "ACS SA"'
    # A longer name that contains it is another company.
    assert detect("X SL", [("merger", "Sociedades absorbidas: INGENIERIA ACS SA")], idx) is None
    # On the board, with powers of attorney or as auditor: not a group relation.
    assert detect("X SL", [("appointment", "Consejero: ACS SA")], idx) is None
    assert detect("X SL", [("appointment", "Apoderado: ACS SA")], idx) is None


def test_section_b_listed_companies_are_mentions() -> None:
    idx = index(mention_names=mention_keys([("ACS, S.A.", 1)]))
    acts = [("merger_project", "Listed with: OTRA SL; ACS SA. Absorbidas: TERCERA SL")]
    found = detect("X SL", acts, idx)
    assert found is not None and found.evidence == 'co_listed "ACS SA"'


def test_bare_names_only_when_long_enough() -> None:
    keys = mention_keys([("MATRIZ GRUPO EJEMPLO, S.A.", 1), ("CORTO, S.A.", 2)], 12, 3)
    assert "MATRIZ GRUPO EJEMPLO" in keys  # 20 characters, 3 words
    assert "CORTO" not in keys
    assert "CORTO SA" in keys


def test_signal_order_and_crowded_address() -> None:
    hq = at("CL TALAVERO PIMIENTA 44", "MADRID")
    idx = index(
        mention_names={"MATRIZ SA": 1},
        hqs=[(hq, 1)],
        tokens={"MARCA": 1},
    )
    acts = [("address_change", "CL TALAVERO PIMIENTA 44 (MADRID)")]
    assert [c.reason for c in signals("MARCA NUEVA SL", acts, idx)] == ["address", "token"]
    idx.hq_noise[hq.label] = 10  # an office building shared by many: not used
    assert [c.reason for c in signals("MARCA NUEVA SL", acts, idx)] == ["token"]
    assert detect("OTRA SL", acts, idx) is None


def test_token_matches_whole_words_two_word_tokens_first() -> None:
    idx = index(tokens={"BANCO EJEMPLO": 1, "EJEMPLO": 2}, groups={1: "A", 2: "B"})
    found = detect("BANCO EJEMPLO SERVICIOS SL", [], idx)
    assert found is not None and found.evidence == 'token "BANCO EJEMPLO"'
    assert detect("EJEMPLOS SL", [], idx) is None


def test_person_signal_skips_personal_holdings_and_hides_the_pair() -> None:
    idx = index(persons={"ALFA BETA": 1})
    found = detect("X SL", [], idx, person_pairs={"ALFA BETA"})
    assert found is not None and "ALFA" not in found.evidence
    assert detect("X SL", [], idx, person_pairs={"ALFA BETA"}, personal_holding=True) is None


@pytest.mark.parametrize(
    ("token", "why"),
    [
        ("SL", "legal-form word"),
        ("ACS", "shorter than 4 letters"),
        ("MADRID", "generic or geographic word"),
        ("UNO DOS TRES", "a token has one or two words"),
        ("REPSOL", None),
    ],
)
def test_token_shapes(token: str, why: str | None) -> None:
    assert is_valid_token_shape(token, frozenset({"MADRID"})) == why


def test_no_approved_file_means_no_tokens(tmp_path: Path) -> None:
    assert load_tokens(None) == {}
    assert load_tokens(tmp_path / "missing.json") == {}
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"Marca  Uno": "G"}), encoding="utf-8")
    assert load_tokens(path) == {"MARCA UNO": "G"}
