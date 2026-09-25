"""Order the discovery candidates so that a person can decide them. It decides nothing.

The score adds four things that can be measured:

1. **which signal found it**, weighted by that signal's measured precision (from the
   last ``evaluate`` run; without one, by the fixed signal order);
2. **whether the company is active**: number of announcements, with a ceiling;
3. **recency**: last announcement within 90 or 365 days of the reference date;
4. **the group's brand in the name** (an approved token of the same group).

Tiers are cut-offs on that score: "review first", "worth a look", "long tail".
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from borme_radar.discovery import SIGNAL_ORDER, name_grams

# Without a measured precision, the signal order decides (mention strongest).
DEFAULT_SIGNAL_WEIGHT = {"mention": 50.0, "address": 35.0, "token": 20.0, "person": 10.0}
WEIGHT_TRUTH = "relations_or_shared_officers"
TIER_FIRST = 70.0
TIER_LOOK = 45.0


@dataclass(frozen=True, slots=True)
class Scored:
    score: float
    tier: str
    candidate_id: int
    group: str
    company_name: str
    province: str
    reason: str
    evidence: str
    n_announcements: int
    first_date: str
    last_date: str
    brand_in_name: bool


def signal_weights(path: Path | None) -> dict[str, float]:
    """50 x measured precision (lower bound) per signal, from ``evaluate`` output."""
    if path is None or not path.exists():
        return dict(DEFAULT_SIGNAL_WEIGHT)
    data = json.loads(path.read_text(encoding="utf-8"))
    weights = dict(DEFAULT_SIGNAL_WEIGHT)
    for row in data.get("signals", []):
        # The broader ground truth: against relations alone, a real subsidiary that
        # published no relation in the window counts as a false positive.
        if row.get("truth", WEIGHT_TRUTH) != WEIGHT_TRUTH:
            continue
        if row["reason"] in SIGNAL_ORDER and row.get("precision") is not None:
            weights[row["reason"]] = round(50.0 * float(row["precision"]), 1)
    return weights


def score_one(
    reason: str,
    n_announcements: int,
    last_date: date,
    reference: date,
    brand_in_name: bool,
    weights: dict[str, float],
) -> float:
    points = weights.get(reason, 0.0)
    points += 2.0 * min(n_announcements, 10)
    age = (reference - last_date).days
    points += 20.0 if age <= 90 else 10.0 if age <= 365 else 0.0
    points += 25.0 if brand_in_name else 0.0
    return round(points, 1)


def tier_of(points: float) -> str:
    if points >= TIER_FIRST:
        return "review first"
    if points >= TIER_LOOK:
        return "worth a look"
    return "long tail"


def triage(
    conn: sqlite3.Connection,
    tokens: dict[str, int],
    reference: date,
    weights: dict[str, float],
) -> list[Scored]:
    rows = conn.execute(
        """
        SELECT c.candidate_id, g.name AS grp, c.group_id, c.company_name, c.province,
               c.reason, c.evidence, c.n_announcements, c.first_date, c.last_date
        FROM candidates c JOIN groups g USING (group_id)
        WHERE c.status = 'pending'
        """
    ).fetchall()
    scored: list[Scored] = []
    for row in rows:
        grams = set(name_grams(row["company_name"]))
        brand = any(t in grams for t, g in tokens.items() if g == row["group_id"])
        points = score_one(
            row["reason"],
            row["n_announcements"],
            date.fromisoformat(row["last_date"]),
            reference,
            brand,
            weights,
        )
        scored.append(
            Scored(
                points,
                tier_of(points),
                row["candidate_id"],
                row["grp"],
                row["company_name"],
                row["province"],
                row["reason"],
                row["evidence"],
                row["n_announcements"],
                row["first_date"],
                row["last_date"],
                brand,
            )
        )
    scored.sort(key=lambda s: (-s.score, s.group, s.company_name))
    return scored
