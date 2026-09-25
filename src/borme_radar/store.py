"""SQLite storage: versioned schema, idempotent upserts and the queries the CLI needs."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path

from borme_radar.matching import WatchIndex
from borme_radar.models import Document, DocumentRef
from borme_radar.normalize import normalize_name
from borme_radar.watchlist import WatchedCompany

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def now() -> str:
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
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


# ------------------------------------------------------------------ watchlist


@dataclass(frozen=True, slots=True)
class ImportResult:
    groups: int
    companies: int
    new_companies: int
    fields_filled: int


def import_watchlist(
    conn: sqlite3.Connection, entries: Iterable[WatchedCompany], today: date | None = None
) -> ImportResult:
    """Upsert groups and companies from the watchlist.

    A record field is written from the watchlist only while the Registro has said
    nothing about it: the watchlist is a secondary source. Its values are also kept as
    observations with ``source='watchlist'``, dated on the import day.
    """
    stamp = now()
    day = (today or date.today()).isoformat()
    new = filled = 0
    entries = list(entries)
    with conn:
        for entry in entries:
            conn.execute("INSERT OR IGNORE INTO groups (name) VALUES (?)", (entry.group,))
            group_id = _scalar(conn, "SELECT group_id FROM groups WHERE name = ?", (entry.group,))
            row = conn.execute(
                "SELECT company_id FROM companies WHERE name_norm = ?", (entry.norm,)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    """
                    INSERT INTO companies (group_id, name, name_norm, incorporated,
                        created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        entry.name,
                        entry.norm,
                        entry.incorporated.isoformat() if entry.incorporated else None,
                        stamp,
                        stamp,
                    ),
                )
                company_id = int(cur.lastrowid or 0)
                new += 1
            else:
                company_id = int(row[0])
                conn.execute(
                    "UPDATE companies SET group_id = ?, incorporated = COALESCE(?, incorporated)"
                    " WHERE company_id = ?",
                    (
                        group_id,
                        entry.incorporated.isoformat() if entry.incorporated else None,
                        company_id,
                    ),
                )
            values = {
                "address": entry.address,
                "city": entry.city,
                "capital": None if entry.capital is None else f"{entry.capital:.2f}",
                "status": entry.status,
                "purpose": entry.purpose,
            }
            for field, value in values.items():
                if value is None:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO observations (company_id, field, value, obs_date,
                        source, created_at)
                    VALUES (?, ?, ?, ?, 'watchlist', ?)
                    """,
                    (company_id, field, value, day, stamp),
                )
                said = _scalar(
                    conn,
                    "SELECT COUNT(*) FROM observations WHERE company_id = ? AND field = ?"
                    " AND source = 'registry'",
                    (company_id, field),
                )
                if not said:
                    cur = conn.execute(
                        f"UPDATE companies SET {field} = ?, updated_at = ?"
                        f" WHERE company_id = ? AND {field} IS NOT ?",
                        (
                            float(value) if field == "capital" else value,
                            stamp,
                            company_id,
                            float(value) if field == "capital" else value,
                        ),
                    )
                    filled += cur.rowcount
            if entry.sheet and entry.province:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO company_sheets (province, sheet, company_id, origin,
                        learned_at)
                    VALUES (?, ?, ?, 'watchlist', ?)
                    """,
                    (entry.province, entry.sheet, company_id, stamp),
                )
    return ImportResult(
        groups=_scalar(conn, "SELECT COUNT(*) FROM groups"),
        companies=_scalar(conn, "SELECT COUNT(*) FROM companies"),
        new_companies=new,
        fields_filled=filled,
    )


def load_watch_index(conn: sqlite3.Connection) -> WatchIndex:
    index = WatchIndex()
    for row in conn.execute("SELECT company_id, name_norm, incorporated FROM companies"):
        index.add_name(row["name_norm"], row["company_id"])
        index.incorporated[row["company_id"]] = (
            date.fromisoformat(row["incorporated"]) if row["incorporated"] else None
        )
    for row in conn.execute("SELECT alias_norm, company_id FROM company_aliases"):
        index.add_name(row["alias_norm"], row["company_id"])
    for row in conn.execute("SELECT province, sheet, company_id FROM company_sheets"):
        index.by_sheet[(row["province"], row["sheet"])] = row["company_id"]
    return index


def groups(conn: sqlite3.Connection) -> dict[int, str]:
    return {row[0]: row[1] for row in conn.execute("SELECT group_id, name FROM groups")}


def group_id_of(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT group_id FROM groups WHERE name = ?", (name,)).fetchone()
    return int(row[0]) if row else None


def watched_names(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """(name, group_id) of every watched company and alias."""
    rows = conn.execute(
        """
        SELECT c.name, c.group_id FROM companies c
        UNION ALL
        SELECT a.alias, c.group_id FROM company_aliases a JOIN companies c USING (company_id)
        """
    )
    return [(row[0], row[1]) for row in rows]


def watched_addresses(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(street, city, group_id) of the registered addresses of watched companies."""
    rows = conn.execute(
        """
        SELECT c.address, c.city, c.group_id FROM companies c
        WHERE c.address IS NOT NULL AND c.city IS NOT NULL
        UNION
        SELECT a.value, ct.value, c.group_id
        FROM current_values a
        JOIN current_values ct ON ct.company_id = a.company_id AND ct.field = 'city'
        JOIN companies c ON c.company_id = a.company_id
        WHERE a.field = 'address'
        """
    )
    return [(row[0], row[1], row[2]) for row in rows]


# ------------------------------------------------------------------ days / documents


def record_day(
    conn: sqlite3.Connection,
    day: date,
    status: str,
    n_documents: int = 0,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO days (pub_date, status, n_documents, error, checked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (pub_date) DO UPDATE SET
                status = excluded.status,
                n_documents = excluded.n_documents,
                error = excluded.error,
                checked_at = excluded.checked_at
            """,
            (day.isoformat(), status, n_documents, error, now()),
        )


def days_with_status(conn: sqlite3.Connection, *statuses: str) -> list[date]:
    marks = ",".join("?" for _ in statuses)
    return [
        date.fromisoformat(row[0])
        for row in conn.execute(
            f"SELECT pub_date FROM days WHERE status IN ({marks}) ORDER BY pub_date", statuses
        )
    ]


def upsert_document(
    conn: sqlite3.Connection,
    ref: DocumentRef,
    doc: Document,
    counts: dict[str, int],
    act_counts: dict[tuple[str, str], int],
    lag_counts: dict[int, int],
) -> None:
    conn.execute(
        """
        INSERT INTO documents (document_id, pub_date, section, gazette_number, province,
            url_xml, url_pdf, n_announcements, n_acts, n_with_sheet, n_unparsed_prefix,
            n_without_acts, n_stored, loaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (document_id) DO UPDATE SET
            pub_date = excluded.pub_date, section = excluded.section,
            gazette_number = excluded.gazette_number, province = excluded.province,
            url_xml = excluded.url_xml, url_pdf = excluded.url_pdf,
            n_announcements = excluded.n_announcements, n_acts = excluded.n_acts,
            n_with_sheet = excluded.n_with_sheet,
            n_unparsed_prefix = excluded.n_unparsed_prefix,
            n_without_acts = excluded.n_without_acts, n_stored = excluded.n_stored,
            loaded_at = excluded.loaded_at
        """,
        (
            doc.document_id,
            doc.pub_date.isoformat(),
            doc.section,
            doc.gazette_number,
            doc.province,
            ref.url_xml,
            ref.url_pdf,
            counts["announcements"],
            counts["acts"],
            counts["with_sheet"],
            counts["unparsed_prefix"],
            counts["without_acts"],
            counts["stored"],
            now(),
        ),
    )
    conn.execute("DELETE FROM document_act_counts WHERE document_id = ?", (doc.document_id,))
    conn.executemany(
        "INSERT INTO document_act_counts (document_id, act_type, scope, n) VALUES (?, ?, ?, ?)",
        [(doc.document_id, t, s, n) for (t, s), n in act_counts.items()],
    )
    conn.execute("DELETE FROM document_lag_counts WHERE document_id = ?", (doc.document_id,))
    conn.executemany(
        "INSERT INTO document_lag_counts (document_id, lag_days, n) VALUES (?, ?, ?)",
        [(doc.document_id, lag, n) for lag, n in lag_counts.items()],
    )


def upsert_announcement(conn: sqlite3.Connection, row: dict[str, object]) -> None:
    """Insert or refresh a stored announcement **without ever losing a match**.

    On conflict the derived columns are refreshed, but ``company_id`` and ``match_via``
    only change when the new value is not NULL: re-processing a document before its
    sheet is known must not undo a match found later.
    """
    conn.execute(
        """
        INSERT INTO announcements (document_id, number, pub_date, fact_date, province,
            company_name, company_norm, sheet, registry_data, company_id, match_via, kept_as)
        VALUES (:document_id, :number, :pub_date, :fact_date, :province, :company_name,
            :company_norm, :sheet, :registry_data, :company_id, :match_via, :kept_as)
        ON CONFLICT (document_id, number) DO UPDATE SET
            pub_date = excluded.pub_date,
            fact_date = excluded.fact_date,
            province = excluded.province,
            company_name = excluded.company_name,
            company_norm = excluded.company_norm,
            sheet = COALESCE(excluded.sheet, announcements.sheet),
            registry_data = excluded.registry_data,
            company_id = COALESCE(excluded.company_id, announcements.company_id),
            match_via = COALESCE(excluded.match_via, announcements.match_via),
            kept_as = CASE WHEN COALESCE(excluded.company_id, announcements.company_id)
                           IS NOT NULL THEN 'watched' ELSE excluded.kept_as END
        """,
        row,
    )


def upsert_acts(conn: sqlite3.Connection, document_id: str, number: int, acts: list[tuple]) -> None:
    """Refresh the acts of one announcement: classification may have improved."""
    conn.executemany(
        """
        INSERT INTO acts (document_id, number, seq, act_type, heading, detail, scope, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (document_id, number, seq) DO UPDATE SET
            act_type = excluded.act_type, heading = excluded.heading,
            detail = excluded.detail, scope = excluded.scope, priority = excluded.priority
        """,
        [(document_id, number, *act) for act in acts],
    )
    conn.execute(
        "DELETE FROM acts WHERE document_id = ? AND number = ? AND seq > ?",
        (document_id, number, len(acts)),
    )


def learn_sheet(
    conn: sqlite3.Connection, province: str, sheet: str, company_id: int, document_id: str
) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO company_sheets (province, sheet, company_id, origin, document_id,
            learned_at)
        VALUES (?, ?, ?, 'exact_name', ?, ?)
        """,
        (province, sheet, company_id, document_id, now()),
    )
    return cur.rowcount > 0


def orphans_of_sheet(conn: sqlite3.Connection, province: str, sheet: str) -> list[tuple[str, int]]:
    rows = conn.execute(
        """
        SELECT document_id, number FROM announcements
        WHERE company_id IS NULL AND province = ? AND sheet = ?
        """,
        (province, sheet),
    )
    return [(row[0], row[1]) for row in rows]


def link(conn: sqlite3.Connection, document_id: str, number: int, company_id: int) -> None:
    """Attach an orphan announcement to a company found through its sheet."""
    conn.execute(
        """
        UPDATE announcements SET company_id = ?, match_via = 'sheet', kept_as = 'watched'
        WHERE document_id = ? AND number = ? AND company_id IS NULL
        """,
        (company_id, document_id, number),
    )
    conn.execute(
        "DELETE FROM review_queue WHERE document_id = ? AND number = ?", (document_id, number)
    )
    conn.execute(
        "DELETE FROM candidate_announcements WHERE document_id = ? AND number = ?",
        (document_id, number),
    )


def add_alias(
    conn: sqlite3.Connection, name: str, company_id: int, origin: str, document_id: str | None
) -> bool:
    norm = normalize_name(name)
    if not norm:
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO company_aliases (alias_norm, company_id, alias, origin,
            document_id, created_at)
        SELECT ?, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (SELECT 1 FROM companies WHERE name_norm = ?)
        """,
        (norm, company_id, name, origin, document_id, now(), norm),
    )
    return cur.rowcount > 0


def insert_observations(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """(company_id, field, value, obs_date, document_id, number) registry observations."""
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO observations (company_id, field, value, obs_date, source,
            document_id, number, created_at)
        VALUES (?, ?, ?, ?, 'registry', ?, ?, ?)
        """,
        [(*row, now()) for row in rows],
    )
    return conn.total_changes - before


def insert_officer_events(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO officer_events (company_id, holder, holder_norm, role, role_raw,
            event, scope, event_date, is_company, document_id, number, seq)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return conn.total_changes - before


def upsert_review(
    conn: sqlite3.Connection,
    document_id: str,
    number: int,
    company_id: int,
    score: float,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO review_queue (document_id, number, company_id, score, reason)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (document_id, number) DO UPDATE SET
            company_id = excluded.company_id, score = excluded.score, reason = excluded.reason
        """,
        (document_id, number, company_id, score, reason),
    )


def upsert_candidate(
    conn: sqlite3.Connection,
    *,
    company_norm: str,
    province: str,
    company_name: str,
    sheet: str | None,
    group_id: int,
    reason: str,
    evidence: str,
    fact_date: str,
    document_id: str,
    number: int,
) -> int:
    """Register one announcement of a candidate; aggregates are refreshed afterwards."""
    stamp = now()
    conn.execute(
        """
        INSERT INTO candidates (company_norm, province, company_name, sheet, group_id, reason,
            evidence, first_date, last_date, n_announcements, status, judged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending', ?)
        ON CONFLICT (company_norm, province) DO UPDATE SET
            company_name = excluded.company_name,
            sheet = COALESCE(excluded.sheet, candidates.sheet),
            status = CASE WHEN candidates.status = 'dismissed' THEN 'pending'
                          ELSE candidates.status END,
            judged_at = excluded.judged_at
        """,
        (
            company_norm,
            province,
            company_name,
            sheet,
            group_id,
            reason,
            evidence,
            fact_date,
            fact_date,
            stamp,
        ),
    )
    candidate_id = _scalar(
        conn,
        "SELECT candidate_id FROM candidates WHERE company_norm = ? AND province = ?",
        (company_norm, province),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO candidate_announcements (document_id, number, candidate_id)
        VALUES (?, ?, ?)
        """,
        (document_id, number, candidate_id),
    )
    return candidate_id


def refresh_candidate_aggregates(conn: sqlite3.Connection) -> None:
    """First and last date and number of announcements, from the stored announcements
    (recomputed, not incremented: re-running a day never double counts)."""
    with conn:
        conn.execute(
            """
            UPDATE candidates SET
                n_announcements = (SELECT COUNT(*) FROM candidate_announcements ca
                                   WHERE ca.candidate_id = candidates.candidate_id),
                first_date = COALESCE((SELECT MIN(a.fact_date)
                    FROM candidate_announcements ca JOIN announcements a USING (document_id, number)
                    WHERE ca.candidate_id = candidates.candidate_id), first_date),
                last_date = COALESCE((SELECT MAX(a.fact_date)
                    FROM candidate_announcements ca JOIN announcements a USING (document_id, number)
                    WHERE ca.candidate_id = candidates.candidate_id), last_date)
            """
        )


def candidates_to_judge(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Pending and machine-dismissed candidates with the text of their stored acts.

    LEFT JOIN on purpose: a candidate without stored acts must still be judged (on its
    name); with an inner join it would never be re-judged and stay pending forever.
    """
    return conn.execute(
        """
        SELECT c.candidate_id, c.company_name, c.company_norm, c.province, c.sheet, c.status,
               group_concat(t.act_type || char(31) || t.detail, char(30)) AS acts
        FROM candidates c
        LEFT JOIN candidate_announcements ca ON ca.candidate_id = c.candidate_id
        LEFT JOIN acts t ON t.document_id = ca.document_id AND t.number = ca.number
        WHERE c.status IN ('pending', 'dismissed')
        GROUP BY c.candidate_id
        """
    ).fetchall()


def set_candidate_verdict(
    conn: sqlite3.Connection,
    candidate_id: int,
    status: str,
    group_id: int | None = None,
    reason: str | None = None,
    evidence: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE candidates SET status = ?, group_id = COALESCE(?, group_id),
            reason = COALESCE(?, reason), evidence = COALESCE(?, evidence), judged_at = ?
        WHERE candidate_id = ?
        """,
        (status, group_id, reason, evidence, now(), candidate_id),
    )


# ------------------------------------------------------------------ rebuild / prune


def clear_derived(conn: sqlite3.Connection) -> None:
    """Delete everything derived from the gazette (it is regenerated from the cache).

    Kept: the watched companies and groups, watchlist observations and sheets, the
    log of applied changes (so ``sync`` can revert what lost its evidence) and the
    day checks (a day without gazette is a network fact, not a derived one).
    """
    with conn:
        for sql in (
            "DELETE FROM candidate_announcements",
            "DELETE FROM candidates",
            "DELETE FROM review_queue",
            "DELETE FROM acts",
            "DELETE FROM announcements",
            "DELETE FROM document_act_counts",
            "DELETE FROM document_lag_counts",
            "DELETE FROM documents",
            "DELETE FROM observations WHERE source = 'registry'",
            "DELETE FROM officer_events",
            "DELETE FROM company_sheets WHERE origin = 'exact_name'",
            "DELETE FROM company_aliases WHERE origin = 'name_change'",
            "DELETE FROM days WHERE status <> 'no_gazette'",
        ):
            conn.execute(sql)


def prune(conn: sqlite3.Connection) -> int:
    """Delete stored announcements that are neither watched, in review, nor candidates.

    Nothing is lost for good: the documents stay in the cache and ``rebuild
    --store-all`` brings the full gazette back.
    """
    with conn:
        cur = conn.execute(
            """
            DELETE FROM announcements
            WHERE company_id IS NULL
              AND NOT EXISTS (SELECT 1 FROM review_queue r
                              WHERE r.document_id = announcements.document_id
                                AND r.number = announcements.number)
              AND NOT EXISTS (SELECT 1 FROM candidate_announcements ca
                              JOIN candidates c USING (candidate_id)
                              WHERE ca.document_id = announcements.document_id
                                AND ca.number = announcements.number
                                AND c.status = 'pending')
            """
        )
        return cur.rowcount


# ------------------------------------------------------------------ statistics


@dataclass(frozen=True, slots=True)
class Stats:
    first_day: str | None
    last_day: str | None
    days_published: int
    days_without_gazette: int
    days_failed: int
    documents: dict[str, int]  # by section
    announcements: dict[str, int]  # by section
    acts: int
    with_sheet: int
    announcements_a: int
    unparsed_prefix: int
    without_acts: int
    stored: int
    acts_by_type: list[tuple[str, int]]
    acts_by_scope: dict[str, int]
    officer_acts_by_scope: dict[str, int]
    lag: dict[int, int]  # Section A: days from inscription to publication -> announcements

    def lag_quantile(self, q: float) -> int | None:
        total = sum(self.lag.values())
        if not total:
            return None
        target = q * total
        running = 0
        for lag in sorted(self.lag):
            running += self.lag[lag]
            if running >= target:
                return lag
        return max(self.lag)


def stats(conn: sqlite3.Connection) -> Stats:
    first, last = conn.execute("SELECT MIN(pub_date), MAX(pub_date) FROM documents").fetchone()
    docs = dict(conn.execute("SELECT section, COUNT(*) FROM documents GROUP BY section").fetchall())
    anns = dict(
        conn.execute(
            "SELECT section, SUM(n_announcements) FROM documents GROUP BY section"
        ).fetchall()
    )
    by_type = [
        (row[0], int(row[1]))
        for row in conn.execute(
            "SELECT act_type, SUM(n) FROM document_act_counts GROUP BY act_type ORDER BY 2 DESC, 1"
        )
    ]
    by_scope = dict(
        conn.execute("SELECT scope, SUM(n) FROM document_act_counts GROUP BY scope").fetchall()
    )
    officer_scope = dict(
        conn.execute(
            """
            SELECT scope, SUM(n) FROM document_act_counts
            WHERE act_type IN ('appointment', 'officer_removal', 'revocation', 'reelection',
                               'ex_officio_cancellation')
            GROUP BY scope
            """
        ).fetchall()
    )
    lag = dict(
        conn.execute(
            """
            SELECT l.lag_days, SUM(l.n) FROM document_lag_counts l
            JOIN documents d USING (document_id) WHERE d.section = 'A' GROUP BY l.lag_days
            """
        ).fetchall()
    )
    totals = conn.execute(
        """
        SELECT SUM(n_acts), SUM(CASE WHEN section = 'A' THEN n_with_sheet END),
               SUM(CASE WHEN section = 'A' THEN n_announcements END),
               SUM(n_unparsed_prefix), SUM(n_without_acts), SUM(n_stored)
        FROM documents
        """
    ).fetchone()
    return Stats(
        first_day=first,
        last_day=last,
        days_published=_scalar(conn, "SELECT COUNT(*) FROM days WHERE status = 'published'"),
        days_without_gazette=_scalar(conn, "SELECT COUNT(*) FROM days WHERE status = 'no_gazette'"),
        days_failed=_scalar(conn, "SELECT COUNT(*) FROM days WHERE status = 'failed'"),
        documents={k: int(v) for k, v in docs.items()},
        announcements={k: int(v or 0) for k, v in anns.items()},
        acts=int(totals[0] or 0),
        with_sheet=int(totals[1] or 0),
        announcements_a=int(totals[2] or 0),
        unparsed_prefix=int(totals[3] or 0),
        without_acts=int(totals[4] or 0),
        stored=int(totals[5] or 0),
        acts_by_type=by_type,
        acts_by_scope={k: int(v) for k, v in by_scope.items()},
        officer_acts_by_scope={k: int(v) for k, v in officer_scope.items()},
        lag={int(k): int(v) for k, v in lag.items()},
    )
