"""Match announcements to watched companies: registry sheet first, the name to start.

The BORME publishes no tax ID. Three levels, deliberately conservative:

1. **Registry sheet** (``(province, sheet)`` learned before): exact. It survives renames
   and every way of writing the name.
2. **Identical normalised name** (or a known alias): accepted, and the sheet of the
   announcement is **learned**, so the company never depends on its spelling again.
3. **Similar name** (``fuzz.ratio`` of the names without legal form, or the same name
   with another legal form): **never matched**. It goes to a review queue.

A foreign act filed under a watched company is worse than a missed act: the first is
shown as true, the second appears on the next run once the sheet is known.

Guards:

* a sheet is not learned from an announcement that closes it (extinction, sheet
  closure): in a merger the absorbing company can take the absorbed company's name, and
  both announcements of the same day then match the same record by name;
* acts published long before the company's incorporation are not attributed to it: a
  homonym that existed earlier would otherwise hand its whole history to the new one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from rapidfuzz import fuzz, process

from borme_radar.normalize import base_and_form, normalize_name

# Both values were derived from 12 months of public gazettes (docs/calibration.md).
# Similar names: no announcement carrying the sheet of a watched company fell in any
# similarity band from 70 up (normalisation and the sheet absorb every true variant);
# at 90 the review queue holds 2 names a year, against 12 at 85, 95 at 80 and 642 at 70,
# all of them other companies.
FUZZY_CUTOFF = 90.0
# Incorporations are inscribed up to 93 days BEFORE the start of operations they
# declare in 1 case out of 1,000 (inscription minus start: p0.1 = -93 days,
# n = 135,798). With ``incorporated`` = the declared start of operations, the company's
# own first acts fall inside the window.
INCORPORATION_GUARD_DAYS = 93
CLOSING_ACTS = frozenset({"extinction", "registry_sheet_closure"})

Via = Literal["sheet", "name", "none"]


@dataclass(frozen=True, slots=True)
class MatchResult:
    company_id: int | None
    via: Via
    suggestion: int | None = None  # review queue: the watched company it resembles
    score: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class WatchIndex:
    by_name: dict[str, int] = field(default_factory=dict)  # normalised name/alias -> id
    by_sheet: dict[tuple[str, str], int] = field(default_factory=dict)
    incorporated: dict[int, date | None] = field(default_factory=dict)
    bases: dict[str, list[str]] = field(default_factory=dict)  # base -> normalised names
    base_list: list[str] = field(default_factory=list)

    def add_name(self, norm: str, company_id: int) -> bool:
        """Register a name or alias; returns False if it already belongs to someone."""
        if not norm or norm in self.by_name:
            return False
        self.by_name[norm] = company_id
        base = base_and_form(norm)[0]
        if base not in self.bases:
            self.base_list.append(base)
        self.bases.setdefault(base, []).append(norm)
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

    def existed(self, company_id: int, when: date | None) -> bool:
        """Could the company have published this act? Unknown dates pass."""
        incorporated = self.index.incorporated.get(company_id)
        if when is None or incorporated is None:
            return True
        return when >= incorporated - timedelta(days=self.guard_days)

    def watched(self, name: str, province: str, sheet: str | None) -> int | None:
        """Sheet or exact name only: the cheap check used to tell watched from foreign."""
        if sheet and (company_id := self.index.by_sheet.get((province, sheet))):
            return company_id
        return self.index.by_name.get(normalize_name(name))

    def match(
        self, name: str, province: str, sheet: str | None, when: date | None = None
    ) -> MatchResult:
        if sheet:
            company_id = self.index.by_sheet.get((province, sheet))
            if company_id is not None and self.existed(company_id, when):
                return MatchResult(company_id, "sheet")
        norm = normalize_name(name)
        company_id = self.index.by_name.get(norm)
        if company_id is not None:
            if self.existed(company_id, when):
                return MatchResult(company_id, "name")
            return MatchResult(
                None, "none", company_id, 100.0, "same name, published before incorporation"
            )
        return self._similar(norm)

    def _similar(self, norm: str) -> MatchResult:
        base, form = base_and_form(norm)
        if not base or not self.index.base_list:
            return MatchResult(None, "none")
        best = process.extractOne(
            base, self.index.base_list, scorer=fuzz.ratio, score_cutoff=self.fuzzy_cutoff
        )
        if best is None:
            return MatchResult(None, "none")
        similar_base, score, _ = best
        watched_norm = self.index.bases[similar_base][0]
        watched_form = base_and_form(watched_norm)[1]
        reason = (
            f"same name, legal form {form or 'none'} vs {watched_form or 'none'}"
            if similar_base == base
            else f"similar name ({score:.1f})"
        )
        return MatchResult(None, "none", self.index.by_name[watched_norm], float(score), reason)

    def sheet_learnable(self, province: str, sheet: str | None, act_types: list[str]) -> bool:
        """Learn a sheet only if unknown and not closed by this very announcement."""
        if not sheet or (province, sheet) in self.index.by_sheet:
            return False
        return not any(t in CLOSING_ACTS for t in act_types)

    def learn_sheet(self, province: str, sheet: str, company_id: int) -> None:
        self.index.by_sheet[(province, sheet)] = company_id
