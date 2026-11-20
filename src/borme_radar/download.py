"""Download one day, or a range of days, of the gazette through the polite client.

The documents of a day are fetched in parallel, on at most ``MAX_WORKERS`` threads.
Errors are not raised: each day comes back with a status. A day where any document
failed is ``failed`` and keeps the error; asking for it again later downloads only what
is missing, since what did arrive is already in the cache.
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

# What became of a day. no_gazette: the summary is a 404; not_cached: offline, not in the cache.
DayStatus = Literal["published", "no_gazette", "failed", "not_cached"]


@dataclass(slots=True)
class DayDownload:
    day: date
    status: DayStatus
    refs: list[DocumentRef] = field(default_factory=list)
    payloads: dict[str, bytes] = field(default_factory=dict)  # document_id -> XML
    error: str | None = None


def days_between(start: date, end: date) -> list[date]:
    """Every day from start to end, both included."""
    span = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span + 1)]


class Downloader:
    """Fetches days of the gazette. Use it as a context manager to close its threads."""

    def __init__(self, source: BormeSource, workers: int = MAX_WORKERS) -> None:
        if workers < 1 or workers > MAX_WORKERS:
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

    def _fetch(self, ref: DocumentRef) -> bytes | str:
        """XML of one document, or its error as text: one failure must not stop the others."""
        try:
            return self.source.document_xml(ref)
        except HttpError as exc:
            return str(exc)

    def day(self, day: date) -> DayDownload:
        """Summary and documents of one day; at most the first three errors are reported."""
        try:
            refs = self.source.documents_for(day)
        except NotCached:
            return DayDownload(day, "not_cached")
        except HttpError as exc:
            return DayDownload(day, "failed", error=f"summary: {exc}")
        if refs is None:
            return DayDownload(day, "no_gazette")
        fetch = self._pool.map if self._pool is not None else map
        got = list(zip(refs, fetch(self._fetch, refs), strict=True))
        payloads = {ref.document_id: body for ref, body in got if isinstance(body, bytes)}
        errors = [f"{ref.document_id}: {body}" for ref, body in got if isinstance(body, str)]
        status: DayStatus = "failed" if errors else "published"
        return DayDownload(day, status, refs, payloads, "; ".join(errors[:3]) or None)

    def days(self, start: date, end: date) -> Iterator[DayDownload]:
        """The days from start to end in order, each downloaded when it is consumed."""
        return map(self.day, days_between(start, end))
