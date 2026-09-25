"""Explicit company-to-company relations published in the BORME.

Some acts name another company in a role that ties the two together:

* ``Declaración de unipersonalidad. Socio único: X SA`` and ``Cambio de identidad del
  socio único: X SA`` -> the announced company is owned by X;
* ``Fusión por absorción. Sociedades absorbidas: X SL. Y SL`` (Section A) and
  ``Absorbidas: X SL; Y SL`` (Section B) -> the announced company absorbs X and Y;
* ``Sociedades beneficiarias de la escisión: X``, ``Beneficiarios de la segregación:``,
  ``Beneficiarias de la cesión:`` and Section B ``Beneficiarias:`` -> the announced
  company transfers (part of) its business to X;
* a company as sole or joint administrator (``Adm. Unico: X SA``) -> directed by X;
* the other companies listed in the same Section B deposit.

Only legal persons are kept (the other party must end with a legal form), so these
relations carry no personal data. They serve two purposes: the *mention* discovery
signal reads the same text, and the evaluation uses them as ground truth for the other
signals (see ``evaluate.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from borme_radar.normalize import has_legal_form, normalize_name, plain

RelationKind = Literal["owned_by", "absorbs", "transfers_to", "directed_by", "co_listed"]

# Foreign legal forms seen in the gazette: they end a company name as well.
_FOREIGN_FORMS = frozenset(
    {
        "GMBH",
        "LTD",
        "LIMITED",
        "INC",
        "LLC",
        "LLP",
        "LP",
        "BV",
        "NV",
        "AG",
        "KG",
        "SARL",
        "SAS",
        "SPA",
        "SRL",
        "AB",
        "AS",
        "OY",
        "PLC",
        "SE",
        "CORPORATION",
    }
)
_LIST_SPLIT_RE = re.compile(r";\s*|\.\s+")
_TAIL_CHARS = 90  # longer than any legal form with a registry note after it
_OWNER_RE = re.compile(r"Socio\s+[úu]nico\s*:\s*(?P<names>.+)", re.I)
_ABSORBED_RE = re.compile(r"(?:Sociedades\s+absorbidas|Absorbidas?)\s*:\s*(?P<names>.+)", re.I)
_BENEFICIARY_RE = re.compile(
    r"(?:Sociedades\s+beneficiarias\s+de\s+la\s+escisi[oó]n|Beneficiari[oa]s(?:\s+de\s+la\s+"
    r"(?:segregaci[oó]n|cesi[oó]n|escisi[oó]n))?)\s*:\s*(?P<names>.+)",
    re.I,
)
_ADMIN_ROLES = frozenset({"sole_director", "joint_several_director", "joint_director"})
_LISTED_RE = re.compile(r"Listed with:\s*(?P<names>[^.]+(?:\.\S[^.]*)*?)\.(?:\s|$)")


@dataclass(frozen=True, slots=True)
class Relation:
    kind: RelationKind
    other: str  # the other company, as published
    other_norm: str


def ends_as_company(name: str) -> bool:
    """Does the text end with a legal form? Only its tail is looked at: the legal form
    is at the end, and normalising a long buffer again and again is quadratic."""
    tail = name[-_TAIL_CHARS:]
    if has_legal_form(tail):
        return True
    words = plain(tail).split()
    return bool(words) and words[-1] in _FOREIGN_FORMS


def split_company_list(text: str) -> list[str]:
    """``"A SL. B SOCIEDAD LIMITADA; C, SL"`` -> ``["A SL", "B SOCIEDAD LIMITADA", "C, SL"]``.

    Pieces without a legal form are joined to the next piece, so a name with a full
    stop inside ("UNO CORP. LATINOAMERICA SA") stays one name.
    """
    names: list[str] = []
    buffer = ""
    for piece in _LIST_SPLIT_RE.split(text.strip().rstrip(".")):
        piece = piece.strip(" ,")
        if not piece:
            continue
        buffer = f"{buffer}. {piece}" if buffer else piece
        if ends_as_company(buffer):
            names.append(buffer)
            buffer = ""
    if buffer:
        names.append(buffer)
    return names


def _make(kind: RelationKind, names: list[str], subject_norm: str) -> list[Relation]:
    out: list[Relation] = []
    for name in names:
        norm = normalize_name(name)
        if norm and norm != subject_norm:
            out.append(Relation(kind, name.strip(), norm))
    return out


def relation_names(
    act_type: str, detail: str, holders: tuple = ()
) -> list[tuple[RelationKind, str]]:
    """Names written in the relation slots of one act, legal persons or not.

    ``holders`` are the parsed officer holdings of the act (``officers.holdings``),
    passed in to avoid parsing the roles twice.
    """
    detail = detail or ""
    found: list[tuple[RelationKind, str]] = []

    def add(kind: RelationKind, regex: re.Pattern[str] | None) -> None:
        match = regex.search(detail) if regex else None
        text = match.group("names") if match else (detail if regex is None else "")
        found.extend((kind, name) for name in split_company_list(text))

    if act_type == "sole_shareholder_declared":
        add("owned_by", _OWNER_RE)
    elif act_type == "sole_shareholder_change":
        add("owned_by", None)
    elif act_type in ("merger", "merger_project"):
        add("absorbs", _ABSORBED_RE)
    elif act_type in ("split", "split_project", "global_asset_transfer", "global_transfer_project"):
        add("transfers_to", _BENEFICIARY_RE)
    listed = _LISTED_RE.match(detail)
    if listed:  # Section B: the other companies of the deposit, as stored in the detail
        found.extend(("co_listed", name.strip()) for name in listed.group("names").split(";"))
    found.extend(("directed_by", h.holder) for h in holders if h.role in _ADMIN_ROLES)
    return found


def relations_of_act(
    act_type: str, detail: str, subject_norm: str = "", holders: tuple = ()
) -> list[Relation]:
    """Company-to-company relations stated by one act: the relation slots whose name
    is a legal person (see module docstring)."""
    found: list[Relation] = []
    for kind, name in relation_names(act_type, detail, holders):
        if kind != "co_listed" and ends_as_company(name):
            found += _make(kind, [name], subject_norm)
    return found


def relations_of_announcement(
    acts: list[tuple[str, str, tuple]], other_companies: tuple[str, ...], subject_norm: str
) -> list[Relation]:
    """All relations of one announcement: its acts plus the Section B co-listed firms."""
    found: list[Relation] = []
    for act_type, detail, holders in acts:
        found += relations_of_act(act_type, detail, subject_norm, holders)
    found += _make("co_listed", [n for n in other_companies if ends_as_company(n)], subject_norm)
    unique: dict[tuple[str, str], Relation] = {}
    for relation in found:
        unique.setdefault((relation.kind, relation.other_norm), relation)
    return list(unique.values())
