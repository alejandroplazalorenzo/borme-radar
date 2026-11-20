"""Company names reduced to a key that compares, and the word lists shipped in ``lists/``.

The gazette writes one company in many ways: "DISEÑOS EJEMPLO, S.L.U.", "Diseños
Ejemplo SL", "DISEÑOS EJEMPLO SOCIEDAD LIMITADA". :func:`normalize_name` turns all of
them into one key, "DISENOS EJEMPLO SL":

1. what the Registro appends to a name is dropped: a registry note "(R.M. ...)" and a
   year in brackets at the end, "(2019)" (:func:`strip_annotations`);
2. upper case, without accents or other combining marks (Ñ becomes N);
3. dotted acronyms are closed up ("S. L. U." -> "SLU") and any other character that is
   not a letter or a digit becomes a space;
4. the words of a passing status are dropped from the end, before or after the legal form
   (``STATUS_WORDS``: liquidation, insolvency, a single shareholder);
5. the legal form at the end, in any of its spellings, becomes its canonical code (SA,
   SL, SLL, SLP, SLNE, SAL, SAD, SE, AIE, COOP); the sole-shareholder variants (SAU,
   SLU) are the same form.

The words of the name are never reordered. A legal form is only looked for at the end,
and a name made of a form alone keeps it as its name. :func:`base_and_form` splits a key
into the name and the form; :func:`plain` is the lighter form used to compare words,
which knows nothing of legal forms.

The word lists are text files in ``lists/``: one entry per line, ``#`` starts a
comment. Some are measured on the corpus by ``borme-radar formats`` (docs/history.md).
"""

from __future__ import annotations

import re
import unicodedata
from importlib import resources

# "(R.M. PALMA DE MALLORCA)" or "(RM EIVISSA)", sometimes with the bracket left open.
_REGISTRY_NOTE_RE = re.compile(r"\(\s*R\s?\.\s?M\s?\.[^)]*\)?|\(\s*RM\s[^)]*\)?")
# Letter-dot sequences, last letter's dot optional: S.A. / S. L. U. / S.L / S.M.E.
_DOTTED_ACRONYM_RE = re.compile(r"\b(?:[A-Z]\.\s?)+[A-Z]\b\.?")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]+")
# The words a company carries in its name while a status lasts, by status: the law asks
# for "en liquidación" while a company is wound up and for "unipersonal" while it has one
# shareholder, and the Registro prints "en concurso" during insolvency proceedings. They
# come and go, so they are not part of the name.
STATUS_WORDS: dict[str, tuple[str, ...]] = {
    "insolvency": ("EN CONCURSO", "EN CONCURSO DE ACREEDORES"),
    "liquidation": ("EN LIQUIDACION",),
    "single shareholder": ("SOCIEDAD UNIPERSONAL", "UNIPERSONAL"),
}
# Any run of them at the end of a name, after at least one other word.
_STATUS_TAIL_RE = re.compile(
    r"(?:\s+(?:"
    + "|".join(sorted((w for ws in STATUS_WORDS.values() for w in ws), key=len, reverse=True))
    + r"))+$"
)

# Canonical legal form -> how it is written once the dots are gone. Being unipersonal is a
# status, not another company, so SAU and SLU are SA and SL.
_FORM_SPELLINGS: dict[str, tuple[str, ...]] = {
    "AIE": ("AGRUPACION DE INTERES ECONOMICO", "AIE"),
    "COOP": ("SOCIEDAD COOPERATIVA", "S COOP", "SCOOP", "COOP"),
    "SA": ("SOCIEDAD ANONIMA", "SOCIEDAD ANONIMA UNIPERSONAL", "SA", "SAU"),
    "SAD": ("SOCIEDAD ANONIMA DEPORTIVA", "SAD"),
    "SAL": ("SOCIEDAD ANONIMA LABORAL", "SAL"),
    "SE": ("SOCIEDAD EUROPEA", "SE"),
    "SL": (
        "SOCIEDAD LIMITADA",
        "SOCIEDAD LIMITADA UNIPERSONAL",
        "SOCIEDAD DE RESPONSABILIDAD LIMITADA",
        "SOCIEDAD DE RESPONSABILIDAD LIMITADA UNIPERSONAL",
        "SL",
        "SLU",
        "SRL",
    ),
    "SLL": ("SOCIEDAD LIMITADA LABORAL", "SLL"),
    "SLNE": ("SOCIEDAD LIMITADA NUEVA EMPRESA", "SLNE"),
    "SLP": ("SOCIEDAD LIMITADA PROFESIONAL", "SLP"),
}
_LEGAL_FORMS: dict[tuple[str, ...], str] = {
    tuple(spelling.split()): form
    for form, spellings in _FORM_SPELLINGS.items()
    for spelling in spellings
}
# Articles and contractions of three or four letters (Spanish, Catalan, Galician): they
# pass a three-letter floor yet name nothing by themselves. A matter of grammar.
ARTICLES = frozenset({"DEL", "DELS", "LAS", "LOS", "LES", "ELS", "DAS", "DOS"})
_MAX_FORM_LEN = max(len(key) for key in _LEGAL_FORMS)
LEGAL_FORM_TOKENS = frozenset(token for key in _LEGAL_FORMS for token in key)


# The Unicode blocks of combining marks (the half marks are added below): after NFKD an
# accent is a code point of its own in one of them, and translate() deletes it.
_MARK_BLOCKS = ((0x300, 0x36F), (0x1AB0, 0x1AFF), (0x1DC0, 0x1DFF), (0x20D0, 0x20FF))
_ACCENT_MARKS = dict.fromkeys(
    code for first, last in (*_MARK_BLOCKS, (0xFE20, 0xFE2F)) for code in range(first, last + 1)
)


def word_set(text: str) -> frozenset[str]:
    """The words of a list written in the code, separated by whitespace."""
    return frozenset(text.split())


def word_list(name: str) -> frozenset[str]:
    """The first word of each row of the shipped list ``lists/<name>``."""
    return frozenset(row[0] for row in _list_rows(name))


def word_table(name: str) -> dict[str, str]:
    """A shipped list of two columns, as a mapping from the first word to the second."""
    return {row[0]: row[1] for row in _list_rows(name)}


def _list_rows(name: str) -> list[list[str]]:
    text = resources.files("borme_radar").joinpath("lists", name).read_text(encoding="utf-8")
    rows = (line.partition("#")[0].split() for line in text.splitlines())
    return [row for row in rows if row]


def strip_accents(text: str) -> str:
    """The text without accents or other combining marks ("Ñ" becomes "N")."""
    if text.isascii():
        return text
    return unicodedata.normalize("NFKD", text).translate(_ACCENT_MARKS)


def plain(text: str) -> str:
    """Upper case, no accents, letters and digits only, single-spaced: the form in which
    words are compared."""
    text = _NON_ALNUM_RE.sub(" ", strip_accents(text.upper()))
    return " ".join(text.split())


def _without_status(tokens: list[str]) -> list[str]:
    return _STATUS_TAIL_RE.sub("", " ".join(tokens)).split()


def split_legal_form(tokens: list[str]) -> tuple[list[str], str | None]:
    """Split the longest spelling of a legal form off the end of the tokens and return
    the rest with the canonical form, or the tokens and None. At least one token is left
    as the name."""
    for size in range(min(_MAX_FORM_LEN, len(tokens) - 1), 0, -1):
        form = _LEGAL_FORMS.get(tuple(tokens[-size:]))
        if form is not None:
            return tokens[:-size], form
    return tokens, None


def base_and_form(norm: str) -> tuple[str, str | None]:
    """A normalised name split into the name without its legal form and the form:
    "DISENOS EJEMPLO SL" -> ("DISENOS EJEMPLO", "SL")."""
    base, form = split_legal_form(norm.split())
    return " ".join(base), form


def _final_year(text: str) -> int | None:
    """Where the last bracket starts when all it holds is a year (closed or not), or None."""
    opening = text.rfind("(")
    inside = text[opening + 1 :].strip().removesuffix(")").strip()
    is_year = len(inside) == 4 and inside.isdigit() and inside[:2] in ("19", "20")
    return opening if opening >= 0 and is_year else None


def strip_annotations(name: str) -> str:
    """The name without what the Registro appends to it: registry notes "(R.M. ...)"
    wherever they are, then any brackets holding only a year at the end ("X SL(2019)",
    "X SL (2019"), however they were mixed."""
    text = _REGISTRY_NOTE_RE.sub(" ", name).strip()
    while (cut := _final_year(text)) is not None:
        text = text[:cut].strip()
    return text


def normalize_name(name: str) -> str:
    """The key a company name is compared by: "TALLERES EJEMPLO, S.A. EN LIQUIDACIÓN" ->
    "TALLERES EJEMPLO SA" (the steps are in the module docstring)."""
    text = strip_accents(strip_annotations(name).upper())
    text = _DOTTED_ACRONYM_RE.sub(lambda m: re.sub(r"[.\s]", "", m.group(0)) + " ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    base, form = split_legal_form(_without_status(text.split()))
    # "X SA EN LIQUIDACION" and "X EN LIQUIDACION SA" both reduce to "X SA".
    base = _without_status(base)
    return " ".join([*base, form] if form else base)


def has_legal_form(name: str) -> bool:
    """Does the name end in a Spanish legal form, in any of its spellings?"""
    return base_and_form(normalize_name(name))[1] is not None
