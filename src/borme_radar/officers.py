"""Officer acts read into positions: each holder with its role, and whether it is a company.

An officer act (appointment, removal, revocation, re-election, ex officio cancellation)
is a list of role labels, each closed by a colon and followed by its holders, with ";"
between holders. The Registro abbreviates the labels in many ways ("Adm. Unico",
"ADM.UNICO", "Apo.Manc.") and they carry dots and spaces of their own, so the colon is
where a label is sure to end, and its start has to be found: the colon of the label
before, when it lies within ``_MAX_LABEL`` characters, or else the last "." or ";" of that
stretch. Whatever the stretch took from the holders before it, the tail of a dotted legal
form ("S.L.") included, is given back to them. The label is kept as published and mapped
to a canonical role by its letters (:func:`canonical_role`).

Two kinds of label are printed without a separator: glued to the last holder written in
capitals ("PERSONA DOSSecretario:") and after a spaced dash ("PERSONA UNO - Consejero:").
:func:`split_glued_roles` puts a ". " in front of them before the labels are read.

Not every piece between two labels is a holder. A sentence printed after the names is cut
off where its lowercase text starts (:func:`cut_holder`), and a piece is dropped when it
cannot be a name (a single word, a code), when it is prose (two or more standalone
lowercase words: the Registro writes names in capitals) or when it carries an ID number
(five digits or more in a row) and is not a company name. A name in mixed case is still
a name.

A holder is a legal person by what its name carries, never by what it lacks
(:func:`is_company_name`): a Spanish legal form at the end; anywhere, a word that only the
names of legal persons use (the long words of the legal forms, foreign and professional
forms, the types of legal person and the measured company words); as the last word only, a
form that inside a name is a surname, a first name or initials ("SAS", "SPA", "AG",
"SAP"); or the start of a legal-form word where the Registro cut a long name.

Holders are kept as published, never reordered: across companies the same words in
another order can be two people. Inside one company and one role they are one person, and
:func:`holder_key` is the key that says so (``positions.py``). :func:`auditor_class` reads
from an auditor's label which accounts the appointment is for (individual, consolidated
or substitute): an auditor is replaced by the next one of its class, not removed.
:func:`act_scope` gives every act its scope (board, attorney, auditor or company).

The role labels (``lists/role_labels.txt``) and the company words (``lists/company_words.txt``)
are measured on the public corpus by ``borme-radar formats``; the rule for each is in
docs/history.md. The legal persons that carry no legal form are known by the type of entity
their name starts with, which the law sets (:data:`LEGAL_PERSON_WORDS`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

from borme_radar.normalize import (
    LEGAL_FORM_TOKENS,
    has_legal_form,
    normalize_name,
    plain,
    word_list,
    word_set,
    word_table,
)
from borme_radar.persons import name_words

# What each officer act does to the positions it lists.
EVENT_BY_ACT = {
    "appointment": "appointment",
    "officer_removal": "removal",
    "ex_officio_cancellation": "removal",
    "revocation": "revocation",
    "reelection": "reelection",
}
OFFICER_ACTS = frozenset(EVENT_BY_ACT)

# The canonical roles. Every one but the last four belongs to the governing body.
ROLES = (
    "sole_director",
    "joint_several_director",
    "joint_director",
    "insolvency_administrator",
    "director",
    "managing_director",
    "board_member",
    "vice_chair",
    "chair",
    "vice_secretary",
    "secretary",
    "liquidator",
    "committee_member",
    "representative",
    "attorney",
    "auditor",
    "professional_partner",
    "trustee",
)
# How the role is read from the letters of a label, the first rule that fits. Written from
# the labels the gazette prints; a committee (member, chair or secretary of it) is not the
# board, and "MANCOM" is a joint director, not a committee.
_LABEL_RULES = tuple(
    (re.compile(pattern), role)
    for pattern, role in (
        (r"ADM\w*UNI", "sole_director"),
        (r"ADM\w*SOL", "joint_several_director"),
        (r"ADM\w*(MANC|CONJ)", "joint_director"),
        (r"ADM\w*CONC", "insolvency_administrator"),
        (r"ADM", "director"),
        (r"APO", "attorney"),
        (r"AUD", "auditor"),
        (r"LIQ", "liquidator"),
        (r"(CONS?\w*DEL|CODE)", "managing_director"),
        (r"(?!\w*MANCOM)\w+COM", "committee_member"),
        (r"(CONS|CONIND|MIECONS)", "board_member"),
        (r"(VICEP|VCP|VPR)", "vice_chair"),
        (r"(PRES|PRECONS)", "chair"),
        (r"(VICESEC|VSEC)", "vice_secretary"),
        (r"SEC", "secretary"),
        (r"MIE", "committee_member"),
        (r"(REP|RLC)", "representative"),
        (r"SOC\w*PROF", "professional_partner"),
        (r"PATRON", "trustee"),
    )
)
# The labels used most in 18 years with the role the rules read in them (measured by
# `borme-radar formats`; docs/history.md): a label seen there is looked up directly.
_ROLE_OF_LABEL = word_table("role_labels.txt")
BOARD_ROLES = frozenset(ROLES[:-4])
SCOPE_OF_ROLE = dict.fromkeys(BOARD_ROLES, "board") | {
    "attorney": "attorney",
    "auditor": "auditor",
}

# Longest role label: what lies further than this before a colon is free text.
_MAX_LABEL = 32
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÜÑÇ"
_LOWER = _UPPER.lower()
# Wording about the governing body inside "Otros conceptos".
_BOARD_WORDS_RE = re.compile(r"ORGANO DE ADMINISTRACION|ADMINISTRADOR|CONSEJ")
# A capitalised word: where a label printed without separator starts.
_CAPITALISED_RE = re.compile(f"[{_UPPER}][{_LOWER}]")
# Lowercase words of three letters or more, standing alone (not inside "PERSONa").
_LOWERCASE_WORD_RE = re.compile(f"(?<![{_UPPER}{_LOWER}])[{_LOWER}]{{3,}}(?![{_UPPER}{_LOWER}])")
# How many lowercase words make a piece a sentence rather than a name; one alone is not
# enough. `formats` counts holders by how many they carry.
PROSE_LOWER_WORDS = 2
# A run of this many digits is an ID number (as printed after a person's name), which only
# a company name may carry. `formats` counts holders by their longest run.
ID_DIGITS = 5
# A dotted legal form ("S.L.") whose tail the label window swallowed: "L. Apo.Sol".
_FORM_TAIL_RE = re.compile(r"^(?:[A-Z]\.\s*)*[A-Z]\.?\s+(?=\S)")
# Where a label starts when it is far from the previous holders.
_BOUNDARY_RE = re.compile(r"[.;]\s*")

# The long words of the Spanish legal forms written in full ("SOCIEDAD", "LIMITADA"); the
# short ones ("DE", "S") are also parts of people's names.
_LONG_FORM_WORDS = frozenset(w for w in LEGAL_FORM_TOKENS if len(w) >= 6)
# Foreign and professional legal forms that count anywhere in a holder's name; those that
# could be a name were checked where the corpus has them (`formats`, docs/history.md).
FORMS_ANYWHERE = frozenset(
    {"BV", "CB", "COOP", "GMBH", "INC", "LIMITED", "LLC", "LLP", "LTD", "LTDA", "NV", "PLC"}
    | {"SARL", "SAT", "SC", "SRC", "SRCP", "SRL"}
)
# Forms that inside a name are surnames, first names or initials: they count only as the
# last word.
FORMS_AT_END = frozenset({"AG", "SAP", "SAS", "SPA"})
# Legal persons that carry no legal form, named by the type of entity the law makes them:
# foundations, associations, city councils, universities, savings banks, public
# institutes, federations, consortia and agencies, also in Catalan and Galician. They are
# not measured: by position in a name they look like surnames (docs/history.md).
LEGAL_PERSON_WORDS = word_set(
    "FUNDACION FUNDACIO ASOCIACION ASSOCIACIO AYUNTAMIENTO AJUNTAMENT CONCELLO UNIVERSIDAD"
    " UNIVERSITAT CAJA CAIXA INSTITUTO CONFEDERACION FEDERACION CONSORCIO AGENCIA"
)
# Words that in holders appear almost only in names with a legal form, so they count
# anywhere; measured by `borme-radar formats` (rule in docs/history.md).
MEASURED_COMPANY_WORDS = word_list("company_words.txt")


def company_vocabulary(measured: frozenset[str]) -> frozenset[str]:
    """Every word that makes a holder a legal person wherever it stands: the legal-person
    words, ``measured`` (the company words, shipped or just derived by ``formats``), the
    long words of the Spanish legal forms and the forms that count anywhere."""
    return LEGAL_PERSON_WORDS | measured | _LONG_FORM_WORDS | FORMS_ANYWHERE


COMPANY_WORDS = company_vocabulary(MEASURED_COMPANY_WORDS)
# The Registro cuts holders at 50 characters: from this length (trailing punctuation
# stripped) a name may have lost its end. Measured by `formats` (docs/history.md).
TRUNCATED_LENGTH = 49


@dataclass(frozen=True, slots=True)
class Holding:
    role: str  # canonical role, e.g. "sole_director"
    role_raw: str  # as published, e.g. "Adm. Unico"
    holder: str  # as published, words never reordered
    is_company: bool


def canonical_role(raw: str) -> str:
    """The canonical role of a published label, from its letters alone ("Adm. Unico" ->
    ADMUNICO): the role of the longest prefix the role table knows, or "other"."""
    letters = "".join(ch for ch in plain(raw) if "A" <= ch <= "Z")
    return _ROLE_OF_LABEL.get(letters) or role_of_label(letters) or "other"


def role_of_label(letters: str) -> str | None:
    """The role the letters of a label stand for ("ADMUNICO": sole director), or None."""
    for pattern, role in _LABEL_RULES:
        if pattern.match(letters):
            return role
    return None


# ------------------------------------------------------------------ legal persons


class _Name(NamedTuple):
    raw: str
    words: list[str]  # plain words as printed
    joined: list[str]  # plain words with dotted abbreviations joined ("S.C." -> "SC")
    company_words: frozenset[str]
    cut_length: int


def _has_form(name: _Name) -> bool:
    return has_legal_form(name.raw)


def _has_company_word(name: _Name) -> bool:
    return not name.company_words.isdisjoint((*name.words, *name.joined))


def _ends_in_form(name: _Name) -> bool:
    return bool(name.joined) and name.joined[-1] in FORMS_AT_END


def _cut_in_form(name: _Name) -> bool:
    """A name of the Registro's cut length or longer whose last word (three letters or more)
    starts a long legal-form word: "... FOTOVOLTAICO SOCIED" lost the rest of its form."""
    if len(name.raw.strip()) < name.cut_length:
        return False
    tail = name.words[-1]
    return len(tail) >= 3 and any(word.startswith(tail) for word in _LONG_FORM_WORDS)


_COMPANY_TESTS = (_has_form, _has_company_word, _ends_in_form, _cut_in_form)


def is_company_name(
    holder: str, words: frozenset[str] | None = None, cut_length: int | None = None
) -> bool:
    """Is the holder a legal person? By what its name carries (see the module docstring),
    read as printed and in its normalised form, where dotted abbreviations are joined
    ("S.C." -> SC). ``words`` and ``cut_length`` default to :data:`COMPANY_WORDS` and
    :data:`TRUNCATED_LENGTH`; ``formats`` passes the values it measures."""
    plain_words = plain(holder).split()
    if not plain_words:
        return False
    name = _Name(
        holder,
        plain_words,
        normalize_name(holder).split(),
        COMPANY_WORDS if words is None else words,
        TRUNCATED_LENGTH if cut_length is None else cut_length,
    )
    return any(test(name) for test in _COMPANY_TESTS)


def holder_key(holder: str) -> str:
    """The key that makes one person of the same words in another order, meant for one
    company and one role: the words of the name (:func:`persons.name_words`) other than
    legal-form words, sorted, so "APELLIDOA APELLIDOB NOMBRE" and "NOMBRE APELLIDOA
    APELLIDOB" share it. A name left with none of them keeps all its plain words, sorted."""
    kept = sorted(w for w in name_words(holder) if w not in LEGAL_FORM_TOKENS)
    return " ".join(kept or sorted(plain(holder).split()))


def auditor_class(role_raw: str) -> str:
    """Which accounts an auditor label is for: "substitute", "consolidated" or "individual".

    The first word ("Aud", "AUDIT", "Auditor") is skipped and the rest read as dotted
    abbreviations: one starting with SU is a substitute, also for consolidated accounts
    ("Aud.Su.C.Con"); CON, CC, C.C or a word starting with CONSOL is consolidated, but not
    CONJUN (a joint audit). Anything else is individual."""
    rest = "." + ".".join(re.findall(r"[A-Z]+", plain(role_raw))[1:]) + "."
    if re.search(r"\.SU", rest):
        return "substitute"
    if re.search(r"\.(?:CON|CC|C\.C)\.|\.CONSOL", rest):
        return "consolidated"
    return "individual"


# ------------------------------------------------------------------ reading the text


def _is_upper(ch: str) -> bool:
    return ch in _UPPER


def _label_ahead(text: str, start: int) -> bool:
    """Could a role label start at ``start``: a colon within a label's length after it, and
    no ";" (a list of holders) before that colon?"""
    colon = text.find(":", start, start + _MAX_LABEL + 3)
    return colon > start and ";" not in text[start:colon]


def split_glued_roles(detail: str) -> str:
    """The act text with ". " put in front of every label printed without a separator: a
    capitalised word with a colon a label's length ahead, glued to two capitals (the end
    of the last holder, its dot aside: "PERSONA DOSSecretario:") or after a spaced dash
    ("PERSONA UNO - Consejero:"). A stray lowercase letter inside a name has no colon
    after it and is left alone."""
    text = (detail or "").strip()
    cuts: list[tuple[int, int]] = []  # (from, to) replaced by ". "
    for match in _CAPITALISED_RE.finditer(text):
        at = match.start()
        if not _label_ahead(text, at):
            continue
        before = at - 1 if at and text[at - 1] == "." else at
        if before >= 2 and _is_upper(text[before - 1]) and _is_upper(text[before - 2]):
            cuts.append((before, at))
        elif at >= 3 and text[at - 3 : at] == " - ":
            cuts.append((at - 3, at))
    for begin, end in reversed(cuts):
        text = f"{text[:begin]}. {text[end:]}"
    return text


class _Label(NamedTuple):
    start: int  # where the label begins
    value: int  # where its holders begin (after the colon)
    text: str


def _trim_label(window: str) -> str:
    """The label at the end of the text before a colon. Pieces before a ". " that hold a
    space or a ";" are holders and are dropped (the last piece always stays: "Consejero
    Delegado"), and so is the tail of a dotted legal form caught in front ("L. Apo.Sol")."""
    pieces = window.split(". ")
    first = 0
    while first < len(pieces) - 1 and (" " in pieces[first].strip() or ";" in pieces[first]):
        first += 1
    label = ". ".join(pieces[first:]).strip(" .;")
    return label if len(label) <= 2 else _FORM_TAIL_RE.sub("", label)


def _find_labels(text: str) -> list[_Label]:
    """The role labels of the act text, in order. A label ends at a colon and starts at the
    previous label's colon (or the start of the text) when that is no further back than
    :data:`_MAX_LABEL`, else after the first "." or ";" within that distance. A colon with
    neither, or a label that does not start with a letter, belongs to free text."""
    labels: list[_Label] = []
    floor = 0
    colon = text.find(":")
    while colon != -1:
        if colon - floor <= _MAX_LABEL:
            begin = floor
        else:
            boundary = _BOUNDARY_RE.search(text, colon - _MAX_LABEL, colon)
            begin = boundary.end() if boundary else -1
        if begin >= 0:
            window = text[begin:colon]
            label = _trim_label(window)
            if label[:1].isalpha():
                labels.append(_Label(begin + window.rfind(label), colon + 1, label))
                floor = colon + 1
        colon = text.find(":", colon + 1)
    return labels


def _lowercase_follows(text: str) -> bool:
    """Does lowercase text start here, spaces aside: a lowercase letter, or a capital
    followed by one, as at the start of a sentence?"""
    head = text.lstrip()[:2]
    if not head:
        return False
    return head[0] in _LOWER or (len(head) == 2 and _is_upper(head[0]) and head[1] in _LOWER)


def cut_holder(piece: str) -> tuple[str, bool]:
    """The holder in a piece, and whether it was cut: the piece ends at the first dot that
    lowercase text follows ("PERSONA UNO. Cancelaciones de oficio ..."), not at the dots
    of an abbreviation ("S.L.")."""
    dot = piece.find(".")
    while dot != -1:
        if _lowercase_follows(piece[dot + 1 :]):
            return piece[:dot].strip(" .,;"), True
        dot = piece.find(".", dot + 1)
    return piece.strip(" .,;"), False


def is_prose(text: str) -> bool:
    """Is the text a sentence rather than a name: :data:`PROSE_LOWER_WORDS` or more
    standalone lowercase words of three letters or more."""
    words = _LOWERCASE_WORD_RE.findall(text)
    return len(words) >= PROSE_LOWER_WORDS


def longest_digit_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"\d+", text)), default=0)


def readable(holder: str) -> bool:
    """Could the piece be a name at all: two words or more, five characters or more, and a
    word made only of letters (a code such as "B12X9Z" has none)."""
    words = plain(holder).split()
    return len(words) >= 2 and len(" ".join(words)) >= 5 and any(map(str.isalpha, words))


def _is_holder(holder: str) -> bool:
    if not readable(holder) or is_prose(holder):
        return False
    return longest_digit_run(holder) < ID_DIGITS or is_company_name(holder)


def label_pieces(detail: str) -> list[tuple[str, str]]:
    """(label as published, piece of text) for every slot of an officer act: glued labels
    split off, and the text after each label up to the next one split at ";". The pieces
    are not yet cut or checked as holders (:func:`holdings` does that)."""
    text = split_glued_roles(detail)
    labels = _find_labels(text)
    ends = [label.start for label in labels[1:]] + [len(text)] if labels else []
    return [
        (label.text, piece)
        for label, end in zip(labels, ends, strict=True)
        for piece in text[label.value : end].split(";")
    ]


def glued_labels(detail: str) -> int:
    """How many more labels the act has once :func:`split_glued_roles` has split the glued
    ones off (counted by ``formats``)."""
    return len(_find_labels(split_glued_roles(detail))) - len(_find_labels((detail or "").strip()))


def roles_in(detail: str) -> list[str]:
    """The canonical role of every label of the act, in order."""
    return [canonical_role(label.text) for label in _find_labels(split_glued_roles(detail))]


def holdings(detail: str) -> list[Holding]:
    """The positions an officer act lists, in the order printed. Each piece is cut where a
    sentence starts and kept only if it can be a holder (readable, not prose, no ID number
    unless it is a company name); the same holder (in plain form) under the same role is
    kept once."""
    found: dict[tuple[str, str], Holding] = {}
    for label, piece in label_pieces(detail):
        holder = cut_holder(piece)[0]
        if not _is_holder(holder):
            continue
        role = canonical_role(label)
        if (role, plain(holder)) not in found:
            found[(role, plain(holder))] = Holding(role, label, holder, is_company_name(holder))
    return list(found.values())


def act_scope(act_type: str, detail: str) -> str:
    """Who the act is about: "board", "attorney", "auditor" or "company".

    An officer act takes the scope of its roles, board first, then auditor, then attorney,
    and is board when no label can be read (the conservative side: board acts are never of
    low priority). Of the other acts, only an "other" act ("Otros conceptos") that names
    the governing body is about the board."""
    if act_type not in OFFICER_ACTS:
        about_board = act_type == "other" and _BOARD_WORDS_RE.search(plain(detail))
        return "board" if about_board else "company"
    scopes = {SCOPE_OF_ROLE.get(role, "company") for role in roles_in(detail)}
    if not scopes:
        return "board"
    return next((s for s in ("board", "auditor", "attorney") if s in scopes), "company")
