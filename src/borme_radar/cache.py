"""Permanent on-disk cache for immutable published documents.

A gazette issue never changes once published (corrections are published as new
announcements), so a successful response is cached forever and re-runs never hit the
network for it. Only complete, validated payloads are stored, written atomically.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path


class DiskCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, url: str) -> Path:
        """Readable, collision-free file name: sanitised URL tail plus a short hash."""
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        tail = re.sub(r"[^A-Za-z0-9._-]+", "_", url.rsplit("/", 1)[-1])[-60:]
        return self.root / digest[:2] / f"{tail}.{digest}"

    def get(self, url: str) -> bytes | None:
        path = self.path_for(url)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def put(self, url: str, payload: bytes) -> None:
        path = self.path_for(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file in the same directory and rename: a crash never
        # leaves a half-written entry that later runs would trust.
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
