"""Evaluate the discovery signals against published evidence of group membership.

Two ground truths, reported separately:

1. **Explicit relations** (the primary one): companies tied to a watched group by an
   explicit company-to-company relation published in the gazette over the window (see
   ``relations.py``): owned or directed by a group company, absorbed by or absorbing
   one, receiving a split from one, or listed with one in a merger deposit. Membership
   is propagated along those relations (closure). These are facts about legal persons.
2. **Explicit relations or shared officers** (added in this rebuild): also companies
   with at least two officers who are officers of a watched company of the group.
   Relations published within a year reach few of a large group's companies (most
   subsidiaries declared their sole shareholder years ago); their officers are the
   group's people. Computed locally; only counts are published.

What the numbers mean:

* **precision is a lower bound**: a real group company with no published evidence in
  the window counts as a false positive;
* **recall** is measured on the companies of the ground truth that published an
  announcement of their own in the window, since a signal can only fire on those;
* the *mention* signal reads the relation slots the first ground truth is built from,
  so against it its precision is expected to be high: it checks the matching, not an
  independent source. *address* and *token* use evidence that neither truth uses, and
  are the informative ones. The *person* signal uses officers' surnames, so the second
  truth is not independent of it.
"""

from __future__ import annotations

from dataclasses import dataclass

from borme_radar.corpus import SignalHits

REASONS = ("mention", "address", "token", "person", "detector")


@dataclass(frozen=True, slots=True)
class SignalScore:
    truth: str
    reason: str
    flagged: int
    true_positive: int
    wrong_group: int
    precision: float | None
    recall: float | None


def score(
    truth_name: str,
    truth: dict[str, tuple[int, int]],
    subjects: set[str],
    hits: SignalHits,
) -> list[SignalScore]:
    """Precision (lower bound) and recall of every signal against one ground truth."""
    discoverable = {n for n in truth if n in subjects}
    out: list[SignalScore] = []
    for reason in REASONS:
        flagged = hits.hits.get(reason, {})
        tp = {n for n, g in flagged.items() if n in truth and truth[n][0] == g}
        wrong_group = sum(1 for n, g in flagged.items() if n in truth and truth[n][0] != g)
        out.append(
            SignalScore(
                truth=truth_name,
                reason=reason,
                flagged=len(flagged),
                true_positive=len(tp),
                wrong_group=wrong_group,
                precision=len(tp) / len(flagged) if flagged else None,
                recall=len(tp & discoverable) / len(discoverable) if discoverable else None,
            )
        )
    return out


def union(*truths: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
    """Merge ground truths; the first one that names a company decides its group."""
    merged: dict[str, tuple[int, int]] = {}
    for truth in truths:
        for norm, value in truth.items():
            merged.setdefault(norm, value)
    return merged
