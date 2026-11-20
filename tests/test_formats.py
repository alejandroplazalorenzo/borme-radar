"""The values ``formats`` derives from the corpus: cut lengths, company words, street filler."""

from __future__ import annotations

from collections import Counter
from datetime import date

from borme_radar import formats, officers
from borme_radar.corpus import Seen
from conftest import announcement, document


def seen_of(*acts: tuple[str, str]) -> Seen:
    ann = announcement(1, "EJEMPLO SL", *acts)
    _, doc = document("BORME-A-2026-1-28", date(2026, 1, 2), ann)
    return Seen(doc, ann, "EJEMPLO SL", None)


def test_the_cut_length_is_where_the_counts_rise_again() -> None:
    falling = {20: 900, 21: 1000, 22: 800, 23: 600, 24: 400, 25: 300, 26: 290}
    assert formats.cut_length(falling | {27: 350, 28: 600}) == 27
    assert formats.cut_length(falling | {27: 300}) is None  # a rise under 5% is noise
    assert formats.cut_length(falling) is None
    assert formats.cut_length({}) is None


def test_company_words_need_holders_names_and_legal_forms() -> None:
    total = {"INVERSIONES": 5000, "DELOITTE": 9000, "GARCIA": 90000, "RARA": 300}
    with_form = {"INVERSIONES": 4600, "DELOITTE": 8990, "GARCIA": 300, "RARA": 300}
    names = {"INVERSIONES": 4000, "DELOITTE": 12, "GARCIA": 40000, "RARA": 290}
    assert formats.company_words(with_form, total, names) == ["INVERSIONES"]


def test_street_filler_by_where_the_word_sits() -> None:
    words = {
        "CALLE": (9000, 8000, 0, 0),  # opens the street
        "PLANTA": (4000, 0, 100, 3800),  # after the house number
        # Right before the number: "NUMERO", but also the last word of a street name.
        "NUMERO": (3000, 0, 2900, 0),
        "GOYA": (5000, 50, 4600, 10),
        "BAJO": (500, 0, 0, 500),  # too rare to decide
        # After the number, but a town: "... 5 ELCHE" and "CALLE ELCHE 5".
        "ELCHE": (3000, 0, 300, 2000),
    }
    towns = {"ELCHE": 40000, "CALLE": 3}
    assert formats.street_filler(words, towns) == ["CALLE", "PLANTA"]


def test_the_words_of_the_legal_forms_are_not_company_words() -> None:
    total = {"SAS": 2500, "SOCIEDAD": 150000, "GESTION": 40000}
    with_form = dict(total)
    names = {"SAS": 900, "SOCIEDAD": 90000, "GESTION": 9000}
    assert formats.company_words(with_form, total, names) == ["GESTION"]


def test_registry_streets_count_where_each_word_sits() -> None:
    streets = formats.RegistryStreets()
    streets.observe(
        seen_of(
            ("address_change", "CALLE MAYOR NUMERO 5 PLANTA 2 (MADRID)"),
            ("incorporation", "Objeto social: Comercio. Domicilio: CALLE SOL 3 (CIUDAD)"),
        )
    )
    assert streets.words["CALLE"] == [2, 2, 0, 0]
    assert streets.words["NUMERO"] == [1, 0, 1, 0]
    assert streets.words["PLANTA"] == [1, 0, 1, 1]
    assert streets.city_words == Counter({"MADRID": 1, "CIUDAD": 1})
    assert streets.lengths == Counter({len("CALLE MAYOR NUMERO 5 PLANTA 2"): 1, 11: 1})


def test_holders_and_the_company_split_with_derived_values() -> None:
    seen = seen_of(
        ("appointment", "Consejero: PERSONA UNO;EJEMPLO GESTORES SL. Apoderado: GESTORES DOS"),
        ("appointment", "Apoderado: la Junta General acuerda la modificación del artículo 5"),
    )
    holders = formats.Holders()
    holders.observe(seen)
    assert (holders.counts["pieces"], holders.counts["rejected_prose"]) == (4, 1)
    assert holders.words_total["GESTORES"] == 2
    assert holders.words_with_form["GESTORES"] == 1
    assert holders.words_names["GESTORES"] == 2
    plain_split = formats.CompanySplit(officers.company_vocabulary(frozenset()), 49)
    plain_split.observe(seen)
    measured = formats.CompanySplit(officers.company_vocabulary(frozenset({"GESTORES"})), 49)
    measured.observe(seen)
    assert (plain_split.counts["company"], plain_split.counts["person"]) == (1, 2)
    assert (measured.counts["company"], measured.counts["person"]) == (2, 1)


def test_a_form_anywhere_marks_the_holder_for_the_company_words() -> None:
    assert formats.carries_form("EJEMPLO BANK PLC SUCURSAL EN ESPAÑA")
    assert formats.carries_form("EJEMPLO GESTION SL")
    assert not formats.carries_form("FUNDACION EJEMPLO")
    assert not formats.carries_form("APELLIDO APELLIDO NOMBRE")


def test_the_role_of_a_label_is_read_from_its_letters() -> None:
    reads = {
        "ADMUNICO": "sole_director",
        "ADMMANCOM": "joint_director",
        "ADMCONJUNTO": "joint_director",
        "CONSDELSOL": "managing_director",
        "APOMANSOLI": "attorney",
        "MIEMCOMCTR": "committee_member",
        "PRESCOMCTR": "committee_member",
        "PRESIDENTE": "chair",
        "GERENTE": None,
    }
    assert {key: officers.role_of_label(key) for key in reads} == reads


def test_role_labels_lists_the_labels_used_most_that_have_a_role() -> None:
    uses = {"APODERADO": 900, "ADMUNICO": 500, "GERENTE": 300, "ADMUNIC": 5}
    assert formats.role_labels(uses, min_uses=100) == [
        ("APODERADO", "attorney"),
        ("ADMUNICO", "sole_director"),
    ]


def test_generic_street_words_are_shared_by_many_street_names() -> None:
    # word -> (streets, opening, before number, after number, town addresses, street names)
    counts = {
        "SAN": (9000, 0, 10, 0, 0, 800),
        "CALLE": (9000, 8000, 0, 0, 0, 900),
        "GOYA": (4000, 0, 3000, 0, 0, 12),
        "DE": (9000, 0, 0, 0, 0, 900),
    }
    assert formats.generic_street_words(counts, frozenset({"CALLE"}), min_names=500) == ["SAN"]
