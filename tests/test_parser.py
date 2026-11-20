"""Parser on real BORME XML (trimmed, natural-person names pseudonymised)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from borme_radar.models import Announcement, Document
from borme_radar.normalize import normalize_name
from borme_radar.parser import (
    NAME_MAX,
    ParseError,
    parse_document,
    parse_inscription_date,
    parse_sheet,
)


@pytest.fixture
def madrid(fixtures: Path) -> Document:
    return parse_document((fixtures / "BORME-A-2026-183-28.xml").read_bytes())


@pytest.fixture
def balears(fixtures: Path) -> Document:
    return parse_document((fixtures / "BORME-A-2026-183-07.xml").read_bytes())


def by_number(doc: Document, number: int) -> Announcement:
    return next(a for a in doc.announcements if a.number == number)


def act_types(doc: Document, number: int) -> list[str]:
    return [act.act_type for act in by_number(doc, number).acts]


def test_document_metadata(madrid: Document) -> None:
    assert madrid.document_id == "BORME-A-2026-183-28"
    assert madrid.pub_date == date(2026, 9, 22)
    assert madrid.gazette_number == 183
    assert madrid.province == "MADRID"
    assert len(madrid.announcements) == 13


def test_every_announcement_is_fully_covered(madrid: Document, balears: Document) -> None:
    for ann in (*madrid.announcements, *balears.announcements):
        assert ann.unparsed_prefix == "", ann.number
        assert ann.acts, ann.number
        assert ann.registry_data and ann.inscription_date, ann.number


def test_announcement_header_and_registry_data(madrid: Document) -> None:
    first = madrid.announcements[0]
    assert (first.number, first.company_name) == (423901, "EUROPETS SOCIEDAD LIMITADA")
    assert first.registry_data == "S 8 , H M 786863, I/A 11 ( 9.09.26)"
    assert first.inscription_date == date(2026, 9, 9)
    assert [(a.act_type, a.heading) for a in first.acts] == [("erratum", "Fe de erratas")]


def test_many_acts_in_one_announcement_keep_their_order(madrid: Document) -> None:
    assert act_types(madrid, 424101) == [
        "sole_shareholder_declared",
        "sole_shareholder_lost",
        "capital_reduction",
        "capital_increase",
        "capital_increase",
        "bylaws_amendment",
        "other",
    ]
    reduction = by_number(madrid, 424101).acts[2]
    assert reduction.seq == 3
    assert (
        reduction.detail
        == "Importe reducción: 5.000,00 Euros. Resultante Suscrito: 95.000,00 Euros"
    )


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (
            423908,
            [
                "transformation",
                "sole_shareholder_declared",
                "appointment",
                "other",
                "address_change",
            ],
        ),
        (423948, ["capital_reduction", "address_change", "business_purpose_change", "split"]),
        (423967, ["officer_removal", "appointment", "dissolution", "extinction"]),
        (424100, ["name_change"]),
        (424186, ["registry_sheet_closure", "insolvency"]),
        (424335, ["ex_officio_cancellation"]),
        (424360, ["capital_call_paid", "other"]),
        (424372, ["incorporation", "appointment", "website"]),
        (424395, ["insolvency", "insolvency"]),
    ],
)
def test_act_types_madrid(madrid: Document, number: int, expected: list[str]) -> None:
    assert act_types(madrid, number) == expected


def test_insolvency_detail_keeps_the_court_decision(madrid: Document) -> None:
    detail = by_number(madrid, 424395).acts[0].detail
    assert "Auto de declaración de concurso" in detail
    assert by_number(madrid, 424395).inscription_date == date(2026, 9, 15)  # "I/A A (...)"


def test_balears_merger_and_registry_annotation(balears: Document) -> None:
    merger = by_number(balears, 423266)
    assert merger.company_name == "MAJORCA PALMA PEARLS SL(R.M. PALMA DE MALLORCA)"
    assert normalize_name(merger.company_name) == "MAJORCA PALMA PEARLS SL"
    assert [(a.act_type, a.heading) for a in merger.acts] == [("merger", "Fusión por absorción")]
    assert merger.acts[0].detail.startswith("Sociedades absorbidas: MARINAINA SL.")
    assert act_types(balears, 423269) == [
        "sole_shareholder_lost",
        "officer_removal",
        "revocation",
        "dissolution",
        "extinction",
    ]


def test_index_document_yields_no_announcements() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <documento><metadatos>
      <identificador>BORME-A-2026-183-99</identificador>
      <titulo>ÍNDICE ALFABÉTICO DE SOCIEDADES</titulo>
      <diario_numero>183</diario_numero><fecha_publicacion>20260922</fecha_publicacion>
    </metadatos><texto><table><tbody><tr><td>ACME SL.</td></tr></tbody></table></texto>
    </documento>"""
    assert parse_document(xml.encode()).announcements == ()


def test_truncated_or_malformed_documents_raise(fixtures: Path) -> None:
    payload = (fixtures / "BORME-A-2026-183-28.xml").read_bytes()
    with pytest.raises(ParseError):
        parse_document(payload[: len(payload) // 2])
    with pytest.raises(ParseError, match="metadatos"):
        parse_document(b"<documento/>")
    bad_header = payload.replace(b"423901 - EUROPETS", b"EUROPETS")
    with pytest.raises(ParseError, match="unexpected announcement header"):
        parse_document(bad_header)


@pytest.mark.parametrize(
    ("registry", "expected"),
    [
        ("S 8 , H M 786863, I/A 11 ( 9.09.26)", date(2026, 9, 9)),
        ("T 45583 , F 220, S 8, H M 415866, I/A 12 (17.10.23)", date(2023, 10, 17)),
        ("S 9 , H MU 306,SLNE , I/ 1 .H.)11 09 26", date(2026, 9, 11)),  # garbled variant
        ("S 8 , H M 1", None),
        (None, None),
    ],
)
def test_parse_inscription_date(registry: str | None, expected: date | None) -> None:
    assert parse_inscription_date(registry) == expected


@pytest.mark.parametrize(
    ("registry", "sheet"),
    [
        ("S 8 , H M 786863, I/A 11 ( 9.09.26)", "M 786863"),
        ("S 8 , H PM 34762, I/A 5", "PM 34762"),
        ("S 2 , H 156220, I/A 1 ( 2.09.26)", "156220"),  # some registries print no letters
        ("S 8, H GR 12687", "GR 12687"),
        ("sin hoja", None),
        (None, None),
    ],
)
def test_parse_sheet(registry: str | None, sheet: str | None) -> None:
    assert parse_sheet(registry) == sheet


def test_announcements_carry_the_registry_sheet(madrid: Document) -> None:
    assert madrid.announcements[0].sheet == "M 786863"
    assert all(a.sheet for a in madrid.announcements)


def test_scope_and_priority_of_acts(madrid: Document, balears: Document) -> None:
    for doc in (madrid, balears):
        for ann in doc.announcements:
            for act in ann.acts:
                assert act.scope in {"board", "attorney", "auditor", "company"}
                if act.scope in ("attorney", "auditor"):
                    assert act.priority == "low"  # never above low, whatever the type
    extinction = by_number(madrid, 423967).acts[-1]
    assert (extinction.act_type, extinction.scope, extinction.priority) == (
        "extinction",
        "company",
        "high",
    )


@pytest.fixture
def granada(fixtures: Path) -> Document:
    return parse_document((fixtures / "BORME-B-2026-113-18.xml").read_bytes())


def test_section_b_types_come_from_the_table_heading(granada: Document) -> None:
    assert granada.section == "B"
    assert [(a.number, a.acts[0].act_type) for a in granada.announcements] == [
        (342, "registry_sheet_reopening"),
        (343, "registry_sheet_reopening"),
        (344, "registry_sheet_reopening"),
        (345, "global_transfer_project"),
        (346, "merger_project"),
        (347, "split_project"),
    ]
    merger = by_number(granada, 346)
    assert merger.company_name == "MULTISER MALAGA SL"
    assert merger.inscription_date == date(2026, 1, 2)  # deposit date
    assert merger.acts[0].detail == "Absorbidas: CASA OSSORIO CALVACHE SOCIEDAD LIMITADA"
    assert merger.acts[0].priority == "high"


def test_section_b_registry_data_glued_to_the_name(granada: Document) -> None:
    reopened = by_number(granada, 342)
    assert reopened.company_name == "COMPAÑIA MINERA DEL MARQUESADO SLL EN LIQUIDACION"
    assert reopened.sheet == "GR 12687"
    assert normalize_name(reopened.company_name) == "COMPANIA MINERA DEL MARQUESADO SLL"


def test_section_b_long_header_is_a_list_of_companies() -> None:
    names = ". ".join(f"EMPRESA NUMERO {i} SOCIEDAD LIMITADA" for i in range(12))
    xml = f"""<documento><metadatos>
      <identificador>BORME-B-2026-1-28</identificador><titulo>MADRID</titulo>
      <diario_numero>1</diario_numero><fecha_publicacion>20260102</fecha_publicacion>
    </metadatos><texto>
      <p class="centro_redonda">Depósitos de proyectos de fusión por unión</p>
      <p class="articulo">5 - {names}. (01/12/2025)</p>
    </texto></documento>"""
    ann = parse_document(xml.encode()).announcements[0]
    assert len(names) > NAME_MAX
    assert ann.company_name == "EMPRESA NUMERO 0 SOCIEDAD LIMITADA"
    assert len(ann.other_companies) == 11
    assert ann.acts[0].act_type == "merger_project"
    assert ann.acts[0].detail.startswith("Listed with: EMPRESA NUMERO 1 SOCIEDAD LIMITADA;")
