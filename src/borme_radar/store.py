"""The SQLite database: schema, watchlist, record, kept announcements and candidates.

The schema is the numbered files of ``sql/``, applied in order and tracked with
``PRAGMA user_version``. What comes from the gazette is loaded with upserts that never
lose a match: processing a document again before its sheet is known keeps the company
an announcement was linked to later. Everything the gazette gave can be deleted and
rebuilt from the cache (:func:`clear_derived`); the watchlist and what it gave stay.
"""

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

_SCHEMA_FILE = re.compile(r"(\d{3})_\w+\.sql")
# A table every database of this layout has; one of an earlier layout lacks it.
_MARK_TABLE = "status_stages"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _schema_files() -> Iterator[tuple[int, str]]:
    files = {}
    for entry in (resources.files("borme_radar") / "sql").iterdir():
        found = _SCHEMA_FILE.fullmatch(entry.name)
        if found:
            files[int(found.group(1))] = entry.read_text(encoding="utf-8")
    yield from sorted(files.items())


def schema_version(conn: sqlite3.Connection) -> int:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    return int(version)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the schema files newer than the database and return the version reached.

    A database made with an earlier, incompatible layout is refused.
    """
    have = schema_version(conn)
    if (
        have
        and not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_MARK_TABLE,)
        ).fetchone()
    ):
        raise RuntimeError(
            "this database was made by an earlier version of borme-radar: start a new data"
            " directory and run `rebuild` (the cache is kept)"
        )
    for number, script in _schema_files():
        if number > have:
            # The file and the new version number commit together or not at all.
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
            have = number
    return have


def connect(path: Path | str) -> sqlite3.Connection:
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    for pragma in ("foreign_keys = ON", "journal_mode = WAL"):
        conn.execute(f"PRAGMA {pragma}")
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


def _group(conn: sqlite3.Connection, label: str) -> int:
    conn.execute("INSERT INTO watch_groups (label) VALUES (?) ON CONFLICT DO NOTHING", (label,))
    return _scalar(conn, "SELECT group_id FROM watch_groups WHERE label = ?", (label,))


def _company(conn: sqlite3.Connection, entry: WatchedCompany, group_id: int, stamp: str) -> bool:
    """Insert or update a watched company from the watchlist; True if it is new.

    A joined year given in the watchlist is marked as typed by a person
    (``joined_manual``): ``joined --apply`` never overwrites it.
    """
    born = entry.incorporated.isoformat() if entry.incorporated else None
    cur = conn.execute(
        """
        INSERT INTO watched (name_key, group_id, name, incorporated, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (name_key) DO UPDATE SET
            group_id = excluded.group_id,
            incorporated = IFNULL(excluded.incorporated, watched.incorporated)
        """,
        (entry.norm, group_id, entry.name, born, stamp, stamp),
    )
    is_new = cur.rowcount == 1 and conn.execute("SELECT changes()").fetchone()[0] == 1
    conn.execute(
        """
        UPDATE watched SET activity = IFNULL(:activity, activity), heads_group = :head,
            joined_year = IIF(:joined IS NULL, joined_year, :joined),
            joined_manual = IIF(:joined IS NULL, joined_manual, 1),
            joined_evidence = IIF(:joined IS NULL, joined_evidence, NULL)
        WHERE name_key = :key
        """,
        {
            "activity": entry.activity,
            "head": int(entry.head),
            "joined": entry.joined,
            "key": entry.norm,
        },
    )
    return is_new


def import_watchlist(
    conn: sqlite3.Connection, entries: Iterable[WatchedCompany], today: date | None = None
) -> ImportResult:
    """Load the watchlist: groups, companies, the record fields it gives and the sheets.

    The fields are stored as observations of source ``watchlist`` dated ``today``.
    """
    stamp = now()
    day = (today or date.today()).isoformat()
    new = filled = 0
    with conn:
        for entry in entries:
            existed = _scalar(
                conn, "SELECT COUNT(*) FROM watched WHERE name_key = ?", (entry.norm,)
            )
            _company(conn, entry, _group(conn, entry.group), stamp)
            new += not existed
            company_id = _scalar(
                conn, "SELECT company_id FROM watched WHERE name_key = ?", (entry.norm,)
            )
            given = {
                "address": entry.address,
                "city": entry.city,
                "capital": None if entry.capital is None else f"{entry.capital:.2f}",
                "status": entry.status,
                "purpose": entry.purpose,
            }
            for field, value in given.items():
                if value is not None:
                    filled += _from_watchlist(conn, company_id, field, value, day, stamp)
            if entry.sheet and entry.province:
                conn.execute(
                    "INSERT INTO sheet_owner (company_id, province, sheet, how, learned_at)"
                    " VALUES (?, ?, ?, 'listed', ?) ON CONFLICT DO NOTHING",
                    (company_id, entry.province, entry.sheet, stamp),
                )
    return ImportResult(
        groups=_scalar(conn, "SELECT COUNT(*) FROM watch_groups"),
        companies=_scalar(conn, "SELECT COUNT(*) FROM watched"),
        new_companies=new,
        fields_filled=filled,
    )


def _from_watchlist(
    conn: sqlite3.Connection, company_id: int, field: str, value: str, day: str, stamp: str
) -> int:
    """Store a watchlist value as an observation and write it to the record.

    The record is left alone when the Registro has said something about the field.
    Returns the number of record rows changed.
    """
    conn.execute(
        "INSERT INTO seen_values (company_id, field, day, value, source, added_at)"
        " VALUES (?, ?, ?, ?, 'watchlist', ?) ON CONFLICT DO NOTHING",
        (company_id, field, day, value, stamp),
    )
    registry_said = conn.execute(
        "SELECT 1 FROM seen_values WHERE company_id = ? AND field = ? AND source = 'registry'",
        (company_id, field),
    ).fetchone()
    if registry_said:
        return 0
    typed: object = float(value) if field == "capital" else value
    cur = conn.execute(
        f"UPDATE watched SET {field} = :v, updated_at = :t"
        f" WHERE company_id = :c AND {field} IS NOT :v",
        {"v": typed, "t": stamp, "c": company_id},
    )
    return cur.rowcount


def load_watch_index(conn: sqlite3.Connection) -> WatchIndex:
    index = WatchIndex()
    for company_id, key, born in conn.execute(
        "SELECT company_id, name_key, incorporated FROM watched"
    ):
        index.add_name(key, company_id)
        index.incorporated[company_id] = date.fromisoformat(born) if born else None
    for key, company_id in conn.execute("SELECT name_key, company_id FROM other_names"):
        index.add_name(key, company_id)
    for province, sheet, company_id in conn.execute(
        "SELECT province, sheet, company_id FROM sheet_owner"
    ):
        index.by_sheet[(province, sheet)] = company_id
    return index


def groups(conn: sqlite3.Connection) -> dict[int, str]:
    return dict(conn.execute("SELECT group_id, label FROM watch_groups").fetchall())


def group_id_of(conn: sqlite3.Connection, name: str) -> int | None:
    found = conn.execute("SELECT group_id FROM watch_groups WHERE label = ?", (name,)).fetchone()
    return None if found is None else int(found[0])


def watched_names(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Names and aliases of the watched companies, with their group."""
    query = """
        SELECT w.name, w.group_id FROM watched w
        UNION ALL
        SELECT o.name, w.group_id FROM other_names o, watched w WHERE w.company_id = o.company_id
    """
    return [(name, group) for name, group in conn.execute(query)]


def watched_addresses(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """Registered addresses of the watched companies, as given and as in the record now."""
    query = """
        SELECT address, city, group_id FROM watched
        WHERE address IS NOT NULL AND city IS NOT NULL
        UNION
        SELECT street.value, town.value, w.group_id
        FROM watched w, record_now street, record_now town
        WHERE street.company_id = w.company_id AND street.field = 'address'
          AND town.company_id = w.company_id AND town.field = 'city'
    """
    return [(street, town, group) for street, town, group in conn.execute(query)]


# ------------------------------------------------------------------ days and documents


def record_day(
    conn: sqlite3.Connection,
    day: date,
    status: str,
    n_documents: int = 0,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO issue_days (day, asked_at, outcome, docs, failure)"
            " VALUES (?, ?, ?, ?, ?)",
            (day.isoformat(), now(), status, n_documents, error),
        )


def days_with_status(conn: sqlite3.Connection, *statuses: str) -> list[date]:
    wanted = set(statuses)
    rows = conn.execute("SELECT day, outcome FROM issue_days ORDER BY day")
    return [date.fromisoformat(day) for day, outcome in rows if outcome in wanted]


# Measures of ``doc_tally`` and the keys the pipeline counts them under.
_TALLY = {
    "announcements": "announcements",
    "acts": "acts",
    "with_sheet": "with_sheet",
    "prefix": "unparsed_prefix",
    "no_acts": "without_acts",
    "kept": "stored",
}


def upsert_document(
    conn: sqlite3.Connection,
    ref: DocumentRef,
    doc: Document,
    counts: dict[str, int],
    act_counts: dict[tuple[str, str], int],
    lag_counts: dict[int, int],
) -> None:
    doc_id = doc.document_id
    conn.execute(
        """
        INSERT INTO gazette_docs (doc_id, section, province, day, issue_no, xml_url, pdf_url,
            read_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (doc_id) DO UPDATE SET
            (section, province, day, issue_no, xml_url, pdf_url, read_at) =
            (excluded.section, excluded.province, excluded.day, excluded.issue_no,
             excluded.xml_url, excluded.pdf_url, excluded.read_at)
        """,
        (doc_id, doc.section, doc.province, doc.pub_date.isoformat(), doc.gazette_number,
         ref.url_xml, ref.url_pdf, now()),
    )  # fmt: skip
    # Tallies are replaced, not added to: reading a document again must not count it twice.
    for table in ("doc_tally", "act_tally", "lag_tally"):
        conn.execute(f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO doc_tally (doc_id, what, n) VALUES (?, ?, ?)",
        [(doc_id, what, counts[key]) for what, key in _TALLY.items()],
    )
    conn.executemany(
        "INSERT INTO act_tally (doc_id, scope, act_type, n) VALUES (?, ?, ?, ?)",
        [(doc_id, scope, kind, n) for (kind, scope), n in act_counts.items()],
    )
    conn.executemany(
        "INSERT INTO lag_tally (doc_id, days, n) VALUES (?, ?, ?)",
        [(doc_id, days, n) for days, n in lag_counts.items()],
    )


# Upsert of a kept announcement that never loses a match: a company, sheet or match
# found earlier stays when this run finds none, and a linked announcement stays watched.
_PUT_NOTICE = """
    INSERT INTO notices (doc_id, num, province, day, fact_day, firm, firm_key, sheet,
        registry_data, kept_as, company_id, via)
    VALUES (:document_id, :number, :province, :pub_date, :fact_date, :company_name,
        :company_norm, :sheet, :registry_data, :kept_as, :company_id, :match_via)
    ON CONFLICT (doc_id, num) DO UPDATE SET
        province = excluded.province, day = excluded.day, fact_day = excluded.fact_day,
        firm = excluded.firm, firm_key = excluded.firm_key,
        sheet = IFNULL(excluded.sheet, notices.sheet),
        registry_data = excluded.registry_data,
        company_id = IFNULL(excluded.company_id, notices.company_id),
        via = IFNULL(excluded.via, notices.via),
        kept_as = IIF(IFNULL(excluded.company_id, notices.company_id) IS NULL,
                      excluded.kept_as, 'watched')
"""


def upsert_announcement(conn: sqlite3.Connection, row: dict[str, object]) -> None:
    conn.execute(_PUT_NOTICE, row)


def upsert_acts(conn: sqlite3.Connection, document_id: str, number: int, acts: list[tuple]) -> None:
    """Replace the stored acts of an announcement; acts beyond the new count are deleted."""
    conn.execute(
        "DELETE FROM notice_acts WHERE doc_id = ? AND num = ? AND seq > ?",
        (document_id, number, len(acts)),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO notice_acts"
        " (doc_id, num, seq, act_type, heading, detail, scope, priority)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(document_id, number, *act) for act in acts],
    )


_LEARN_SHEET = (
    "INSERT INTO sheet_owner (company_id, province, sheet, how, doc_id, learned_at)"
    " VALUES (:c, :p, :s, 'name_match', :d, :t) ON CONFLICT DO NOTHING"
)


def learn_sheet(
    conn: sqlite3.Connection, province: str, sheet: str, company_id: int, document_id: str
) -> bool:
    """Record the owner of a sheet learned by exact name; False if it already had one."""
    owner = {"p": province, "s": sheet, "c": company_id, "d": document_id, "t": now()}
    return conn.execute(_LEARN_SHEET, owner).rowcount > 0


def orphans_of_sheet(conn: sqlite3.Connection, province: str, sheet: str) -> list[tuple[str, int]]:
    query = (
        "SELECT doc_id, num FROM notices WHERE sheet = ? AND province = ? AND company_id IS NULL"
    )
    return [(doc_id, num) for doc_id, num in conn.execute(query, (sheet, province))]


# The changes the pipeline makes to a kept announcement, by name: what is set and, for
# some, the condition the announcement must meet. ``untied`` drops a similar-name
# suggestion and a lead, which a watched company's announcement never keeps.
_UNTIED = "near_company = NULL, near_score = NULL, near_reason = NULL, lead_id = NULL"
_NOTICE_CHANGES = {
    "watched": (
        f"company_id = :company, via = 'sheet', kept_as = 'watched', {_UNTIED}",
        " AND company_id IS NULL",
    ),
    "untied": (_UNTIED, ""),
    "near": ("near_company = :company, near_score = :score, near_reason = :reason", ""),
    "lead": ("lead_id = :lead", ""),
    "followed": ("lead_id = IFNULL(lead_id, :lead)", ""),
}


def change_notice(
    conn: sqlite3.Connection, change: str, document_id: str, number: int, **values: object
) -> None:
    """Apply one of the named changes of ``_NOTICE_CHANGES`` to a kept announcement:
    ``watched`` gives an unmatched one to a company through its sheet, ``untied``, ``near``
    (a similar watched name), ``lead`` and ``followed`` (a lead's sheet; a lead it already
    has stays)."""
    assignments, condition = _NOTICE_CHANGES[change]
    statement = f"UPDATE notices SET {assignments} WHERE doc_id = :doc AND num = :num{condition}"
    conn.execute(statement, {**values, "doc": document_id, "num": number})


def add_alias(
    conn: sqlite3.Connection, name: str, company_id: int, origin: str, document_id: str | None
) -> bool:
    key = normalize_name(name)
    if not key or _scalar(conn, "SELECT COUNT(*) FROM watched WHERE name_key = ?", (key,)):
        return False
    cur = conn.execute(
        "INSERT INTO other_names (name_key, name, company_id, added_by, added_at, doc_id)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (key, name, company_id, origin, now(), document_id),
    )
    return cur.rowcount > 0


def insert_observations(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert registry observations, skipping those already there; returns how many are new."""
    stamp = now()
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO seen_values (company_id, field, value, day, doc_id, num, source,"
        " added_at) VALUES (?, ?, ?, ?, ?, ?, 'registry', ?) ON CONFLICT DO NOTHING",
        [(*row, stamp) for row in rows],
    )
    return conn.total_changes - before


def insert_officer_events(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert officer events, skipping those already there; returns how many are new."""
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO role_changes (company_id, holder, holder_key, role, label, kind, scope,"
        " day, legal_person, doc_id, num, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT DO NOTHING",
        rows,
    )
    return conn.total_changes - before


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
    """Create or refresh the candidate of a company and attach the announcement to it.

    Candidates are keyed by normalised name and province. One found again keeps the group,
    signal and evidence it was first found by (re-judging updates them) and, if it had been
    gone, is open again. Returns its id.
    """
    lead = {
        "key": company_norm, "province": province, "firm": company_name, "sheet": sheet,
        "group": group_id, "why": reason, "evidence": evidence, "day": fact_date, "t": now(),
    }  # fmt: skip
    conn.execute(
        """
        INSERT INTO leads (firm_key, province, firm, sheet, group_id, found_by, evidence,
            first_day, last_day, notice_count, judged_at)
        VALUES (:key, :province, :firm, :sheet, :group, :why, :evidence, :day, :day, 1, :t)
        ON CONFLICT (firm_key, province) DO UPDATE SET
            firm = excluded.firm, sheet = IFNULL(excluded.sheet, leads.sheet),
            state = IIF(leads.state = 'gone', 'open', leads.state),
            judged_at = excluded.judged_at
        """,
        lead,
    )
    lead_id = _scalar(
        conn, "SELECT lead_id FROM leads WHERE firm_key = :key AND province = :province", lead
    )
    change_notice(conn, "lead", document_id, number, lead=lead_id)
    return lead_id


def followed_sheets(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """Sheets to keep following: for each open lead found by mention, its sheet and id."""
    pending = conn.execute(
        "SELECT province, sheet, lead_id FROM leads WHERE found_by = 'mention'"
        " AND state = 'open' AND sheet IS NOT NULL ORDER BY lead_id DESC"
    )
    # Ordered from the newest lead, so the first lead of each sheet is written last.
    return dict(((row[0], row[1]), row[2]) for row in pending)


def refresh_stored_count(conn: sqlite3.Connection, document_id: str) -> None:
    """Recount the kept announcements of a document whose other tallies stay as they are."""
    conn.execute(
        "UPDATE doc_tally SET n = (SELECT COUNT(*) FROM notices WHERE doc_id = :d)"
        " WHERE doc_id = :d AND what = 'kept'",
        {"d": document_id},
    )


def has_document(conn: sqlite3.Connection, document_id: str) -> bool:
    """Has this document been read before?"""
    query = "SELECT EXISTS (SELECT 1 FROM gazette_docs WHERE doc_id = ?)"
    (read,) = conn.execute(query, (document_id,)).fetchone()
    return bool(read)


def refresh_candidate_aggregates(conn: sqlite3.Connection) -> None:
    """Recompute each candidate's number of announcements and its first and last day."""
    with conn:
        conn.execute(
            """
            WITH per_lead AS (
                SELECT lead_id, COUNT(*) AS n, MIN(fact_day) AS first, MAX(fact_day) AS last
                FROM notices WHERE lead_id IS NOT NULL GROUP BY lead_id
            )
            UPDATE leads SET
                notice_count = IFNULL((SELECT n FROM per_lead p WHERE p.lead_id = leads.lead_id),
                                      0),
                first_day = IFNULL((SELECT first FROM per_lead p WHERE p.lead_id = leads.lead_id),
                                   first_day),
                last_day = IFNULL((SELECT last FROM per_lead p WHERE p.lead_id = leads.lead_id),
                                  last_day)
            """
        )


def candidates_to_judge(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Candidates not yet watched, with the acts of their announcements in one column.

    In ``acts``, type and detail are separated by U+001F, and one act from the next by U+001E.
    """
    return conn.execute(
        """
        SELECT l.lead_id, l.firm, l.firm_key, l.province, l.sheet, l.state,
               (SELECT group_concat(t.act_type || char(31) || t.detail, char(30))
                FROM notices n JOIN notice_acts t ON t.doc_id = n.doc_id AND t.num = n.num
                WHERE n.lead_id = l.lead_id) AS acts
        FROM leads l
        WHERE l.state <> 'watched'
        ORDER BY l.lead_id
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
        UPDATE leads SET state = :state, judged_at = :t, group_id = IFNULL(:group, group_id),
            found_by = IFNULL(:why, found_by), evidence = IFNULL(:evidence, evidence)
        WHERE lead_id = :lead
        """,
        {"state": status, "t": now(), "group": group_id, "why": reason, "evidence": evidence,
         "lead": candidate_id},
    )  # fmt: skip


# ------------------------------------------------------------------ rebuild and prune


def clear_derived(conn: sqlite3.Connection) -> None:
    """Delete everything derived from the gazette, before a rebuild from the cache.

    The watchlist and what it gave are kept, and so are the days without a gazette.
    """
    with conn:
        conn.execute("DELETE FROM open_roles")
        conn.execute("DELETE FROM role_changes")
        conn.execute("DELETE FROM seen_values WHERE source <> 'watchlist'")
        conn.execute("DELETE FROM notices")  # their acts go with them
        conn.execute("DELETE FROM leads")
        conn.execute("DELETE FROM gazette_docs")  # and the tallies
        conn.execute("DELETE FROM sheet_owner WHERE how <> 'listed'")
        conn.execute("DELETE FROM other_names WHERE added_by <> 'watchlist'")
        conn.execute("DELETE FROM issue_days WHERE outcome IN ('published', 'failed')")


def prune(conn: sqlite3.Connection) -> int:
    """Delete kept announcements tied to no watched company, review or open candidate.

    Returns how many were deleted.
    """
    with conn:
        cur = conn.execute(
            """
            DELETE FROM notices
            WHERE company_id IS NULL AND near_company IS NULL
              AND (lead_id IS NULL
                   OR lead_id NOT IN (SELECT lead_id FROM leads WHERE state = 'open'))
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
    lag: dict[int, int]  # Section A: days from inscription to issue -> announcements

    def lag_quantile(self, q: float) -> int | None:
        total = sum(self.lag.values())
        if not total:
            return None
        running = 0
        for days in sorted(self.lag):
            running += self.lag[days]
            if running >= q * total:
                return days
        return max(self.lag)


_OFFICER_ACTS = (
    "appointment",
    "officer_removal",
    "revocation",
    "reelection",
    "ex_officio_cancellation",
)


def stats(conn: sqlite3.Connection) -> Stats:
    first, last = conn.execute("SELECT MIN(day), MAX(day) FROM gazette_docs").fetchone()
    days = dict(
        conn.execute("SELECT outcome, COUNT(*) FROM issue_days GROUP BY outcome").fetchall()
    )
    docs = dict(
        conn.execute("SELECT section, COUNT(*) FROM gazette_docs GROUP BY section").fetchall()
    )
    # Totals of each measure, by section.
    tally: dict[tuple[str, str], int] = {
        (what, section): int(n)
        for what, section, n in conn.execute(
            "SELECT t.what, d.section, SUM(t.n) FROM doc_tally t"
            " JOIN gazette_docs d ON d.doc_id = t.doc_id GROUP BY t.what, d.section"
        )
    }

    def total(what: str, section: str | None = None) -> int:
        return sum(n for (w, s), n in tally.items() if w == what and section in (None, s))

    by_type: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    officer_scope: dict[str, int] = {}
    for scope, kind, n in conn.execute(
        "SELECT scope, act_type, SUM(n) FROM act_tally GROUP BY scope, act_type"
    ):
        by_type[kind] = by_type.get(kind, 0) + n
        by_scope[scope] = by_scope.get(scope, 0) + n
        if kind in _OFFICER_ACTS:
            officer_scope[scope] = officer_scope.get(scope, 0) + n
    lag = dict(
        conn.execute(
            "SELECT l.days, SUM(l.n) FROM lag_tally l JOIN gazette_docs d ON d.doc_id = l.doc_id"
            " WHERE d.section = 'A' GROUP BY l.days"
        ).fetchall()
    )
    return Stats(
        first_day=first,
        last_day=last,
        days_published=int(days.get("published", 0)),
        days_without_gazette=int(days.get("no_gazette", 0)),
        days_failed=int(days.get("failed", 0)),
        documents={k: int(v) for k, v in docs.items()},
        announcements={s: total("announcements", s) for s in docs},
        acts=total("acts"),
        with_sheet=total("with_sheet", "A"),
        announcements_a=total("announcements", "A"),
        unparsed_prefix=total("prefix"),
        without_acts=total("no_acts"),
        stored=total("kept"),
        acts_by_type=sorted(by_type.items(), key=lambda item: (-item[1], item[0])),
        acts_by_scope=by_scope,
        officer_acts_by_scope=officer_scope,
        lag={int(k): int(v) for k, v in lag.items()},
    )
