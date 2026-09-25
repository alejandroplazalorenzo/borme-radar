"""Command line interface.

    borme-radar fetch --from 2026-09-01 --to 2026-09-23 --watchlist examples/watchlist.csv
    borme-radar rebuild --from 2025-09-24 --to 2026-09-23      # offline, from the cache
    borme-radar sync                                           # shows; --apply writes
    borme-radar report --from 2026-09-01 --to 2026-09-23 --no-details
    borme-radar calibrate --from ... --to ... --tokens FILE --addresses ...
    borme-radar evaluate --from ... --to ...

Nothing is scheduled: the radar is run by hand when someone wants to look. Every
command is idempotent.
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

from borme_radar import corpus, discovery, record, store
from borme_radar.acts import ACT_TYPES
from borme_radar.cache import DiskCache
from borme_radar.discovery import (
    GroupIndex,
    address_key,
    is_valid_token_shape,
    load_tokens,
    mention_keys,
)
from borme_radar.download import Downloader
from borme_radar.evaluate import score, union
from borme_radar.httpclient import MAX_WORKERS, HttpError, PoliteClient
from borme_radar.matching import FUZZY_CUTOFF, INCORPORATION_GUARD_DAYS, Matcher
from borme_radar.parser import ParseError
from borme_radar.persons import (
    PAIR_NOISE_MAX,
    best_pair,
    name_words,
    pairs_in,
    possible_pairs,
)
from borme_radar.pipeline import Pipeline, derive_rows
from borme_radar.report import render
from borme_radar.runner import RunReport, run_range
from borme_radar.source import BormeSource
from borme_radar.triage import signal_weights, triage
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
            "watchlist: %d groups, %d companies (%d new, %d record fields filled)",
            result.groups,
            result.companies,
            result.new_companies,
            result.fields_filled,
        )


def _matcher(args: argparse.Namespace, conn: sqlite3.Connection) -> Matcher:
    return Matcher(store.load_watch_index(conn), args.fuzzy_cutoff, args.guard_days)


def _token_ids(conn: sqlite3.Connection, path: Path | None) -> dict[str, int]:
    """Token file maps tokens to group names; unknown groups are an error."""
    ids: dict[str, int] = {}
    for token, group in load_tokens(path).items():
        group_id = store.group_id_of(conn, str(group))
        if group_id is None:
            raise ValueError(f"{path}: token {token!r} names an unknown group {group!r}")
        ids[token] = group_id
    return ids


def _group_index(args: argparse.Namespace, conn: sqlite3.Connection) -> GroupIndex:
    hqs = []
    for street, city, group_id in store.watched_addresses(conn):
        key = address_key(street, city)
        if key is not None:
            hqs.append((key, group_id))
    noise_file = _calibration_dir(args) / ADDRESS_NOISE_FILE
    noise = json.loads(noise_file.read_text(encoding="utf-8")) if noise_file.exists() else {}
    persons: dict[str, int] = {}
    if getattr(args, "persons", False):
        persons = {r[0]: r[1] for r in conn.execute("SELECT pair, group_id FROM person_keys")}
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
    """Plain full names of the natural persons who are officers of watched companies
    (local database only) -> the groups they are officers in."""
    names: dict[str, set[int]] = {}
    for holder_norm, group_id in conn.execute(
        """
        SELECT DISTINCT e.holder_norm, c.group_id
        FROM officer_events e JOIN companies c USING (company_id)
        WHERE e.is_company = 0
        """
    ):
        names.setdefault(" ".join(holder_norm.split()), set()).add(group_id)
    return names


def _pipeline(args: argparse.Namespace, conn: sqlite3.Connection) -> Pipeline:
    return Pipeline(
        conn,
        _matcher(args, conn),
        _group_index(args, conn),
        store_all=getattr(args, "store_all", False),
        pairs_of=pairs_in if getattr(args, "persons", False) else None,
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
        row = conn.execute("SELECT MIN(pub_date), MAX(pub_date) FROM days").fetchone()
        if row[0] is None:
            raise SystemExit("no days recorded yet: give --from and --to")
        start = start or date.fromisoformat(row[0])
        end = end or date.fromisoformat(row[1])
    started = time.perf_counter()
    store.clear_derived(conn)
    log.info(
        "derived data cleared (%.1fs); reprocessing %s..%s offline",
        time.perf_counter() - started,
        start,
        end,
    )
    with (
        PoliteClient(cache=DiskCache(args.data_dir / "cache"), offline=True) as client,
        Downloader(BormeSource(client), workers=1) as downloader,
    ):
        report = run_range(conn, downloader, _pipeline(args, conn), start, end, reprocess=True)
        _print_run(report, client, 1)
    return 0


def _rebuild_history(conn: sqlite3.Connection) -> int:
    """Re-derive observations and officer events from the stored acts (seconds)."""
    started = time.perf_counter()
    with conn:
        conn.execute("DELETE FROM observations WHERE source = 'registry'")
        conn.execute("DELETE FROM officer_events")
        n_obs = n_events = 0
        rows = conn.execute(
            """
            SELECT a.document_id, a.number, a.company_id, a.fact_date
            FROM announcements a WHERE a.company_id IS NOT NULL
            ORDER BY a.fact_date, a.document_id, a.number
            """
        ).fetchall()
        for row in rows:
            acts = conn.execute(
                "SELECT seq, act_type, detail FROM acts WHERE document_id = ? AND number = ?"
                " ORDER BY seq",
                (row["document_id"], row["number"]),
            ).fetchall()
            obs, events = derive_rows(
                row["company_id"],
                row["document_id"],
                row["number"],
                row["fact_date"],
                [tuple(a) for a in acts],
            )
            n_obs += store.insert_observations(conn, obs)
            n_events += store.insert_officer_events(conn, events)
    print(
        f"{len(rows)} watched announcements read, {n_obs} observations and {n_events} officer "
        f"events derived in {time.perf_counter() - started:.1f}s"
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    conn = _db(args)
    found = record.sync(conn, apply=args.apply)
    if not found:
        print("The record matches the Registro in every field.")
        return 0
    verb = "Applied" if args.apply else "Pending"
    print(f"{verb}: {len(found)} fields\n")
    for d in found:
        current = (d.record or "(empty)")[:40]
        print(
            f"  {d.company[:34]:34} {d.field:8} {current:40} -> {d.registry[:40]}  "
            f"({d.kind}, {d.fact_date})"
        )
    if not args.apply:
        print("\nNothing written. Run again with --apply to write them.")
    return 0


def cmd_relink(args: argparse.Namespace) -> int:
    """Attach stored orphan announcements to companies whose sheet is known."""
    conn = _db(args)
    rows = conn.execute(
        """
        SELECT s.company_id, c.name, s.province, s.sheet, COUNT(*) AS n
        FROM company_sheets s JOIN companies c USING (company_id)
        JOIN announcements a ON a.province = s.province AND a.sheet = s.sheet
                            AND a.company_id IS NULL
        GROUP BY s.company_id, s.province, s.sheet ORDER BY n DESC
        """
    ).fetchall()
    if not rows:
        print("No orphan announcement belongs to a known sheet.")
        return 0
    for row in rows:
        print(f"  {row['n']:5}  {row['name'][:40]:40} {row['province'][:16]:16} {row['sheet']}")
    if not args.apply:
        print("\nNothing written. Run again with --apply to link them.")
        return 0
    pipeline = _pipeline(args, conn)
    with conn:
        total = sum(pipeline.relink(r["province"], r["sheet"], r["company_id"]) for r in rows)
    print(f"{total} announcements linked")
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
        SELECT COUNT(*), SUM(match_via = 'sheet'), SUM(match_via = 'name')
        FROM announcements WHERE company_id IS NOT NULL
        """
    ).fetchone()
    print(
        f"\nwatched announcements: {watched[0]} (via sheet {watched[1] or 0}, via name "
        f"{watched[2] or 0}); sheets known: "
        f"{store._scalar(conn, 'SELECT COUNT(*) FROM company_sheets')}; aliases: "
        f"{store._scalar(conn, 'SELECT COUNT(*) FROM company_aliases')}"
    )
    watched_scope = dict(
        conn.execute(
            """
            SELECT t.scope, COUNT(*) FROM acts t JOIN announcements a USING (document_id, number)
            WHERE a.company_id IS NOT NULL
              AND t.act_type IN ('appointment', 'officer_removal', 'revocation', 'reelection',
                                 'ex_officio_cancellation')
            GROUP BY t.scope
            """
        ).fetchall()
    )
    print(f"officer acts of watched companies by scope: {watched_scope}")
    for label, sql in (
        ("observations (registry)", "SELECT COUNT(*) FROM observations WHERE source='registry'"),
        ("officer events", "SELECT COUNT(*) FROM officer_events"),
        ("current officers", "SELECT COUNT(*) FROM current_officers"),
        ("review queue", "SELECT COUNT(*) FROM review_queue"),
    ):
        print(f"{label}: {store._scalar(conn, sql)}")
    events = conn.execute(
        "SELECT event, scope, SUM(is_company), COUNT(*) FROM officer_events GROUP BY 1, 2"
    ).fetchall()
    for row in events:
        print(f"  officer events {row[0]:12} {row[1]:9} {row[3]:6} (holder is a company: {row[2]})")
    candidates = conn.execute(
        "SELECT status, reason, COUNT(*) FROM candidates GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    for row in candidates:
        print(f"  candidates {row[0]:12} {row[1]:8} {row[2]}")
    return 0


def _coverage(conn: sqlite3.Connection, start: date, end: date) -> dict[str, object]:
    window = (start.isoformat(), end.isoformat())
    days = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM days WHERE pub_date BETWEEN ? AND ? GROUP BY status",
            window,
        ).fetchall()
    )
    docs = conn.execute(
        """
        SELECT COUNT(*), SUM(n_announcements), SUM(n_acts), SUM(n_stored)
        FROM documents WHERE pub_date BETWEEN ? AND ?
        """,
        window,
    ).fetchone()
    watched = store._scalar(
        conn,
        "SELECT COUNT(*) FROM announcements WHERE company_id IS NOT NULL"
        " AND pub_date BETWEEN ? AND ?",
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
            conn, "SELECT COUNT(*) FROM company_sheets"
        ),
        "record observations from the Registro (whole database)": store._scalar(
            conn, "SELECT COUNT(*) FROM observations WHERE source = 'registry'"
        ),
        "officer events (whole database)": store._scalar(
            conn, "SELECT COUNT(*) FROM officer_events"
        ),
    }


def cmd_report(args: argparse.Namespace) -> int:
    conn = _db(args)
    tokens = _token_ids(conn, args.tokens)
    weights = signal_weights(args.evaluation or _calibration_dir(args) / EVALUATION_FILE)
    candidates = triage(conn, tokens, args.date_to, weights)
    differences = record.differences(conn) + record.retired(conn)
    text = render(
        conn,
        args.date_from,
        args.date_to,
        differences,
        candidates,
        _coverage(conn, args.date_from, args.date_to),
        details=not args.no_details,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    conn = _db(args)
    tokens = _token_ids(conn, args.tokens)
    weights = signal_weights(args.evaluation or _calibration_dir(args) / EVALUATION_FILE)
    reference = args.reference or date.today()
    scored = triage(conn, tokens, reference, weights)
    tiers = Counter(s.tier for s in scored)
    print(f"signal weights: {weights}")
    print(f"{len(scored)} pending candidates: {dict(tiers)}")
    by_group = Counter(s.group for s in scored if s.tier != "long tail")
    print(f"reviewable by group: {dict(by_group.most_common())}")
    for s in scored[: args.top]:
        print(
            f"  {s.score:5.1f} {s.tier:13} {s.group[:18]:18} {s.company_name[:44]:44} "
            f"{s.reason:8} {s.n_announcements:3} {s.last_date}"
        )
    return 0


# ------------------------------------------------------------------ calibrate


def _histogram(values: Sequence[int], edges: Sequence[int]) -> list[tuple[str, int]]:
    """Counts per bin; ``edges`` are the upper bounds of the bins (inclusive)."""
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
        c["name_norm"]: c["group_id"]
        for c in conn.execute("SELECT name_norm, group_id FROM companies")
    }
    collectors: list[object] = []
    relations = corpus.Relations()
    shared = corpus.SharedOfficers(_officer_names(conn), args.shared_officers)
    collectors += [relations, shared]
    proposed: dict[str, int | str] = load_tokens(args.tokens) if args.tokens else {}
    token_noise = corpus.TokenNoise(proposed) if proposed else None
    addresses = None
    if args.addresses:
        hqs = [(k, g) for s, c, g in store.watched_addresses(conn) if (k := address_key(s, c))]
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
                SELECT DISTINCT e.holder, c.group_id FROM officer_events e
                JOIN companies c USING (company_id)
                WHERE e.is_company = 0 AND e.scope IN ('board', 'attorney')
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
            f"> **PARTIAL MEASUREMENT: only the first {info.documents:,} documents were read.** "
            "Counts are a fraction of the real ones and the cut means nothing yet. "
            "No proposal was written.",
            "",
        ]
    cal = _calibration_dir(args)

    if token_noise is not None:
        geographic = frozenset(registered.cities) if registered is not None else frozenset()
        unrelated = token_noise.unrelated(related)
        related_hits = {
            t: len(token_noise.hits.get(t, set()) & set(truth)) for t in token_noise.tokens
        }
        rows = []
        approved: dict[str, int | str] = {}
        for token in token_noise.tokens:
            n = len(unrelated[token])
            shape = is_valid_token_shape(token, geographic)
            verdict = shape or ("keep" if n <= args.token_noise_max else "drop: noisy")
            if verdict == "keep":
                approved[token] = proposed[token]
            examples = "; ".join(sorted(unrelated[token])[:3])
            rows.append((token, proposed[token], n, related_hits[token], verdict, examples))
        rows.sort(key=lambda r: (-r[2], r[0]))
        out["tokens"] = [
            {"token": r[0], "group": r[1], "unrelated": r[2], "related": r[3], "verdict": r[4]}
            for r in rows
        ]
        out["token_histogram"] = _histogram([r[2] for r in rows], _NOISE_EDGES)
        lines += [
            "## Brand tokens: unrelated companies whose name carries the token",
            "",
            f"Cut: keep tokens found in at most {args.token_noise_max} unrelated companies. "
            "Single words that are a municipality in the registered addresses of the window "
            f"are geographic ({len(geographic):,} municipalities seen).",
            "",
            "| Unrelated companies | Tokens |",
            "|---|---:|",
            *[f"| {b} | {n} |" for b, n in out["token_histogram"]],  # type: ignore[union-attr]
            "",
            "| Token | Group | Unrelated | Related | Verdict | Examples of unrelated |",
            "|---|---|---:|---:|---|---|",
            *[f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5][:90]} |" for r in rows],
            "",
        ]
        if not info.partial:
            proposal = cal / "tokens.proposal.json"
            proposal.write_text(
                json.dumps(approved, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            lines += [
                f"Proposal ({len(approved)} tokens) written to `{proposal}`. A person "
                "reviews it and saves it as the approved list; nothing uses it before.",
                "",
            ]

    if addresses is not None:
        unrelated = addresses.unrelated(related)
        counts = {label: len(v) for label, v in unrelated.items()}
        out["address_noise"] = counts
        out["address_histogram"] = _histogram(list(counts.values()), _NOISE_EDGES)
        lines += [
            "## Registered addresses of the watched groups: unrelated companies there",
            "",
            f"An address with {args.address_noise_max} or more unrelated companies is degraded "
            "(it no longer decides alone).",
            "",
            "| Unrelated companies | Addresses |",
            "|---|---:|",
            *[f"| {b} | {n} |" for b, n in out["address_histogram"]],  # type: ignore[union-attr]
            "",
            "| Address (street words, number, municipality) | Unrelated | Related | Degraded |",
            "|---|---:|---:|---|",
            *[
                f"| {label} | {n} | {len(addresses.hits.get(label, set()) & set(truth))} | "
                f"{'yes' if n >= args.address_noise_max else 'no'} |"
                for label, n in sorted(counts.items(), key=lambda kv: -kv[1])
            ],
            "",
        ]
        if not info.partial:
            (cal / ADDRESS_NOISE_FILE).write_text(
                json.dumps(counts, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    if registered is not None and args.hq:
        by_group = registered.group_addresses(truth)
        proposal_rows = []
        for group_id, labels in by_group.items():
            for label, n in labels.most_common(3):
                if n >= args.hq_min_members:
                    key = registered.keys[label]
                    unrelated_there = len(registered.cities.get(key.city, set()))
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
                conn.execute("DELETE FROM person_keys")
                conn.executemany(
                    "INSERT INTO person_keys (pair, group_id, n_foreign, calibrated_at)"
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


def discovery_role(raw: str) -> str:
    from borme_radar.officers import canonical_role

    return canonical_role(raw)


def cmd_evaluate(args: argparse.Namespace) -> int:
    conn = _db(args)
    matcher = _matcher(args, conn)
    index = _group_index(args, conn)
    relations = corpus.Relations()
    shared = corpus.SharedOfficers(_officer_names(conn), args.shared_officers)
    hits = corpus.SignalHits(index, pairs_in if args.persons else None)
    info = corpus.scan(
        args.data_dir / "cache",
        args.date_from,
        args.date_to,
        matcher,
        [relations, shared, hits],
    )
    seeds = {c[0]: c[1] for c in conn.execute("SELECT name_norm, group_id FROM companies")}
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


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="borme-radar",
        description="Keep company records in sync with Spain's Companies Register gazette "
        "(BORME) and discover new companies of watched groups.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path("data"), help="cache and database (default: data)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def window(p: argparse.ArgumentParser, required: bool = True) -> None:
        p.add_argument("--from", dest="date_from", type=date.fromisoformat, required=required)
        p.add_argument("--to", dest="date_to", type=date.fromisoformat, required=required)

    def thresholds(p: argparse.ArgumentParser) -> None:
        p.add_argument("--fuzzy-cutoff", type=float, default=FUZZY_CUTOFF)
        p.add_argument("--guard-days", type=int, default=INCORPORATION_GUARD_DAYS)
        p.add_argument("--address-noise-max", type=int, default=discovery.ADDRESS_NOISE_MAX)
        p.add_argument("--bare-min-chars", type=int, default=discovery.BARE_MENTION_MIN_CHARS)
        p.add_argument("--bare-min-words", type=int, default=discovery.BARE_MENTION_MIN_WORDS)

    def sources(p: argparse.ArgumentParser) -> None:
        p.add_argument("--watchlist", type=Path, help="CSV of watched companies (imported first)")
        p.add_argument("--tokens", type=Path, help="APPROVED brand tokens (JSON token -> group)")
        p.add_argument(
            "--persons", action="store_true", help="use the calibrated surname pairs (local only)"
        )

    fetch = sub.add_parser("fetch", help="download, parse and process a date range")
    window(fetch)
    thresholds(fetch)
    sources(fetch)
    fetch.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"parallel downloads, 1-{MAX_WORKERS} (default {MAX_WORKERS})",
    )
    fetch.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="seconds each worker waits between requests (default 1)",
    )
    fetch.add_argument("--download-only", action="store_true", help="only fill the cache")
    fetch.add_argument("--reprocess", action="store_true", help="process days already done")
    fetch.add_argument(
        "--store-all",
        action="store_true",
        help="keep every announcement, not only the relevant ones",
    )
    fetch.set_defaults(func=cmd_fetch)

    rebuild = sub.add_parser("rebuild", help="regenerate all derived data offline, from the cache")
    window(rebuild, required=False)
    thresholds(rebuild)
    sources(rebuild)
    rebuild.add_argument("--store-all", action="store_true")
    rebuild.add_argument(
        "--history-only",
        action="store_true",
        help="only re-derive observations and officer events from stored acts",
    )
    rebuild.set_defaults(func=cmd_rebuild)

    sync = sub.add_parser("sync", help="compare the record with the Registro; --apply writes")
    sync.add_argument("--apply", action="store_true")
    sync.set_defaults(func=cmd_sync)

    relink = sub.add_parser("relink", help="attach stored orphans to known sheets; --apply")
    relink.add_argument("--apply", action="store_true")
    thresholds(relink)
    sources(relink)
    relink.set_defaults(func=cmd_relink)

    prune = sub.add_parser("prune", help="delete stored announcements no longer relevant")
    prune.set_defaults(func=cmd_prune)

    stats = sub.add_parser("stats", help="coverage, publication lag, acts by type and scope")
    stats.set_defaults(func=cmd_stats)

    report = sub.add_parser("report", help="Markdown report of a window")
    window(report)
    report.add_argument("--out", type=Path)
    report.add_argument("--tokens", type=Path)
    report.add_argument("--evaluation", type=Path, help="evaluation JSON for triage weights")
    report.add_argument(
        "--no-details", action="store_true", help="no act text and no personal names (shareable)"
    )
    report.set_defaults(func=cmd_report)

    tri = sub.add_parser("triage", help="order the pending candidates (it decides nothing)")
    tri.add_argument("--tokens", type=Path)
    tri.add_argument("--evaluation", type=Path)
    tri.add_argument("--reference", type=date.fromisoformat, help="date for recency")
    tri.add_argument("--top", type=int, default=30)
    tri.set_defaults(func=cmd_triage)

    cal = sub.add_parser("calibrate", help="measure signals against the cached corpus")
    window(cal)
    thresholds(cal)
    cal.add_argument("--tokens", type=Path, help="PROPOSED brand tokens to measure")
    cal.add_argument("--token-noise-max", type=int, default=discovery.TOKEN_NOISE_MAX)
    cal.add_argument("--addresses", action="store_true", help="noise at the watched addresses")
    cal.add_argument("--hq", action="store_true", help="addresses shared by related companies")
    cal.add_argument("--hq-min-members", type=int, default=2)
    cal.add_argument("--collisions", action="store_true", help="bare names vs person names")
    cal.add_argument("--sample-buckets", type=int, default=10)
    cal.add_argument("--fuzzy", action="store_true", help="similarity bands vs sheet truth")
    cal.add_argument("--names", action="store_true", help="name length distribution")
    cal.add_argument("--roles", action="store_true", help="officer role labels")
    cal.add_argument("--lag", action="store_true", help="incorporation dates")
    cal.add_argument("--renames", action="store_true", help="registry sheet stability")
    cal.add_argument("--person-pairs", action="store_true", help="surname pairs (local only)")
    cal.add_argument("--pair-noise-max", type=int, default=PAIR_NOISE_MAX)
    cal.add_argument("--memory", action="store_true", help="full index vs targeted memory")
    cal.add_argument("--memory-days", type=int, nargs="+", default=[6, 30, 90])
    cal.add_argument("--limit-documents", type=int, help="partial run: never writes proposals")
    cal.add_argument("--shared-officers", type=int, default=corpus.SHARED_OFFICERS_MIN)
    cal.set_defaults(func=cmd_calibrate)

    ev = sub.add_parser("evaluate", help="signals vs explicit relations (ground truth)")
    window(ev)
    thresholds(ev)
    sources(ev)
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
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
