"""Explicit company-to-company relations (formats seen in real announcements)."""

from __future__ import annotations

from borme_radar.officers import holdings
from borme_radar.relations import relations_of_act, relations_of_announcement, split_company_list


def kinds(act_type: str, detail: str) -> list[tuple[str, str]]:
    found = relations_of_act(act_type, detail, "SUBJECT SL", tuple(holdings(detail)))
    return [(r.kind, r.other_norm) for r in found]


def test_sole_shareholder_that_is_a_company() -> None:
    assert kinds("sole_shareholder_declared", "Socio único: MATRIZ EJEMPLO, S.A") == [
        ("owned_by", "MATRIZ EJEMPLO SA")
    ]
    assert kinds("sole_shareholder_change", "MATRIZ EJEMPLO SOCIEDAD LIMITADA") == [
        ("owned_by", "MATRIZ EJEMPLO SL")
    ]


def test_a_natural_person_is_never_a_relation() -> None:
    assert kinds("sole_shareholder_declared", "Socio único: PERSONA UNO DOS") == []


def test_merger_lists_absorbed_companies_in_both_sections() -> None:
    section_a = "Sociedades absorbidas: UNA SL. OTRA SOCIEDAD LIMITADA. TERCERA, SL"
    assert [o for _, o in kinds("merger", section_a)] == ["UNA SL", "OTRA SL", "TERCERA SL"]
    section_b = "Absorbidas: UNA SOCIEDAD LIMITADA; OTRA SL"
    assert kinds("merger_project", section_b) == [("absorbs", "UNA SL"), ("absorbs", "OTRA SL")]


def test_split_beneficiaries() -> None:
    assert kinds("split", "Sociedades beneficiarias de la escisión: NUEVA SL") == [
        ("transfers_to", "NUEVA SL")
    ]
    assert kinds("split", "Beneficiarios de la segregación: NUEVA SL") == [
        ("transfers_to", "NUEVA SL")
    ]
    assert kinds("split_project", "Beneficiarias: NUEVA SL; OTRA SL") == [
        ("transfers_to", "NUEVA SL"),
        ("transfers_to", "OTRA SL"),
    ]


def test_company_as_administrator() -> None:
    assert kinds("appointment", "Adm. Unico: MATRIZ EJEMPLO SA") == [
        ("directed_by", "MATRIZ EJEMPLO SA")
    ]
    # A company on the board (not an administrator) is not a group tie by itself.
    assert kinds("appointment", "Consejero: MATRIZ EJEMPLO SA") == []


def test_names_with_a_full_stop_inside_stay_whole() -> None:
    assert split_company_list("UNO CORP. LATINOAMERICA SA. DOS S.L") == [
        "UNO CORP. LATINOAMERICA SA",
        "DOS S.L",
    ]


def test_section_b_co_listed_and_deduplicated() -> None:
    found = relations_of_announcement(
        [("merger_project", "Absorbidas: UNA SL", ())], ("UNA SL", "DOS SA"), "SUBJECT SL"
    )
    assert sorted((r.kind, r.other_norm) for r in found) == [
        ("absorbs", "UNA SL"),
        ("co_listed", "DOS SA"),
        ("co_listed", "UNA SL"),
    ]
