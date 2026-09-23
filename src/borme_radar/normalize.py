"""Company name normalisation used for matching.

The same company appears as ``TELEFÓNICA, S.A.``, ``TELEFONICA SA`` or
``TELEFONICA SOCIEDAD ANONIMA``. Normalisation makes those identical:

1. uppercase and strip accents (``Ñ`` becomes ``N``);
2. drop registry annotations such as ``(R.M. PALMA DE MALLORCA)``;
3. collapse dotted acronyms (``S.L.U.`` -> ``SLU``) and remove punctuation;
4. drop the ``EN LIQUIDACION`` status suffix (a company in liquidation is the same
   legal person);
5. map the trailing legal form to one canonical token (``SOCIEDAD ANONIMA`` -> ``SA``).
   Unipersonal variants (``SAU``, ``SLU``) map to the base form: being single-member is
   a status that changes over time, not a different company.
"""

from __future__ import annotations

import re
import unicodedata

_REGISTRY_NOTE_RE = re.compile(r"\(\s*R\.?\s*M\.?[^)]*\)")
# Letter-dot sequences, last letter's dot optional: S.A. / S. L. U. / S.L / S.M.E.
_DOTTED_ACRONYM_RE = re.compile(r"\b(?:[A-Z]\.\s?)+[A-Z]\b\.?")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]+")
_STATUS_SUFFIXES: tuple[tuple[str, ...], ...] = (("EN", "LIQUIDACION"),)

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


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


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


def normalize_name(name: str) -> str:
    """Canonical matching key for a company name (see module docstring)."""
    text = _strip_accents(name.upper())
    text = _REGISTRY_NOTE_RE.sub(" ", text)
    text = _DOTTED_ACRONYM_RE.sub(lambda m: re.sub(r"[.\s]", "", m.group(0)) + " ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    tokens = _strip_suffixes(text.split())
    base, form = split_legal_form(tokens)
    # "X SA EN LIQUIDACION" and "X EN LIQUIDACION SA" both reduce to "X SA".
    base = _strip_suffixes(base)
    return " ".join([*base, form] if form else base)
