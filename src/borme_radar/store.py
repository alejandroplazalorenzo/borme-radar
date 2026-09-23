"""SQLite storage: versioned schema, idempotent loads and the queries the CLI needs."""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path

from borme_radar.models import Document
from borme_radar.normalize import normalize_name

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _migrations() -> Iterator[tuple[int, str]]:
    folder = resources.files("borme_radar") / "sql"
    found: list[tuple[int, str]] = []
    for entry in folder.iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    yield from sorted(found)


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending ``sql/NNN_*.sql`` files in order; returns the resulting version."""
    for number, script in _migrations():
        if number > schema_version(conn):
            # One transaction per migration: the schema and its version move together.
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    return schema_version(conn)


def connect(path: Path | str) -> sqlite3.Connection:
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def record_day(conn: sqlite3.Connection, day: date, published: bool, n_documents: int) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO days (pub_date, status, n_documents, checked_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (pub_date) DO UPDATE SET
                status = excluded.status,
                n_documents = excluded.n_documents,
                checked_at = excluded.checked_at
            """,
            (day.isoformat(), "published" if published else "no_gazette", n_documents, _now()),
        )


def load_document(conn: sqlite3.Connection, doc: Document, url_xml: str) -> None:
    """Replace everything stored for ``doc`` in one transaction (idempotent)."""
    with conn:
        conn.execute("DELETE FROM acts WHERE document_id = ?", (doc.document_id,))
        conn.execute("DELETE FROM announcements WHERE document_id = ?", (doc.document_id,))
        conn.execute(
            """
            INSERT INTO documents
                (document_id, pub_date, gazette_number, province, url_xml, loaded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (document_id) DO UPDATE SET
                pub_date = excluded.pub_date,
                gazette_number = excluded.gazette_number,
                province = excluded.province,
                url_xml = excluded.url_xml,
                loaded_at = excluded.loaded_at
            """,
            (
                doc.document_id,
                doc.pub_date.isoformat(),
                doc.gazette_number,
                doc.province,
                url_xml,
                _now(),
            ),
        )
        conn.executemany(
            """
            INSERT INTO announcements (document_id, number, company_name, company_norm,
                registry_data, inscription_date, unparsed_prefix, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    doc.document_id,
                    a.number,
                    a.company_name,
                    normalize_name(a.company_name),
                    a.registry_data,
                    a.inscription_date.isoformat() if a.inscription_date else None,
                    a.unparsed_prefix,
                    a.raw_text,
                )
                for a in doc.announcements
            ],
        )
        conn.executemany(
            """
            INSERT INTO acts (document_id, number, seq, act_type, heading, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (doc.document_id, a.number, act.seq, act.act_type, act.heading, act.detail)
                for a in doc.announcements
                for act in a.acts
            ],
        )


# --------------------------------------------------------------------------- queries


@dataclass(frozen=True, slots=True)
class Stats:
    first_day: str | None
    last_day: str | None
    days_published: int
    days_without_gazette: int
    documents: int
    announcements: int
    acts: int
    announcements_with_unparsed_prefix: int
    announcements_without_acts: int
    acts_by_type: list[tuple[str, int]]
    lag_days: list[float]  # publication date minus inscription date, one per announcement

    @property
    def lag_median(self) -> float | None:
        return statistics.median(self.lag_days) if self.lag_days else None

    def lag_quantile(self, q: float) -> float | None:
        if not self.lag_days:
            return None
        ordered = sorted(self.lag_days)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0] or 0)


def stats(conn: sqlite3.Connection) -> Stats:
    first, last = conn.execute("SELECT MIN(pub_date), MAX(pub_date) FROM days").fetchone()
    by_type = [
        (row[0], int(row[1]))
        for row in conn.execute(
            "SELECT act_type, COUNT(*) FROM acts GROUP BY act_type ORDER BY 2 DESC, 1"
        )
    ]
    lags = [
        float(row[0])
        for row in conn.execute(
            """
            SELECT julianday(d.pub_date) - julianday(a.inscription_date)
            FROM announcements a JOIN documents d USING (document_id)
            WHERE a.inscription_date IS NOT NULL
            """
        )
    ]
    return Stats(
        first_day=first,
        last_day=last,
        days_published=_scalar(conn, "SELECT COUNT(*) FROM days WHERE status = 'published'"),
        days_without_gazette=_scalar(conn, "SELECT COUNT(*) FROM days WHERE status = 'no_gazette'"),
        documents=_scalar(conn, "SELECT COUNT(*) FROM documents"),
        announcements=_scalar(conn, "SELECT COUNT(*) FROM announcements"),
        acts=_scalar(conn, "SELECT COUNT(*) FROM acts"),
        announcements_with_unparsed_prefix=_scalar(
            conn, "SELECT COUNT(*) FROM announcements WHERE unparsed_prefix <> ''"
        ),
        announcements_without_acts=_scalar(
            conn,
            """
            SELECT COUNT(*) FROM announcements a
            WHERE NOT EXISTS (SELECT 1 FROM acts t
                              WHERE t.document_id = a.document_id AND t.number = a.number)
            """,
        ),
        acts_by_type=by_type,
        lag_days=lags,
    )


def company_names(conn: sqlite3.Connection, since: date | None = None) -> dict[str, str]:
    """Distinct normalised names (-> one published spelling) seen since ``since``."""
    rows = conn.execute(
        """
        SELECT a.company_norm, MIN(a.company_name)
        FROM announcements a JOIN documents d USING (document_id)
        WHERE d.pub_date >= ?
        GROUP BY a.company_norm
        """,
        ((since or date.min).isoformat(),),
    )
    return {row[0]: row[1] for row in rows}


@dataclass(frozen=True, slots=True)
class StoredAct:
    act_type: str
    heading: str
    detail: str


@dataclass(frozen=True, slots=True)
class StoredAnnouncement:
    document_id: str
    number: int
    pub_date: str
    province: str
    company_name: str
    company_norm: str
    inscription_date: str | None
    acts: tuple[StoredAct, ...]


def announcements_for(
    conn: sqlite3.Connection, norms: Iterable[str], since: date | None = None
) -> list[StoredAnnouncement]:
    """Announcements (with acts) of the given normalised names, oldest first."""
    wanted = sorted(set(norms))
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)  # values are bound, never interpolated
    rows = conn.execute(
        f"""
        SELECT a.document_id, a.number, d.pub_date, d.province, a.company_name,
               a.company_norm, a.inscription_date, t.act_type, t.heading, t.detail
        FROM announcements a
        JOIN documents d USING (document_id)
        LEFT JOIN acts t ON t.document_id = a.document_id AND t.number = a.number
        WHERE a.company_norm IN ({placeholders}) AND d.pub_date >= ?
        ORDER BY d.pub_date, a.document_id, a.number, t.seq
        """,
        [*wanted, (since or date.min).isoformat()],
    )
    result: list[StoredAnnouncement] = []
    current_key: tuple[str, int] | None = None
    acts: list[StoredAct] = []
    head: sqlite3.Row | None = None
    for row in rows:
        key = (row["document_id"], row["number"])
        if key != current_key:
            if head is not None:
                result.append(_to_announcement(head, acts))
            current_key, head, acts = key, row, []
        if row["act_type"] is not None:
            acts.append(StoredAct(row["act_type"], row["heading"], row["detail"]))
    if head is not None:
        result.append(_to_announcement(head, acts))
    return result


def _to_announcement(row: sqlite3.Row, acts: list[StoredAct]) -> StoredAnnouncement:
    return StoredAnnouncement(
        document_id=row["document_id"],
        number=int(row["number"]),
        pub_date=row["pub_date"],
        province=row["province"],
        company_name=row["company_name"],
        company_norm=row["company_norm"],
        inscription_date=row["inscription_date"],
        acts=tuple(acts),
    )
