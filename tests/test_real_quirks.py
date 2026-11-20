"""The format quirks of schema 3 on real 2026 documents (pseudonymised fixtures)."""

from __future__ import annotations

from pathlib import Path

import pytest

from borme_radar.models import Announcement
from borme_radar.officers import auditor_class, holdings
from borme_radar.parser import parse_document
from borme_radar.record import observations_of_act


def load(fixtures: Path, doc_id: str) -> dict[int, Announcement]:
    doc = parse_document((fixtures / f"{doc_id}.xml").read_bytes())
    return {a.number: a for a in doc.announcements}


def acts(ann: Announcement) -> list[str]:
    return [a.act_type for a in ann.acts]


def observed(ann: Announcement) -> list[tuple[str, str]]:
    return [(o.field, o.value) for a in ann.acts for o in observations_of_act(a.act_type, a.detail)]


@pytest.fixture
def madrid(fixtures: Path) -> dict[int, Announcement]:
    return load(fixtures, "BORME-A-2026-69-28")


def test_purpose_extension_is_its_own_act_and_gives_no_purpose(madrid: dict) -> None:
    ann = madrid[178012]
    assert acts(ann) == ["business_purpose_extension", "address_change"]
    assert [f for f, _ in observed(ann)] == ["address", "city"]


def test_subscribed_capital_of_an_incorporation(madrid: dict) -> None:
    assert ("capital", "1200000.00") in observed(madrid[178035])


def test_a_role_glued_to_a_company_holder(madrid: dict) -> None:
    removal = madrid[178075].acts[0]
    found = [(h.role_raw, h.holder, h.is_company) for h in holdings(removal.detail)]
    assert found == [
        ("Adm. Solid", "PERSONA 11", False),
        ("Adm. Solid", "LOS CAÑOS DE LA FUENTE INVERSIONES SL", True),
        ("Adm. Solid", "FRESNO REAL STATE SL", True),
        ("Representan", "PERSONA 2", False),
        ("Representan", "PERSONA 3", False),
    ]


def test_two_numbers_after_the_sheet(madrid: dict) -> None:
    assert madrid[178139].sheet is None  # "H M 846910,180220": cannot tell which
    assert madrid[178144].sheet == "M 883400"  # "H M 883400,2": the 2 is noise


def test_professional_legal_form_at_the_end(madrid: dict) -> None:
    appointment = madrid[178157].acts[1]
    assert [(h.holder, h.is_company) for h in holdings(appointment.detail)] == [
        ("GRUPO DE AUDITORES PUBLICOS SAP", True)
    ]


def test_substitute_auditor(madrid: dict) -> None:
    revocation = madrid[178271].acts[0]
    assert [auditor_class(h.role_raw) for h in holdings(revocation.detail)] == [
        "individual",
        "substitute",
    ]


def test_heading_glued_to_the_last_name(fixtures: Path) -> None:
    ann = load(fixtures, "BORME-A-2026-58-28")[150862]
    assert acts(ann) == ["revocation", "appointment", "other"]
    revoked = [h.holder for h in holdings(ann.acts[0].detail)]
    assert revoked[-1] == "PERSONA QUINCE"  # not "PERSONA QUINCENombramientos"
    assert [h.holder for h in holdings(ann.acts[1].detail)] == ["PERSONA 7", "PERSONA 8"]


def test_sas_inside_a_name_is_a_person(fixtures: Path) -> None:
    ann = load(fixtures, "BORME-A-2026-58-28")[150832]
    flagged = {h.holder: h.is_company for h in holdings(ann.acts[0].detail)}
    assert flagged["PERSONA SAS DOCE"] is False


def test_street_renamed_by_the_city_council(fixtures: Path) -> None:
    ann = load(fixtures, "BORME-A-2026-137-04")[337841]
    assert observed(ann) == [
        ("address", "Avenida Parque Cientifico-Tecnológico de Almeria (PITA) 1, planta 4, 04131")
    ]


def test_heading_after_a_spaced_dash(fixtures: Path) -> None:
    ann = load(fixtures, "BORME-A-2026-12-44")[28634]
    assert acts(ann) == ["bylaws_amendment", "business_purpose_change"]
    assert observed(ann)[0][1].startswith("Servicios de Agente Financiero")


def test_an_announcement_printed_without_its_number_is_kept(fixtures: Path) -> None:
    # Seen in a 2011 Madrid document: " - EJEMPLO SA." with no announcement number.
    payload = (fixtures / "BORME-A-2026-183-28.xml").read_bytes()
    doc = parse_document(payload.replace(b"423901 - EUROPETS", b" - EUROPETS"))
    unnumbered = [a for a in doc.announcements if a.number < 0]
    assert [(a.number, a.company_name[:8]) for a in unnumbered] == [(-1, "EUROPETS")]
    assert unnumbered[0].acts


def test_an_erratum_under_a_bare_dash_header_is_kept(fixtures: Path) -> None:
    # Seen in two 2013 documents: "<p class="articulo"> - </p>" before "Fe de erratas".
    payload = (fixtures / "BORME-A-2026-183-28.xml").read_bytes()
    doc = parse_document(payload.replace(b"423901 - EUROPETS SOCIEDAD LIMITADA.", b" - "))
    unnumbered = [a for a in doc.announcements if a.number < 0]
    assert [(a.number, a.company_name) for a in unnumbered] == [(-1, "")]


@pytest.mark.parametrize(
    ("registry", "sheet"),
    [
        ("S 8 , H M 883400,2 , I/ 1", "M 883400"),
        ("S 8 , H M 883400,215 , I/ 1", "M 883400"),
        ("S 8 , H BI 85876,85876, I/A 3", "BI 85876"),
        ("S 8 , H M 846910,1802 , I/ 3", None),
        ("S 8 , H V 10888,194956, I/A 2", None),
    ],
)
def test_second_number_after_the_sheet(registry: str, sheet: str | None) -> None:
    from borme_radar.parser import parse_sheet

    assert parse_sheet(registry) == sheet
