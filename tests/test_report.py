"""Report: seven sections, clusters of powers aggregated, no names in the shared version."""

from __future__ import annotations

import sqlite3
from datetime import date

from borme_radar import record, store
from borme_radar.discovery import GroupIndex
from borme_radar.matching import Matcher
from borme_radar.pipeline import Pipeline
from borme_radar.report import render
from conftest import announcement, document, watched

DAY = date(2026, 9, 21)


def loaded(conn: sqlite3.Connection) -> None:
    store.import_watchlist(
        conn, [watched("Grupo", "MATRIZ SA"), watched("Grupo", "FILIAL UNO SA")], DAY
    )
    p = Pipeline(conn, Matcher(store.load_watch_index(conn)), GroupIndex({}, {}, [], {}))
    powers = "Apoderado: PERSONA UNO;PERSONA DOS"
    ref, doc = document(
        "BORME-A-2026-181-28",
        DAY,
        announcement(1, "MATRIZ SA", ("revocation", powers), ("insolvency", "Auto"), inscribed=DAY),
        announcement(2, "FILIAL UNO SA", ("revocation", powers), inscribed=DAY),
        announcement(
            3, "FILIAL UNO SA", ("appointment", "Adm. Unico: PERSONA TRES"), inscribed=DAY
        ),
    )
    p.process_document(ref, doc)


def text(conn: sqlite3.Connection, details: bool) -> str:
    return render(
        conn,
        DAY,
        DAY,
        record.differences(conn),
        [],
        {"documents": 1},
        details=details,
    )


def test_sections_links_and_clusters(conn: sqlite3.Connection) -> None:
    loaded(conn)
    report = text(conn, details=True)
    for n in range(1, 8):
        assert f"## {n}." in report
    assert "[PDF](https://www.boe.es/borme/dias/2026/09/21/pdfs/BORME-A-2026-181-28.pdf)" in report
    alerts = report.split("## 3.")[1].split("## 4.")[0]
    assert "Insolvency proceedings" in alerts
    assert "Revocation" not in alerts  # attorney acts never reach the alerts
    clusters = report.split("## 6.")[1].split("## 7.")[0]
    assert "PERSONA UNO" in clusters and "| 2 |" in clusters  # two group companies


def test_shared_version_has_no_names_and_aggregates_people(conn: sqlite3.Connection) -> None:
    loaded(conn)
    report = text(conn, details=False)
    assert "PERSONA" not in report
    clusters = report.split("## 6.")[1].split("## 7.")[0]
    assert "| 2026-09-21 | Grupo | revocation | 2 | 4 |" in clusters
    movements = report.split("## 7.")[1]
    assert "| Grupo | FILIAL UNO SA | sole_director | appointment | 1 |" in movements
