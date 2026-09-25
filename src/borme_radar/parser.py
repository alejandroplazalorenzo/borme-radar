"""Parse one BORME province document (XML, Section A or B) into announcements and acts.

Structure of the XML published by the BOE (inspected on real documents)::

    Section A
    <documento>
      <metadatos> identificador, titulo (province), diario_numero, fecha_publicacion ...
      <texto>
        <p class="articulo">423011 - NOVERA LIVING SOCIEDAD LIMITADA.</p>
        <p class="parrafo">Constitución. ... Nombramientos. ... Datos registrales. ...</p>

    Section B
      <texto>
        <p class="centro_redonda">Depósitos de proyectos de fusión por absorción</p>
        <p class="articulo">537 - ABSORBING COMPANY SL. (30/06/2026)</p>
        <p class="parrafo">Absorbidas: ONE SL; TWO SOCIEDAD LIMITADA.</p>

Two rules: no text is lost (what precedes the first known heading is kept in
``unparsed_prefix`` and counted), and the published text is kept in each act's detail.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from borme_radar.acts import REGISTRY_DATA, priority, section_b_type, split_acts
from borme_radar.models import Act, Announcement, Document
from borme_radar.officers import act_scope
from borme_radar.relations import ends_as_company, split_company_list

_HEADER_RE = re.compile(r"^\s*(?P<number>\d+)\s*-\s*(?P<name>.+?)\s*$", re.DOTALL)
# Section B headers end with the deposit date: "531 - NOATUM TERMINALS SL. (17/08/2026)".
_B_DATE_RE = re.compile(r"\(\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*\)\s*$")
_B_REGISTRY_RE = re.compile(r"\(\s*(S\s*\d+\s*,\s*H\s[^)]*)\)\s*$")
# Inscription date at the end of "Datos registrales", e.g. "I/A 1 (15.09.26)" or "( 9.09.26)".
_INSCRIPTION_RE = re.compile(r"\(\s*(\d{1,2})\.(\d{1,2})\.(\d{2})\s*\)")
# One registry publishes a garbled variant: "S 9 , H MU 306,SLNE , I/ 1 .H.)11 09 26".
_INSCRIPTION_FALLBACK_RE = re.compile(r"\)\s*(\d{1,2}) (\d{1,2}) (\d{2})\s*$")
# Registry sheet ("hoja registral"): "S 8 , H M 786863, I/A 11" -> "M 786863". A few
# registries print it without letters ("H 156220").
_SHEET_RE = re.compile(r"\bH\s+([A-Z]{1,3}\s*-?\s*)?(\d+)")
# Longest company name kept as a name. Section B prints every company of a merger on
# the header line; a longer "name" is a list and is split. The value is derived from
# the longest Section A name in 12 months of gazettes: 191 characters (p99.9: 88,
# n = 597,166; docs/calibration.md).
NAME_MAX = 191


class ParseError(ValueError):
    """The payload is not a well-formed BORME document."""


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _required(meta: ET.Element, tag: str) -> str:
    value = _text(meta.find(tag))
    if not value:
        raise ParseError(f"missing <{tag}> in <metadatos>")
    return value


def parse_inscription_date(registry_data: str | None) -> date | None:
    """Return the last bracketed dd.mm.yy date of a "Datos registrales" block."""
    if not registry_data:
        return None
    found = _INSCRIPTION_RE.findall(registry_data)
    if not found:
        found = _INSCRIPTION_FALLBACK_RE.findall(registry_data)
    if not found:
        return None
    day, month, year = (int(part) for part in found[-1])
    try:
        return date(2000 + year, month, day)
    except ValueError:
        return None


def parse_sheet(registry_data: str | None) -> str | None:
    """The registry sheet of a "Datos registrales" block, e.g. "M 786863"."""
    if not registry_data:
        return None
    match = _SHEET_RE.search(registry_data)
    if match is None:
        return None
    letters = re.sub(r"[^A-Z]", "", match.group(1) or "")
    return f"{letters} {int(match.group(2))}" if letters else str(int(match.group(2)))


def _act(seq: int, act_type: str, heading: str, detail: str) -> Act:
    scope = act_scope(act_type, detail)
    return Act(seq, act_type, heading, detail, scope, priority(act_type, scope))


def _build_announcement(number: int, name: str, body: str) -> Announcement:
    prefix, segments = split_acts(body)
    acts: list[Act] = []
    registry_data: str | None = None
    for segment in segments:
        if segment.act_type == REGISTRY_DATA:
            registry_data = segment.detail
            continue
        acts.append(_act(len(acts) + 1, segment.act_type, segment.heading, segment.detail))
    return Announcement(
        number=number,
        company_name=name,
        acts=tuple(acts),
        raw_text=body,
        registry_data=registry_data,
        inscription_date=parse_inscription_date(registry_data),
        unparsed_prefix=prefix,
        sheet=parse_sheet(registry_data),
    )


def _split_names(text: str) -> tuple[str, tuple[str, ...]]:
    """Subject and the other companies of a Section B header line.

    The header line of a deposit can list every company of the operation. It is cut
    into company names when every piece ends with a legal form, or when it is longer
    than :data:`NAME_MAX` (then it cannot be one name); the first name is the subject.
    """
    name = text.strip().rstrip(".").strip()
    names = split_company_list(name)
    if len(names) > 1 and (len(name) > NAME_MAX or all(ends_as_company(n) for n in names)):
        return names[0][:NAME_MAX], tuple(n[:NAME_MAX] for n in names[1:])
    return name[:NAME_MAX], ()


def _build_section_b(number: int, header: str, heading: str, paragraphs: list[str]) -> Announcement:
    deposit: date | None = None
    match = _B_DATE_RE.search(header)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            deposit = date(year, month, day)
        except ValueError:
            deposit = None
        header = header[: match.start()]
    # Closures and reopenings carry the registry data glued to the name:
    # "X SL EN LIQUIDACION(S 8, H GR 12687)".
    registry_data: str | None = None
    glued = _B_REGISTRY_RE.search(header.rstrip(" ."))
    if glued:
        registry_data = glued.group(1).strip()
        header = header[: glued.start()]
    name, others = _split_names(header)
    detail = " ".join(paragraphs).strip().rstrip(".")
    if others:
        # The other companies of the operation were only on the header line; without
        # them in the detail, a merger that swallows a watched company is invisible.
        detail = f"Listed with: {'; '.join(others)}. {detail}".strip().rstrip(".")
    act = _act(1, section_b_type(heading), heading, detail)
    return Announcement(
        number=number,
        company_name=name,
        acts=(act,),
        raw_text=" ".join(paragraphs),
        registry_data=registry_data,
        inscription_date=deposit,
        sheet=parse_sheet(registry_data),
        other_companies=others,
    )


def parse_document(payload: bytes) -> Document:
    """Parse the XML of one province document. Raises ParseError on malformed input."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ParseError(f"invalid XML: {exc}") from exc
    meta = root.find("metadatos")
    if meta is None:
        raise ParseError("missing <metadatos>")

    document_id = _required(meta, "identificador")
    pub_date = datetime.strptime(_required(meta, "fecha_publicacion"), "%Y%m%d").date()
    gazette_number = int(_required(meta, "diario_numero"))
    province = _required(meta, "titulo")
    parts = document_id.split("-")
    section = parts[1] if len(parts) > 1 else "A"

    announcements: list[Announcement] = []
    header: tuple[int, str] | None = None
    body_parts: list[str] = []
    heading = ""

    def flush() -> None:
        if header is None:
            return
        if section == "B":
            announcements.append(_build_section_b(*header, heading, body_parts))
        else:
            name = header[1].removesuffix(".").strip()
            announcements.append(_build_announcement(header[0], name, " ".join(body_parts)))

    texto = root.find("texto")
    for paragraph in texto.iter("p") if texto is not None else ():
        text = _text(paragraph)
        css = paragraph.get("class")
        if css == "centro_redonda" and section == "B":
            flush()
            header, body_parts = None, []
            heading = text.rstrip(".")
        elif css == "articulo":
            flush()
            match = _HEADER_RE.match(text)
            if match is None:
                if section == "B":  # lenient: Section B is a table, not the core feed
                    header, body_parts = None, []
                    continue
                raise ParseError(f"{document_id}: unexpected announcement header {text!r}")
            header = (int(match.group("number")), match.group("name"))
            body_parts = []
        elif header is not None and text:
            body_parts.append(text)
    flush()

    return Document(document_id, pub_date, gazette_number, province, tuple(announcements), section)
