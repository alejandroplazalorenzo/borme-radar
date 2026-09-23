from __future__ import annotations

from datetime import date

from borme_radar.matching import WatchEntry, match_names
from borme_radar.normalize import normalize_name
from borme_radar.report import render_alerts
from borme_radar.store import StoredAct, StoredAnnouncement


def entry(name: str) -> WatchEntry:
    return WatchEntry(name, normalize_name(name))


def candidates(*names: str) -> dict[str, str]:
    return {normalize_name(n): n for n in names}


def announcement(name: str, *acts: tuple[str, str]) -> StoredAnnouncement:
    return StoredAnnouncement(
        document_id="BORME-A-2026-183-28",
        number=1,
        pub_date="2026-09-22",
        province="MADRID",
        company_name=name,
        company_norm=normalize_name(name),
        inscription_date="2026-09-15",
        acts=tuple(StoredAct(t, t, d) for t, d in acts),
    )


def test_report_separates_confirmed_from_review_and_can_hide_details() -> None:
    watch = [entry("ACME SA"), entry("OTRA SA")]
    matches = match_names(watch, candidates("ACME SOCIEDAD ANONIMA", "ACME SL"))
    anns = {
        "ACME SA": [
            announcement(
                "ACME SOCIEDAD ANONIMA",
                ("appointment", "Consejero: PERSONA 1"),
                ("insolvency", "Auto de declaración de concurso"),
            )
        ],
        "ACME SL": [announcement("ACME SL", ("address_change", "C/ MAYOR 1 (MADRID)"))],
    }
    report = render_alerts(watch, matches, anns, since=date(2026, 9, 1), threshold=90)

    confirmed, review = report.split("## Needs review")
    assert "### ACME SOCIEDAD ANONIMA" in confirmed
    assert "announcements: 1 | highest priority: high" in confirmed
    assert "Consejero: PERSONA 1" in confirmed
    assert "| ACME SA | ACME SL | 100.0 | same name, legal form SL vs SA | 1 |" in review
    assert "- OTRA SA" in review  # listed under entries without hits

    hidden = render_alerts(watch, matches, anns, since=None, threshold=90, details=False)
    assert "PERSONA 1" not in hidden
    assert "Insolvency proceedings (concurso)" in hidden


def test_report_without_matches() -> None:
    report = render_alerts([entry("ACME SA")], [], {}, since=None, threshold=90)
    assert "No exact matches in this window." in report
    assert "No similar names above the threshold." in report
