"""Discovery signals: address keys, mention slots, brand tokens, order and degradation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from borme_radar.discovery import (
    GroupIndex,
    address_key,
    detect,
    is_valid_token_shape,
    load_tokens,
    mention_keys,
    registered_address,
    same_address,
    signals,
)


def key(street: str, city: str = "MADRID"):  # type: ignore[no-untyped-def]
    found = address_key(street, city)
    assert found is not None
    return found


def test_address_key_drops_street_type_entrance_detail_and_postcode() -> None:
    a = key("CL MENDEZ ALVARO NUM.44 BAJO 28045")
    assert a.street == frozenset({"MENDEZ", "ALVARO"})
    assert a.number == "44"
    assert a.city == "MADRID"


def test_no_number_no_signal() -> None:
    assert address_key("RONDA DE LA COMUNICACION S/N", "MADRID") is None


def test_road_kilometre_with_decimals_is_the_number() -> None:
    a = key("CTRA NACIONAL 120 KM 17,4", "VILLAEJEMPLO")
    b = key("CARRETERA NACIONAL 120, KM 17,400", "VILLAEJEMPLO")
    c = key("CTRA NACIONAL 120 KM 17,9", "VILLAEJEMPLO")
    assert a.number == "KM17.4"
    assert same_address(a, b)
    assert not same_address(a, c)
    d = key("CTRA N-120 KM 17,4", "VILLAEJEMPLO")
    assert d.street == frozenset({"N120"})
    assert same_address(d, key("CARRETERA N-120 KM 17,400", "VILLAEJEMPLO"))


def test_generic_street_words_alone_do_not_make_the_same_address() -> None:
    assert not same_address(key("C/ GENERAL ALFA 9"), key("C/ GENERAL BETA 9"))
    assert same_address(key("C/ GENERAL ALFA 9"), key("CALLE GENERAL ALFA, 9 BIS"))
    assert not same_address(key("C/ GENERAL ALFA 9"), key("C/ GENERAL ALFA 9", "TOLEDO"))


def test_registered_address_from_incorporation_or_change() -> None:
    inc = "Objeto social: X. Domicilio: CL MAYOR 5 (SORIA). Capital: 3.000,00 Euros"
    assert registered_address("incorporation", inc) == key("CL MAYOR 5", "SORIA")
    assert registered_address("address_change", "CL MAYOR 5 (SORIA)") == key("CL MAYOR 5", "SORIA")
    assert registered_address("capital_increase", "Capital: 1,00 Euros") is None


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


def test_signal_order_and_degraded_address() -> None:
    hq = key("CL MENDEZ ALVARO 44")
    idx = index(
        mention_names={"MATRIZ SA": 1},
        hqs=[(hq, 1)],
        tokens={"MARCA": 1},
    )
    acts = [("address_change", "CL MENDEZ ALVARO 44 (MADRID)")]
    assert [c.reason for c in signals("MARCA NUEVA SL", acts, idx)] == ["address", "token"]
    idx.hq_noise[hq.label] = 10  # shared office building: no longer decides alone
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
