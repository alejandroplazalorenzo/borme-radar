"""Download one gazette day: the summary, then its documents with up to 4 workers.

A day is the unit of failure. If the summary or any document of the day cannot be
downloaded (retries exhausted, or an offline run without it in the cache), the whole
day is reported as failed and skipped; the run goes on with the next day and lists the
failed ones at the end. Documents already downloaded stay in the permanent cache, so
running the same range again only asks the network for what is missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from borme_radar.httpclient import MAX_WORKERS, HttpError, NotCached
from borme_radar.models import DocumentRef
from borme_radar.source import BormeSource

# "not_cached": offline run and the day's summary is not in the cache (a day without
# gazette, whose 404 is never cached, or a day that was never downloaded).
DayStatus = Literal["published", "no_gazette", "failed", "not_cached"]


@dataclass(slots=True)
class DayDownload:
    day: date
    status: DayStatus
    refs: list[DocumentRef] = field(default_factory=list)
    payloads: dict[str, bytes] = field(default_factory=dict)  # document_id -> XML
    error: str | None = None


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


class Downloader:
    """Fetches gazette days; documents of a day are downloaded in parallel."""

    def __init__(self, source: BormeSource, workers: int = MAX_WORKERS) -> None:
        if not 1 <= workers <= MAX_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
        self.source = source
        self.workers = workers
        self._pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    def __enter__(self) -> Downloader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _document(self, ref: DocumentRef) -> tuple[DocumentRef, bytes | None, str | None]:
        try:
            return ref, self.source.document_xml(ref), None
        except HttpError as exc:
            return ref, None, str(exc)

    def day(self, day: date) -> DayDownload:
        try:
            refs = self.source.documents_for(day)
        except NotCached:
            return DayDownload(day, "not_cached")
        except HttpError as exc:
            return DayDownload(day, "failed", error=f"summary: {exc}")
        if refs is None:
            return DayDownload(day, "no_gazette")
        results = (
            self._pool.map(self._document, refs)
            if self._pool is not None
            else map(self._document, refs)
        )
        payloads: dict[str, bytes] = {}
        errors: list[str] = []
        for ref, payload, error in results:
            if payload is None:
                errors.append(f"{ref.document_id}: {error}")
            else:
                payloads[ref.document_id] = payload
        if errors:
            return DayDownload(day, "failed", refs, payloads, "; ".join(errors[:3]))
        return DayDownload(day, "published", refs, payloads)

    def days(self, start: date, end: date) -> Iterator[DayDownload]:
        for day in days_between(start, end):
            yield self.day(day)
