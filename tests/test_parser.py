"""Parser on real BORME XML (trimmed, natural-person names pseudonymised)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from borme_radar.models import Announcement, Document
from borme_radar.normalize import normalize_name
from borme_radar.parser import ParseError, parse_document, parse_inscription_date


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
