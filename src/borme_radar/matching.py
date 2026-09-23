"""Watchlist matching: exact on normalised names, fuzzy only as "needs review".

A registry alert triggers work (calling a client, freezing credit, reviewing a
supplier), so a false positive is expensive. Only an exact match on the normalised name
is reported as confirmed. Fuzzy similarity is used to surface possible misspellings or
legal-form mismatches for a human to check; it is never promoted to confirmed.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rapidfuzz import fuzz, process

from borme_radar.normalize import base_and_form, normalize_name

DEFAULT_THRESHOLD = 90.0

MatchStatus = Literal["confirmed", "needs_review"]


@dataclass(frozen=True, slots=True)
class WatchEntry:
    name: str
    norm: str


@dataclass(frozen=True, slots=True)
class Match:
    watch: WatchEntry
    company_norm: str
    company_name: str  # one published spelling
    status: MatchStatus
    score: float  # 100 for confirmed; similarity of the base names otherwise
    reason: str


def load_watchlist(path: Path) -> list[WatchEntry]:
    """Read a CSV with a ``name`` column; duplicates (after normalisation) are dropped."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "name" not in reader.fieldnames:
            raise ValueError(f"{path}: the watchlist needs a 'name' column")
        entries: dict[str, WatchEntry] = {}
        for row in reader:
            name = (row.get("name") or "").strip()
            if name:
                norm = normalize_name(name)
                entries.setdefault(norm, WatchEntry(name, norm))
    return list(entries.values())


def match_names(
    watchlist: list[WatchEntry],
    candidates: Mapping[str, str],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Match]:
    """Match watchlist entries against ``{normalised name: published name}``.

    * ``confirmed``: identical normalised name (base name and legal form).
    * ``needs_review``: base names (legal form removed) with ``fuzz.ratio >= threshold``,
      including an identical base name under a different legal form.

    ``fuzz.ratio`` is used instead of token-set scorers on purpose: a token-set score
    rates ``TELEFONICA SA`` vs ``TELEFONICA DE ESPANA SA`` as 100, i.e. it would
    flag every subsidiary of a watched parent.
    """
    by_base: dict[str, list[str]] = {}
    for norm in candidates:
        by_base.setdefault(base_and_form(norm)[0], []).append(norm)
    bases = list(by_base)

    matches: list[Match] = []
    for entry in watchlist:
        if entry.norm in candidates:
            matches.append(
                Match(entry, entry.norm, candidates[entry.norm], "confirmed", 100.0, "exact")
            )
        watch_base, watch_form = base_and_form(entry.norm)
        similar = process.extract(
            watch_base, bases, scorer=fuzz.ratio, score_cutoff=threshold, limit=None
        )
        for base, score, _ in similar:
            for norm in by_base[base]:
                if norm == entry.norm:
                    continue
                form = base_and_form(norm)[1]
                reason = (
                    f"same name, legal form {form or 'none'} vs {watch_form or 'none'}"
                    if base == watch_base
                    else f"similar name ({score:.1f})"
                )
                matches.append(
                    Match(entry, norm, candidates[norm], "needs_review", float(score), reason)
                )
    matches.sort(key=lambda m: (m.status != "confirmed", -m.score, m.watch.name, m.company_norm))
    return matches
