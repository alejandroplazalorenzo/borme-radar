"""Permanent disk cache of downloaded responses, keyed by URL.

An entry is never refreshed: it is trusted forever, so only validated, unmodified
responses are put in. Writes are atomic, so parallel downloads and interrupted runs
never leave half a file behind.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TAIL_CHARS = 60


@dataclass(frozen=True)
class DiskCache:
    root: Path

    def path_for(self, url: str) -> Path:
        """Where a URL is stored: the readable tail of the URL plus a short hash of it.

        Entries are spread over subfolders named after the first two characters of the hash.
        """
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        readable = _UNSAFE_RE.sub("_", url[url.rfind("/") + 1 :])[-_TAIL_CHARS:]
        return self.root / digest[:2] / f"{readable}.{digest}"

    def get(self, url: str) -> bytes | None:
        entry = self.path_for(url)
        return entry.read_bytes() if entry.is_file() else None

    def put(self, url: str, payload: bytes) -> None:
        """Write atomically: a temporary file private to this thread, renamed over the entry."""
        entry = self.path_for(url)
        os.makedirs(entry.parent, exist_ok=True)
        partial = entry.with_name(f".tmp-{os.getpid()}-{threading.get_ident()}-{entry.name}")
        try:
            partial.write_bytes(payload)
            partial.replace(entry)
        finally:
            partial.unlink(missing_ok=True)
