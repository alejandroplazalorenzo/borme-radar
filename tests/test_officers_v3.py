"""Officer holders: glued roles, prose, ID numbers, legal persons, person key, auditor class.

Snippets modelled on real Section A text; names of natural persons are invented.
"""

from __future__ import annotations

import pytest

from borme_radar import officers
from borme_radar.officers import auditor_class, holder_key, holdings, is_company_name


def pairs(detail: str) -> list[tuple[str, str]]:
    return [(h.role_raw, h.holder) for h in holdings(detail)]


def test_a_role_glued_to_the_last_holder_is_split_off() -> None:
    detail = "Consejero: PERSONA UNO;PERSONA DOSSecretario: PERSONA DOS. Cons.Del.Sol: PERSONA UNO"
    assert pairs(detail) == [
        ("Consejero", "PERSONA UNO"),
        ("Consejero", "PERSONA DOS"),
        ("Secretario", "PERSONA DOS"),
        ("Cons.Del.Sol", "PERSONA UNO"),
    ]
    detail = "Consejero: EJEMPLO DE LA CALLE SLSecretario: EJEMPLO DE LA CALLE SL"
    assert pairs(detail) == [
        ("Consejero", "EJEMPLO DE LA CALLE SL"),
        ("Secretario", "EJEMPLO DE LA CALLE SL"),
    ]


def test_a_stray_lowercase_letter_is_not_a_glued_role_and_still_a_name() -> None:
    # Published with a typo; there is no colon after it, so it is not a role.
    assert pairs("Adm. Mancom.: PERSONA UNO;PERSONA DOs TRES") == [
        ("Adm. Mancom", "PERSONA UNO"),
        ("Adm. Mancom", "PERSONA DOs TRES"),
    ]


def test_prose_and_text_after_the_holder_are_not_names() -> None:
    detail = "Adm. Unico: PERSONA UNO. Cancelaciones de oficio por caducidad del cargo"
    assert pairs(detail) == [("Adm. Unico", "PERSONA UNO")]
    assert holdings("Apoderado: la Junta General acuerda la modificación del artículo 5") == []


def test_a_name_in_mixed_case_is_still_a_name() -> None:
    assert pairs("Consejero: Persona Uno Ejemplo;Persona de la Dos") == [
        ("Consejero", "Persona Uno Ejemplo"),
        ("Consejero", "Persona de la Dos"),
    ]


def test_a_role_after_a_spaced_dash_is_split_off() -> None:
    assert pairs("Presidente: PERSONA UNO - Consejero: PERSONA DOS") == [
        ("Presidente", "PERSONA UNO"),
        ("Consejero", "PERSONA DOS"),
    ]


def test_an_id_number_fits_only_inside_a_company_name() -> None:
    assert holdings("Apoderado: PERSONA UNO 12345678Z") == []
    found = holdings("Adm. Unico: EJEMPLO 2018 INVERSIONES SL")
    assert [(h.holder, h.is_company) for h in found] == [("EJEMPLO 2018 INVERSIONES SL", True)]


def test_a_dotted_legal_form_before_the_next_role_stays_with_its_holder() -> None:
    detail = "Apo.Sol.: EJEMPLO PROJECTS 2025 S.L. Apo.Manc.: PERSONA UNO"
    found = holdings(detail)
    assert [(h.role, h.role_raw, h.holder, h.is_company) for h in found] == [
        ("attorney", "Apo.Sol", "EJEMPLO PROJECTS 2025 S.L", True),
        ("attorney", "Apo.Manc", "PERSONA UNO", False),
    ]


@pytest.mark.parametrize(
    ("holder", "company"),
    [
        # Abbreviations that are also surnames or initials count at the end...
        ("EJEMPLO AUDITORS SAP", True),
        ("EJEMPLO Y CIA AUDITORES S.R.C.", True),
        ("EJEMPLO AUTOMOTIVE SAS", True),
        ("EJEMPLO SOLAR 20 SAP", True),
        ("EJEMPLO ASSOCIATS SC", True),
        # ...but in the middle they are surnames or initials: these are people.
        ("SAS RIO PERSONA", False),
        ("DE SAS APELLIDO PERSONA", False),
        ("SPA APELLIDO PERSONA", False),
        ("APELLIDO AG PERSONA NOMBRE", False),
        ("SAP PERSONA", False),
        # Community of goods and civil partnerships carry the form in front.
        ("CB APELLIDO APELLIDO", True),
        ("EJEMPLO ECONOMISTES S.C.", True),
        ("EJEMPLO Y ASOCIADOS S.R.C.", True),
        ("SC DE ENSEÑANZA EJEMPLO", True),
        # Foreign forms never seen in a person's name count anywhere, and so do words
        # only a legal person uses.
        ("EJEMPLO BANK PLC SUCURSAL EN ESPAÑA", True),
        ("EJEMPLO SERVICES PLC", True),
        ("EJEMPLO INC WA", True),
        ("EJEMPLO AUDIT LLP", True),
        ("SAT NUMERO 1234 EJEMPLO", True),
        ("EJEMPLO AGRICOLA COOP V", True),
        ("FUNDACION EJEMPLO PARA LA CULTURA", True),
        ("COOPERATIVA AGRICOLA EJEMPLO", True),
        ("EJEMPLO SOCIEDAD LIMITADA EN LIQUIDACION Y CONCURSO", True),
        # Cut by the Registro at 50 characters in the middle of the legal form.
        ("EJEMPLO PROMOCION Y DESARROLLO FOTOVOLTAICO SOCIED", True),
        ("APELLIDO APELLIDO APELLIDO APELLIDO PERSONA NOMBRE", False),
        ("PERSONA UNO", False),
    ],
)
def test_legal_persons_by_what_the_name_carries(holder: str, company: bool) -> None:
    assert is_company_name(holder) is company


def test_measured_company_words_count_anywhere() -> None:
    measured = officers.company_vocabulary(frozenset({"CONSULTORES", "ASESORES"}))
    holder = "ASESORES Y CONSULTORES FINANCIEROS INDEPENDIENTES"
    assert is_company_name(holder, measured)
    assert not is_company_name(holder, officers.company_vocabulary(frozenset()))
    assert not is_company_name("PERSONA UNO ASESORA", measured)


def test_the_cut_length_is_a_parameter() -> None:
    cut = "EJEMPLO PROMOCION Y DESARROLLO FOTOVOLTAICO SOCIED"
    assert is_company_name(cut, cut_length=len(cut))
    assert not is_company_name(cut, cut_length=len(cut) + 1)


def test_the_shipped_company_words_are_measured() -> None:
    assert officers.MEASURED_COMPANY_WORDS
    assert officers.MEASURED_COMPANY_WORDS <= officers.COMPANY_WORDS
    assert officers.LEGAL_PERSON_WORDS <= officers.COMPANY_WORDS


def test_holder_key_ignores_word_order_particles_and_legal_form() -> None:
    assert holder_key("APELLIDOA APELLIDOB NOMBRE") == holder_key("NOMBRE APELLIDOA APELLIDOB")
    assert holder_key("NOMBRE DE LA APELLIDOA") == holder_key("APELLIDOA NOMBRE")
    assert holder_key("EJEMPLO BIENES SOCIEDAD LIMITADA") == holder_key("EJEMPLO BIENES SL")
    assert holder_key("APELLIDOA APELLIDOB NOMBRE") != holder_key("APELLIDOA NOMBRE")


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Auditor", "individual"),
        ("AUDIT.CUENT", "individual"),
        ("AUDIT.INDIV", "individual"),
        ("AUDIT.ART265", "individual"),
        ("AUDIT.CONJUN", "individual"),
        ("Aud.C.Con", "consolidated"),
        ("AUDT.CTS.CON", "consolidated"),
        ("AUD.C.C.CONJ", "consolidated"),
        ("Aud.Supl", "substitute"),
        ("AUDITOR SUP", "substitute"),
        ("Aud.Su.C.Con", "substitute"),
        ("AUDIT.CC.SUP", "substitute"),
        ("Aud.Supl.C.C", "substitute"),
    ],
)
def test_auditor_class_from_the_published_label(label: str, kind: str) -> None:
    assert auditor_class(label) == kind
