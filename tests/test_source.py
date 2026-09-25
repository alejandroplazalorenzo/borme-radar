from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from borme_radar.httpclient import PoliteClient
from borme_radar.source import (
    BormeSource,
    section_a_documents,
    section_documents,
    validate_summary,
)

SUMMARY = "sumario_20260922.json"


def test_section_a_documents_from_real_summary(fixtures: Path) -> None:
    summary = validate_summary((fixtures / SUMMARY).read_bytes())
    refs = section_a_documents(summary)
    assert [r.document_id for r in refs] == ["BORME-A-2026-183-07", "BORME-A-2026-183-28"]
    assert [r.province for r in refs] == ["ILLES BALEARS", "MADRID"]
    assert refs[1].url_xml == "https://www.boe.es/diario_borme/xml.php?id=BORME-A-2026-183-28"
    assert refs[1].url_pdf == (
        "https://www.boe.es/borme/dias/2026/09/22/pdfs/BORME-A-2026-183-28.pdf"
    )


def test_alphabetical_index_is_skipped(fixtures: Path) -> None:
    summary = validate_summary((fixtures / SUMMARY).read_bytes())
    titles = [
        item["titulo"]
        for section in summary["data"]["sumario"]["diario"][0]["seccion"]
        for item in section.get("item", [])
    ]
    assert any(t.startswith("ÍNDICE") for t in titles)  # present in the source...
    assert all(not r.province.startswith("ÍNDICE") for r in section_documents(summary))


def _summary(*sections: tuple[str, object]) -> dict[str, object]:
    return {
        "data": {
            "sumario": {"diario": {"seccion": [{"codigo": c, "item": i} for c, i in sections]}}
        }
    }


def _item(doc_id: str) -> dict[str, object]:
    return {
        "identificador": doc_id,
        "titulo": "MADRID",
        "url_xml": {"texto": f"https://www.boe.es/diario_borme/xml.php?id={doc_id}"},
        "url_pdf": {"texto": f"https://www.boe.es/borme/{doc_id}.pdf"},
    }


def test_sections_a_and_b_are_read_c_is_not() -> None:
    summary = _summary(
        ("A", _item("BORME-A-2026-1-28")),  # a bare object instead of a list
        ("B", [_item("BORME-B-2026-1-28")]),
        ("C", [_item("BORME-C-2026-1-28")]),
    )
    refs = section_documents(summary)
    assert [(r.document_id, r.section) for r in refs] == [
        ("BORME-A-2026-1-28", "A"),
        ("BORME-B-2026-1-28", "B"),
    ]
    assert refs[1].url_pdf == "https://www.boe.es/borme/BORME-B-2026-1-28.pdf"


def test_foreign_document_urls_are_rejected() -> None:
    item = {"identificador": "X", "titulo": "MADRID", "url_xml": "https://evil.example/x.xml"}
    with pytest.raises(ValueError, match="unexpected document URL"):
        section_documents(_summary(("A", [item])))


def test_truncated_summary_is_invalid(fixtures: Path) -> None:
    body = (fixtures / SUMMARY).read_bytes()
    with pytest.raises(ValueError):
        validate_summary(body[: len(body) // 2])
    with pytest.raises(ValueError):
        validate_summary(json.dumps({"status": {"code": "200"}}).encode())


def test_day_without_gazette_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "application/json"
        assert request.url.path.endswith("/20260920")
        return httpx.Response(404, content=b"<response/>")

    client = PoliteClient(transport=httpx.MockTransport(handler), min_interval=0)
    assert BormeSource(client).documents_for(date(2026, 9, 20)) is None
