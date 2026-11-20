"""Run the pipeline over a range of days, and sweep a long history in passes.

:func:`run_range` goes day by day: download, parse, store what is kept and record the
outcome of the day in ``issue_days``. Days already processed are skipped unless asked to
reprocess. The outcome ``failed`` (a download that did not complete, or a document the
parser refused) is the only one that does not count as done: the run goes on, prints
those days at the end, and the next run over the same range does them again.

:func:`sweep` processes a long history again one calendar year at a time, most recent
year first by default, in passes. The registry sheet of a watched company is learned
from an announcement under its current name; once the sheet is known, older
announcements match by sheet whatever the company was called then, and a rename
(published under the old name) leaves an alias. Inside a year the days still run in
date order, so what was published before a sheet or alias was learned is picked up by
the next pass. Pass 1 matches and discovers; the later passes look only at exact
matches and at the sheets of candidates found by mention. The sweep stops after a later
pass that adds nothing, or at ``max_passes``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from borme_radar import positions, store
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
    rejudge: bool = True,
) -> RunReport:
    """Process every day from ``start`` to ``end`` and return what was counted.

    Days already published are skipped unless ``reprocess``; ``download_only`` only fills
    the cache. Afterwards the current officer positions are recomputed and, unless
    ``rejudge`` is False, every candidate is judged again.
    """
    report = RunReport()
    clock = time.perf_counter()
    done = frozenset() if reprocess else frozenset(store.days_with_status(conn, "published"))
    seen = report.counters
    for day in days_between(start, end):
        seen["days_checked"] += 1
        if not download_only and day in done:
            seen["days_skipped_done"] += 1
            continue
        got = downloader.day(day)
        if got.status in ("not_cached", "no_gazette"):
            seen[f"days_{got.status}"] += 1
            if got.status == "no_gazette":
                store.record_day(conn, day, "no_gazette")
            continue
        why: str | None = None
        if got.status == "failed":
            why = got.error or "unknown error"
        elif download_only:
            seen["days_published"] += 1
            seen["documents"] += len(got.payloads)
            continue
        else:
            try:
                # One document the parser refuses marks its day failed; the documents of
                # that day stored before it are stored again on the next run (upserts).
                for ref in got.refs:
                    doc = parse_document(got.payloads[ref.document_id])
                    seen.update(pipeline.process_document(ref, doc))
            except ParseError as exc:
                why = f"parse error: {exc}"
        if why is not None:
            # Not done: the outcome `failed` keeps the day out of `done` next time.
            seen["days_failed"] += 1
            report.failed.append((day, why))
            store.record_day(conn, day, "failed", len(got.refs), why)
            log.warning("%s skipped: %s", day, why)
            continue
        seen["days_published"] += 1
        store.record_day(conn, day, "published", len(got.refs))
        kept = sum(seen[k] for k in ("watched", "review", "candidate", "all"))
        log.info(
            "%s read: %d documents; so far %d announcements seen and %d kept",
            day,
            len(got.refs),
            seen["announcements"],
            kept,
        )
    if not download_only:
        positions.refresh(conn)
        if rejudge:
            report.verdicts = pipeline.rejudge()
    report.elapsed = time.perf_counter() - clock
    return report


# ------------------------------------------------------------------ history sweep

ORDERS = ("recent-first", "chronological")


@dataclass(slots=True)
class PassReport:
    number: int
    mode: str  # "full" (match + discovery) or "known-only"
    seconds: float
    counters: Counter[str]
    before: dict[str, int]
    after: dict[str, int]
    failed: list[tuple[date, str]] = field(default_factory=list)
    verdicts: Counter[str] = field(default_factory=Counter)

    def gained(self, key: str) -> int:
        return self.after[key] - self.before[key]

    @property
    def added_anything(self) -> bool:
        """Did it learn a sheet or alias, or link announcements to companies or candidates?"""
        return any(map(self.gained, ("sheets", "aliases", "watched", "candidate_announcements")))


def year_ranges(start: date, end: date, order: str) -> list[tuple[date, date]]:
    """The calendar years from start to end, clipped to the range, in the given order."""
    if order not in ORDERS:
        raise ValueError(f"order must be one of {ORDERS}")
    years = range(start.year, end.year + 1)
    if order == "recent-first":
        years = years[::-1]
    return [(max(start, date(y, 1, 1)), min(end, date(y, 12, 31))) for y in years]


# What a sweep compares between passes: each number counts these rows.
_SWEEP_COUNTS = {
    "sheets": "sheet_owner WHERE how = 'name_match'",
    "aliases": "other_names WHERE added_by = 'name_change'",
    "watched": "notices WHERE company_id IS NOT NULL",
    "candidates": "leads WHERE state = 'open'",
    "candidate_announcements": "notices WHERE lead_id IS NOT NULL",
    "observations": "seen_values WHERE source = 'registry'",
    "officer_events": "role_changes",
}


def sweep_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        label: store._scalar(conn, "SELECT COUNT(*) FROM " + rows)
        for label, rows in _SWEEP_COUNTS.items()
    }


def sweep(
    conn: sqlite3.Connection,
    downloader: Downloader,
    make_pipeline: Callable[[bool], Pipeline],
    start: date,
    end: date,
    *,
    order: str = "recent-first",
    max_passes: int = 3,
) -> list[PassReport]:
    """Process ``start`` to ``end`` again, year by year, in passes; one report per pass.

    ``make_pipeline(later)`` builds the pipeline of a pass: ``later`` is True from the
    second pass on (exact matches and followed sheets only). Candidates are judged again
    once per pass, after its last year.
    """
    passes: list[PassReport] = []
    for number in range(1, max_passes + 1):
        later = number > 1
        pipeline = make_pipeline(later)
        before = sweep_counts(conn)
        clock = time.perf_counter()
        report = PassReport(number, "known-only" if later else "full", 0.0, Counter(), before, {})
        for first, last in year_ranges(start, end, order):
            part = run_range(conn, downloader, pipeline, first, last, reprocess=True, rejudge=False)
            report.counters.update(part.counters)
            report.failed += part.failed
            log.info(
                "after pass %d over %s..%s the counts are %s",
                number,
                first,
                last,
                sweep_counts(conn),
            )
        report.verdicts = pipeline.rejudge()
        report.seconds = time.perf_counter() - clock
        report.after = sweep_counts(conn)
        passes.append(report)
        if later and not report.added_anything:
            break
    return passes
