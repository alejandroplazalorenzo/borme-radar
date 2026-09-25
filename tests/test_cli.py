"""End-to-end through the CLI, served entirely from a pre-filled cache (no network)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from borme_radar.cache import DiskCache
from borme_radar.cli import main
from borme_radar.source import SUMMARY_URL

DAY = date(2026, 9, 22)
XML_URL = "https://www.boe.es/diario_borme/xml.php?id={}"
WATCHLIST = """group,name,address,city
Perlas,"MAJORCA PALMA PEARLS, S.L.",,
Obwan,"OBWAN NETWORKS AND SERVICES, S.L.",,
Zelos,"NE MAJO, S.A.",,
Uno,"UNO CORP. LATINOAMERICA, S.A.",,
Albastru,"INMOALBASTRU, S.A.",,
Musal,"MUSAL DISTRIBUTIONS GROUP, S.L.",CALLE DIEGO DE LEON 61,MADRID
"""


@pytest.fixture
def data_dir(tmp_path: Path, fixtures: Path) -> Path:
    cache = DiskCache(tmp_path / "cache")
    cache.put(SUMMARY_URL.format(day=DAY), (fixtures / "sumario_20260922.json").read_bytes())
    for doc_id in ("BORME-A-2026-183-07", "BORME-A-2026-183-28"):
        cache.put(XML_URL.format(doc_id), (fixtures / f"{doc_id}.xml").read_bytes())
    (tmp_path / "watch.csv").write_text(WATCHLIST, encoding="utf-8")
    return tmp_path


def cli(data_dir: Path, *args: str) -> int:
    return main(["--data-dir", str(data_dir), *args])


def fetch(data_dir: Path, *extra: str) -> int:
    return cli(
        data_dir,
        "fetch",
        "--from",
        "2026-09-22",
        "--to",
        "2026-09-22",
        "--watchlist",
        str(data_dir / "watch.csv"),
        *extra,
    )


def counts(data_dir: Path) -> tuple[int, ...]:
    with sqlite3.connect(data_dir / "borme.sqlite") as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("documents", "announcements", "acts", "observations", "candidates")
        )


def test_fetch_from_cache_keeps_only_what_matters(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fetch(data_dir) == 0
    out = capsys.readouterr().out
    assert "documents 2  announcements read 19" in out
    assert "network requests 0, cache hits 3" in out
    assert "watched 4, review 1, candidates 2" in out
    first = counts(data_dir)

    # Idempotent: the day is done; reprocessing it changes nothing either.
    assert fetch(data_dir) == 0
    assert "already done 1" in capsys.readouterr().out
    assert fetch(data_dir, "--reprocess") == 0
    assert counts(data_dir) == first


def test_stats_sync_and_report(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fetch(data_dir)
    capsys.readouterr()

    assert cli(data_dir, "stats") == 0
    stats = capsys.readouterr().out
    assert "Section A announcements with a registry sheet: 19 (100.00%)" in stats
    assert "announcements stored: 7" in stats

    assert cli(data_dir, "sync") == 0
    pending = capsys.readouterr().out
    assert "Nothing written" in pending
    assert "ZELOS ASSET MANAGEMENT HOLDING SA" in pending  # the rename
    assert cli(data_dir, "sync", "--apply") == 0
    capsys.readouterr()
    assert cli(data_dir, "sync") == 0
    assert "matches the Registro" in capsys.readouterr().out

    report = data_dir / "report.md"
    args = ["report", "--from", "2026-09-22", "--to", "2026-09-22", "--out", str(report)]
    assert cli(data_dir, *args, "--no-details") == 0
    text = report.read_text(encoding="utf-8")
    for n in range(1, 8):
        assert f"## {n}." in text
    assert "PERSONA" not in text  # shareable: no natural persons
    assert "https://www.boe.es/borme/dias/2026/09/22/pdfs/BORME-A-2026-183-28.pdf" in text
    assert "UNO CORP. COLOMBIA" in text  # candidate of the watched group "Uno"
    assert "INMOALBASTRU SL" in text  # review queue

    assert cli(data_dir, *args) == 0
    assert "PERSONA 3" in report.read_text(encoding="utf-8")  # local, detailed version


def test_rebuild_regenerates_the_same_derived_data_offline(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch(data_dir)
    before = counts(data_dir)
    assert cli(data_dir, "rebuild", "--from", "2026-09-22", "--to", "2026-09-22") == 0
    assert "network requests 0" in capsys.readouterr().out
    assert counts(data_dir) == before
    assert cli(data_dir, "rebuild", "--history-only") == 0
    assert counts(data_dir) == before


def test_calibrate_partial_never_writes_a_proposal(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch(data_dir)
    tokens = data_dir / "proposed.json"
    tokens.write_text(json.dumps({"PERLAS": "Perlas", "MAJORCA": "Perlas"}), encoding="utf-8")
    window = ["--from", "2026-09-22", "--to", "2026-09-22"]
    assert (
        cli(data_dir, "calibrate", *window, "--tokens", str(tokens), "--limit-documents", "1") == 0
    )
    assert "PARTIAL MEASUREMENT" in capsys.readouterr().out
    assert not (data_dir / "calibration" / "tokens.proposal.json").exists()

    assert cli(data_dir, "calibrate", *window, "--tokens", str(tokens), "--addresses") == 0
    proposal = json.loads((data_dir / "calibration" / "tokens.proposal.json").read_text("utf-8"))
    assert set(proposal) <= {"PERLAS", "MAJORCA"}
    assert (data_dir / "calibration" / "address_noise.json").exists()

    assert cli(data_dir, "evaluate", *window) == 0
    out = capsys.readouterr().out
    assert "| mention |" in out
    assert (data_dir / "calibration" / "evaluation.json").exists()


def test_workers_out_of_range_are_refused(data_dir: Path) -> None:
    with pytest.raises(SystemExit):
        fetch(data_dir, "--workers", "8")
