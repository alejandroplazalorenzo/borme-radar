"""Person signal: follow a group through who signs for it (LOCAL ONLY).

The only route for groups whose companies have generic names. Searching full names
does not work: Spanish surnames collide. The unit is an **unordered pair of words** of
an officer's name, chosen by the rarity of each word in the gazette; first names are
never special-cased (no rule can tell them apart: the Registro writes "SURNAME SURNAME
NAME" in one act and "NAME SURNAME SURNAME" in another), the rarest pair wins by itself.

These are names of natural persons published in an official gazette. They stay in the
local, gitignored database (table ``person_keys``); nothing here writes them to a
report, and the evaluation publishes only aggregate precision and recall.
"""

from __future__ import annotations

import itertools

from borme_radar.normalize import plain

# Particles that are not half of a surname pair.
_PARTICLES = frozenset({"DE", "DEL", "LA", "LAS", "LOS", "Y", "SAN", "SANTA", "VAN", "VON", "DA"})
_MAX_WORDS = 40  # ceiling for very long act texts
# A pair found in more unrelated companies than this does not identify anyone. Measured
# on the train window for the watched groups' officers: half of them (3,302 of 6,678)
# have a pair found in no unrelated company, and the count drops to 647 at one; only
# pairs never seen outside the group are kept.
PAIR_NOISE_MAX = 0


def name_words(name: str) -> list[str]:
    return [w for w in plain(name).split() if len(w) >= 3 and w.isalpha() and w not in _PARTICLES]


def pair_key(a: str, b: str) -> str:
    return " ".join(sorted((a, b)))


def possible_pairs(name: str) -> set[str]:
    words = sorted(set(name_words(name)))
    return {pair_key(a, b) for a, b in itertools.combinations(words, 2)}


def pairs_in(text: str) -> set[str]:
    """Pairs present in an act text, to look up in the calibrated keys.

    A paragraph with three people yields crossed pairs between different people; that
    is harmless because only rare, calibrated pairs are in the index.
    """
    words = list(dict.fromkeys(name_words(text)))[:_MAX_WORDS]
    return {pair_key(a, b) for a, b in itertools.combinations(words, 2)}


def best_pair(
    name: str, word_counts: dict[str, int], pair_counts: dict[str, int]
) -> tuple[str, int]:
    """The pair of a name whose words are rarest, and how many unrelated companies carry it.

    Ranked by the rarity of each word, not by how often the pair occurs together: the
    companies where the pair occurs are exactly the ones the signal wants to find, so
    ranking by co-occurrence would penalise the good pair.
    """
    best: tuple[int, int, str] | None = None
    for pair in possible_pairs(name):
        a, b = pair.split()
        rarity = word_counts.get(a, 0) + word_counts.get(b, 0)
        candidate = (rarity, pair_counts.get(pair, 0), pair)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return "", 0
    return best[2], best[1]
