"""Command line of borme-radar.

Every command reads and writes under ``--data-dir``: the cache of BOE responses, the SQLite
database and the measurement outputs (``calibration/``).

- ``fetch``: download a range of gazette days and process them.
- ``rebuild``: regenerate every derived table offline from the cache, most recent year
  first and in passes; ``--history-only`` re-derives only observations and officer events.
- ``sync``: where the record differs from the Registro; ``--apply`` writes it.
- ``relink``: stored orphan announcements whose sheet is now known; ``--apply`` links them.
- ``prune``: delete stored announcements that are no longer relevant.
- ``stats``: counts of the database (parser coverage, publication lag, acts, officers).
- ``report``: Markdown report of a window; ``--no-details`` leaves out act text and names.
- ``triage``: the open discovery leads, ordered by score, in tiers.
- ``calibrate``: measure the thresholds of matching and discovery on the cached corpus.
- ``formats``: count the format quirks of the cached corpus and derive the parser's lists.
- ``joined``: the year each company joined its group; ``--apply`` writes it.
- ``evaluate``: precision and recall of each discovery signal on a cached window.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from borme_radar import corpus, discovery, formats, joining, normalize, positions, record, store
from borme_radar.acts import ACT_TYPES
from borme_radar.cache import DiskCache
from borme_radar.discovery import (
    GroupIndex,
    is_valid_token_shape,
    load_tokens,
    mention_keys,
    site,
)
from borme_radar.download import Downloader
from borme_radar.evaluate import score, union
from borme_radar.httpclient import MAX_WORKERS, HttpError, PoliteClient
from borme_radar.matching import FUZZY_CUTOFF, INCORPORATION_GUARD_DAYS, Matcher
from borme_radar.parser import ParseError
from borme_radar.persons import (
    PAIR_NOISE_MAX,
    PairFinder,
    best_pair,
    name_words,
    possible_pairs,
)
from borme_radar.pipeline import Pipeline, derive_rows
from borme_radar.report import render
from borme_radar.runner import ORDERS, PassReport, RunReport, run_range, sweep
from borme_radar.source import BormeSource
from borme_radar.triage import TIERS, signal_precision, triage
from borme_radar.watchlist import load_watchlist

log = logging.getLogger("borme_radar")

ADDRESS_NOISE_FILE = "address_noise.json"
EVALUATION_FILE = "evaluation.json"


def _db(args: argparse.Namespace) -> sqlite3.Connection:
    return store.connect(args.data_dir / "borme.sqlite")


def _calibration_dir(args: argparse.Namespace) -> Path:
    path: Path = args.data_dir / "calibration"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _import(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    if getattr(args, "watchlist", None):
        result = store.import_watchlist(conn, load_watchlist(args.watchlist))
        log.info(
            "%d groups and %d companies in the watchlist, %d of them new; %d record fields filled",
            result.groups,
            result.companies,
            result.new_companies,
            result.fields_filled,
        )


def _matcher(args: argparse.Namespace, conn: sqlite3.Connection) -> Matcher:
    return Matcher(store.load_watch_index(conn), args.fuzzy_cutoff, args.guard_days)


def _token_ids(conn: sqlite3.Connection, path: Path | None) -> dict[str, int]:
    """Approved brand tokens with their group id; a token naming an unknown group is an error."""
    ids: dict[str, int] = {}
    for token, group in load_tokens(path).items():
        group_id = store.group_id_of(conn, str(group))
        if group_id is None:
            raise ValueError(f"{path}: token {token!r} names an unknown group {group!r}")
        ids[token] = group_id
    return ids


def _group_index(args: argparse.Namespace, conn: sqlite3.Connection) -> GroupIndex:
    hqs = [
        (known, group_id)
        for street, city, group_id in store.watched_addresses(conn)
        if (known := site(street, city)) is not None
    ]
    noise_file = getattr(args, "address_noise", None) or _calibration_dir(args) / ADDRESS_NOISE_FILE
    noise = json.loads(noise_file.read_text(encoding="utf-8")) if noise_file.exists() else {}
    persons: dict[str, int] = {}
    if getattr(args, "persons", False):
        persons = {r[0]: r[1] for r in conn.execute("SELECT pair, group_id FROM surname_pairs")}
    return GroupIndex(
        groups=store.groups(conn),
        mention_names=mention_keys(
            store.watched_names(conn), args.bare_min_chars, args.bare_min_words
        ),
        hqs=hqs,
        tokens=_token_ids(conn, getattr(args, "tokens", None)),
        hq_noise={k: int(v) for k, v in noise.items()},
        persons=persons,
        address_noise_max=args.address_noise_max,
    )


def _officer_names(conn: sqlite3.Connection) -> dict[str, set[int]]:
    """Natural persons who are officers of watched companies (name key, single-spaced) and
    the groups they serve in: the input of the shared-officers ground truth."""
    names: dict[str, set[int]] = {}
    for holder_norm, group_id in conn.execute(
        """
        SELECT DISTINCT r.holder_key, w.group_id
        FROM role_changes r, watched w
        WHERE w.company_id = r.company_id AND r.legal_person = 0
        """
    ):
        names.setdefault(" ".join(holder_norm.split()), set()).add(group_id)
    return names


def _pipeline(
    args: argparse.Namespace, conn: sqlite3.Connection, known_only: bool = False
) -> Pipeline:
    index = _group_index(args, conn)
    return Pipeline(
        conn,
        _matcher(args, conn),
        index,
        store_all=getattr(args, "store_all", False),
        pairs_of=PairFinder(index.persons) if index.persons else None,
        known_only=known_only,
    )


def _print_run(report: RunReport, client: PoliteClient, workers: int) -> None:
    c = report.counters
    stats = client.stats
    kept = c["watched"] + c["review"] + c["candidate"] + c["all"]
    print(
        f"days: checked {c['days_checked']}, processed {c['days_published']}, "
        f"no gazette {c['days_no_gazette']}, failed {c['days_failed']}, "
        f"already done {c['days_skipped_done']}, not in cache {c['days_not_cached']}\n"
        f"documents {c['documents']}  announcements read {c['announcements']}  acts {c['acts']}\n"
        f"kept {kept} (watched {c['watched']}, review {c['review']}, candidates "
        f"{c['candidate']}, store-all {c['all']}), discarded {c['discarded']}\n"
        f"sheets learned {c['sheets_learned']}, orphans re-linked {c['relinked']}, aliases "
        f"{c['aliases']}, observations {c['observations']}, officer events "
        f"{c['officer_events']}\n"
        f"candidate announcements by signal: "
        + ", ".join(f"{k[10:]} {v}" for k, v in sorted(c.items()) if k.startswith("candidate_"))
        + f"\ncandidates re-judged: {dict(report.verdicts)}\n"
        f"http: network requests {stats.network_requests}, cache hits {stats.cache_hits}, "
        f"retries {stats.retries}, 404 {stats.not_found}, median latency "
        f"{(stats.latency_median or 0):.3f}s, workers {workers}\n"
        f"elapsed: {report.elapsed:.1f}s"
    )
    for day, error in report.failed:
        print(f"FAILED {day}: {error}")
    if report.failed:
        print(f"{len(report.failed)} days failed and were skipped: run the same range again.")


# ------------------------------------------------------------------ commands


def cmd_fetch(args: argparse.Namespace) -> int:
    if args.date_from > args.date_to:
        raise SystemExit("--from must not be after --to")
    conn = _db(args)
    _import(args, conn)
    with (
        PoliteClient(cache=DiskCache(args.data_dir / "cache"), min_interval=args.delay) as client,
        Downloader(BormeSource(client), workers=args.workers) as downloader,
    ):
        report = run_range(
            conn,
            downloader,
            _pipeline(args, conn),
            args.date_from,
            args.date_to,
            reprocess=args.reprocess,
            download_only=args.download_only,
        )
        _print_run(report, client, args.workers)
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    conn = _db(args)
    _import(args, conn)
    if args.history_only:
        return _rebuild_history(conn)
    start, end = args.date_from, args.date_to
    if start is None or end is None:
        row = conn.execute("SELECT MIN(day), MAX(day) FROM issue_days").fetchone()
        if row[0] is None:
            raise SystemExit("no days recorded yet: give --from and --to")
        start = start or date.fromisoformat(row[0])
        end = end or date.fromisoformat(row[1])
    started = time.perf_counter()
    store.clear_derived(conn)
    log.info(
        "derived tables cleared in %.1fs; rebuilding %s to %s from the cache",
        time.perf_counter() - started,
        start,
        end,
    )
    with (
        PoliteClient(cache=DiskCache(args.data_dir / "cache"), offline=True) as client,
        Downloader(BormeSource(client), workers=1) as downloader,
    ):
        passes = sweep(
            conn,
            downloader,
            lambda known_only: _pipeline(args, conn, known_only),
            start,
            end,
            order=args.order,
            max_passes=args.passes,
        )
    _print_sweep(passes, args.order)
    out = _calibration_dir(args) / f"sweep_{args.order}_{start}_{end}.json"
    out.write_text(
        json.dumps(
            {
                "window": [start.isoformat(), end.isoformat()],
                "order": args.order,
                "passes": [
                    {
                        "pass": p.number,
                        "mode": p.mode,
                        "seconds": round(p.seconds, 1),
                        "before": p.before,
                        "after": p.after,
                        "counters": {
                            k: v
                            for k, v in sorted(p.counters.items())
                            if k.startswith(("days_", "candidate_"))
                            or k
                            in (
                                "documents",
                                "announcements",
                                "acts",
                                "watched",
                                "review",
                                "candidate",
                                "sheets_learned",
                                "relinked",
                                "aliases",
                                "followed",
                            )
                        },
                        "failed_days": [str(d) for d, _ in p.failed],
                        "verdicts": dict(p.verdicts),
                    }
                    for p in passes
                ],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"written {out}")
    return 0


def _print_sweep(passes: list[PassReport], order: str) -> None:
    print(f"order: {order}")
    print("pass  mode        seconds  sheets  aliases  watched ann.  followed ann.  candidates")
    for p in passes:
        print(
            f"{p.number:>4}  {p.mode:10} {p.seconds:8.0f}  {p.gained('sheets'):+6}  "
            f"{p.gained('aliases'):+7}  {p.gained('watched'):+12}  "
            f"{p.gained('candidate_announcements'):+13}  {p.after['candidates']:10}"
        )
    last = passes[-1]
    c = sum((p.counters for p in passes), Counter())
    print(
        f"days processed {c['days_published']} (no gazette {c['days_no_gazette']}, failed "
        f"{c['days_failed']}, not in cache {c['days_not_cached']}); "
        f"documents read {c['documents']}, announcements read {c['announcements']}\n"
        f"final: {last.after['sheets']} sheets learned, {last.after['aliases']} aliases, "
        f"{last.after['watched']} watched announcements, {last.after['observations']} "
        f"observations, {last.after['officer_events']} officer events, "
        f"{last.after['candidates']} open leads"
    )
    for day, error in (f for p in passes for f in p.failed):
        print(f"FAILED {day}: {error}")


def _rebuild_history(conn: sqlite3.Connection) -> int:
    """Derive the dated record values and the officer changes of the watched companies
    again, from the acts kept in the database (the cache is not read). Both tables are
    emptied first; the positions held are worked out again at the end."""
    started = time.perf_counter()
    with conn:
        conn.execute("DELETE FROM seen_values WHERE source = 'registry'")
        conn.execute("DELETE FROM role_changes")
        n_obs = n_events = 0
        rows = conn.execute(
            """
            SELECT doc_id, num, company_id, fact_day FROM notices
            WHERE company_id IS NOT NULL ORDER BY fact_day, doc_id, num
            """
        ).fetchall()
        for row in rows:
            acts = conn.execute(
                "SELECT seq, act_type, detail FROM notice_acts WHERE doc_id = ? AND num = ?"
                " ORDER BY seq",
                (row["doc_id"], row["num"]),
            ).fetchall()
            obs, events = derive_rows(
                row["company_id"],
                row["doc_id"],
                row["num"],
                row["fact_day"],
                [tuple(a) for a in acts],
            )
            n_obs += store.insert_observations(conn, obs)
            n_events += store.insert_officer_events(conn, events)
    held = positions.refresh(conn)
    print(
        f"{len(rows)} watched announcements read, {n_obs} observations and {n_events} officer "
        f"events derived ({held} positions held) in {time.perf_counter() - started:.1f}s"
    )
    return 0


def _cut(value: object, width: int = 56) -> str:
    text = "-" if value in (None, "") else " ".join(str(value).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _columns(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    """Rows in columns as wide as their longest value, under a header and a rule."""
    cells = [list(header), *([_cut(v) for v in row] for row in rows)]
    widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in cells
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return lines


def _closing(applied: bool, count: int, what: str) -> str:
    """The last line of a command that writes only with --apply (``what`` in the plural)."""
    if not count:
        return f"No {what} to write."
    if applied:
        return f"{count} {what} written."
    return f"Preview only: add --apply to write {'it' if count == 1 else f'these {count}'}."


def cmd_sync(args: argparse.Namespace) -> int:
    """Show where the record differs from the Registro, including reverts of applied writes
    whose evidence is gone; nothing is written without ``--apply``."""
    conn = _db(args)
    spelled = record.spelled_otherwise(conn)
    found = record.sync(conn, apply=args.apply)
    print(
        f"record against the Registro: fields that differ {len(found)}; addresses or"
        f" municipalities written another way but the same place {spelled}"
    )
    if found:
        rows = [[p.company, p.field, p.held, p.proposed, p.why, p.day] for p in found]
        print("\n".join(_columns(["company", "field", "record", "proposed", "why", "day"], rows)))
    print(_closing(args.apply, len(found), "fields"))
    return 0


def cmd_relink(args: argparse.Namespace) -> int:
    """List stored announcements without a company whose sheet now belongs to a watched
    company; with ``--apply``, link them and derive their observations and officer events."""
    conn = _db(args)
    rows = conn.execute(
        """
        SELECT o.company_id, w.name, o.province, o.sheet,
               (SELECT COUNT(*) FROM notices n WHERE n.sheet = o.sheet
                  AND n.province = o.province AND n.company_id IS NULL) AS n
        FROM sheet_owner o JOIN watched w ON w.company_id = o.company_id
        WHERE n > 0 ORDER BY w.name, o.province, o.sheet
        """
    ).fetchall()
    orphans = sum(r["n"] for r in rows)
    print(f"kept announcements without a company, on a sheet that has one now: {orphans}")
    if rows:
        table = [[r["name"], r["province"], r["sheet"], r["n"]] for r in rows]
        print("\n".join(_columns(["company", "province", "sheet", "announcements"], table)))
    if not (args.apply and rows):
        print(_closing(False, orphans, "links"))
        return 0
    pipeline = _pipeline(args, conn)
    with conn:
        total = sum(pipeline.relink(r["province"], r["sheet"], r["company_id"]) for r in rows)
    positions.refresh(conn)
    print(_closing(True, total, "links"))
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    conn = _db(args)
    print(f"{store.prune(conn)} stored announcements that are no longer relevant deleted")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = _db(args)
    s = store.stats(conn)
    read = sum(s.announcements.values())
    print(f"gazette days: {s.first_day} .. {s.last_day}")
    print(
        f"days processed: {s.days_published}, without gazette: {s.days_without_gazette}, "
        f"failed: {s.days_failed}"
    )
    print(f"documents by section: {s.documents}")
    print(f"announcements by section: {s.announcements} (total {read})")
    print(f"acts: {s.acts}")
    if s.announcements_a:
        print(
            f"Section A announcements with a registry sheet: {s.with_sheet} "
            f"({100 * s.with_sheet / s.announcements_a:.2f}%)"
        )
    print(f"announcements with text before the first known heading: {s.unparsed_prefix}")
    print(f"announcements without any recognised act: {s.without_acts}")
    print(f"announcements stored: {s.stored} ({100 * s.stored / max(read, 1):.2f}% of those read)")
    if s.lag:
        total = sum(s.lag.values())
        mode = max(s.lag, key=s.lag.__getitem__)
        print(
            f"days from inscription to publication (Section A): median {s.lag_quantile(0.5)}, "
            f"p10 {s.lag_quantile(0.1)}, p90 {s.lag_quantile(0.9)}, n={total}; "
            f"{100 * s.lag[mode] / total:.1f}% exactly {mode} days"
        )
    officer = sum(s.officer_acts_by_scope.values())
    if officer:
        shares = ", ".join(
            f"{k} {v} ({100 * v / officer:.1f}%)"
            for k, v in sorted(s.officer_acts_by_scope.items(), key=lambda kv: -kv[1])
        )
        print(f"officer acts by scope: {shares}")
    print(f"all acts by scope: {s.acts_by_scope}")
    print("\n| Act type | Code | Count |\n|---|---|---:|")
    for code, count in s.acts_by_type:
        label = ACT_TYPES[code].label if code in ACT_TYPES else code
        print(f"| {label} | `{code}` | {count:,} |")
    watched = conn.execute(
        """
        SELECT COUNT(*), SUM(via = 'sheet'), SUM(via = 'name')
        FROM notices WHERE company_id IS NOT NULL
        """
    ).fetchone()
    print(
        f"\nwatched announcements: {watched[0]} (via sheet {watched[1] or 0}, via name "
        f"{watched[2] or 0}); sheets known: "
        f"{store._scalar(conn, 'SELECT COUNT(*) FROM sheet_owner')}; aliases: "
        f"{store._scalar(conn, 'SELECT COUNT(*) FROM other_names')}"
    )
    watched_scope = dict(
        conn.execute(
            """
            SELECT t.scope, COUNT(*) FROM notice_acts t
            JOIN notices n ON n.doc_id = t.doc_id AND n.num = t.num
            WHERE n.company_id IS NOT NULL
              AND t.act_type IN ('appointment', 'officer_removal', 'revocation', 'reelection',
                                 'ex_officio_cancellation')
            GROUP BY t.scope
            """
        ).fetchall()
    )
    print(f"officer acts of watched companies by scope: {watched_scope}")
    for label, sql in (
        ("observations (registry)", "SELECT COUNT(*) FROM seen_values WHERE source='registry'"),
        ("officer events", "SELECT COUNT(*) FROM role_changes"),
        ("current officers", "SELECT COUNT(*) FROM officers_now"),
        ("review queue", "SELECT COUNT(*) FROM notices WHERE near_company IS NOT NULL"),
    ):
        print(f"{label}: {store._scalar(conn, sql)}")
    by_field = conn.execute(
        "SELECT field, COUNT(*) FROM seen_values WHERE source = 'registry'"
        " GROUP BY field ORDER BY 2 DESC, 1"
    ).fetchall()
    print(f"registry observations by field: {dict(map(tuple, by_field))}")
    # The rule of version 0.2, for comparison: last event per name as published and role.
    naive = """
        SELECT company_id, role FROM (
            SELECT company_id, role, kind, ROW_NUMBER() OVER (
                PARTITION BY company_id, holder_key, role
                ORDER BY day DESC, change_id DESC) AS rn
            FROM role_changes)
        WHERE rn = 1 AND kind IN ('appointment', 'reelection')
    """
    for label, source in (("now", "officers_now"), ("by name as published", f"({naive})")):
        by_role = dict(
            conn.execute(
                f"""
                SELECT CASE WHEN role IN ('auditor', 'sole_director', 'attorney') THEN role
                            ELSE 'other' END, COUNT(*)
                FROM {source} GROUP BY 1
                """
            ).fetchall()
        )
        several = store._scalar(
            conn,
            f"""SELECT COUNT(*) FROM (SELECT company_id FROM {source}
                WHERE role = 'sole_director' GROUP BY company_id HAVING COUNT(*) > 1)""",
        )
        print(
            f"current positions ({label}): {sum(by_role.values())} {by_role}; "
            f"companies with more than one sole director: {several}"
        )
    names, persons, merged = positions.reordered_names(conn)
    print(
        f"(company, role, name as published): {names}; the person key (same words in another "
        f"order) makes {persons} persons of {merged} of them"
    )
    events = conn.execute(
        "SELECT kind, scope, SUM(legal_person), COUNT(*) FROM role_changes GROUP BY 1, 2"
    ).fetchall()
    for row in events:
        print(f"  officer events {row[0]:12} {row[1]:9} {row[3]:6} (holder is a company: {row[2]})")
    candidates = conn.execute(
        "SELECT state, found_by, COUNT(*) FROM leads GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    for row in candidates:
        print(f"  candidates {row[0]:12} {row[1]:8} {row[2]}")
    return 0


def _coverage(conn: sqlite3.Connection, start: date, end: date) -> dict[str, object]:
    window = (start.isoformat(), end.isoformat())
    days = dict(
        conn.execute(
            "SELECT outcome, COUNT(*) FROM issue_days WHERE day BETWEEN ? AND ? GROUP BY 1",
            window,
        ).fetchall()
    )
    docs = conn.execute(
        """
        SELECT COUNT(DISTINCT d.doc_id), SUM(t.n * (t.what = 'announcements')),
               SUM(t.n * (t.what = 'acts')), SUM(t.n * (t.what = 'kept'))
        FROM gazette_docs d JOIN doc_tally t ON t.doc_id = d.doc_id
        WHERE d.day BETWEEN ? AND ?
        """,
        window,
    ).fetchone()
    watched = store._scalar(
        conn,
        "SELECT COUNT(*) FROM notices WHERE company_id IS NOT NULL AND day BETWEEN ? AND ?",
        window,
    )
    return {
        "days processed / failed": f"{days.get('published', 0)} / {days.get('failed', 0)}",
        "documents": docs[0] or 0,
        "announcements read": docs[1] or 0,
        "acts read": docs[2] or 0,
        "announcements stored (watched, review, candidates)": docs[3] or 0,
        "announcements discarded (unrelated to the watched groups)": (docs[1] or 0)
        - (docs[3] or 0),
        "announcements of watched companies": watched,
        "registry sheets known (whole database)": store._scalar(
            conn, "SELECT COUNT(*) FROM sheet_owner"
        ),
        "record observations from the Registro (whole database)": store._scalar(
            conn, "SELECT COUNT(*) FROM seen_values WHERE source = 'registry'"
        ),
        "officer events (whole database)": store._scalar(conn, "SELECT COUNT(*) FROM role_changes"),
    }


def cmd_report(args: argparse.Namespace) -> int:
    conn = _db(args)
    tokens = _token_ids(conn, args.tokens)
    precision = signal_precision(args.evaluation or _calibration_dir(args) / EVALUATION_FILE)
    leads = triage(conn, tokens, args.date_to, precision)
    text = render(
        conn,
        args.date_from,
        args.date_to,
        record.proposals(conn),
        leads,
        _coverage(conn, args.date_from, args.date_to),
        details=not args.no_details,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(text.encode("utf-8"))
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    conn = _db(args)
    tokens = _token_ids(conn, args.tokens)
    precision = signal_precision(args.evaluation or _calibration_dir(args) / EVALUATION_FILE)
    scored = triage(conn, tokens, args.reference or date.today(), precision)
    by_tier = Counter(s.tier for s in scored)
    print(
        "precision of each signal: "
        + ", ".join(f"{signal} {value:.2f}" for signal, value in precision.items())
    )
    print(f"open leads: {len(scored)} (" + ", ".join(f"{t} {by_tier[t]}" for t in TIERS) + ")")
    rows = [
        [
            f"{s.score:.3f}",
            s.tier,
            s.group,
            s.company_name,
            s.reason,
            s.n_announcements,
            s.last_date,
        ]
        for s in scored[: args.top]
    ]
    header = ["score", "tier", "group", "company", "signal", "announcements", "last"]
    print("\n".join(_columns(header, rows)))
    return 0


# ------------------------------------------------------------------ calibrate


def _histogram(values: Sequence[int], edges: Sequence[int]) -> list[tuple[str, int]]:
    """Count ``values`` in the bins closed by each edge (``0``, ``1-2``, ...), plus an open
    bin above the last edge."""
    out: list[tuple[str, int]] = []
    low = 0
    for high in edges:
        label = f"{low}" if low == high else f"{low}-{high}"
        out.append((label, sum(1 for v in values if low <= v <= high)))
        low = high + 1
    out.append((f"{low}+", sum(1 for v in values if v >= low)))
    return out


_NOISE_EDGES = (0, 2, 5, 10, 20, 50, 200)


def cmd_calibrate(args: argparse.Namespace) -> int:
    conn = _db(args)
    matcher = _matcher(args, conn)
    group_names = store.groups(conn)
    seeds = {
        c["name_key"]: c["group_id"] for c in conn.execute("SELECT name_key, group_id FROM watched")
    }
    collectors: list[object] = []
    relations = corpus.Relations()
    shared = corpus.SharedOfficers(_officer_names(conn), args.shared_officers)
    collectors += [relations, shared]
    proposed: dict[str, int | str] = load_tokens(args.tokens) if args.tokens else {}
    token_noise = corpus.TokenNoise(proposed) if proposed else None
    addresses = None
    if args.addresses:
        hqs = [(k, g) for s, c, g in store.watched_addresses(conn) if (k := site(s, c))]
        addresses = corpus.AddressNoise(hqs)
    registered = corpus.RegisteredAddresses() if (args.hq or proposed) else None
    collisions = corpus.BareNameCollisions(args.sample_buckets) if args.collisions else None
    fuzzy = corpus.FuzzyScores(matcher) if args.fuzzy else None
    names = corpus.NameLengths() if args.names else None
    roles = corpus.Roles() if args.roles else None
    lag = corpus.IncorporationLag() if args.lag else None
    renames = corpus.SheetStability() if args.renames else None
    pair_counts = None
    person_names: list[tuple[str, int]] = []
    if args.person_pairs:
        person_names = [
            (r[0], r[1])
            for r in conn.execute(
                """
                SELECT DISTINCT r.holder, w.group_id FROM role_changes r
                JOIN watched w ON w.company_id = r.company_id
                WHERE r.legal_person = 0 AND r.scope IN ('board', 'attorney')
                """
            )
        ]
        words = {w for n, _ in person_names for w in name_words(n)}
        pairs = {p for n, _ in person_names for p in possible_pairs(n)}
        pair_counts = corpus.PairCounts(words, pairs)
    for extra in (
        token_noise,
        addresses,
        registered,
        collisions,
        fuzzy,
        names,
        roles,
        lag,
        renames,
        pair_counts,
    ):
        if extra is not None:
            collectors.append(extra)

    info = corpus.scan(
        args.data_dir / "cache",
        args.date_from,
        args.date_to,
        matcher,
        collectors,  # type: ignore[arg-type]
        limit_documents=args.limit_documents,
    )
    by_relation = relations.closure(seeds)
    truth = union(by_relation, shared.members())
    related = set(truth) | set(seeds)
    out: dict[str, object] = {
        "window": [args.date_from.isoformat(), args.date_to.isoformat()],
        "partial": info.partial,
        "days": info.days,
        "documents": info.documents,
        "announcements": info.announcements,
        "distinct_companies": len(info.companies),
        "watched_announcements": info.watched_announcements,
        "seconds": round(info.seconds, 1),
        "related_by_relation": len(by_relation),
        "related_by_shared_officers": len(shared.members()),
        "related_companies_excluded": len(truth),
        "shared_officers_histogram": dict(sorted(shared.histogram().items())),
        "relations": dict(relations.kinds()),
    }
    lines = [
        f"# Calibration {args.date_from} to {args.date_to}",
        "",
        f"{info.documents:,} documents, {info.announcements:,} announcements, "
        f"{len(info.companies):,} distinct companies (normalised names), {info.seconds:.0f}s. "
        f"Excluded as related to the watched groups: {len(truth):,} companies "
        f"({len(by_relation):,} by explicit relations, {len(shared.members()):,} sharing at "
        f"least {args.shared_officers} officers with a watched company).",
        "",
    ]
    if info.partial:
        lines += [
            f"> `--limit-documents` stopped the reading after {info.documents:,} documents: the"
            " counts are partial, so no proposal and no noise file are saved.",
            "",
        ]
    cal = _calibration_dir(args)

    if token_noise is not None:
        lines += _token_section(args, token_noise, proposed, registered, related, truth, info, out)
    if addresses is not None:
        lines += _address_section(args, addresses, related, truth, info, out)

    if registered is not None and args.hq:
        by_group = registered.group_addresses(truth)
        proposal_rows = []
        for group_id, labels in by_group.items():
            for label, n in labels.most_common(3):
                if n >= args.hq_min_members:
                    known = registered.keys[label]
                    unrelated_there = len(registered.cities.get(known.town, set()))
                    proposal_rows.append(
                        (group_names.get(group_id, str(group_id)), label, n, unrelated_there)
                    )
        proposal_rows.sort()
        out["hq_proposal"] = proposal_rows
        lines += [
            "## Registered addresses shared by companies of each group",
            "",
            f"Addresses where at least {args.hq_min_members} companies explicitly related to "
            "the group are registered (a proposal for the watchlist `address` column).",
            "",
            "| Group | Address | Related companies there |",
            "|---|---|---:|",
            *[f"| {g} | {label} | {n} |" for g, label, n, _ in proposal_rows],
            "",
        ]
        if not info.partial:
            (cal / "hq.proposal.json").write_text(
                json.dumps([r[:3] for r in proposal_rows], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    if collisions is not None:
        table = collisions.table()
        out["bare_name_collisions"] = table
        lines += [
            "## Company names without legal form that equal a natural person's name",
            "",
            f"Deterministic 1-in-{args.sample_buckets} hash sample on both sides: "
            f"{len(collisions.bare):,} bare company names, {len(collisions.persons):,} person "
            "names.",
            "",
            "| Characters (from) | Words | Bare names | Equal to a person | Rate |",
            "|---:|---:|---:|---:|---:|",
            *[f"| {c} | {w} | {n} | {h} | {100 * h / n:.2f}% |" for c, w, n, h in table if n],
            "",
        ]

    if fuzzy is not None:
        bands = []
        for band in sorted(fuzzy.bands, reverse=True):
            same = len(fuzzy.bands[band]["same_company"])
            different = len(fuzzy.bands[band]["different"])
            bands.append((band, same, different))
        out["fuzzy_bands"] = bands
        lines += [
            "## Similar names: best similarity to a watched name, truth from the sheet",
            "",
            "`same company` = the announcement carries the sheet of the watched company it "
            "resembles (a spelling variant); `different` = any other sheet.",
            "",
            "| Score band | Same company | Different company |",
            "|---|---:|---:|",
            *[f"| {b}-{b + 4} | {s} | {d} |" for b, s, d in bands],
            "",
        ]

    if names is not None:
        lengths = sorted(names.section_a.elements())
        top = lengths[-1] if lengths else 0
        p999 = lengths[int(0.999 * (len(lengths) - 1))] if lengths else 0
        out["name_lengths"] = {
            "section_a_max": top,
            "section_a_p999": p999,
            "section_b_list_sizes": dict(sorted(names.section_b_lists.items())),
        }
        lines += [
            "## Name lengths",
            "",
            f"Section A company names: max {top} characters, p99.9 {p999} (n={len(lengths):,}). "
            "Section B entries by number of companies listed: "
            f"{dict(sorted(names.section_b_lists.items()))}.",
            "",
        ]

    if roles is not None:
        known, total = roles.coverage()
        out["roles"] = {"known": known, "total": total, "top": roles.raw.most_common(60)}
        lines += [
            "## Officer role labels as published",
            "",
            f"{total:,} role mentions, {100 * known / max(total, 1):.2f}% mapped to a canonical "
            f"role; {len(roles.raw):,} distinct labels.",
            "",
            "| Label | Count | Canonical |",
            "|---|---:|---|",
            *[f"| {raw} | {n} | {discovery_role(raw)} |" for raw, n in roles.raw.most_common(60)],
            "",
        ]

    if lag is not None:
        diffs = sorted(lag.inscription_minus_start.elements())
        if diffs:
            q = {
                f"p{int(p * 1000) / 10:g}": diffs[int(p * (len(diffs) - 1))]
                for p in (0.0, 0.001, 0.01, 0.05, 0.5, 0.95, 0.99)
            }
            negative = sum(1 for d in diffs if d < 0)
            out["incorporation_lag"] = {"n": len(diffs), "negative": negative, **q}
            lines += [
                "## Incorporation: inscription date minus start of operations",
                "",
                f"n={len(diffs):,}; quantiles (days): {q}. Inscribed BEFORE the start of "
                f"operations: {negative} ({100 * negative / len(diffs):.2f}%).",
                "",
            ]

    if renames is not None:
        out["sheet_stability"] = renames.summary()
        lines += ["## Registry sheet stability", "", f"{renames.summary()}", ""]

    if pair_counts is not None:
        pair_unrelated = {p: len(v - related) for p, v in pair_counts.pair_hits.items()}
        best = [best_pair(n, pair_counts.word_counts, pair_unrelated) for n, _ in person_names]
        values = [n for pair, n in best if pair]
        out["person_pairs"] = {
            "people": len(person_names),
            "with_pair": len(values),
            "histogram": _histogram(values, (0, 1, 2, 3, 5, 10, 20)),
        }
        kept = {}
        for (_name, group_id), (pair, n) in zip(person_names, best, strict=True):
            if pair and n <= args.pair_noise_max:
                previous = kept.get(pair)
                if previous is None or n < previous[1]:
                    kept[pair] = (group_id, n)
        out["person_pairs"]["kept"] = len(kept)  # type: ignore[index]
        lines += [
            "## Surname pairs of the watched groups' officers (aggregates only)",
            "",
            f"{len(person_names):,} people; best pair found for {len(values):,}. Unrelated "
            f"companies carrying the best pair: {out['person_pairs']['histogram']}. "  # type: ignore[index]
            f"Kept (at most {args.pair_noise_max}): {len(kept):,}.",
            "",
        ]
        if not info.partial:
            with conn:
                conn.execute("DELETE FROM surname_pairs")
                conn.executemany(
                    "INSERT INTO surname_pairs (pair, group_id, outside, measured_at)"
                    " VALUES (?, ?, ?, ?)",
                    [(p, g, n, store.now()) for p, (g, n) in kept.items()],
                )

    if args.memory:
        windows = [
            (args.date_from, min(args.date_to, args.date_from + timedelta(days=d)))
            for d in args.memory_days
        ]
        memory = []
        for start, end in windows:
            result = corpus.memory_comparison(
                args.data_dir / "cache", start, end, matcher, proposed or {"X": 0}
            )
            memory.append({"from": start.isoformat(), "to": end.isoformat(), **result})
        out["memory"] = memory
        lines += [
            "## Memory: full inverted index versus the targeted count",
            "",
            "| Window | Documents | Full index keys | Full index peak MB | Targeted peak MB |",
            "|---|---:|---:|---:|---:|",
            *[
                f"| {m['from']} to {m['to']} | {m['documents']:.0f} | "
                f"{m['full_index_keys']:,.0f} | {m['full_index_peak_mb']} | "
                f"{m['targeted_peak_mb']} |"
                for m in memory
            ],
            "",
        ]

    stem = f"calibration_{args.date_from}_{args.date_to}" + ("_partial" if info.partial else "")
    (cal / f"{stem}.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=list), encoding="utf-8"
    )
    (cal / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten: {cal / stem}.md and .json")
    return 0


def _token_section(
    args: argparse.Namespace,
    noise: corpus.TokenNoise,
    proposed: dict[str, int | str],
    registered: corpus.RegisteredAddresses | None,
    related: set[str],
    truth: dict[str, tuple[int, int]],
    info: corpus.ScanInfo,
    out: dict[str, object],
) -> list[str]:
    """Count each proposed brand token outside and inside the groups, keep those that pass,
    and give the precision at every cut, so the chosen one can be checked."""
    towns = frozenset(registered.cities) if registered is not None else frozenset()
    outside = noise.unrelated(related)
    rows = []
    for token in sorted(noise.tokens):
        unrelated, inside = len(outside[token]), len(noise.hits.get(token, set()) & set(truth))
        shape = is_valid_token_shape(token, towns)
        decision = shape or ("kept" if unrelated <= args.token_noise_max else "too common")
        rows.append((token, proposed[token], unrelated, inside, decision))
    rows.sort(key=lambda r: (r[4] != "kept", r[0]))
    out["tokens"] = [
        dict(zip(("token", "group", "unrelated", "related", "decision"), r, strict=True))
        for r in rows
    ]
    out["token_histogram"] = _histogram([r[2] for r in rows], _NOISE_EDGES)
    valid = [r for r in rows if not is_valid_token_shape(r[0], towns)]
    cuts = []
    for cut in sorted({r[2] for r in valid}):
        kept = [r for r in valid if r[2] <= cut]
        inside, unrelated = sum(r[3] for r in kept), sum(r[2] for r in kept)
        share = inside / (inside + unrelated) if inside + unrelated else None
        cuts.append((cut, len(kept), inside, unrelated, share))
    out["token_cuts"] = cuts
    lines = [
        "## Brand tokens",
        "",
        "Each proposed token is counted in the names of the window's companies: those outside "
        "every watched group (unrelated) and those tied to one (related). A token is kept when "
        f"its shape allows it and at most {args.token_noise_max} unrelated companies carry it; "
        f"a single word that is a municipality of the window ({len(towns):,} seen) never is.",
        "",
        *_md_table(["Unrelated companies", "Tokens"], out["token_histogram"]),  # type: ignore[arg-type]
        "",
        *_md_table(["Token", "Group", "Unrelated", "Related", "Decision"], rows),
        "",
        "Precision of the tokens a cut keeps, their hits pooled (related / all):",
        "",
        *_md_table(
            ["Cut", "Tokens kept", "Related", "Unrelated", "Precision"],
            [(c, k, i, u, _pct(s)) for c, k, i, u, s in cuts],
        ),
        "",
    ]
    examples = [
        f"- {r[0]}: " + "; ".join(sorted(outside[r[0]])[:3]) for r in rows if r[4] == "too common"
    ]
    if examples:
        lines += ["Unrelated companies that carry the tokens found too common (first three):", ""]
        lines += [*examples, ""]
    if not info.partial:
        kept = {r[0]: r[1] for r in rows if r[4] == "kept"}
        proposal = _calibration_dir(args) / "tokens.proposal.json"
        proposal.write_bytes(json.dumps(kept, indent=2, ensure_ascii=False).encode("utf-8"))
        lines += [
            f"The {len(kept)} kept tokens are saved as `{proposal}`. That file is a proposal: "
            "no command reads it until it is copied to the approved list passed with `--tokens`.",
            "",
        ]
    return lines


def _address_section(
    args: argparse.Namespace,
    addresses: corpus.AddressNoise,
    related: set[str],
    truth: dict[str, tuple[int, int]],
    info: corpus.ScanInfo,
    out: dict[str, object],
) -> list[str]:
    """Count the unrelated companies registered at each watched address; save the counts
    that discovery reads to leave crowded addresses out."""
    counts = {label: len(v) for label, v in addresses.unrelated(related).items()}
    out["address_noise"] = counts
    out["address_histogram"] = _histogram(list(counts.values()), _NOISE_EDGES)
    rows = [
        (label, n, len(addresses.hits.get(label, set()) & set(truth)),
         "no" if n >= args.address_noise_max else "yes")
        for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]  # fmt: skip
    lines = [
        "## Watched addresses",
        "",
        "Companies registered in the window at the address of a watched company, outside the "
        f"groups (unrelated) and tied to one (related). From {args.address_noise_max} unrelated "
        "companies the address is crowded and discovery does not use it.",
        "",
        *_md_table(["Unrelated companies", "Addresses"], out["address_histogram"]),  # type: ignore[arg-type]
        "",
        *_md_table(["Address", "Unrelated", "Related", "Used"], rows),
        "",
    ]
    if not info.partial:
        noise = _calibration_dir(args) / ADDRESS_NOISE_FILE
        noise.write_bytes(
            json.dumps(counts, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        lines += [f"The counts are saved as `{noise}`, which `fetch` and `rebuild` read.", ""]
    return lines


def _md_table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    numeric = [bool(rows) and all(isinstance(r[i], int) for r in rows) for i in range(len(header))]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join("---:" if n else "---" for n in numeric) + "|")
    lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in rows]
    return lines


def discovery_role(raw: str) -> str:
    from borme_radar.officers import canonical_role

    return canonical_role(raw)


def cmd_evaluate(args: argparse.Namespace) -> int:
    conn = _db(args)
    matcher = _matcher(args, conn)
    index = _group_index(args, conn)
    relations = corpus.Relations()
    shared = corpus.SharedOfficers(_officer_names(conn), args.shared_officers)
    hits = corpus.SignalHits(index, PairFinder(index.persons) if index.persons else None)
    info = corpus.scan(
        args.data_dir / "cache",
        args.date_from,
        args.date_to,
        matcher,
        [relations, shared, hits],
    )
    seeds = {c[0]: c[1] for c in conn.execute("SELECT name_key, group_id FROM watched")}
    by_relation = relations.closure(seeds)
    by_both = union(by_relation, shared.members())
    scores = score("relations", by_relation, relations.subjects, hits) + score(
        "relations_or_shared_officers", by_both, relations.subjects, hits
    )
    sizes = {
        "truth_relations": len(by_relation),
        "truth_relations_discoverable": len(set(by_relation) & relations.subjects),
        "truth_relations_depth1": sum(1 for _, d in by_relation.values() if d == 1),
        "truth_relations_or_shared": len(by_both),
        "truth_relations_or_shared_discoverable": len(set(by_both) & relations.subjects),
        "relations": len(relations.edges),
    } | {f"relations_{k}": v for k, v in relations.kinds().items()}
    out = {
        "window": [args.date_from.isoformat(), args.date_to.isoformat()],
        "documents": info.documents,
        "announcements": info.announcements,
        "seconds": round(info.seconds, 1),
        "thresholds": {
            "address_noise_max": args.address_noise_max,
            "bare_mention_min_chars": args.bare_min_chars,
            "bare_mention_min_words": args.bare_min_words,
            "tokens": len(index.tokens),
            "watched_addresses": len(index.hqs),
            "person_pairs": len(index.persons),
        },
        **sizes,
        "signals": [asdict(s) for s in scores],
    }
    cal = _calibration_dir(args)
    path = args.out or cal / EVALUATION_FILE
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(
        f"window {args.date_from}..{args.date_to}: {info.documents} documents, "
        f"{info.announcements} announcements, {info.seconds:.0f}s"
    )
    print(f"ground truth: {sizes}")
    print(
        "| Ground truth | Signal | Flagged | True (same group) | Other group "
        "| Precision (lower bound) | Recall |\n|---|---|---:|---:|---:|---:|---:|"
    )
    for s in scores:
        print(
            f"| {s.truth} | {s.reason} | {s.flagged} | {s.true_positive} | {s.wrong_group} | "
            f"{_pct(s.precision)} | {_pct(s.recall)} |"
        )
    print(f"written: {path}")
    return 0


def _derived(holders: formats.Holders, streets: formats.RegistryStreets) -> dict[str, object]:
    """What ``formats`` measured (cut lengths, company words, street filler) next to the
    constants the code uses, plus the candidate words around each cut with their counts."""
    from borme_radar import discovery, officers
    from borme_radar import record as rec

    company_list = holders.derived_company_words()
    filler = streets.derived_filler()
    street_counts = {
        w: (*c, streets.city_words[w], streets.names_per_word[w]) for w, c in streets.words.items()
    }
    not_names = frozenset(filler) | rec.WRITTEN_IN_FULL | normalize.ARTICLES
    measured = {
        "holder_cut_length": formats.cut_length(holders.lengths),
        "street_cut_length": formats.cut_length(streets.lengths),
        "company_words": company_list,
        "street_filler": filler,
        "role_labels": [f"{k} {r}" for k, r in formats.role_labels(holders.label_keys)],
        "generic_street_words": formats.generic_street_words(street_counts, not_names),
    }
    in_code = {
        "holder_cut_length": officers.TRUNCATED_LENGTH,
        "street_cut_length": rec.REGISTRY_STREET_CUT,
        "company_words": sorted(officers.MEASURED_COMPANY_WORDS),
        "street_filler": sorted(rec.MEASURED_STREET_FILLER),
        "role_labels": [f"{k} {r}" for k, r in officers._ROLE_OF_LABEL.items()],
        "generic_street_words": sorted(discovery.GENERIC_STREET_WORDS),
    }
    # The candidates around the cuts, with their counts, for the tables of the docs.
    words = {
        w: [n, holders.words_with_form[w], holders.words_names[w]]
        for w, n in holders.words_total.most_common()
        if n >= formats.COMPANY_WORD_MIN_HOLDERS and holders.words_with_form[w] >= 0.5 * n
    }
    words |= {
        w: [holders.words_total[w], holders.words_with_form[w], holders.words_names[w]]
        for w in sorted(formats.PREVIOUS_COMPANY_WORDS & officers.LEGAL_PERSON_WORDS)
    }
    places = {
        w: [*c, streets.city_words[w]]
        for w, c in sorted(streets.words.items(), key=lambda kv: -kv[1][0])
        if c[0] >= formats.STREET_WORD_MIN_STREETS and max(c[1:]) >= 0.3 * c[0]
    }
    return {
        "measured": measured,
        "in_code": in_code,
        "same": {k: measured[k] == in_code[k] for k in measured},
        "company_word_candidates": words,
        "street_word_candidates": places,
        "street_word_counts": {
            w: [*c, streets.city_words[w], streets.names_per_word[w]]
            for w, c in sorted(streets.words.items(), key=lambda kv: -kv[1][0])
            if c[0] >= formats.STREET_WORD_MIN_STREETS
        },
    }


def _write_list(path: Path, words: list[str], rule: str) -> None:
    header = f"# {rule}\n# Written by `borme-radar formats`; one entry per line.\n"
    path.write_bytes((header + "".join(f"{w}\n" for w in words)).encode("utf-8"))


def cmd_formats(args: argparse.Namespace) -> int:
    """Count the format quirks of the cached corpus and measure the constants of the parser.

    The corpus is read twice: the second pass looks for a single sheet of the companies
    printed with two numbers, and splits holders with the company words of the first. The
    word lists are written only after a complete scan; the samples quote the gazette and
    stay local.
    """
    from borme_radar import officers

    conn = _db(args)
    matcher = _matcher(args, conn)
    boundaries = formats.Boundaries()
    pairs = formats.SheetPairs()
    holders = formats.Holders()
    streets = formats.RegistryStreets()
    quirks = formats.RecordQuirks()
    cache = args.data_dir / "cache"
    first_pass = [boundaries, pairs, holders, streets, quirks]
    info = corpus.scan(
        cache, args.date_from, args.date_to, matcher, first_pass, args.limit_documents
    )
    derived = _derived(holders, streets)
    measured: dict = derived["measured"]  # type: ignore[assignment]
    sightings = formats.SheetSightings(pairs.keys())
    split = formats.CompanySplit(
        officers.company_vocabulary(frozenset(measured["company_words"])),
        measured["holder_cut_length"] or officers.TRUNCATED_LENGTH,
    )
    second = corpus.scan(
        cache, args.date_from, args.date_to, matcher, [sightings, split], args.limit_documents
    )
    verdicts = formats.sheet_verdicts(pairs.cases, sightings)
    out: dict[str, object] = {
        "window": [args.date_from.isoformat(), args.date_to.isoformat()],
        "partial": info.partial,
        "documents": info.documents,
        "announcements": info.announcements,
        "seconds": round(info.seconds + second.seconds, 1),
        "act_boundaries": dict(boundaries.counts),
        "sheets": dict(pairs.counts),
        "sheet_second_number": {k: dict(v) for k, v in sorted(verdicts.items())},
        "holders": dict(holders.counts) | dict(split.counts),
        "holder_lengths": dict(sorted(holders.lengths.items())),
        "holder_lowercase_words": dict(sorted(holders.lowercase_words.items())),
        "holder_digit_runs": {f"{k} {n}": v for (k, n), v in sorted(split.digit_runs.items())},
        "ambiguous_forms": {
            f"{w} {place}": n for (w, place), n in sorted(holders.ambiguous.items())
        },
        "auditor_labels": formats.auditor_classes(holders.auditor_labels),
        "role_label_keys": {k: n for k, n in holders.label_keys.most_common() if n >= 10},
        "lowercase_words": {
            f"{w} {k}": n for (w, k), n in holders.lower_words.most_common() if n >= 10
        },
        "holder_word_places": {
            w: [n, holders.words_first[w], holders.words_last[w], holders.words_names[w]]
            for w, n in holders.words_total.most_common()
            if n >= 500
        },
        "person_short_words": {w: n for w, n in split.person_short_words.most_common() if n >= 100},
        "street_lengths": dict(sorted(streets.lengths.items())),
        "record": dict(quirks.counts),
        "street_cue_wordings": dict(quirks.cue_wordings.most_common()),
        "derived": derived,
    }
    samples = {
        "act_boundaries": boundaries.samples,
        "holders": holders.samples | split.samples,
        "record": quirks.samples,
    }
    cal = _calibration_dir(args)
    stem = f"formats_{args.date_from}_{args.date_to}"
    (cal / f"{stem}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), "utf-8")
    # Samples quote the gazette (names of people included): local only.
    (cal / f"{stem}.samples.json").write_text(
        json.dumps(samples, indent=1, ensure_ascii=False), "utf-8"
    )
    if not info.partial:
        window = f"{args.date_from} to {args.date_to}"
        _write_list(
            cal / "company_words.txt",
            measured["company_words"],
            f"Words of officer holders, {window}: in >= {formats.COMPANY_WORD_MIN_HOLDERS}"
            f" holders and >= {formats.COMPANY_WORD_MIN_NAMES} different names, >="
            f" {formats.COMPANY_WORD_MIN_SHARE:.0%} of those holders with a legal form (at the"
            " end, or a form abbreviation anywhere); legal-form words left out.",
        )
        _write_list(
            cal / "street_filler.txt",
            measured["street_filler"],
            f"Words of registered streets, {window}: in >= {formats.STREET_WORD_MIN_STREETS}"
            f" streets, opening the street or after its house number in >="
            f" {formats.STREET_WORD_MIN_SHARE:.0%} of them, and not a word of a municipality"
            f" of >= {formats.STREET_CITY_MIN} addresses.",
        )
        rules = {
            "role_labels": f"Role labels of officer acts by their letters, {window}: used >="
            f" {formats.ROLE_LABEL_MIN} times, with the role officers.role_of_label reads in"
            " them; most used first.",
            "generic_street_words": f"Words of street names, {window}: in >="
            f" {formats.GENERIC_STREET_MIN_NAMES} different street names and >="
            f" {formats.STREET_WORD_MIN_STREETS} streets, not a street type or entrance word.",
        }
        for name, rule in rules.items():
            _write_list(cal / f"{name}.txt", measured[name], rule)
    summary = {k: v for k, v in out.items() if k != "derived"}
    summary["derived"] = {k: derived[k] for k in ("measured", "same")}
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    print(f"written {cal / stem}.json (samples, local only: {stem}.samples.json)")
    return 0


def cmd_joined(args: argparse.Namespace) -> int:
    """Date the year each company joined its group from its acts and save a summary to
    ``joining.json``; the years of watched companies are written only with ``--apply``."""
    conn = _db(args)
    first = joining.first_gazette_day(conn)
    if first is None:
        raise SystemExit("nothing processed yet: run fetch or rebuild first")
    names = store.groups(conn)
    dated: list[joining.Dated] = []
    for group in joining.load_groups(conn):
        dated += joining.analyse(group, first)
    by_evidence: Counter[str] = Counter()
    by_rule: Counter[tuple[str, str, str]] = Counter()
    for d in dated:
        confirmed = "confirmed" if d.confirmed else "not confirmed"
        by_evidence[f"{d.evidence or 'no year'}, {confirmed}"] += 1
        by_rule[(d.member.origin, d.entry.rule, d.evidence or "no year")] += 1
    if args.details:
        ordered = sorted(dated, key=lambda x: (names.get(x.member.group_id, ""), x.member.name))
        rows = [
            [names.get(d.member.group_id, "?"), d.member.name, d.member.origin, d.year,
             d.evidence, "yes" if d.confirmed else "no", d.entry.rule,
             " ".join(d.entry.codes), d.member.tie]
            for d in ordered
        ]  # fmt: skip
        header = [
            "group",
            "company",
            "from",
            "year",
            "evidence",
            "confirmed",
            "rule",
            "codes",
            "tie",
        ]
        print("\n".join(_columns(header, rows)))
    summary = {
        "first_gazette_day": first.isoformat(),
        "companies": len(dated),
        "watched": sum(d.member.origin == "watchlist" for d in dated),
        "candidates_kept": sum(d.member.origin == "candidate" for d in dated),
        "by_evidence": dict(sorted(by_evidence.items())),
        "by_origin_rule_evidence": {"/".join(k): n for k, n in sorted(by_rule.items())},
        "officers_against_owner": joining.officers_against_owner(dated),
        "window_checks": joining.window_checks(dated),
    }
    print(json.dumps(summary, indent=1))
    out = _calibration_dir(args) / "joining.json"
    out.write_bytes(json.dumps(summary, indent=1).encode("utf-8"))
    changed = joining.apply(conn, dated) if args.apply else len(joining.changes(dated))
    print(_closing(args.apply, changed, "years of watched companies"))
    return 0


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


# ------------------------------------------------------------------ parser


# Options shared by several commands: (flags, keyword arguments of add_argument).
_DAY = date.fromisoformat
_OPTION_SETS: dict[str, list[tuple[tuple[str, ...], dict[str, object]]]] = {
    "window": [
        (("--from",), {"dest": "date_from", "type": _DAY, "required": True}),
        (("--to",), {"dest": "date_to", "type": _DAY, "required": True}),
    ],
    "window?": [
        (("--from",), {"dest": "date_from", "type": _DAY}),
        (("--to",), {"dest": "date_to", "type": _DAY}),
    ],
    "thresholds": [
        (("--fuzzy-cutoff",), {"type": float, "default": FUZZY_CUTOFF}),
        (("--guard-days",), {"type": int, "default": INCORPORATION_GUARD_DAYS}),
        (("--address-noise-max",), {"type": int, "default": discovery.ADDRESS_NOISE_MAX}),
        (
            ("--address-noise",),
            {
                "type": Path,
                "help": "JSON of `calibrate --addresses` (default: calibration/address_noise.json"
                " of the data directory; without it no address is left out as crowded)",
            },
        ),
        (("--bare-min-chars",), {"type": int, "default": discovery.BARE_MENTION_MIN_CHARS}),
        (("--bare-min-words",), {"type": int, "default": discovery.BARE_MENTION_MIN_WORDS}),
    ],
    "sources": [
        (("--watchlist",), {"type": Path, "help": "CSV of watched companies (imported first)"}),
        (("--tokens",), {"type": Path, "help": "APPROVED brand tokens (JSON token -> group)"}),
        (
            ("--persons",),
            {"action": "store_true", "help": "use the calibrated surname pairs (local only)"},
        ),
    ],
}


def _options(parser: argparse.ArgumentParser, kind: str) -> None:
    for flags, settings in _OPTION_SETS[kind]:
        parser.add_argument(*flags, **settings)  # type: ignore[arg-type]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="borme-radar",
        description="Follow watched companies in the BORME and discover new group companies.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="folder of the cache, the database and the outputs (default: data)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug messages too")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download a range of gazette days and process them")
    _options(fetch, "window")
    _options(fetch, "thresholds")
    _options(fetch, "sources")
    fetch.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"documents fetched at once, at most {MAX_WORKERS} (default {MAX_WORKERS})",
    )
    fetch.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="pause of each worker before its next request, in seconds (default 1.0)",
    )
    fetch.add_argument(
        "--download-only", action="store_true", help="download without processing anything"
    )
    fetch.add_argument(
        "--reprocess", action="store_true", help="redo days that were processed before"
    )
    fetch.add_argument(
        "--store-all",
        action="store_true",
        help="keep every announcement read, whether or not it concerns a watched group",
    )
    fetch.set_defaults(func=cmd_fetch)

    rebuild = sub.add_parser(
        "rebuild", help="rebuild everything derived, offline, from the cached documents"
    )
    _options(rebuild, "window?")
    _options(rebuild, "thresholds")
    _options(rebuild, "sources")
    rebuild.add_argument("--store-all", action="store_true")
    rebuild.add_argument(
        "--history-only",
        action="store_true",
        help="derive the dated values and officer changes again from the kept acts only",
    )
    rebuild.add_argument(
        "--order",
        choices=ORDERS,
        default="recent-first",
        help="order of the years in the sweep (default: recent-first)",
    )
    rebuild.add_argument(
        "--passes",
        type=int,
        default=3,
        help="at most this many passes, fewer when one adds nothing (default: 3)",
    )
    rebuild.set_defaults(func=cmd_rebuild)

    sync = sub.add_parser(
        "sync", help="compare the record with the Registro; --apply writes what differs"
    )
    sync.add_argument("--apply", action="store_true")
    sync.set_defaults(func=cmd_sync)

    relink = sub.add_parser(
        "relink", help="give kept orphan announcements to the company their sheet has now"
    )
    relink.add_argument("--apply", action="store_true")
    _options(relink, "thresholds")
    _options(relink, "sources")
    relink.set_defaults(func=cmd_relink)

    prune = sub.add_parser(
        "prune", help="drop kept announcements that no watched company, review or lead needs"
    )
    prune.set_defaults(func=cmd_prune)

    stats = sub.add_parser("stats", help="counts of the database: coverage, lag, acts, officers")
    stats.set_defaults(func=cmd_stats)

    report = sub.add_parser(
        "report", help="write the report of a date range in Markdown, to stdout unless --out"
    )
    _options(report, "window")
    report.add_argument("--out", type=Path)
    report.add_argument("--tokens", type=Path)
    report.add_argument(
        "--evaluation", type=Path, help="JSON written by evaluate, to weight the signals"
    )
    report.add_argument(
        "--no-details", action="store_true", help="shareable: no act text and no personal names"
    )
    report.set_defaults(func=cmd_report)

    tri = sub.add_parser("triage", help="order the open leads by score, in tiers")
    tri.add_argument("--tokens", type=Path)
    tri.add_argument("--evaluation", type=Path)
    tri.add_argument("--reference", type=date.fromisoformat, help="as-of date (default: today)")
    tri.add_argument("--top", type=int, default=30)
    tri.set_defaults(func=cmd_triage)

    cal = sub.add_parser("calibrate", help="measure thresholds on the cached corpus (offline)")
    _options(cal, "window")
    _options(cal, "thresholds")
    cal.add_argument(
        "--tokens", type=Path, help="JSON of proposed brand tokens (token -> group) to count"
    )
    cal.add_argument("--token-noise-max", type=int, default=discovery.TOKEN_NOISE_MAX)
    cal.add_argument("--addresses", action="store_true", help="unrelated companies at each address")
    cal.add_argument("--hq", action="store_true", help="propose addresses shared in each group")
    cal.add_argument("--hq-min-members", type=int, default=2)
    cal.add_argument(
        "--collisions",
        action="store_true",
        help="watched names without legal form that people also have",
    )
    cal.add_argument("--sample-buckets", type=int, default=10)
    cal.add_argument("--fuzzy", action="store_true", help="similarity bands against watched names")
    cal.add_argument("--names", action="store_true", help="company name lengths, Section B lists")
    cal.add_argument("--roles", action="store_true", help="count the role labels as printed")
    cal.add_argument("--lag", action="store_true", help="inscription minus start of operations")
    cal.add_argument("--renames", action="store_true", help="sheet stability across renames")
    cal.add_argument("--person-pairs", action="store_true", help="officers' surname pairs (local)")
    cal.add_argument("--pair-noise-max", type=int, default=PAIR_NOISE_MAX)
    cal.add_argument("--memory", action="store_true", help="peak memory: full index vs targeted")
    cal.add_argument("--memory-days", type=int, nargs="+", default=[6, 30, 90])
    cal.add_argument("--limit-documents", type=int, help="stop after N documents (no proposals)")
    cal.add_argument("--shared-officers", type=int, default=corpus.SHARED_OFFICERS_MIN)
    cal.set_defaults(func=cmd_calibrate)

    fm = sub.add_parser("formats", help="count format quirks and derive the word lists")
    _options(fm, "window")
    _options(fm, "thresholds")
    fm.add_argument("--limit-documents", type=int, help="stop after N documents (no word lists)")
    fm.set_defaults(func=cmd_formats)

    jn = sub.add_parser(
        "joined", help="date when each company joined its group; --apply stores the years"
    )
    jn.add_argument("--apply", action="store_true")
    jn.add_argument("--details", action="store_true", help="one line per company with its evidence")
    jn.set_defaults(func=cmd_joined)

    ev = sub.add_parser("evaluate", help="precision and recall of each discovery signal")
    _options(ev, "window")
    _options(ev, "thresholds")
    _options(ev, "sources")
    ev.add_argument("--out", type=Path)
    ev.add_argument("--shared-officers", type=int, default=corpus.SHARED_OFFICERS_MIN)
    ev.set_defaults(func=cmd_evaluate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    if getattr(args, "workers", 1) and not 1 <= getattr(args, "workers", 1) <= MAX_WORKERS:
        raise SystemExit(f"--workers must be between 1 and {MAX_WORKERS}")
    try:
        return int(args.func(args))
    except (HttpError, ParseError, ValueError) as exc:
        log.error("failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
