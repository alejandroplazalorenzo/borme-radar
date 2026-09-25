"""Access to the BOE open-data API for the BORME.

* Daily summary: ``GET /datosabiertos/api/borme/sumario/YYYYMMDD`` (``Accept:
  application/json``). Days without gazette (weekends, holidays) answer 404.
* Each item of Sections A ("Actos inscritos") and B ("Otros actos publicados") links to
  one province document in XML (``url_xml``) and to the official PDF (``url_pdf``).
  Section C (legal notices) is free prose and is not read.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

from borme_radar.httpclient import NotFound, PoliteClient
from borme_radar.models import DocumentRef

SUMMARY_URL = "https://www.boe.es/datosabiertos/api/borme/sumario/{day:%Y%m%d}"
ALLOWED_PREFIX = "https://www.boe.es/"
SECTIONS = ("A", "B")
# The last Section A item of each issue is an alphabetical index of the companies
# already listed in the province documents: parsing it would duplicate them.
_INDEX_TITLE_PREFIX = "ÍNDICE"


def _as_list(value: Any) -> list[Any]:
    """The API returns a bare object instead of a one-element list; accept both."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _url(value: Any) -> str:
    """URLs come either as a string or as ``{"texto": url, "szBytes": ...}``."""
    if isinstance(value, dict):
        value = value.get("texto", "")
    return str(value or "")


def validate_summary(body: bytes) -> dict[str, Any]:
    """Parse the summary JSON; raise ValueError if truncated or not a summary."""
    data = json.loads(body)  # JSONDecodeError is a ValueError
    if not isinstance(data, dict) or "sumario" not in data.get("data", {}):
        raise ValueError("response is not a BORME summary")
    return data


def validate_xml(body: bytes) -> None:
    try:
        ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"XML does not parse: {exc}") from exc


def section_documents(
    summary: dict[str, Any], sections: tuple[str, ...] = SECTIONS
) -> list[DocumentRef]:
    """Province documents of the given sections in a daily summary (index excluded)."""
    refs: list[DocumentRef] = []
    for issue in _as_list(summary["data"]["sumario"].get("diario")):
        for section in _as_list(issue.get("seccion")):
            code = section.get("codigo")
            if code not in sections:
                continue
            for item in _as_list(section.get("item")):
                title = str(item.get("titulo", "")).strip()
                url = _url(item.get("url_xml"))
                if title.upper().startswith(_INDEX_TITLE_PREFIX):
                    continue
                if not url.startswith(ALLOWED_PREFIX):
                    raise ValueError(f"unexpected document URL in summary: {url!r}")
                pdf = _url(item.get("url_pdf"))
                refs.append(
                    DocumentRef(
                        str(item["identificador"]),
                        title,
                        url,
                        section=str(code),
                        url_pdf=pdf if pdf.startswith(ALLOWED_PREFIX) else None,
                    )
                )
    return refs


def section_a_documents(summary: dict[str, Any]) -> list[DocumentRef]:
    """Section A documents only (kept for callers that do not read Section B)."""
    return section_documents(summary, ("A",))


class BormeSource:
    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    def documents_for(self, day: date) -> list[DocumentRef] | None:
        """Section A and B documents published on ``day``; None if there was no gazette."""
        url = SUMMARY_URL.format(day=day)
        try:
            body = self.client.get(
                url, headers={"Accept": "application/json"}, validate=validate_summary
            )
        except NotFound:
            return None
        return section_documents(validate_summary(body))

    def document_xml(self, ref: DocumentRef) -> bytes:
        return self.client.get(ref.url_xml, validate=validate_xml)
