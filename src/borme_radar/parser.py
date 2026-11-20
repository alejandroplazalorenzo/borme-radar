"""Parse one BORME XML document into announcements and typed acts.

The document carries its metadata (identifier, date, issue number, province) and a list
of paragraphs. A paragraph of class ``articulo`` opens an announcement, as in
"423901 - NAME SL.", and the paragraphs after it are its body:

* Section A: the body is cut into acts (:func:`acts.split_acts`), and the "Datos
  registrales" at its end give the registry sheet and the inscription date;
* Section B is a table: paragraphs of class ``centro_redonda`` are headings that give
  the act type of the rows below them, and the header of a row ends with the deposit
  date. A header names one company unless it splits into names that all end in a legal
  form, or it is longer than ``NAME_MAX``, the longest Section A name of a year.

Index documents have no announcements and parse to none. An announcement printed
without its number gets a negative one, unique inside its document.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from borme_radar.acts import REGISTRY_DATA, Segment, priority, section_b_type, split_acts
from borme_radar.models import Act, Announcement, Document
from borme_radar.officers import act_scope
from borme_radar.relations import ends_as_company, split_company_list

# Announcement header: "123 - NAME SL." The number is missing in a few old documents.
_HEADER_RE = re.compile(r"^\s*(?P<number>\d+)?\s*-\s*(?P<name>.*?)\s*$", re.DOTALL)
# Section B headers end with the deposit date: "531 - NOATUM TERMINALS SL. (17/08/2026)".
_B_DATE_RE = re.compile(r"\(\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*\)\s*$")
_B_REGISTRY_RE = re.compile(r"\(\s*(S\s*\d+\s*,\s*H\s[^)]*)\)\s*$")
# Inscription date at the end of "Datos registrales", e.g. "I/A 1 (15.09.26)" or "( 9.09.26)".
_INSCRIPTION_RE = re.compile(r"\(\s*(\d{1,2})\.(\d{1,2})\.(\d{2})\s*\)")
# One registry publishes a garbled variant: "S 9 , H MU 306,SLNE , I/ 1 .H.)11 09 26".
_INSCRIPTION_FALLBACK_RE = re.compile(r"\)\s*(\d{1,2}) (\d{1,2}) (\d{2})\s*$")
# The pieces "Datos registrales" is read in: runs of digits, runs of letters, and any
# other character that is not a space.
_PIECE_RE = re.compile(r"\d+|[^\W\d_]+|\S")
# A second number of up to 3 digits is noise and the first is the sheet; a longer one
# that differs leaves the sheet unknown (measured on 18 years, docs/history.md).
SHEET_SECOND_NUMBER_NOISE = 3
# Longest Section A company name in a year of gazettes: a Section B header longer than
# this cannot be one name (docs/calibration.md). Names are cut to it.
NAME_MAX = 191


class ParseError(ValueError):
    """The payload is not a BORME document this parser can read."""


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
    """Inscription date at the end of "Datos registrales", e.g. "( 9.09.26)", or None.

    The last date in the text counts, and its year is 20xx. The garbled variant one
    registry prints ("...)11 09 26") is read too.
    """
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


def _province(piece: str) -> bool:
    return 1 <= len(piece) <= 3 and all("A" <= ch <= "Z" for ch in piece)


def sheet_parts(registry_data: str | None) -> tuple[str, str, str | None] | None:
    """The registry sheet as printed: the province letters ("" when the registry prints
    none), the number and the second number after a comma, if any; or None.

    The sheet follows the first "H" that stands alone and is followed by a space: up to
    three capital letters (a dash may follow them), then the number.
    """
    text = registry_data or ""
    pieces = list(_PIECE_RE.finditer(text))
    for i, piece in enumerate(pieces):
        alone = piece.start() == 0 or not text[piece.start() - 1].isalnum()
        spaced = text[piece.end() : piece.end() + 1].isspace()
        if piece.group() != "H" or not (alone and spaced):
            continue
        after = [p.group() for p in pieces[i + 1 : i + 6]]
        letters = after.pop(0) if after and _province(after[0]) else ""
        if letters and after[:1] == ["-"]:
            after.pop(0)
        if not (after and after[0].isdigit()):
            continue
        second = after[2] if after[1:2] == [","] and after[2:3] and after[2].isdigit() else None
        return letters, after[0], second
    return None


def parse_sheet(registry_data: str | None) -> str | None:
    """Registry sheet from "Datos registrales", e.g. "M 786863", or None.

    When two different numbers follow the "H", a second number longer than
    ``SHEET_SECOND_NUMBER_NOISE`` digits leaves the sheet unknown, and the announcement is
    matched by name.
    """
    parts = sheet_parts(registry_data)
    if parts is None:
        return None
    letters, first, second = parts
    if second and int(second) != int(first) and len(second) > SHEET_SECOND_NUMBER_NOISE:
        return None
    return f"{letters} {int(first)}" if letters else str(int(first))


def _numbered(segments: list[Segment]) -> tuple[Act, ...]:
    """Acts numbered from 1, each with its scope and the priority that follows."""
    scopes = [act_scope(s.act_type, s.detail) for s in segments]
    return tuple(
        Act(n, s.act_type, s.heading, s.detail, scope, priority(s.act_type, scope))
        for n, (s, scope) in enumerate(zip(segments, scopes, strict=True), 1)
    )


def _build_announcement(number: int, name: str, body: str) -> Announcement:
    prefix, segments = split_acts(body)
    registry = [s.detail for s in segments if s.act_type == REGISTRY_DATA]
    registry_data = registry[-1] if registry else None
    return Announcement(
        number=number,
        company_name=name,
        acts=_numbered([s for s in segments if s.act_type != REGISTRY_DATA]),
        raw_text=body,
        registry_data=registry_data,
        inscription_date=parse_inscription_date(registry_data),
        unparsed_prefix=prefix,
        sheet=parse_sheet(registry_data),
    )


def _split_names(text: str) -> tuple[str, tuple[str, ...]]:
    """Company of a Section B header, and the other companies listed with it.

    The header is split only when it is too long to be one name or when every piece ends
    with a legal form; every name is cut to ``NAME_MAX`` characters.
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
    # Some rows carry the registry data glued to the name: "NAME SL (S 8 , H GR 12687)".
    registry_data: str | None = None
    glued = _B_REGISTRY_RE.search(header.rstrip(" ."))
    if glued:
        registry_data = glued.group(1).strip()
        header = header[: glued.start()]
    name, others = _split_names(header)
    detail = " ".join(paragraphs).strip().rstrip(".")
    if others:
        # The other companies stay in the act text, where relations.py reads them as co-listed.
        detail = f"Listed with: {'; '.join(others)}. {detail}".strip().rstrip(".")
    return Announcement(
        number=number,
        company_name=name,
        acts=_numbered([Segment(section_b_type(heading), heading, detail)]),
        raw_text=" ".join(paragraphs),
        registry_data=registry_data,
        inscription_date=deposit,
        sheet=parse_sheet(registry_data),
        other_companies=others,
    )


def parse_document(payload: bytes) -> Document:
    """Parse one province document.

    Raises ParseError for invalid XML, missing metadata or a Section A header that cannot
    be read; a Section B row without a readable header is skipped.
    """
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
    unnumbered = 0

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
            if match.group("number"):
                number = int(match.group("number"))
            else:
                # Printed without a number: a negative one keeps it unique inside the document.
                unnumbered -= 1
                number = unnumbered
            header = (number, match.group("name"))
            body_parts = []
        elif header is not None and text:
            body_parts.append(text)
    flush()

    return Document(document_id, pub_date, gazette_number, province, tuple(announcements), section)
