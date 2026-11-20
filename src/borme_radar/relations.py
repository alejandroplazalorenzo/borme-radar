"""Ties between two companies, as the acts of an announcement state them.

Some acts name another company in a fixed place, and the place says what the tie is:

* ``owned_by``: the other company is the sole shareholder ("Socio único: X" in a
  declaration of sole shareholder; the whole text of a change of sole shareholder);
* ``absorbs``: the companies absorbed in a merger, or in a merger project deposited in
  Section B;
* ``transfers_to``: the beneficiaries of a split, a segregation or a global transfer of
  assets and liabilities, done or deposited as a project;
* ``directed_by``: a company in a director role (sole, joint, or joint and several) of an
  officer act. A company on the board, with powers of attorney or as auditor is not a tie;
* ``co_listed``: the other companies of the same Section B entry, which the parser keeps
  at the start of the act text as "Listed with: ...".

Names are read from these places only, never searched for in the rest of the text. A
name is a company when it ends in a legal form, Spanish or foreign, so a natural person
(a sole shareholder, a director) is never a relation. :func:`relation_names` gives the
names as published, for discovery and the year of joining a group, which compare them
with known names; :func:`relations_of_announcement` gives the relations themselves, which
the evaluation follows from the watched companies to tell who belongs to each group.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from borme_radar.normalize import has_legal_form, normalize_name, plain
from borme_radar.officers import FORMS_ANYWHERE, FORMS_AT_END

RelationKind = Literal["owned_by", "absorbs", "transfers_to", "directed_by", "co_listed"]

# Foreign legal forms seen in the gazette: they end a company name as well.
_FOREIGN_FORMS = FORMS_ANYWHERE | FORMS_AT_END
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
    """Does the name end in a legal form, Spanish (any spelling) or foreign? Only its tail
    is read, so a long text is never normalised whole."""
    tail = name[-_TAIL_CHARS:]
    return has_legal_form(tail) or plain(tail).rsplit(" ", 1)[-1] in _FOREIGN_FORMS


def split_company_list(text: str) -> list[str]:
    """Split a published list of companies at ";" and at ". ".

    A piece is joined to the next ones until they end in a legal form, so a name with a
    full stop inside ("EJEMPLO CORP. NORTE SA") stays whole; what is left at the end
    without a form is one last name.
    """
    names: list[str] = []
    pending: list[str] = []
    for piece in _LIST_SPLIT_RE.split(text.strip().rstrip(".")):
        if piece := piece.strip(" ,"):
            pending.append(piece)
            if ends_as_company(". ".join(pending)):
                names.append(". ".join(pending))
                pending = []
    return names + ([". ".join(pending)] if pending else [])


def _relations(kind: RelationKind, names: list[str], subject_norm: str) -> list[Relation]:
    """Relations of one kind to the named companies; the subject itself is left out."""
    keyed = ((name.strip(), normalize_name(name)) for name in names)
    return [Relation(kind, name, norm) for name, norm in keyed if norm and norm != subject_norm]


# Act type -> the tie it states and the label that opens the list of names (Section A and
# Section B word it differently); None when the whole text is the name.
_SLOTS: dict[str, tuple[RelationKind, re.Pattern[str] | None]] = {
    "sole_shareholder_declared": ("owned_by", _OWNER_RE),
    "sole_shareholder_change": ("owned_by", None),
    "merger": ("absorbs", _ABSORBED_RE),
    "merger_project": ("absorbs", _ABSORBED_RE),
} | dict.fromkeys(
    ("split", "split_project", "global_asset_transfer", "global_transfer_project"),
    ("transfers_to", _BENEFICIARY_RE),
)


def relation_names(
    act_type: str, detail: str, holders: tuple = ()
) -> list[tuple[RelationKind, str]]:
    """The (kind, name) pairs an act states, with the names as published and not yet
    checked to be companies: the names in its slot, the co-listed companies of a Section B
    entry, and the holders of ``holders`` in a director role."""
    detail = detail or ""
    found: list[tuple[RelationKind, str]] = []
    slot = _SLOTS.get(act_type)
    if slot is not None:
        kind, pattern = slot
        names = detail
        if pattern is not None:
            match = pattern.search(detail)
            names = match.group("names") if match else ""
        found += [(kind, name) for name in split_company_list(names)]
    listed = _LISTED_RE.match(detail)
    if listed:  # Section B: the other companies of the deposit, as stored in the detail
        found += [("co_listed", name.strip()) for name in listed.group("names").split(";")]
    return found + [("directed_by", h.holder) for h in holders if h.role in _ADMIN_ROLES]


def relations_of_act(
    act_type: str, detail: str, subject_norm: str = "", holders: tuple = ()
) -> list[Relation]:
    """The relations an act states: the names that end as a company, normalised, other
    than the subject. Co-listed companies are left to :func:`relations_of_announcement`,
    which takes them from the parsed entry."""
    return [
        relation
        for kind, name in relation_names(act_type, detail, holders)
        if kind != "co_listed" and ends_as_company(name)
        for relation in _relations(kind, [name], subject_norm)
    ]


def relations_of_announcement(
    acts: list[tuple[str, str, tuple]], other_companies: tuple[str, ...], subject_norm: str
) -> list[Relation]:
    """Every relation of one announcement, each (kind, company) once: what its acts
    state, ``(act_type, detail, holders)``, and the other companies of its Section B entry
    as co-listed."""
    stated = [r for act in acts for r in relations_of_act(act[0], act[1], subject_norm, act[2])]
    listed = [n for n in other_companies if ends_as_company(n)]
    kept: dict[tuple[str, str], Relation] = {}
    for relation in stated + _relations("co_listed", listed, subject_norm):
        kept.setdefault((relation.kind, relation.other_norm), relation)
    return list(kept.values())
