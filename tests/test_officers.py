"""Officer role lists -> holdings and scope (snippets modelled on real Section A text)."""

from __future__ import annotations

import pytest

from borme_radar.officers import act_scope, canonical_role, holdings


def triples(detail: str) -> list[tuple[str, str, str]]:
    return [(h.role, h.role_raw, h.holder) for h in holdings(detail)]


def test_a_label_with_dots_of_its_own_ends_at_its_colon() -> None:
    detail = (
        "Adm. Solid.: PERSONA UNO;PERSONA DOS. Vicesecret.: PERSONA TRES. Liquidador: PERSONA DOS"
    )
    assert triples(detail) == [
        ("joint_several_director", "Adm. Solid", "PERSONA UNO"),
        ("joint_several_director", "Adm. Solid", "PERSONA DOS"),
        ("vice_secretary", "Vicesecret", "PERSONA TRES"),
        ("liquidator", "Liquidador", "PERSONA DOS"),
    ]


def test_holder_words_swallowed_by_the_label_window_go_back_to_the_holder() -> None:
    detail = "Apo.Manc.: PERSONA UNO;PERSONA DOS. Apo.Sol.: PERSONA TRES"
    assert triples(detail) == [
        ("attorney", "Apo.Manc", "PERSONA UNO"),
        ("attorney", "Apo.Manc", "PERSONA DOS"),
        ("attorney", "Apo.Sol", "PERSONA TRES"),
    ]


def test_holder_words_are_never_reordered() -> None:
    # Sorting words would merge two different people.
    assert [h.holder for h in holdings("Adm. Unico: PERSONA ZETA ALFA")] == ["PERSONA ZETA ALFA"]


def test_company_holders_are_flagged() -> None:
    found = holdings("Auditor: AUDITORES EJEMPLO SL. Adm. Solid.: MATRIZ HOLDING SA;PERSONA UNO")
    assert [(h.role, h.is_company) for h in found] == [
        ("auditor", True),
        ("joint_several_director", True),
        ("joint_several_director", False),
    ]


@pytest.mark.parametrize(
    ("raw", "role"),
    [
        ("Adm. Unico", "sole_director"),
        ("ADM.UNICO", "sole_director"),
        ("Adm. Solid.", "joint_several_director"),
        ("ADM.SOLIDAR.", "joint_several_director"),
        ("Adm. Mancom.", "joint_director"),
        ("Consejero Delegado", "managing_director"),
        ("Co.De.Ma.So", "managing_director"),
        ("Vicepresid.", "vice_chair"),
        ("SecreNoConsj", "secretary"),
        ("Apo.Man.Soli", "attorney"),
        ("APODERAD.SOL", "attorney"),
        ("Liquidador", "liquidator"),
        ("Repres.143 RRM", "representative"),
        ("Auditor", "auditor"),
        ("Entidad Deposit.", "other"),
    ],
)
def test_canonical_roles(raw: str, role: str) -> None:
    assert canonical_role(raw) == role


def test_scope_board_wins_over_attorney_and_auditor() -> None:
    assert act_scope("appointment", "Apoderado: PERSONA UNO. Adm. Unico: PERSONA DOS") == "board"
    assert act_scope("appointment", "Auditor: AUDITORES EJEMPLO SL") == "auditor"
    assert act_scope("revocation", "Apoderado: PERSONA UNO") == "attorney"


def test_scope_of_non_officer_acts() -> None:
    assert act_scope("capital_increase", "Capital: 3.000,00 Euros") == "company"
    assert act_scope("other", "Cambio del Organo de Administración: Consejo") == "board"
    # An officer act whose roles cannot be read counts as board (conservative side).
    assert act_scope("appointment", "texto sin cargos") == "board"


def test_codes_and_single_words_are_not_holders() -> None:
    assert holdings("Apoderado: B12X9Z;X") == []
