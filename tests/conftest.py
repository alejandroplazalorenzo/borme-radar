from __future__ import annotations

import socket
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from borme_radar import store
from borme_radar.acts import priority
from borme_radar.models import Act, Announcement, Document, DocumentRef
from borme_radar.officers import act_scope
from borme_radar.watchlist import WatchedCompany

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must run offline: any real connection attempt fails the test."""

    def refuse(*_: object, **__: object) -> None:
        raise RuntimeError("network access attempted during tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def conn() -> sqlite3.Connection:
    return store.connect(":memory:")


# ------------------------------------------------------------ synthetic documents
# Names of natural persons in synthetic data are always "PERSONA N".


def act(seq: int, act_type: str, detail: str = "", heading: str | None = None) -> Act:
    scope = act_scope(act_type, detail)
    return Act(seq, act_type, heading or act_type, detail, scope, priority(act_type, scope))


def announcement(
    number: int,
    name: str,
    *acts: tuple[str, str],
    sheet: str | None = None,
    inscribed: date | None = None,
    others: tuple[str, ...] = (),
) -> Announcement:
    return Announcement(
        number=number,
        company_name=name,
        acts=tuple(act(i + 1, t, d) for i, (t, d) in enumerate(acts)),
        raw_text="",
        registry_data=f"S 8 , H {sheet}, I/A 1" if sheet else None,
        inscription_date=inscribed,
        sheet=sheet,
        other_companies=others,
    )


def document(
    doc_id: str, day: date, *anns: Announcement, province: str = "MADRID", section: str = "A"
) -> tuple[DocumentRef, Document]:
    ref = DocumentRef(
        doc_id,
        province,
        f"https://www.boe.es/diario_borme/xml.php?id={doc_id}",
        section,
        f"https://www.boe.es/borme/dias/{day:%Y/%m/%d}/pdfs/{doc_id}.pdf",
    )
    return ref, Document(doc_id, day, 1, province, tuple(anns), section)


def watched(group: str, name: str, **fields: object) -> WatchedCompany:
    from borme_radar.normalize import normalize_name

    return WatchedCompany(group=group, name=name, norm=normalize_name(name), **fields)  # type: ignore[arg-type]
