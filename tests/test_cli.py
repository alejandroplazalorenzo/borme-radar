"""End-to-end through the CLI, served entirely from a pre-filled cache (no network)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from borme_radar.cache import DiskCache
from borme_radar.cli import main
from borme_radar.source import SUMMARY_URL

DAY = date(2026, 9, 22)
XML_URL = "https://www.boe.es/diario_borme/xml.php?id={}"


@pytest.fixture
def data_dir(tmp_path: Path, fixtures: Path) -> Path:
    cache = DiskCache(tmp_path / "cache")
    cache.put(SUMMARY_URL.format(day=DAY), (fixtures / "sumario_20260922.json").read_bytes())
    for doc_id in ("BORME-A-2026-183-07", "BORME-A-2026-183-28"):
        cache.put(XML_URL.format(doc_id), (fixtures / f"{doc_id}.xml").read_bytes())
    return tmp_path


def fetch(data_dir: Path) -> int:
    return main(
        ["--data-dir", str(data_dir), "fetch", "--from", "2026-09-22", "--to", "2026-09-22"]
    )


def row_counts(data_dir: Path) -> tuple[int, ...]:
    with sqlite3.connect(data_dir / "borme.sqlite") as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("days", "documents", "announcements", "acts")
        )


def test_fetch_is_served_from_cache_and_idempotent(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fetch(data_dir) == 0
    out = capsys.readouterr().out
    assert "documents: 2  announcements: 19" in out
    assert "network requests 0, cache hits 3" in out
    first = row_counts(data_dir)
    assert first[:3] == (1, 2, 19)

    assert fetch(data_dir) == 0
    assert row_counts(data_dir) == first


def test_stats_and_alerts(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fetch(data_dir)
    capsys.readouterr()

    assert main(["--data-dir", str(data_dir), "stats"]) == 0
    stats = capsys.readouterr().out
    assert "announcements: 19" in stats
    assert "announcements without any recognised act: 0" in stats
    assert "| Insolvency proceedings (concurso) | `insolvency` | 3 |" in stats

    watchlist = data_dir / "watch.csv"
    watchlist.write_text(
        'name\n"Obwan Networks and Services, S.L."\n"INMOALBASTRU, S.A."\n"Telefónica, S.A."\n',
        encoding="utf-8",
    )
    report_path = data_dir / "report.md"
    args = ["--data-dir", str(data_dir), "alerts", "--watchlist", str(watchlist)]
    assert main([*args, "--since", "2026-09-22", "--out", str(report_path)]) == 0
    assert "1 confirmed, 1 needs review" in capsys.readouterr().out

    report = report_path.read_text(encoding="utf-8")
    confirmed, review = report.split("## Needs review")
    assert "### OBWAN NETWORKS AND SERVICES SL" in confirmed
    assert "Registry sheet closed" in confirmed
    assert "INMOALBASTRU" not in confirmed  # a different legal form is never confirmed
    assert "| INMOALBASTRU, S.A. | INMOALBASTRU SL | 100.0 |" in review
    assert "- Telefónica, S.A." in review


def test_alerts_since_a_later_date_finds_nothing(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch(data_dir)
    watchlist = data_dir / "watch.csv"
    watchlist.write_text("name\nOBWAN NETWORKS AND SERVICES SL\n", encoding="utf-8")
    capsys.readouterr()
    args = ["--data-dir", str(data_dir), "alerts", "--watchlist", str(watchlist)]
    assert main([*args, "--since", "2026-09-23"]) == 0
    assert "No exact matches in this window." in capsys.readouterr().out
