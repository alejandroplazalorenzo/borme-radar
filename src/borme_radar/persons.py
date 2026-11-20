"""The person signal of discovery: rare pairs of words of the names of a group's officers.

A natural person on the board of a watched company, or holding its powers, is reduced to
the words of their name that can tell people apart: letters only, three or more, the
articles and the particles of foreign surnames left out (a matter of grammar, not
measured). Any two of those words, in either order, make a pair. The same words,
sorted and without legal-form words, are the person key of ``officers.holder_key``.

``calibrate --person-pairs`` counts over the corpus, outside the watchlist, the
announcements that carry each word and the unrelated companies that carry each pair. For
each person it takes the pair whose words are rarest (:func:`best_pair`) and keeps it only
when at most :data:`PAIR_NOISE_MAX` unrelated companies carry it. With ``--persons``,
discovery then looks for the kept pairs in the acts of every announcement
(:class:`PairFinder`).

The signal is local only: the pairs are personal data, they stay in the local database
(``surname_pairs``), and only aggregate counts are published (docs/calibration.md).
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Iterable

from borme_radar.normalize import ARTICLES, plain, word_set

# Words that link the parts of a name and are never half of a surname pair: the articles
# of normalize.py and the particles of foreign surnames ("VAN", "VON"...).
PARTICLES = ARTICLES | word_set("VAN VON DER DEN")
# Default of `calibrate --pair-noise-max`: unrelated companies a pair may be found in and
# still be kept. 0 is "never seen outside the group" (docs/calibration.md).
PAIR_NOISE_MAX = 0


def _usable(word: str) -> bool:
    """Can the word be half of a pair: letters only, three or more, and not a particle?"""
    return word.isalpha() and len(word) >= 3 and word not in PARTICLES


def name_words(name: str) -> list[str]:
    return list(filter(_usable, plain(name).split()))


def pair_key(a: str, b: str) -> str:
    """The key of a pair of words, the same in either order."""
    return f"{min(a, b)} {max(a, b)}"


def possible_pairs(name: str) -> set[str]:
    words = sorted(set(name_words(name)))
    return set(itertools.starmap(pair_key, itertools.combinations(words, 2)))


class PairFinder:
    """The kept pairs a text carries.

    Each kept pair is filed under both of its words; a text is then read word by word and
    a pair is found when its other word is in the text too. The cost grows with the words
    of the text, not with the pairs they could form, so no text is too long to read.
    """

    def __init__(self, pairs: Iterable[str]) -> None:
        self._partners: dict[str, set[str]] = defaultdict(set)
        for pair in pairs:
            first, second = pair.split()
            self._partners[first].add(second)
            self._partners[second].add(first)

    def __call__(self, text: str) -> set[str]:
        words = set(name_words(text))
        return {
            pair_key(word, other)
            for word in words
            for other in self._partners.get(word, ())
            if other in words
        }


def best_pair(
    name: str, word_counts: dict[str, int], pair_counts: dict[str, int]
) -> tuple[str, int]:
    """The pair that stands for a person: of the pairs of the name, the one whose words are
    rarest (the sum of their ``word_counts``), then the one fewer companies carry in
    ``pair_counts``, then the first in alphabetical order. Returns it with its count in
    ``pair_counts``, or ("", 0) when the name has fewer than two usable words."""

    def rank(pair: str) -> tuple[int, int, str]:
        rarity = sum(word_counts.get(word, 0) for word in pair.split())
        return rarity, pair_counts.get(pair, 0), pair

    best = min(map(rank, possible_pairs(name)), default=None)
    return ("", 0) if best is None else (best[2], best[1])
