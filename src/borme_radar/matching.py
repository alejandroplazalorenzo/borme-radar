"""Match an announcement to a watched company.

The registry sheet is the strongest key: ``(province, sheet)`` of a watched company, from
the watchlist or learned since. Without it, the normalised name has to be identical to a
watched name or to an alias kept from a rename. Both are checked against the homonym
guard: an act dated more than ``INCORPORATION_GUARD_DAYS`` before the company was
incorporated is another company's, so a sheet owner that is too young falls back to the
name, and a name that is too young only becomes a suggestion.

A name that is merely close to a watched one is never a match. When the best watched
name without its legal form scores ``FUZZY_CUTOFF`` or more, or it is the same name with
another legal form, the announcement waits for a person in the review queue.

A match by name teaches the pipeline the sheet the announcement prints, unless the
sheet is known already or the announcement ends it (:meth:`Matcher.can_learn`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from rapidfuzz import fuzz, process

from borme_radar.normalize import base_and_form, normalize_name

# Similarity (fuzz.ratio, without legal form) from which a name goes to the review queue.
# The value comes from the production system and was kept; `calibrate --fuzzy` counts the
# names of a year in each band around it (docs/calibration.md).
FUZZY_CUTOFF = 90.0
# Homonym guard: 99.9% of companies are inscribed no more than 93 days before the start
# of operations they declare (docs/calibration.md); older acts are another company's.
INCORPORATION_GUARD_DAYS = 93
# Acts after which a sheet is not taught to the pipeline: the sheet ends with them.
_SHEET_ENDS = frozenset({"registry_sheet_closure", "extinction"})

Via = Literal["sheet", "name", "none"]


@dataclass(frozen=True, slots=True)
class MatchResult:
    company_id: int | None
    via: Via
    suggestion: int | None = None  # review queue: the watched company it resembles
    score: float = 0.0
    reason: str = ""


NO_MATCH = MatchResult(None, "none")


@dataclass(slots=True)
class WatchIndex:
    by_name: dict[str, int] = field(default_factory=dict)  # normalised name/alias -> id
    by_sheet: dict[tuple[str, str], int] = field(default_factory=dict)
    incorporated: dict[int, date | None] = field(default_factory=dict)
    bases: dict[str, list[str]] = field(default_factory=dict)  # base -> normalised names
    base_list: list[str] = field(default_factory=list)

    def add_name(self, norm: str, company_id: int) -> bool:
        """Index a normalised name or alias of a company; False if empty or already taken.

        Its base without legal form is indexed too, for the similarity search.
        """
        if not norm or norm in self.by_name:
            return False
        self.by_name[norm] = company_id
        base, _form = base_and_form(norm)
        names = self.bases.setdefault(base, [])
        if not names:
            self.base_list.append(base)
        names.append(norm)
        return True


class Matcher:
    def __init__(
        self,
        index: WatchIndex,
        fuzzy_cutoff: float = FUZZY_CUTOFF,
        guard_days: int = INCORPORATION_GUARD_DAYS,
    ) -> None:
        self.index = index
        self.fuzzy_cutoff = fuzzy_cutoff
        self.guard_days = guard_days

    def too_early(self, company_id: int, when: date | None) -> bool:
        """Is ``when`` more than ``guard_days`` before the company was incorporated?"""
        born = self.index.incorporated.get(company_id)
        return when is not None and born is not None and (born - when).days > self.guard_days

    def watched(self, name: str, province: str, sheet: str | None) -> int | None:
        """Watched company by sheet, then by identical name; no guard and no suggestion."""
        by_sheet = self.index.by_sheet.get((province, sheet)) if sheet else None
        return by_sheet or self.index.by_name.get(normalize_name(name))

    def exact(
        self, name: str, province: str, sheet: str | None, when: date | None = None
    ) -> MatchResult:
        """The sheet and the identical name or alias only: no similarity search."""
        return self._exact(normalize_name(name), province, sheet, when)

    def _exact(self, norm: str, province: str, sheet: str | None, when: date | None) -> MatchResult:
        owner = self.index.by_sheet.get((province, sheet)) if sheet else None
        if owner is not None and not self.too_early(owner, when):
            return MatchResult(owner, "sheet")
        named = self.index.by_name.get(norm)
        if named is None:
            return NO_MATCH
        if not self.too_early(named, when):
            return MatchResult(named, "name")
        return MatchResult(None, "none", named, 100.0, "same name, published before incorporation")

    def match(
        self, name: str, province: str, sheet: str | None, when: date | None = None
    ) -> MatchResult:
        norm = normalize_name(name)
        found = self._exact(norm, province, sheet, when)
        if found.company_id is None and found.suggestion is None:
            return self._similar(norm)
        return found

    def _similar(self, norm: str) -> MatchResult:
        """The closest watched name, as a suggestion with its score and reason."""
        base, form = base_and_form(norm)
        candidates = self.index.base_list
        best = (
            process.extractOne(base, candidates, scorer=fuzz.ratio, score_cutoff=self.fuzzy_cutoff)
            if base and candidates
            else None
        )
        if best is None:
            return NO_MATCH
        close, score = best[0], float(best[1])
        watched_norm = self.index.bases[close][0]
        if close == base:
            other = base_and_form(watched_norm)[1]
            why = f"same name, legal form {form or 'none'} vs {other or 'none'}"
        else:
            why = f"similar name ({score:.1f})"
        return MatchResult(None, "none", self.index.by_name[watched_norm], score, why)

    def can_learn(self, province: str, sheet: str | None, act_types: list[str]) -> bool:
        """May a match by name teach this sheet? Not when it has an owner already, and not
        when one of the acts ends it."""
        if not sheet or (province, sheet) in self.index.by_sheet:
            return False
        return not _SHEET_ENDS.intersection(act_types)

    def learn_sheet(self, province: str, sheet: str, company_id: int) -> None:
        self.index.by_sheet[(province, sheet)] = company_id
