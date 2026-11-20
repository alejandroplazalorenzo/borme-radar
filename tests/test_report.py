"""Report: one part per group, powers spread over companies, no names in the shared version."""

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
    return render(conn, DAY, DAY, record.proposals(conn), [], {"documents": 1}, details=details)


def part(report: str, title: str) -> str:
    return report.split(f"### {title}")[1].split("\n### ")[0].split("\n## ")[0]


def test_a_part_per_group_with_links_and_powers(conn: sqlite3.Connection) -> None:
    loaded(conn)
    report = text(conn, details=True)
    assert "| Grupo | 4 | 1 | 1 | 1 | 0 |" in report  # the table of the window
    assert "\n## Grupo\n" in report
    assert (
        "([BOE PDF](https://www.boe.es/borme/dias/2026/09/21/pdfs/BORME-A-2026-181-28.pdf))"
        in report
    )
    first = part(report, "Read first")
    assert "MATRIZ SA · Insolvency proceedings" in first
    assert "Revocation" not in first  # acts of attorneys are never read first
    powers = part(report, "One person, several companies")
    assert "revocation of PERSONA UNO in 2: FILIAL UNO SA, MATRIZ SA" in powers
    assert "appointment of PERSONA TRES as sole_director" in part(report, "Officers")


def test_the_shared_version_names_nobody_and_counts_people(conn: sqlite3.Connection) -> None:
    loaded(conn)
    report = text(conn, details=False)
    assert "PERSONA" not in report
    assert "- 2026-09-21 · revocation: 2 people, 4 company-positions" in part(
        report, "One person, several companies"
    )
    assert "- FILIAL UNO SA: sole_director, appointment of 1" in part(report, "Officers")
