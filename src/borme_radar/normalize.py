"""Company name normalisation used for matching.

The same company appears as ``TELEFÓNICA, S.A.``, ``TELEFONICA SA`` or
``TELEFONICA SOCIEDAD ANONIMA``. Normalisation makes those identical:

1. uppercase and strip accents (``Ñ`` becomes ``N``);
2. drop registry annotations the Registro appends to the name: ``(R.M. PALMA DE
   MALLORCA)`` when the sheet moves to another registry, and the financial year the
   Registro glues to the name in accounts filings, ``EJEMPLO SL(2008)`` (sometimes with
   the bracket left open, sometimes after the ``(R.M. ...)`` note);
3. collapse dotted acronyms (``S.L.U.`` -> ``SLU``) and remove punctuation;
4. drop status suffixes that come and go while the legal person stays the same:
   ``EN LIQUIDACION``, ``EN CONCURSO`` (``DE ACREEDORES``), ``UNIPERSONAL`` and
   ``SOCIEDAD UNIPERSONAL``;
5. map the trailing legal form to one canonical token (``SOCIEDAD ANONIMA`` -> ``SA``).
   Unipersonal variants (``SAU``, ``SLU``) map to the base form: being single-member is
   a status that changes over time, not a different company.
"""

from __future__ import annotations

import re
import unicodedata

# "(R.M. PALMA DE MALLORCA)" or "(RM EIVISSA)", sometimes with the bracket left open.
_REGISTRY_NOTE_RE = re.compile(r"\(\s*R\s?\.\s?M\s?\.[^)]*\)?|\(\s*RM\s[^)]*\)?")
# Financial year glued to the name in accounts filings: "X SL(2008)", "X SL(2008".
_YEAR_SUFFIX_RE = re.compile(r"\(\s*(?:19|20)\d{2}\s*\)?\s*$")
# Letter-dot sequences, last letter's dot optional: S.A. / S. L. U. / S.L / S.M.E.
_DOTTED_ACRONYM_RE = re.compile(r"\b(?:[A-Z]\.\s?)+[A-Z]\b\.?")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]+")
# Longest first: "EN CONCURSO DE ACREEDORES" before "EN CONCURSO".
_STATUS_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("EN", "CONCURSO", "DE", "ACREEDORES"),
    ("EN", "LIQUIDACION"),
    ("EN", "CONCURSO"),
    ("SOCIEDAD", "UNIPERSONAL"),
    ("UNIPERSONAL",),
)

# Trailing legal form (as tokens) -> canonical token. Longest suffix wins.
_LEGAL_FORMS: dict[tuple[str, ...], str] = {
    tuple(key.split()): value
    for key, value in {
        "SOCIEDAD ANONIMA UNIPERSONAL": "SA",
        "SOCIEDAD ANONIMA LABORAL": "SAL",
        "SOCIEDAD ANONIMA DEPORTIVA": "SAD",
        "SOCIEDAD ANONIMA": "SA",
        "SOCIEDAD DE RESPONSABILIDAD LIMITADA UNIPERSONAL": "SL",
        "SOCIEDAD DE RESPONSABILIDAD LIMITADA": "SL",
        "SOCIEDAD LIMITADA UNIPERSONAL": "SL",
        "SOCIEDAD LIMITADA LABORAL": "SLL",
        "SOCIEDAD LIMITADA PROFESIONAL": "SLP",
        "SOCIEDAD LIMITADA NUEVA EMPRESA": "SLNE",
        "SOCIEDAD LIMITADA": "SL",
        "SOCIEDAD COOPERATIVA": "COOP",
        "S COOP": "COOP",
        "SCOOP": "COOP",
        "AGRUPACION DE INTERES ECONOMICO": "AIE",
        "SOCIEDAD EUROPEA": "SE",
        "SAU": "SA",
        "SA": "SA",
        "SAL": "SAL",
        "SAD": "SAD",
        "SLU": "SL",
        "SRL": "SL",
        "SL": "SL",
        "SLL": "SLL",
        "SLP": "SLP",
        "SLNE": "SLNE",
        "AIE": "AIE",
        "COOP": "COOP",
        "SE": "SE",
    }.items()
}
_MAX_FORM_LEN = max(len(key) for key in _LEGAL_FORMS)
LEGAL_FORM_TOKENS = frozenset(token for key in _LEGAL_FORMS for token in key) | {
    "SOCIEDAD",
    "LIMITADA",
    "ANONIMA",
    "UNIPERSONAL",
}


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def plain(text: str) -> str:
    """Uppercase, no accents, only letters, digits and single spaces."""
    text = _NON_ALNUM_RE.sub(" ", strip_accents(text.upper()))
    return " ".join(text.split())


def _strip_suffixes(tokens: list[str]) -> list[str]:
    changed = True
    while changed:
        changed = False
        for suffix in _STATUS_SUFFIXES:
            if len(tokens) > len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                tokens = tokens[: -len(suffix)]
                changed = True
    return tokens


def split_legal_form(tokens: list[str]) -> tuple[list[str], str | None]:
    """Split normalised tokens into (base tokens, canonical legal form or None)."""
    for size in range(min(_MAX_FORM_LEN, len(tokens) - 1), 0, -1):
        form = _LEGAL_FORMS.get(tuple(tokens[-size:]))
        if form is not None:
            return tokens[:-size], form
    return tokens, None


def base_and_form(norm: str) -> tuple[str, str | None]:
    """Split an already normalised name into (base name, canonical legal form)."""
    base, form = split_legal_form(norm.split())
    return " ".join(base), form


def strip_annotations(name: str) -> str:
    """Remove the registry notes the Registro appends to a name (see module doc)."""
    text = name.strip()
    for _ in range(3):  # "X SL(R.M. MADRID)(2008)": year, note, year again
        before = text
        text = _YEAR_SUFFIX_RE.sub("", text).strip()
        text = _REGISTRY_NOTE_RE.sub(" ", text).strip()
        if text == before:
            break
    return text


def normalize_name(name: str) -> str:
    """Canonical matching key for a company name (see module docstring)."""
    text = strip_accents(strip_annotations(name).upper())
    text = _DOTTED_ACRONYM_RE.sub(lambda m: re.sub(r"[.\s]", "", m.group(0)) + " ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    tokens = _strip_suffixes(text.split())
    base, form = split_legal_form(tokens)
    # "X SA EN LIQUIDACION" and "X EN LIQUIDACION SA" both reduce to "X SA".
    base = _strip_suffixes(base)
    return " ".join([*base, form] if form else base)


def has_legal_form(name: str) -> bool:
    """True when a (normalised or raw) name ends with a legal form: a legal person."""
    return base_and_form(normalize_name(name))[1] is not None
