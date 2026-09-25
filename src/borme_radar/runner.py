"""Run a date range: download (or read the cache), parse, process, one day at a time.

A day is processed only when all its documents are available; otherwise it is recorded
as failed, listed at the end and skipped. Running the same range again retries only
what is missing (days already processed are skipped unless ``reprocess``).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from borme_radar import store
from borme_radar.download import Downloader, days_between
from borme_radar.parser import ParseError, parse_document
from borme_radar.pipeline import Pipeline

log = logging.getLogger("borme_radar")


@dataclass(slots=True)
class RunReport:
    counters: Counter[str] = field(default_factory=Counter)
    failed: list[tuple[date, str]] = field(default_factory=list)
    verdicts: Counter[str] = field(default_factory=Counter)
    elapsed: float = 0.0


def run_range(
    conn: sqlite3.Connection,
    downloader: Downloader,
    pipeline: Pipeline,
    start: date,
    end: date,
    *,
    reprocess: bool = False,
    download_only: bool = False,
) -> RunReport:
    report = RunReport()
    started = time.perf_counter()
    done = set() if reprocess else set(store.days_with_status(conn, "published"))
    c = report.counters
    for day in days_between(start, end):
        c["days_checked"] += 1
        if day in done and not download_only:
            c["days_skipped_done"] += 1
            continue
        result = downloader.day(day)
        if result.status == "not_cached":
            c["days_not_cached"] += 1
            continue
        if result.status == "no_gazette":
            c["days_no_gazette"] += 1
            store.record_day(conn, day, "no_gazette")
            continue
        if result.status == "failed":
            c["days_failed"] += 1
            report.failed.append((day, result.error or "unknown error"))
            store.record_day(conn, day, "failed", len(result.refs), result.error)
            log.warning("%s: FAILED, skipped (%s)", day, result.error)
            continue
        if download_only:
            c["days_published"] += 1
            c["documents"] += len(result.payloads)
            continue
        try:
            for ref in result.refs:
                doc = parse_document(result.payloads[ref.document_id])
                c.update(pipeline.process_document(ref, doc))
        except ParseError as exc:
            c["days_failed"] += 1
            report.failed.append((day, f"parse error: {exc}"))
            store.record_day(conn, day, "failed", len(result.refs), f"parse error: {exc}")
            log.warning("%s: FAILED, skipped (parse error: %s)", day, exc)
            continue
        c["days_published"] += 1
        store.record_day(conn, day, "published", len(result.refs))
        log.info(
            "%s: %d documents (run so far: %d announcements read, %d kept)",
            day,
            len(result.refs),
            c["announcements"],
            c["watched"] + c["review"] + c["candidate"] + c["all"],
        )
    if not download_only:
        report.verdicts = pipeline.rejudge()
    report.elapsed = time.perf_counter() - started
    return report
