"""Catalogue of BORME Section A act headings and the splitter that uses it.

An announcement body is a run of ``Heading. detail. Heading. detail...`` where the
headings come from a closed vocabulary used by the Registro Mercantil. The splitter only
cuts at headings from this catalogue, so a heading-like phrase inside free text (for
example inside a by-laws article) does not create a spurious act unless it exactly
matches a known heading followed by ``.`` or ``:``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

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
        # Distress / end of life / structural changes: review promptly.
        ActType("insolvency", "Insolvency proceedings (concurso)", "high"),
        ActType(
            "preventive_annotation",
            "Preventive annotation (e.g. debtor declared insolvent)",
            "high",
        ),
        ActType("uncollectible_debt", "Debt declared uncollectible", "high"),
        ActType("dissolution", "Dissolution", "high"),
        ActType("extinction", "Extinction (company struck off)", "high"),
        ActType("registry_sheet_closure", "Registry sheet closed", "high"),
        ActType("merger", "Merger", "high"),
        ActType("split", "Split / spin-off", "high"),
        ActType("global_asset_transfer", "Global transfer of assets and liabilities", "high"),
        ActType("capital_reduction", "Capital reduction", "high"),
        # Governance, ownership and identity changes.
        ActType("incorporation", "Incorporation", "medium"),
        ActType("appointment", "Appointment (directors, auditors, attorneys)", "medium"),
        ActType("officer_removal", "Removal / resignation of officers", "medium"),
        ActType("revocation", "Revocation (mainly powers of attorney)", "medium"),
        ActType("ex_officio_cancellation", "Appointments cancelled ex officio", "medium"),
        ActType("address_change", "Change of registered address", "medium"),
        ActType("name_change", "Change of company name", "medium"),
        ActType("business_purpose_change", "Change of corporate purpose", "medium"),
        ActType("capital_increase", "Capital increase", "medium"),
        ActType("transformation", "Transformation (change of legal form)", "medium"),
        ActType("sole_shareholder_declared", "Sole shareholder declared", "medium"),
        ActType("sole_shareholder_lost", "Sole-shareholder status lost", "medium"),
        ActType("sole_shareholder_change", "Change of sole shareholder", "medium"),
        ActType("reactivation", "Reactivation", "medium"),
        ActType("registry_sheet_reopening", "Registry sheet reopened", "medium"),
        ActType("branch_opening", "Branch opened", "medium"),
        ActType("branch_closure", "Branch closed", "medium"),
        ActType("bond_issue", "Bond issue", "medium"),
        ActType("accounts_not_approved", "Annual accounts not approved (art. 378.5 RRM)", "medium"),
        # Routine or informational entries.
        ActType("reelection", "Re-election", "low"),
        ActType("bylaws_amendment", "By-laws amendment", "low"),
        ActType("sole_shareholder", "Sole-shareholder company", "low"),
        ActType("capital_call_paid", "Unpaid capital paid up", "low"),
        ActType("unexecuted_capital_increase", "Capital increase agreed, not executed", "low"),
        ActType("duration_change", "Change of duration", "low"),
        ActType("powers_change", "Change of powers", "low"),
        ActType("statutory_adaptation", "Statutory adaptation to a new law", "low"),
        ActType("first_registration", "First registration", "low"),
        ActType("website", "Corporate website", "low"),
        ActType("sole_trader", "Sole trader entry", "low"),
        ActType("erratum", "Erratum", "low"),
        ActType("other", "Other entries (free text)", "low"),
    )
}

# (act_type, headings). Plain headings are matched literally, but tolerant to missing
# acute accents and to repeated whitespace. Entries starting with "re:" are raw regexes.
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
    (
        "business_purpose_change",
        ("Cambio de objeto social", "Ampliación del objeto social", "Ampliacion de objeto social"),
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
    # "Cierre provisional hoja registral Art.485 TRLC": a dot followed by a digit
    # ("Art.485", "art. 485") belongs to the heading itself.
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
# "Datos registrales" always closes the announcement, but is not always preceded by a
# full stop ("... SOCIO UNICO X Datos registrales."), so it is located separately.
_REGISTRY_HEADING = "Datos registrales"
_REGISTRY_RE = re.compile(r"Datos\s+registrales[.:]")

_VOWELS = {"a": "aá", "e": "eé", "i": "ií", "o": "oó", "u": "uúü"}


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _loose(heading: str) -> str:
    """Regex for a literal heading that tolerates missing accents and extra spaces."""
    parts: list[str] = []
    for ch in heading:
        base = _strip_accents(ch)
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


def _build_patterns() -> tuple[re.Pattern[str], dict[str, str]]:
    alternatives: list[tuple[str, str]] = []  # (regex, act_type)
    for act_type, headings in _HEADINGS:
        for heading in headings:
            regex = heading[3:] if heading.startswith("re:") else _loose(heading)
            alternatives.append((regex, act_type))
    # Longest first so that e.g. "Cierre de sucursal" is not shadowed by a shorter one.
    alternatives.sort(key=lambda item: len(item[0]), reverse=True)
    groups = {f"h{i}": act_type for i, (_, act_type) in enumerate(alternatives)}
    alternation = "|".join(f"(?P<h{i}>{regex})" for i, (regex, _) in enumerate(alternatives))
    # A heading starts the text or follows a full stop (sometimes written ".-"), and is
    # followed by "." or ":".
    pattern = re.compile(rf"(?:^|(?<=\.)|(?<=\.-))\s*(?:{alternation})(?=[.:](?:\s|$))")
    return pattern, groups


_PATTERN, _GROUP_TO_TYPE = _build_patterns()


@dataclass(frozen=True, slots=True)
class Segment:
    act_type: str
    heading: str
    detail: str


def _clean_detail(text: str) -> str:
    text = text.lstrip(".:").strip()
    return re.sub(r"\s+", " ", text).rstrip(" .")


def split_acts(text: str) -> tuple[str, list[Segment]]:
    """Split an announcement body into (unparsed_prefix, segments).

    ``unparsed_prefix`` is whatever precedes the first recognised heading; it is empty
    when the body is fully covered by the catalogue. The last segment is the special
    ``registry_data`` one ("Datos registrales") when present.
    """
    registry: Segment | None = None
    found = list(_REGISTRY_RE.finditer(text))
    if found:
        last = found[-1]
        registry = Segment(REGISTRY_DATA, _REGISTRY_HEADING, _clean_detail(text[last.end() :]))
        text = text[: last.start()]

    segments = _split_body(text)
    prefix = text[: segments[0][0]].strip() if segments else text.strip()
    result = [segment for _, segment in segments]
    if registry is not None:
        result.append(registry)
    return prefix, result


def _split_body(text: str) -> list[tuple[int, Segment]]:
    """Segments of the body (registry data removed), with their start offsets."""
    matches = list(_PATTERN.finditer(text))
    ends = [m.start() for m in matches[1:]] + [len(text)] if matches else []
    segments: list[tuple[int, Segment]] = []
    for current, end in zip(matches, ends, strict=True):
        group = current.lastgroup
        assert group is not None  # every alternative is a named group
        segment = Segment(
            act_type=_GROUP_TO_TYPE[group],
            heading=re.sub(r"\s+", " ", current.group(group)),
            detail=_clean_detail(text[current.end() : end]),
        )
        segments.append((current.start(), segment))
    return segments
