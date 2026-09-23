"""Parse one BORME Section A province document (XML) into announcements and acts.

Structure of the XML published by the BOE (inspected on real documents)::

    <documento>
      <metadatos> identificador, titulo (province), diario_numero, fecha_publicacion ...
      <texto>
        <p class="articulo">423011 - NOVERA LIVING SOCIEDAD LIMITADA.</p>
        <p class="parrafo">Constitución. ... Nombramientos. ... Datos registrales. ...</p>
        ...
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from borme_radar.acts import REGISTRY_DATA, split_acts
from borme_radar.models import Act, Announcement, Document

_HEADER_RE = re.compile(r"^\s*(?P<number>\d+)\s*-\s*(?P<name>.+?)\s*$", re.DOTALL)
# Inscription date at the end of "Datos registrales", e.g. "I/A 1 (15.09.26)" or "( 9.09.26)".
_INSCRIPTION_RE = re.compile(r"\(\s*(\d{1,2})\.(\d{1,2})\.(\d{2})\s*\)")
# One registry publishes a garbled variant: "S 9 , H MU 306,SLNE , I/ 1 .H.)11 09 26".
_INSCRIPTION_FALLBACK_RE = re.compile(r"\)\s*(\d{1,2}) (\d{1,2}) (\d{2})\s*$")


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


def _build_announcement(number: int, name: str, body: str) -> Announcement:
    prefix, segments = split_acts(body)
    acts: list[Act] = []
    registry_data: str | None = None
    for segment in segments:
        if segment.act_type == REGISTRY_DATA:
            registry_data = segment.detail
            continue
        acts.append(Act(len(acts) + 1, segment.act_type, segment.heading, segment.detail))
    return Announcement(
        number=number,
        company_name=name,
        acts=tuple(acts),
        raw_text=body,
        registry_data=registry_data,
        inscription_date=parse_inscription_date(registry_data),
        unparsed_prefix=prefix,
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

    announcements: list[Announcement] = []
    header: tuple[int, str] | None = None
    body_parts: list[str] = []

    def flush() -> None:
        if header is not None:
            announcements.append(_build_announcement(*header, " ".join(body_parts)))

    texto = root.find("texto")
    for paragraph in texto.iter("p") if texto is not None else ():
        text = _text(paragraph)
        if paragraph.get("class") == "articulo":
            flush()
            match = _HEADER_RE.match(text)
            if match is None:
                raise ParseError(f"{document_id}: unexpected announcement header {text!r}")
            name = match.group("name").removesuffix(".").strip()
            header = (int(match.group("number")), name)
            body_parts = []
        elif header is not None and text:
            body_parts.append(text)
    flush()

    return Document(document_id, pub_date, gazette_number, province, tuple(announcements))
