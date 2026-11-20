"""History sweep: most recent year first, passes until nothing new; followed candidates."""

from __future__ import annotations

import sqlite3
from datetime import date

from borme_radar import store
from borme_radar.discovery import GroupIndex, mention_keys
from borme_radar.download import DayDownload
from borme_radar.matching import Matcher
from borme_radar.models import DocumentRef
from borme_radar.pipeline import Pipeline
from borme_radar.runner import sweep, year_ranges
from conftest import watched


def xml(doc_id: str, day: date, *anns: tuple[int, str, str, str]) -> bytes:
    """A minimal Section A document: (number, company, body, sheet) per announcement."""
    paragraphs = "".join(
        f'<p class="articulo">{n} - {name}.</p>'
        f'<p class="parrafo">{body} Datos registrales. S 8 , H {sheet}, I/A 2 '
        f"({day:%d.%m.%y}).</p>"
        for n, name, body, sheet in anns
    )
    return (
        f"<documento><metadatos><identificador>{doc_id}</identificador><titulo>MADRID"
        f"</titulo><diario_numero>1</diario_numero><fecha_publicacion>{day:%Y%m%d}"
        f"</fecha_publicacion></metadatos><texto>{paragraphs}</texto></documento>"
    ).encode()


class FakeDownloader:
    """Serves synthetic days from memory (what the cache does in a real rebuild)."""

    def __init__(self, days: dict[date, list[tuple[int, str, str, str]]]) -> None:
        self.days = days

    def day(self, day: date) -> DayDownload:
        if day not in self.days:
            return DayDownload(day, "no_gazette")
        doc_id = f"BORME-A-{day:%Y%m%d}-28"
        ref = DocumentRef(doc_id, "MADRID", f"https://www.boe.es/x?id={doc_id}")
        return DayDownload(day, "published", [ref], {doc_id: xml(doc_id, day, *self.days[day])})


def factory(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    store.import_watchlist(conn, [watched("Grupo", "ACME, S.A.")], date(2026, 1, 1))

    def make(known_only: bool) -> Pipeline:
        index = GroupIndex(
            groups=store.groups(conn),
            mention_names=mention_keys(store.watched_names(conn)),
            hqs=[],
            tokens={},
        )
        return Pipeline(conn, Matcher(store.load_watch_index(conn)), index, known_only=known_only)

    return make


OLD_NAME = (1, "VIEJA DENOMINACION SA", "Nombramientos. Apoderado: PERSONA UNO.", "M 1")
NEW_NAME = (2, "ACME SA", "Reelecciones. Auditor: AUDITORES EJEMPLO SL.", "M 1")


def test_year_ranges_most_recent_first() -> None:
    assert year_ranges(date(2024, 11, 1), date(2026, 2, 1), "recent-first") == [
        (date(2026, 1, 1), date(2026, 2, 1)),
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2024, 11, 1), date(2024, 12, 31)),
    ]
    assert year_ranges(date(2025, 3, 1), date(2025, 4, 1), "chronological") == [
        (date(2025, 3, 1), date(2025, 4, 1))
    ]


def test_recent_first_links_the_old_name_in_one_pass(conn: sqlite3.Connection) -> None:
    days = {date(2024, 3, 4): [OLD_NAME], date(2025, 3, 4): [NEW_NAME]}
    passes = sweep(conn, FakeDownloader(days), factory(conn), date(2024, 1, 1), date(2025, 12, 31))  # type: ignore[arg-type]
    assert [(p.mode, p.gained("watched"), p.gained("sheets")) for p in passes] == [
        ("full", 2, 1),
        ("known-only", 0, 0),
    ]


def test_in_date_order_the_old_name_needs_a_second_pass(conn: sqlite3.Connection) -> None:
    days = {date(2024, 3, 4): [OLD_NAME], date(2025, 3, 4): [NEW_NAME]}
    passes = sweep(
        conn,
        FakeDownloader(days),  # type: ignore[arg-type]
        factory(conn),
        date(2024, 1, 1),
        date(2025, 12, 31),
        order="chronological",
    )
    assert [(p.mode, p.gained("watched")) for p in passes] == [
        ("full", 1),
        ("known-only", 1),
        ("known-only", 0),
    ]


def test_inside_one_year_the_second_pass_picks_up_what_came_first(
    conn: sqlite3.Connection,
) -> None:
    days = {date(2025, 1, 8): [OLD_NAME], date(2025, 6, 9): [NEW_NAME]}
    passes = sweep(conn, FakeDownloader(days), factory(conn), date(2025, 1, 1), date(2025, 12, 31))  # type: ignore[arg-type]
    assert [p.gained("watched") for p in passes] == [1, 1, 0]


def test_a_candidate_found_by_mention_is_followed_by_its_sheet(conn: sqlite3.Connection) -> None:
    days = {
        date(2023, 5, 3): [
            (5, "FILIAL EJEMPLO SL", "Constitución. Capital: 3.000,00 Euros.", "M 9")
        ],
        date(2025, 5, 6): [
            (6, "FILIAL EJEMPLO SL", "Cambio de identidad del socio único. ACME SA.", "M 9")
        ],
    }
    passes = sweep(conn, FakeDownloader(days), factory(conn), date(2023, 1, 1), date(2025, 12, 31))  # type: ignore[arg-type]
    assert passes[0].counters["followed"] == 1
    rows = conn.execute("SELECT num FROM notices WHERE lead_id IS NOT NULL ORDER BY num").fetchall()
    assert [r[0] for r in rows] == [5, 6]
    assert conn.execute("SELECT found_by, state FROM leads").fetchone()[:] == (
        "mention",
        "open",
    )


def test_a_later_pass_does_not_rewrite_document_counters(conn: sqlite3.Connection) -> None:
    days = {date(2025, 1, 8): [OLD_NAME], date(2025, 6, 9): [NEW_NAME]}
    sweep(conn, FakeDownloader(days), factory(conn), date(2025, 1, 1), date(2025, 12, 31))  # type: ignore[arg-type]
    stored = conn.execute(
        "SELECT SUM(n), COUNT(*) FROM doc_tally WHERE what = 'announcements'"
    ).fetchone()
    assert tuple(stored) == (2, 2)
    # ...but the number of stored announcements follows what the later pass stored.
    counted = conn.execute("SELECT SUM(n) FROM doc_tally WHERE what = 'kept'").fetchone()[0]
    assert counted == conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 2
