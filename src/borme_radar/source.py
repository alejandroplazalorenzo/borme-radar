"""Access to the BOE open-data API for the BORME.

* Daily summary: ``GET /datosabiertos/api/borme/sumario/YYYYMMDD`` (``Accept:
  application/json``). Days without gazette (weekends, holidays) answer 404.
* Each Section A item links to one province document in XML (``url_xml``).
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
SECTION_A = "A"
# The last Section A item of each issue is an alphabetical index of the companies
# already listed in the province documents: parsing it would duplicate them.
_INDEX_TITLE_PREFIX = "ÍNDICE"


def _as_list(value: Any) -> list[Any]:
    """The API returns a bare object instead of a one-element list; accept both."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


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


def section_a_documents(summary: dict[str, Any]) -> list[DocumentRef]:
    """Province documents of Section A listed in a daily summary (index excluded)."""
    refs: list[DocumentRef] = []
    for issue in _as_list(summary["data"]["sumario"].get("diario")):
        for section in _as_list(issue.get("seccion")):
            if section.get("codigo") != SECTION_A:
                continue
            for item in _as_list(section.get("item")):
                title = str(item.get("titulo", "")).strip()
                url = str(item.get("url_xml", ""))
                if title.upper().startswith(_INDEX_TITLE_PREFIX):
                    continue
                if not url.startswith(ALLOWED_PREFIX):
                    raise ValueError(f"unexpected document URL in summary: {url!r}")
                refs.append(DocumentRef(str(item["identificador"]), title, url))
    return refs


class BormeSource:
    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    def documents_for(self, day: date) -> list[DocumentRef] | None:
        """Section A documents published on ``day``; None if there was no gazette."""
        url = SUMMARY_URL.format(day=day)
        try:
            body = self.client.get(
                url, headers={"Accept": "application/json"}, validate=validate_summary
            )
        except NotFound:
            return None
        return section_a_documents(validate_summary(body))

    def document_xml(self, ref: DocumentRef) -> bytes:
        return self.client.get(ref.url_xml, validate=validate_xml)
