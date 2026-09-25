"""Officer acts -> dated events: who was appointed, removed, revoked or re-elected.

The Registro publishes the governance of a company as one chained sentence::

    Consejero: PERSONA 1;PERSONA 2. Presidente: PERSONA 1. Cons.Del.Sol: PERSONA 2

that is ``role: holder;holder. role: holder``. Three traps come from the real format:

1. **Roles have dots inside** ("Adm. Unico", "Apo.Man.Soli", "Cons.Del.Sol"), so the
   text cannot be split at full stops. The parser anchors on the colons and reads each
   role backwards from its colon.
2. **There is no official dictionary of abbreviations.** The role is kept twice: as
   published (``role_raw``) and mapped to a canonical role (``role``). The mapping was
   built from the labels counted in the gazette (``borme-radar calibrate --roles``);
   what it does not know becomes ``other`` and the raw text is still there to fix it
   later without re-parsing.
3. **A holder can be a company** (a corporate director, an audit firm). Those are real
   positions and are kept, flagged with ``is_company``.

Holder names are never reordered: sorting the words would merge "MARTINEZ GARCIA JOSE"
and "GARCIA MARTINEZ JOSE", who are two people. A duplicated person is a small problem;
a position attributed to the wrong person is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from borme_radar.normalize import has_legal_form, plain

# Act type -> what happens to the position.
EVENT_BY_ACT = {
    "appointment": "appointment",
    "officer_removal": "removal",
    "ex_officio_cancellation": "removal",
    "revocation": "revocation",
    "reelection": "reelection",
}
OFFICER_ACTS = frozenset(EVENT_BY_ACT)

# Prefix of the role label (letters only, uppercase) -> canonical role. Checked in
# order, most specific first: "VICEPRESIDENTE" contains "PRESIDENTE", and "CONSDELSOL"
# must not fall into "CONSEJERO".
_ROLE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ADMUNICO", "sole_director"),
    ("ADMINISTRADORUNICO", "sole_director"),
    ("ADMSOLID", "joint_several_director"),
    ("ADMINISTRADORSOLID", "joint_several_director"),
    ("ADMMANC", "joint_director"),
    ("ADMINISTRADORMANC", "joint_director"),
    ("ADMCONC", "insolvency_administrator"),
    ("ADMINISTRADORCONC", "insolvency_administrator"),
    ("ADM", "director"),
    ("CONSDEL", "managing_director"),
    ("CONDEL", "managing_director"),
    ("CONSEJERODELEGADO", "managing_director"),
    ("CODE", "managing_director"),
    ("CONIND", "board_member"),  # "Con.Ind": independent director
    ("CONSEJ", "board_member"),
    ("CONSJ", "board_member"),
    ("CONS", "board_member"),
    ("VICEPRES", "vice_chair"),
    ("VPRES", "vice_chair"),
    ("PRES", "chair"),
    ("VICESEC", "vice_secretary"),
    ("VSEC", "vice_secretary"),  # "VsecrNoConsj", "V-SEC NO CON"
    ("SECR", "secretary"),
    ("LIQ", "liquidator"),
    ("MIECONS", "board_member"),  # "Mie.Cons.Rec": governing council of a cooperative
    ("MIEM", "committee_member"),
    ("APO", "attorney"),
    ("AUD", "auditor"),
    ("REPR", "representative"),
    ("REP", "representative"),
    ("RLC", "representative"),  # "R.L.C.Perma": permanent representative
    ("SOCPROF", "professional_partner"),
    ("SOCIOPROF", "professional_partner"),
    ("PATRONO", "trustee"),
)

BOARD_ROLES = frozenset(
    {
        "sole_director",
        "joint_several_director",
        "joint_director",
        "director",
        "managing_director",
        "board_member",
        "chair",
        "vice_chair",
        "secretary",
        "vice_secretary",
        "liquidator",
        "committee_member",
        "representative",
        "insolvency_administrator",
    }
)
SCOPE_OF_ROLE = {role: "board" for role in BOARD_ROLES} | {
    "attorney": "attorney",
    "auditor": "auditor",
}

# A role label is short; anything longer before a colon is free text.
_MAX_LABEL = 32
_BOUNDARY_RE = re.compile(r"[.;]\s*")
# Board wording inside "Otros conceptos" ("Cambio del Organo de Administración").
_BOARD_WORDS_RE = re.compile(r"ORGANO DE ADMINISTRACION|ADMINISTRADOR|CONSEJ")
_COMPANY_WORDS = frozenset(
    {
        "SOCIEDAD",
        "LIMITED",
        "LTD",
        "GMBH",
        "INC",
        "LLC",
        "LLP",
        "BV",
        "NV",
        "AG",
        "SARL",
        "SAS",
        "SPA",
        "SRL",
        "AUDITORES",
        "AUDITORIA",
        "AUDITING",
        "BANCO",
        "BANK",
        "CAJA",
        "FUNDACION",
        "ASOCIACION",
        "AYUNTAMIENTO",
        "UNIVERSIDAD",
        "HOLDING",
        "INVERSIONES",
        "CORPORACION",
    }
)


@dataclass(frozen=True, slots=True)
class Holding:
    role: str  # canonical role, e.g. "sole_director"
    role_raw: str  # as published, e.g. "Adm. Unico"
    holder: str  # as published, words never reordered
    is_company: bool


def canonical_role(raw: str) -> str:
    """'Apo.Man.Soli' -> 'attorney'. Unknown labels map to 'other'."""
    key = re.sub(r"[^A-Z]", "", plain(raw))
    for prefix, role in _ROLE_PREFIXES:
        if key.startswith(prefix):
            return role
    return "other"


def is_company_name(holder: str) -> bool:
    if has_legal_form(holder):
        return True
    return any(word in _COMPANY_WORDS for word in plain(holder).split())


def _trim_label(candidate: str) -> str:
    """Drop holder words the label window swallowed: "PERSONA 1. Apo.Manc" -> "Apo.Manc".

    A label can contain ". " itself ("Adm. Unico"), so the text is not simply cut at
    the last full stop. What tells them apart is that a holder has several words and a
    piece of an abbreviated role has none: leading pieces with a space are dropped.
    """
    pieces = candidate.split(". ")
    while len(pieces) > 1 and (" " in pieces[0].strip() or ";" in pieces[0]):
        pieces.pop(0)
    return ". ".join(pieces).strip(" .;")


def _labels(text: str) -> list[tuple[int, int, str]]:
    """(label start, value start, label) for every role label in ``text``.

    Each colon ends a label. The label starts at the previous value (or the start of
    the text) when that is close, otherwise at the first full stop or semicolon inside
    a short window before the colon; then :func:`_trim_label` removes holder words.
    """
    found: list[tuple[int, int, str]] = []
    floor = 0
    for colon in (m.start() for m in re.finditer(":", text)):
        window_start = max(floor, colon - _MAX_LABEL)
        if window_start == floor:
            start = floor
        else:
            boundary = _BOUNDARY_RE.search(text, window_start, colon)
            if boundary is None:
                continue  # a colon inside free text, far from any boundary
            start = boundary.end()
        raw = text[start:colon]
        label = _trim_label(raw)
        if not label or not label[0].isalpha():
            continue
        found.append((start + raw.find(label), colon + 1, label))
        floor = colon + 1
    return found


def _is_holder(text: str) -> bool:
    """A holder has at least two words, one of them of letters (drops reference codes
    such as "CVA0AQHWFW" and stray fragments)."""
    words = plain(text).split()
    return len(words) >= 2 and len(" ".join(words)) >= 5 and any(w.isalpha() for w in words)


def roles_in(detail: str) -> list[str]:
    """Canonical roles of the labels of an act, whether or not a holder could be read."""
    return [canonical_role(raw) for _, _, raw in _labels((detail or "").strip())]


def holdings(detail: str) -> list[Holding]:
    """One entry per (role, holder) in the detail of an officer act."""
    text = (detail or "").strip()
    labels = _labels(text)
    out: list[Holding] = []
    seen: set[tuple[str, str]] = set()
    for i, (_start, value_start, raw) in enumerate(labels):
        end = labels[i + 1][0] if i + 1 < len(labels) else len(text)
        role = canonical_role(raw)
        for piece in text[value_start:end].split(";"):
            holder = piece.strip(" .,;")
            if not _is_holder(holder):
                continue
            key = (role, plain(holder))
            if key in seen:
                continue
            seen.add(key)
            out.append(Holding(role, raw, holder, is_company_name(holder)))
    return out


def act_scope(act_type: str, detail: str) -> str:
    """board / attorney / auditor / company: who an act is about.

    For officer acts the roles decide, with the board first: one paragraph can appoint
    a director and an attorney, and the director is what matters. An officer act whose
    roles could not be read is counted as board (the conservative side for alerts).
    """
    if act_type not in OFFICER_ACTS:
        if act_type == "other" and _BOARD_WORDS_RE.search(plain(detail)):
            return "board"
        return "company"
    scopes = {SCOPE_OF_ROLE.get(role, "company") for role in roles_in(detail)}
    for scope in ("board", "auditor", "attorney"):
        if scope in scopes:
            return scope
    return "company" if scopes else "board"
