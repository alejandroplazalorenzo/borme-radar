"""Triage of the open discovery leads: a score that orders the review, and three tiers.

The score of a lead is the product of three parts:

* evidence: the precision of the signal that found it, as ``evaluate`` measured it
  against the broader ground truth (relations or shared officers). A brand token of its
  group in its name, when the token is not already the signal, counts as a second clue
  of its own: the two combine as 1 - (1 - p)(1 - q). Without an evaluation the signals
  count by their place in ``SIGNAL_ORDER``: 1, 1/2, 1/3, 1/4;
* freshness: one half for every ``HALF_LIFE_DAYS`` since its last announcement was
  published. The day of publication is used, not the inscription day the announcement
  prints, which is sometimes misprinted (three announcements of the history give a year
  after 2090);
* activity: log2(1 + announcements), so each further announcement adds less.

The tiers cut the ordered list by shares (``TIER_SHARES``): the first fifth, the next
three tenths, and the rest, where a lead with a score of zero always goes. The score only
orders the leads; none is accepted or dropped by it.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from borme_radar.discovery import SIGNAL_ORDER, name_grams

# Both are choices, not measurements: how fast a lead goes stale, and how much of the
# list each tier takes.
HALF_LIFE_DAYS = 180
TIER_SHARES = (0.2, 0.3)
TIERS = ("first", "next", "rest")
# The ground truth of `evaluate` the precisions are read for: relations alone cover too
# few companies of a group, and against them tokens and addresses would look useless.
PRECISION_TRUTH = "relations_or_shared_officers"


@dataclass(frozen=True, slots=True)
class Scored:
    lead_id: int
    group: str
    company_name: str
    province: str
    reason: str
    evidence: str
    n_announcements: int
    first_date: str
    last_date: str
    brand_in_name: bool
    score: float
    tier: str = ""


def signal_precision(path: Path | None) -> dict[str, float]:
    """The precision of each signal from the file ``evaluate`` writes; a signal the file
    does not rate, or every signal without a file, counts by its place in the order."""
    precision = {signal: 1 / place for place, signal in enumerate(SIGNAL_ORDER, 1)}
    if path is None or not path.exists():
        return precision
    for row in json.loads(path.read_text(encoding="utf-8")).get("signals", []):
        rated = row.get("precision") is not None and row["reason"] in precision
        if rated and row.get("truth", PRECISION_TRUTH) == PRECISION_TRUTH:
            precision[row["reason"]] = float(row["precision"])
    return precision


def lead_score(
    reason: str,
    n_announcements: int,
    last_date: date,
    today: date,
    brand_in_name: bool,
    precision: dict[str, float],
) -> float:
    """The score of one lead (see the module docstring)."""
    doubt = 1 - precision.get(reason, 0.0)
    if brand_in_name and reason != "token":
        doubt *= 1 - precision.get("token", 0.0)
    freshness = 0.5 ** (max((today - last_date).days, 0) / HALF_LIFE_DAYS)
    return round((1 - doubt) * freshness * math.log2(1 + n_announcements), 4)


def tiers(count: int) -> list[str]:
    """The tier of each place of an ordered list of ``count`` leads."""
    first = round(count * TIER_SHARES[0])
    second = first + round(count * TIER_SHARES[1])
    return [TIERS[0] if i < first else TIERS[1] if i < second else TIERS[2] for i in range(count)]


# The open leads with the first and last days their announcements were published (the
# lead's own days when none of them is kept any more).
_OPEN_LEADS = """
    WITH published AS (
        SELECT lead_id, MIN(day) AS since, MAX(day) AS until FROM notices
        WHERE lead_id > 0 GROUP BY lead_id
    )
    SELECT leads.*, IFNULL(since, first_day) AS first_published,
           IFNULL(until, last_day) AS last_published
    FROM leads LEFT JOIN published USING (lead_id)
    WHERE state = 'open'
"""


def triage(
    conn: sqlite3.Connection,
    tokens: dict[str, int],
    today: date,
    precision: dict[str, float],
) -> list[Scored]:
    """The open leads, best first, each with its score and tier."""
    labels = dict(conn.execute("SELECT group_id, label FROM watch_groups").fetchall())
    found: list[Scored] = []
    for lead in conn.execute(_OPEN_LEADS).fetchall():
        grams = set(name_grams(lead["firm"]))
        brand = any(g == lead["group_id"] and token in grams for token, g in tokens.items())
        points = lead_score(
            lead["found_by"],
            lead["notice_count"],
            date.fromisoformat(lead["last_published"]),
            today,
            brand,
            precision,
        )
        found.append(
            Scored(lead["lead_id"], labels[lead["group_id"]], lead["firm"], lead["province"],
                   lead["found_by"], lead["evidence"], lead["notice_count"],
                   lead["first_published"], lead["last_published"], brand, points)
        )  # fmt: skip
    found.sort(key=lambda s: (-s.score, s.group, s.company_name))
    # A lead without any evidence (its signal measured at zero precision) is never above
    # the last tier, wherever its place falls.
    return [
        replace(s, tier=tier if s.score > 0 else TIERS[-1])
        for s, tier in zip(found, tiers(len(found)), strict=True)
    ]
