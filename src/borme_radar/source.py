"""The BOE open-data API: daily summaries and the province documents they list.

Each publication day has a JSON summary listing one XML document per province and
section; a day without a gazette answers 404. Only Sections A and B are read, index
documents are skipped, and only URLs on www.boe.es are followed.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date
from itertools import starmap
from typing import Any

from borme_radar.httpclient import NotFound, PoliteClient
from borme_radar.models import DocumentRef

SUMMARY_URL = "https://www.boe.es/datosabiertos/api/borme/sumario/{day:%Y%m%d}"
ALLOWED_PREFIX = "https://www.boe.es/"
SECTIONS = ("A", "B")
# Each issue also lists index documents (alphabetical lists of companies): no acts there.
_INDEX_TITLE_PREFIX = "ÍNDICE"


_AS_JSON = {"Accept": "application/json"}


def _as_list(value: Any) -> list[Any]:
    """The summary gives one element as an object and several as a list: always a list."""
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _url(value: Any) -> str:
    """A URL of the summary, given as a plain string or as ``{"texto": url}``."""
    text = value.get("texto", "") if isinstance(value, dict) else value
    return str(text or "")


def validate_summary(body: bytes) -> dict[str, Any]:
    """The decoded summary; a ValueError if the body is not one, so the client retries."""
    data = json.loads(body)  # JSONDecodeError is a ValueError
    if isinstance(data, dict) and "sumario" in data.get("data", {}):
        return data
    raise ValueError("response is not a BORME summary")


def validate_xml(body: bytes) -> None:
    try:
        ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"XML does not parse: {exc}") from exc


def _items(summary: dict[str, Any], sections: tuple[str, ...]) -> Iterator[tuple[str, Any]]:
    """``(section code, item)`` for every document listed under the wanted sections."""
    for issue in _as_list(summary["data"]["sumario"].get("diario")):
        for section in _as_list(issue.get("seccion")):
            if section.get("codigo") in sections:
                yield from ((section["codigo"], item) for item in _as_list(section.get("item")))


def _ref(code: str, item: Any) -> DocumentRef | None:
    """Reference to a listed document, or None for an index.

    A document URL outside the BOE is an error; an official PDF URL outside it is dropped.
    """
    title = str(item.get("titulo", "")).strip()
    if title.upper().startswith(_INDEX_TITLE_PREFIX):
        return None
    xml = _url(item.get("url_xml"))
    if not xml.startswith(ALLOWED_PREFIX):
        raise ValueError(f"unexpected document URL in summary: {xml!r}")
    pdf = _url(item.get("url_pdf"))
    official = pdf if pdf.startswith(ALLOWED_PREFIX) else None
    return DocumentRef(str(item["identificador"]), title, xml, section=str(code), url_pdf=official)


def section_documents(
    summary: dict[str, Any], sections: tuple[str, ...] = SECTIONS
) -> list[DocumentRef]:
    """References of the documents of a daily summary in the given sections, in order."""
    return list(filter(None, starmap(_ref, _items(summary, sections))))


def section_a_documents(summary: dict[str, Any]) -> list[DocumentRef]:
    """References of the Section A documents of a daily summary."""
    return section_documents(summary, ("A",))


class BormeSource:
    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    def documents_for(self, day: date) -> list[DocumentRef] | None:
        """Documents published on a day, or None when there was no gazette (404)."""
        url = SUMMARY_URL.format(day=day)
        try:
            body = self.client.get(url, headers=_AS_JSON, validate=validate_summary)
        except NotFound:
            return None
        return section_documents(validate_summary(body))

    def document_xml(self, ref: DocumentRef) -> bytes:
        return self.client.get(ref.url_xml, validate=validate_xml)
