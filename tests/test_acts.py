"""Act splitting on announcement bodies (snippets modelled on real Section A text)."""

from __future__ import annotations

from borme_radar.acts import (
    _HEADINGS,
    ACT_TYPES,
    REGISTRY_DATA,
    priority,
    section_b_type,
    split_acts,
)


def types(text: str) -> list[str]:
    return [s.act_type for s in split_acts(text)[1]]


def test_multiple_acts_in_order_with_details() -> None:
    prefix, segments = split_acts(
        "Ceses/Dimisiones. Adm. Unico: PERSONA 1. Nombramientos. Liquidador: PERSONA 1. "
        "Disolución. Voluntaria. Extinción.  Datos registrales. S 8 , H M 1, I/A 2 (15.09.26)."
    )
    assert prefix == ""
    assert [(s.act_type, s.heading, s.detail) for s in segments] == [
        ("officer_removal", "Ceses/Dimisiones", "Adm. Unico: PERSONA 1"),
        ("appointment", "Nombramientos", "Liquidador: PERSONA 1"),
        ("dissolution", "Disolución", "Voluntaria"),
        ("extinction", "Extinción", ""),
        (REGISTRY_DATA, "Datos registrales", "S 8 , H M 1, I/A 2 (15.09.26)"),
    ]


def test_heading_words_inside_free_text_do_not_split() -> None:
    text = (
        "Modificaciones estatutarias. ARTICULO 20.- Disolución de la sociedad por acuerdo "
        "de la junta. Otros conceptos: ACUERDA LA DISOLUCION. EXTINCION DE PODERES. "
        "Datos registrales. S 8 , H M 1, I/A 3 (15.09.26)."
    )
    assert types(text) == ["bylaws_amendment", "other", REGISTRY_DATA]


def test_colon_headings_and_missing_accents() -> None:
    text = (
        "Fe de erratas: Se publicó por error la inscripción 11. "
        "Perdida del caracter de unipersonalidad. Ampliacion de capital. Capital: 3.000,00 Euros."
    )
    assert types(text) == ["erratum", "sole_shareholder_lost", "capital_increase"]


def test_registry_data_after_dash_separator() -> None:
    text = "Otros conceptos: SE HA DESEMBOLSADO EL CAPITAL.- Datos registrales. S 8 , H M 2."
    segments = split_acts(text)[1]
    assert [s.act_type for s in segments] == ["other", REGISTRY_DATA]
    assert segments[0].detail == "SE HA DESEMBOLSADO EL CAPITAL.-"


def test_registry_sheet_closure_heading_keeps_article_number() -> None:
    text = (
        "Cierre provisional hoja registral Art.485 TRLC. Insuficiencia de masa activa. "
        "Situación concursal. Procedimiento concursal 29/2026. Auto de conclusión del concurso."
    )
    segments = split_acts(text)[1]
    assert [s.act_type for s in segments] == ["registry_sheet_closure", "insolvency"]
    assert segments[0].heading == "Cierre provisional hoja registral Art.485 TRLC"
    assert segments[1].detail.startswith("Procedimiento concursal 29/2026")


def test_text_before_first_known_heading_is_reported() -> None:
    prefix, segments = split_acts("Heading desconocido. Algo. Nombramientos. Apoderado: X SL.")
    assert prefix == "Heading desconocido. Algo."
    assert [s.act_type for s in segments] == ["appointment"]


def test_no_heading_at_all() -> None:
    assert split_acts("Texto sin cabeceras") == ("Texto sin cabeceras", [])


def test_catalogue_is_consistent() -> None:
    codes = {code for code, _ in _HEADINGS} - {REGISTRY_DATA}
    assert codes <= set(ACT_TYPES)
    assert {t.priority for t in ACT_TYPES.values()} <= {"high", "medium", "low"}


def test_section_b_headings_map_to_act_types() -> None:
    assert section_b_type("Depósitos de proyectos de fusión por absorción") == "merger_project"
    assert section_b_type("Depósitos de proyectos de fusión por unión") == "merger_project"
    assert section_b_type("Depósitos de proyectos de escisión parcial") == "split_project"
    assert section_b_type("Depósitos de proyectos de segregación") == "split_project"
    assert (
        section_b_type("Depósitos de Proyectos de Cesión Global de Activo y Pasivo")
        == "global_transfer_project"
    )
    assert (
        section_b_type("Cancelaciones de Depósitos de proyectos de fusión por absorción")
        == "project_cancellation"
    )
    assert (
        section_b_type("Cierre provisional de hoja registral (artículo 378.1 del Reglamento)")
        == "registry_sheet_closure"
    )
    assert section_b_type("Un encabezado nuevo") == "section_b_other"


def test_priority_depends_on_scope() -> None:
    assert priority("appointment", "board") == "medium"
    assert priority("appointment", "attorney") == "low"
    assert priority("revocation", "auditor") == "low"
    assert priority("other", "board") == "medium"  # board change filed under "Otros"
    assert priority("merger_project", "company") == "high"
    assert priority("website", "company") == "low"
