"""Command line interface: ``fetch``, ``alerts`` and ``stats``."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from borme_radar import store
from borme_radar.acts import ACT_TYPES
from borme_radar.cache import DiskCache
from borme_radar.httpclient import HttpError, PoliteClient
from borme_radar.matching import DEFAULT_THRESHOLD, load_watchlist, match_names
from borme_radar.parser import ParseError, parse_document
from borme_radar.report import render_alerts
from borme_radar.source import BormeSource

log = logging.getLogger("borme_radar")


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def cmd_fetch(args: argparse.Namespace) -> int:
    if args.date_from > args.date_to:
        raise SystemExit("--from must not be after --to")
    data_dir: Path = args.data_dir
    conn = store.connect(data_dir / "borme.sqlite")
    started = time.perf_counter()
    totals = {"days": 0, "no_gazette": 0, "documents": 0, "announcements": 0, "acts": 0}
    with PoliteClient(cache=DiskCache(data_dir / "cache"), min_interval=args.delay) as client:
        source = BormeSource(client)
        for day in _days(args.date_from, args.date_to):
            totals["days"] += 1
            refs = source.documents_for(day)
            if refs is None:
                totals["no_gazette"] += 1
                store.record_day(conn, day, published=False, n_documents=0)
                log.info("%s: no gazette", day)
                continue
            n_ann = n_acts = 0
            for ref in refs:
                doc = parse_document(source.document_xml(ref))
                store.load_document(conn, doc, ref.url_xml)
                n_ann += len(doc.announcements)
                n_acts += sum(len(a.acts) for a in doc.announcements)
            # Recorded only after every document of the day is stored, so an
            # interrupted run is visible and a re-run completes it from the cache.
            store.record_day(conn, day, published=True, n_documents=len(refs))
            totals["documents"] += len(refs)
            totals["announcements"] += n_ann
            totals["acts"] += n_acts
            log.info("%s: %d documents, %d announcements, %d acts", day, len(refs), n_ann, n_acts)
        stats = client.stats
    elapsed = time.perf_counter() - started
    print(
        f"days checked: {totals['days']} (without gazette: {totals['no_gazette']})\n"
        f"documents: {totals['documents']}  announcements: {totals['announcements']}  "
        f"acts: {totals['acts']}\n"
        f"http: network requests {stats.network_requests}, cache hits {stats.cache_hits}, "
        f"retries {stats.retries}, 404 {stats.not_found}\n"
        f"elapsed: {elapsed:.1f}s"
    )
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    conn = store.connect(args.data_dir / "borme.sqlite")
    watchlist = load_watchlist(args.watchlist)
    matches = match_names(watchlist, store.company_names(conn, args.since), args.threshold)
    announcements = {
        m.company_norm: store.announcements_for(conn, [m.company_norm], args.since) for m in matches
    }
    report = render_alerts(
        watchlist,
        matches,
        announcements,
        since=args.since,
        threshold=args.threshold,
        details=not args.no_details,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        confirmed = sum(1 for m in matches if m.status == "confirmed")
        print(f"wrote {args.out}: {confirmed} confirmed, {len(matches) - confirmed} needs review")
    else:
        sys.stdout.write(report)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = store.connect(args.data_dir / "borme.sqlite")
    s = store.stats(conn)
    print(f"gazette days: {s.first_day} .. {s.last_day}")
    print(f"days with gazette: {s.days_published}, without: {s.days_without_gazette}")
    print(f"documents: {s.documents}")
    print(f"announcements: {s.announcements}")
    print(f"acts: {s.acts}")
    print(
        f"announcements with text before the first known heading: "
        f"{s.announcements_with_unparsed_prefix}"
    )
    print(f"announcements without any recognised act: {s.announcements_without_acts}")
    if s.lag_days:
        print(
            f"days from inscription to publication: median {s.lag_median:g}, "
            f"p10 {s.lag_quantile(0.10):g}, p90 {s.lag_quantile(0.90):g} "
            f"(n={len(s.lag_days)})"
        )
    print("\nacts by type:")
    print("| Act type | Code | Count |")
    print("|---|---|---:|")
    for code, count in s.acts_by_type:
        label = ACT_TYPES[code].label if code in ACT_TYPES else code
        print(f"| {label} | `{code}` | {count} |")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="borme-radar", description="Monitor the BORME (Section A) for a watchlist."
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path("data"), help="cache and database (default: data)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download, parse and store gazettes for a date range")
    fetch.add_argument("--from", dest="date_from", type=date.fromisoformat, required=True)
    fetch.add_argument("--to", dest="date_to", type=date.fromisoformat, required=True)
    fetch.add_argument(
        "--delay", type=float, default=1.0, help="seconds between network requests (default 1)"
    )
    fetch.set_defaults(func=cmd_fetch)

    alerts = sub.add_parser("alerts", help="report stored acts of watchlist companies")
    alerts.add_argument("--watchlist", type=Path, required=True, help="CSV with a 'name' column")
    alerts.add_argument("--since", type=date.fromisoformat, help="only gazettes from this date")
    alerts.add_argument("--out", type=Path, help="write the Markdown report to this file")
    alerts.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"fuzzy similarity for 'needs review' (default {DEFAULT_THRESHOLD:g})",
    )
    alerts.add_argument(
        "--no-details", action="store_true", help="omit act text (it contains personal names)"
    )
    alerts.set_defaults(func=cmd_alerts)

    stats = sub.add_parser("stats", help="summary of the stored data")
    stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    try:
        return int(args.func(args))
    except (HttpError, ParseError, ValueError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
