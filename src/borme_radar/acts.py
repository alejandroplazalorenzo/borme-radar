"""The catalogue of act types, and how the text of an announcement is cut into acts.

A Section A announcement is a run of acts. Each act starts with a heading from a closed
list ("Nombramientos.", "Cambio de domicilio social.", ...) and runs to the next one; the
"Datos registrales" block closes the announcement. Every heading maps to a stable English
code with a label and a base priority.

Headings are matched with or without the accents of their vowels and with any spacing
between their words, and only when a full stop or a colon follows them. Where one is
found, what stands before it decides whether it opens an act (:func:`openings`): nothing
(the start of the text), a full stop, a full stop and a dash, or a capital letter it is
glued to, which happens when the Registro prints a heading right after a name in
capitals. Any other occurrence is a few words of the text of an act.

Section B is a table: the act type of an entry comes from the heading of its table
(:func:`section_b_type`). The priority of an act also depends on its scope
(:func:`priority`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, NamedTuple

from borme_radar.normalize import strip_accents

Priority = Literal["high", "medium", "low"]

REGISTRY_DATA = "registry_data"  # "Datos registrales": metadata, not an act


@dataclass(frozen=True, slots=True)
class ActType:
    code: str
    label: str
    priority: Priority


ACT_TYPES: dict[str, ActType] = {
    t.code: t
    for t in (
        # High: the company may be ending, changing shape or losing capital.
        ActType("capital_reduction", "Capital reduction", "high"),
        ActType("dissolution", "Dissolution", "high"),
        ActType("extinction", "Extinction (company struck off)", "high"),
        ActType("global_asset_transfer", "Global transfer of assets and liabilities", "high"),
        ActType(
            "global_transfer_project",
            "Global asset transfer project deposited (Section B)",
            "high",
        ),
        ActType("insolvency", "Insolvency proceedings (concurso)", "high"),
        ActType("merger", "Merger", "high"),
        ActType("merger_project", "Merger project deposited (Section B)", "high"),
        ActType(
            "preventive_annotation",
            "Preventive annotation (e.g. debtor declared insolvent)",
            "high",
        ),
        ActType("registry_sheet_closure", "Registry sheet closed", "high"),
        ActType("split", "Split / spin-off", "high"),
        ActType("split_project", "Split / spin-off project deposited (Section B)", "high"),
        ActType("transfer_abroad_project", "Transfer abroad project deposited (Section B)", "high"),
        ActType("uncollectible_debt", "Debt declared uncollectible", "high"),
        # Medium: who runs or owns it, its name, address, purpose and capital.
        ActType("accounts_not_approved", "Annual accounts not approved (art. 378.5 RRM)", "medium"),
        ActType("address_change", "Change of registered address", "medium"),
        ActType("appointment", "Appointment (directors, auditors, attorneys)", "medium"),
        ActType("bond_issue", "Bond issue", "medium"),
        ActType("branch_closure", "Branch closed", "medium"),
        ActType("branch_opening", "Branch opened", "medium"),
        ActType("business_purpose_change", "Change of corporate purpose", "medium"),
        ActType("business_purpose_extension", "Corporate purpose extended", "medium"),
        ActType("capital_increase", "Capital increase", "medium"),
        ActType("ex_officio_cancellation", "Appointments cancelled ex officio", "medium"),
        ActType("incorporation", "Incorporation", "medium"),
        ActType("name_change", "Change of company name", "medium"),
        ActType("officer_removal", "Removal / resignation of officers", "medium"),
        ActType("project_cancellation", "Deposited project cancelled (Section B)", "medium"),
        ActType("reactivation", "Reactivation", "medium"),
        ActType("registry_sheet_reopening", "Registry sheet reopened", "medium"),
        ActType("revocation", "Revocation (mainly powers of attorney)", "medium"),
        ActType("section_b_other", "Other Section B entry", "medium"),
        ActType("sole_shareholder_change", "Change of sole shareholder", "medium"),
        ActType("sole_shareholder_declared", "Sole shareholder declared", "medium"),
        ActType("sole_shareholder_lost", "Sole-shareholder status lost", "medium"),
        ActType("transformation", "Transformation (change of legal form)", "medium"),
        # Low: routine entries.
        ActType("bylaws_amendment", "By-laws amendment", "low"),
        ActType("capital_call_paid", "Unpaid capital paid up", "low"),
        ActType("duration_change", "Change of duration", "low"),
        ActType("erratum", "Erratum", "low"),
        ActType("first_registration", "First registration", "low"),
        ActType("other", "Other entries (free text)", "low"),
        ActType("powers_change", "Change of powers", "low"),
        ActType("reelection", "Re-election", "low"),
        ActType("sole_shareholder", "Sole-shareholder company", "low"),
        ActType("sole_trader", "Sole trader entry", "low"),
        ActType("statutory_adaptation", "Statutory adaptation to a new law", "low"),
        ActType("unexecuted_capital_increase", "Capital increase agreed, not executed", "low"),
        ActType("website", "Corporate website", "low"),
    )
}

# Act type -> headings as published. A plain heading is matched loosely (see _loose);
# one starting with "re:" is a regular expression, for headings with variable text.
_HEADINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("incorporation", ("Constitución",)),
    ("appointment", ("Nombramientos",)),
    ("officer_removal", ("Ceses/Dimisiones",)),
    ("revocation", ("Revocaciones",)),
    ("reelection", ("Reelecciones",)),
    ("ex_officio_cancellation", ("Cancelaciones de oficio de nombramientos",)),
    ("bylaws_amendment", ("Modificaciones estatutarias",)),
    ("address_change", ("Cambio de domicilio social",)),
    ("name_change", ("Cambio de denominación social",)),
    ("business_purpose_change", ("Cambio de objeto social",)),
    (
        "business_purpose_extension",
        ("Ampliación del objeto social", "Ampliacion de objeto social"),
    ),
    ("capital_increase", ("Ampliación de capital",)),
    ("capital_reduction", ("Reducción de capital",)),
    ("sole_shareholder_declared", ("Declaración de unipersonalidad",)),
    ("sole_shareholder_lost", ("Pérdida del carácter de unipersonalidad",)),
    ("sole_shareholder_change", ("Cambio de identidad del socio único",)),
    ("sole_shareholder", ("Sociedad unipersonal",)),
    ("dissolution", ("Disolución",)),
    ("extinction", ("Extinción",)),
    ("reactivation", ("Reactivación de la sociedad",)),
    ("transformation", ("Transformación de sociedad",)),
    ("merger", ("Fusión por absorción", "Fusión por unión")),
    ("split", ("Escisión parcial", "Escisión total", "Segregación")),
    ("global_asset_transfer", ("Cesión global de activo y pasivo",)),
    ("insolvency", ("Situación concursal", "Suspensión de pagos")),
    # Seen as "Anotación preventiva. Declaración de deudor fallido." (tax agency).
    ("preventive_annotation", ("Anotación preventiva",)),
    ("uncollectible_debt", ("Crédito incobrable",)),
    # Certificate that the accounts of a year were not approved (avoids registry closure).
    (
        "accounts_not_approved",
        (r"re:Art[ií]culo\s+378\.5\s+del\s+Reglamento\s+del\s+Registro\s+Mercantil",),
    ),
    # The heading carries the reason for the closure, with dotted article numbers ("art. 137.2"):
    # it runs to the first full stop or colon that is not followed by a digit.
    (
        "registry_sheet_closure",
        (r"re:Cierre\s+(?:provisional|definitivo)(?:[^.:]|\.(?=\s?\d))*",),
    ),
    ("registry_sheet_reopening", (r"re:Reapertura\s+(?:de\s+la\s+)?hoja\s+registral",)),
    ("branch_opening", ("Apertura de sucursal", "Primera sucursal de sociedad extranjera")),
    ("branch_closure", ("Cierre de sucursal",)),
    ("bond_issue", ("Emisión de obligaciones",)),
    ("capital_call_paid", ("Desembolso de dividendos pasivos",)),
    ("unexecuted_capital_increase", ("Acuerdo de ampliación de capital social sin ejecutar",)),
    ("duration_change", ("Modificación de duración",)),
    ("powers_change", ("Modificación de poderes",)),
    (
        "statutory_adaptation",
        (
            r"re:Adaptaci[oó]n\s+Ley\s+\d+/\d+",
            r"re:Adaptada\s+seg[uú]n\s+D\.T\.\s+2\s+apartado\s+2\s+Ley\s+2/95",
        ),
    ),
    ("first_registration", (r"re:Primera\s+inscripci[oó]n\s+\(O\.M\.\s+10/6/1\.997\)",)),
    ("website", ("Página web de la sociedad",)),
    ("sole_trader", ("Empresario Individual",)),
    ("erratum", ("Fe de erratas",)),
    ("other", ("Otros conceptos",)),
)
# The "Datos registrales" block (sheet, inscription and its date) closes a Section A
# announcement. It is metadata, not an act, and is read from its last occurrence.
_REGISTRY_HEADING = "Datos registrales"
_REGISTRY_RE = re.compile(r"Datos\s+registrales[.:]")

_VOWELS = {"a": "aá", "e": "eé", "i": "ií", "o": "oó", "u": "uúü"}


# Section B: start of the table heading (lowercase, without accents) -> act type.
_SECTION_B: tuple[tuple[str, str], ...] = (
    ("cancelaciones de depositos de proyectos", "project_cancellation"),
    ("cierre provisional de hoja registral", "registry_sheet_closure"),
    ("reapertura de hoja registral", "registry_sheet_reopening"),
    ("depositos de proyectos de fusion", "merger_project"),
    ("depositos de proyectos de escision", "split_project"),
    ("depositos de proyectos de segregacion", "split_project"),
    ("depositos de proyectos de cesion global", "global_transfer_project"),
    ("depositos de proyectos de traslado", "transfer_abroad_project"),
)


def section_b_type(heading: str) -> str:
    """Act type of a Section B entry from its table heading; ``section_b_other`` if unknown."""
    key = " ".join(strip_accents(heading.lower()).split())
    found = (act_type for prefix, act_type in _SECTION_B if key.startswith(prefix))
    return next(found, "section_b_other")


_RANK: tuple[Priority, ...] = ("low", "medium", "high")
# What the scope of an act allows of the priority of its type: (lowest, highest). Powers
# of attorney and auditors stay low; a change of the board is never below medium, even
# when the Registro files it under a type of low priority such as "Otros conceptos".
_SCOPE_RANGE: dict[str, tuple[Priority, Priority]] = {
    "attorney": ("low", "low"),
    "auditor": ("low", "low"),
    "board": ("medium", "high"),
}


def priority(act_type: str, scope: str) -> Priority:
    """The priority of the act type, held inside the range its scope allows."""
    base = ACT_TYPES[act_type].priority if act_type in ACT_TYPES else "low"
    lowest, highest = _SCOPE_RANGE.get(scope, ("low", "high"))
    rank = min(max(_RANK.index(base), _RANK.index(lowest)), _RANK.index(highest))
    return _RANK[rank]


def _loose(heading: str) -> str:
    """Regex for a heading: each vowel with or without accent, any whitespace between words."""
    parts: list[str] = []
    for ch in heading:
        base = strip_accents(ch)
        if base.lower() in _VOWELS:
            variants = _VOWELS[base.lower()]
            if base.isupper():
                variants = variants.upper()
            parts.append(f"[{variants}]")
        elif ch == " ":
            parts.append(r"\s+")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def _heading_regex() -> tuple[re.Pattern[str], dict[str, str]]:
    """One regex for every heading of the catalogue, followed by a full stop or a colon,
    and the act type of each of its named groups."""
    variants = [
        (heading[3:] if heading.startswith("re:") else _loose(heading), act_type)
        for act_type, headings in _HEADINGS
        for heading in headings
    ]
    # Longest first: of two headings that start alike ("Cierre de sucursal", "Cierre
    # provisional..."), the longer one is tried before the shorter.
    variants.sort(key=lambda variant: len(variant[0]), reverse=True)
    types = {f"h{i}": act_type for i, (_, act_type) in enumerate(variants)}
    body = "|".join(f"(?P<h{i}>{regex})" for i, (regex, _) in enumerate(variants))
    return re.compile(rf"(?:{body})(?=[.:](?:\s|$))"), types


_HEADING_RE, _TYPE_OF_GROUP = _heading_regex()


class Opening(NamedTuple):
    """A heading that opens an act: the text of the previous act ends at ``cut``, the
    heading is ``found``, and ``after`` says what stands before it: ``start``, ``stop``
    (a full stop), ``stop_dash`` (a full stop and a dash) or ``glued`` (a capital)."""

    cut: int
    found: re.Match[str]
    after: str

    @property
    def act_type(self) -> str:
        return _TYPE_OF_GROUP[self.found.lastgroup or ""]


def _before(text: str, at: int) -> tuple[int, str] | None:
    """Can a heading written at ``at`` open an act? Where the previous act ends and what
    stands before the heading, or None when it is only words inside an act."""
    k = at
    while k and text[k - 1].isspace():
        k -= 1
    dash = bool(k) and text[k - 1] == "-"
    if dash:
        k -= 1
        while k and text[k - 1].isspace():
            k -= 1
    if k == 0:
        return None if dash else (0, "start")
    if text[k - 1] == ".":
        return k, "stop_dash" if dash else "stop"
    if k == at and text[at - 1].isupper():
        return at, "glued"
    return None


def openings(text: str) -> list[Opening]:
    """The headings of the text that open an act, in order.

    A heading that opens nothing is passed over one character at a time, so that one
    starting inside it is still found; an opening is passed over whole.
    """
    found: list[Opening] = []
    at = 0
    while (heading := _HEADING_RE.search(text, at)) is not None:
        before = _before(text, heading.start())
        if before is None:
            at = heading.start() + 1
            continue
        found.append(Opening(before[0], heading, before[1]))
        at = heading.end()
    return found


@dataclass(frozen=True, slots=True)
class Segment:
    act_type: str
    heading: str
    detail: str


def _clean_detail(text: str) -> str:
    """Text of an act without the separator after its heading, extra spaces or final stop."""
    return " ".join(text.lstrip(".:").split()).rstrip(" .")


def split_acts(text: str) -> tuple[str, list[Segment]]:
    """Cut the text of an announcement into acts.

    Returns the text before the first act (empty when all of it was recognised) and the
    segments in order. The "Datos registrales" block, if any, is the last segment, of type
    ``REGISTRY_DATA``.
    """
    registry: Segment | None = None
    found = list(_REGISTRY_RE.finditer(text))
    if found:
        last = found[-1]
        registry = Segment(REGISTRY_DATA, _REGISTRY_HEADING, _clean_detail(text[last.end() :]))
        text = text[: last.start()]
    acts = openings(text)
    ends = [o.cut for o in acts[1:]] + [len(text)]
    result = [
        Segment(
            act_type=o.act_type,
            heading=" ".join(o.found.group(o.found.lastgroup or 0).split()),
            detail=_clean_detail(text[o.found.end() : end]),
        )
        for o, end in zip(acts, ends, strict=False)
    ]
    prefix = text[: acts[0].cut].strip() if acts else text.strip()
    if registry is not None:
        result.append(registry)
    return prefix, result
