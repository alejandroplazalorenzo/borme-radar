from __future__ import annotations

import pytest

from borme_radar.normalize import base_and_form, has_legal_form, normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Legal forms: abbreviations, dotted forms and long forms converge.
        ("TELEFÓNICA, S.A.", "TELEFONICA SA"),
        ("Telefonica SA", "TELEFONICA SA"),
        ("TELEFONICA SOCIEDAD ANONIMA", "TELEFONICA SA"),
        ("NOVERA LIVING SOCIEDAD LIMITADA", "NOVERA LIVING SL"),
        ("NOVERA LIVING, S.L.", "NOVERA LIVING SL"),
        ("NOVERA LIVING S. L.", "NOVERA LIVING SL"),
        ("NOVERA LIVING SOCIEDAD DE RESPONSABILIDAD LIMITADA", "NOVERA LIVING SL"),
        # Final dot already stripped by the parser: "S.L" must still be recognised.
        ("SYNCHRONY TRADE S.L", "SYNCHRONY TRADE SL"),
        # Unipersonal variants are a status, not a different company.
        ("ACME S.L.U.", "ACME SL"),
        ("ACME SOCIEDAD LIMITADA UNIPERSONAL", "ACME SL"),
        ("ACME, S.A.U.", "ACME SA"),
        # Other forms keep their own canonical token.
        ("RE-PUEBLING S.L.L.", "RE PUEBLING SLL"),
        ("ESTUDIO SOCIEDAD LIMITADA PROFESIONAL", "ESTUDIO SLP"),
        ("GENERO DE DUDAS A.I.E.", "GENERO DE DUDAS AIE"),
        # Status suffix and registry annotations are dropped.
        ("CONSTRUCCIONES FARELO HERMANOS SA EN LIQUIDACION", "CONSTRUCCIONES FARELO HERMANOS SA"),
        ("FOO SOCIEDAD LIMITADA EN LIQUIDACIÓN", "FOO SL"),
        ("HSP IBIHOLI, S.L.(R.M. EIVISSA)", "HSP IBIHOLI SL"),
        # Financial year glued to the name in accounts filings, closed or not.
        ("APARTAMENTOS EJEMPLO SL(2008)", "APARTAMENTOS EJEMPLO SL"),
        ("APARTAMENTOS EJEMPLO SL(2009", "APARTAMENTOS EJEMPLO SL"),
        ("ISLAS EJEMPLO SL(R.M. MADRID)(2008)", "ISLAS EJEMPLO SL"),
        # Other status suffixes that come and go.
        ("TALLERES NORTE SL EN CONCURSO", "TALLERES NORTE SL"),
        ("TALLERES NORTE SA EN CONCURSO DE ACREEDORES", "TALLERES NORTE SA"),
        ("TALLERES NORTE SL UNIPERSONAL", "TALLERES NORTE SL"),
        ("TALLERES NORTE SL SOCIEDAD UNIPERSONAL", "TALLERES NORTE SL"),
        # Accents, Ñ and punctuation inside the name.
        ("INDUSTRIA DE DISEÑO TEXTIL, S.A.", "INDUSTRIA DE DISENO TEXTIL SA"),
        ("AENA, S.M.E., S.A.", "AENA SME SA"),
        ("A & K BAUSERVICE SL", "A K BAUSERVICE SL"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_initials_inside_a_name_are_not_taken_for_a_legal_form() -> None:
    assert normalize_name("J. GARCIA SL") == "J GARCIA SL"


def test_a_bare_legal_form_is_not_stripped_to_nothing() -> None:
    assert normalize_name("SL") == "SL"


def test_a_year_inside_the_name_is_kept() -> None:
    assert normalize_name("PROMOCIONES 2008 SL") == "PROMOCIONES 2008 SL"


def test_legal_person_detection() -> None:
    assert has_legal_form("REPSOL, S.A.")
    assert has_legal_form("X SOCIEDAD LIMITADA")
    assert not has_legal_form("PERSONA UNO DOS")


def test_base_and_form() -> None:
    assert base_and_form("TELEFONICA SA") == ("TELEFONICA", "SA")
    assert base_and_form("GENERO DE DUDAS AIE") == ("GENERO DE DUDAS", "AIE")
    assert base_and_form("SIN FORMA") == ("SIN FORMA", None)
