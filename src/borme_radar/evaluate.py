"""Precision and recall of each discovery signal against a ground truth taken from the
corpus itself (published company-to-company relations, officers shared with a watched
company)."""

from __future__ import annotations

from dataclasses import dataclass

from borme_radar.corpus import SignalHits

REASONS = ("mention", "address", "token", "person", "detector")
Truth = dict[str, tuple[int, int]]  # normalised name -> (group, depth of the relation)


@dataclass(frozen=True, slots=True)
class SignalScore:
    truth: str
    reason: str
    flagged: int
    true_positive: int
    wrong_group: int
    precision: float | None
    recall: float | None


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _score_one(
    truth_name: str, reason: str, flagged: dict[str, int], truth: Truth, reachable: set[str]
) -> SignalScore:
    in_truth = {name: group for name, group in flagged.items() if name in truth}
    right = {name for name, group in in_truth.items() if truth[name][0] == group}
    return SignalScore(
        truth=truth_name,
        reason=reason,
        flagged=len(flagged),
        true_positive=len(right),
        wrong_group=len(in_truth) - len(right),
        precision=_share(len(right), len(flagged)),
        recall=_share(len(right & reachable), len(reachable)),
    )


def score(truth_name: str, truth: Truth, subjects: set[str], hits: SignalHits) -> list[SignalScore]:
    """One score per signal, plus the combined detector, against ``truth``.

    A flag counts as right only when the truth puts the company in the same group.
    Precision is a lower bound: a real group company with no published evidence counts as
    wrong. Recall is over the truth companies in ``subjects`` (those with an announcement
    of their own), since a signal can only flag a company that publishes something.
    """
    reachable = set(truth) & subjects
    return [
        _score_one(truth_name, reason, hits.hits.get(reason, {}), truth, reachable)
        for reason in REASONS
    ]


def union(*truths: Truth) -> Truth:
    """Merge ground truths; a company found in several keeps the value of the first one."""
    merged: Truth = {}
    for truth in reversed(truths):
        merged |= truth
    return merged
