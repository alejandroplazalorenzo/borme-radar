"""Discovery: signs that a company nobody watches belongs to a watched group.

A company a group has just set up or bought has no sheet the radar knows and no watched
name, so it never matches. Every announcement that matched nothing is read for four
kinds of sign, from the most to the least reliable (``SIGNAL_ORDER``):

``mention``
    a relation slot of one of its acts names a watched company, whole: its sole
    shareholder, a company it absorbs or that a split or a transfer is for, its sole or
    joint director, or another company of the same Section B entry (``relations.py``).
    Board seats and powers of attorney are not slots;
``address``
    an act registers the company at the address of a watched company
    (:func:`same_site`), unless calibration found that address crowded with unrelated
    companies (``address_noise.json``);
``token``
    the company's own name carries an approved brand token of one or two words;
``person``
    local and opt-in: a rare pair of name words of a group officer appears in its acts
    (``persons.py``).

:func:`detect` keeps the first sign found: at most one lead per announcement, for a
person to look at. Nothing here adds a company to the watchlist, and the words of a
watched name never count one by one. docs/calibration.md measures the thresholds.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from borme_radar.normalize import (
    LEGAL_FORM_TOKENS,
    base_and_form,
    normalize_name,
    plain,
    strip_annotations,
    word_list,
)
from borme_radar.officers import OFFICER_ACTS, holdings
from borme_radar.record import Place, city_forms, observations_of_act, place, same_place
from borme_radar.relations import relation_names

Reason = Literal["mention", "address", "token", "person"]
SIGNAL_ORDER: tuple[Reason, ...] = ("mention", "address", "token", "person")

# An address where this many unrelated companies are registered is not used: measured on
# the train window by `calibrate --addresses` (docs/calibration.md).
ADDRESS_NOISE_MAX = 6
# A brand token is kept when at most this many unrelated companies carry it. The value
# comes from the production system and was kept; `calibrate --tokens` prints the precision
# at every cut of the train window to check it (docs/calibration.md).
TOKEN_NOISE_MAX = 5
# A watched name also counts without its legal form if 4+ characters are left: such bare names
# rarely equal a person's name; shorter ones were too few to measure (docs/calibration.md).
BARE_MENTION_MIN_CHARS = 4
BARE_MENTION_MIN_WORDS = 1

# Words found in the names of many different streets ("GENERAL" ...): two addresses that share
# only these are not the same. Measured on the corpus by `borme-radar formats` (docs/history.md).
GENERIC_STREET_WORDS = word_list("generic_street_words.txt")


# ------------------------------------------------------------------ addresses


@dataclass(frozen=True, slots=True)
class Site:
    """A registered address as discovery compares it: every official name of the
    municipality (``record.city_forms``), the parts of the street that place it
    (``record.place``) and the municipality as written, for the label."""

    towns: frozenset[str]
    street: Place
    town: str

    @property
    def label(self) -> str:
        """The site in a few words, "CASTELLANA 257 (MADRID)": the key of the noise counts
        and the evidence of a lead."""
        road = "KM " if self.street.on_road else ""
        return f"{' '.join(sorted(self.street.words))} {road}{self.street.portal} ({self.town})"

    def marks(self) -> Iterator[tuple[str, str]]:
        """What the site is filed under: each municipality name with each word that is not
        generic, and with the kilometre point on a road."""
        marks = sorted(self.street.words - GENERIC_STREET_WORDS)
        if self.street.on_road:
            marks.append(f"KM {self.street.portal}")
        return ((town, mark) for town in sorted(self.towns) for mark in marks)


def site(street: str | None, city: str | None, *, from_registry: bool = False) -> Site | None:
    """The site of an address, or None when it cannot place a company: without a
    municipality, without a number, or with generic words only (a kilometre point is
    enough on a road). ``from_registry`` lets the last word be one the Registro cut."""
    towns = city_forms(city or "")
    if not (street and towns):
        return None
    spot = place(street, may_be_cut=from_registry)
    if spot.portal is None or not (spot.on_road or spot.words - GENERIC_STREET_WORDS):
        return None
    return Site(towns, spot, plain(city or ""))


def same_site(known: Site, seen: Site) -> bool:
    """Is ``seen``, an address the Registro gives, the watched address ``known``? They share
    a municipality, are one place for ``record.same_place`` (the number, the other numbers
    and the words) and have in common a word that is not generic or, on a road, the
    kilometre point."""
    shared = (known.street.words & seen.street.words) - GENERIC_STREET_WORDS
    return (
        bool(known.towns & seen.towns)
        and same_place(known.street, seen.street)
        and bool(shared or (known.street.on_road and seen.street.on_road))
    )


def registered_site(act_type: str, detail: str) -> Site | None:
    """The site an act registers the company at: the new address of a change of address,
    or the domicile of an incorporation, as the record reads them. None for other acts."""
    if act_type not in ("address_change", "incorporation"):
        return None
    said = {o.field: o.value for o in observations_of_act(act_type, detail)}
    return site(said.get("address"), said.get("city"), from_registry=True)


def name_grams(name: str) -> list[str]:
    """The pairs of consecutive words of a name, then its single words: what a brand token
    is compared with, two-word tokens first."""
    words = plain(strip_annotations(name)).split()
    return [" ".join(words[i : i + 2]) for i in range(len(words) - 1)] + words


@dataclass(frozen=True, slots=True)
class Candidate:
    group_id: int
    reason: Reason
    evidence: str


@dataclass(slots=True)
class GroupIndex:
    """What discovery knows of the watched groups, by group id: the names to look for in
    relation slots, the sites of the watched companies, the approved brand tokens and,
    when the person signal is on, the surname pairs. ``hq_noise`` is the calibration's
    count of unrelated companies at each site label; from ``address_noise_max`` a site is
    crowded and not used."""

    groups: dict[int, str]
    mention_names: dict[str, int]  # normalised watched name (full or bare) -> group
    hqs: list[tuple[Site, int]]  # sites of the watched companies, with their group
    tokens: dict[str, int]  # approved brand token -> group
    hq_noise: dict[str, int] = field(default_factory=dict)  # site label -> unrelated
    persons: dict[str, int] = field(default_factory=dict)  # surname pair -> group
    address_noise_max: int = ADDRESS_NOISE_MAX
    _filed: dict[tuple[str, str], list[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Each site under its keys, by its position in ``hqs``: a site is only compared
        # with those that share a key, and the first watched one in the list wins.
        filed: dict[tuple[str, str], list[int]] = defaultdict(list)
        for position, (known, _group) in enumerate(self.hqs):
            for key in known.marks():
                filed[key].append(position)
        self._filed = dict(filed)

    def watched_site(self, seen: Site) -> tuple[Site, int] | None:
        """The first watched site that ``seen`` is, with its group, or None."""
        near = sorted({i for key in seen.marks() for i in self._filed.get(key, ())})
        return next((self.hqs[i] for i in near if same_site(self.hqs[i][0], seen)), None)

    def crowded(self, known: Site) -> bool:
        return self.hq_noise.get(known.label, 0) >= self.address_noise_max

    def token_in(self, name: str) -> str | None:
        """The approved token the name carries as whole words, two-word tokens first, or
        None."""
        grams = name_grams(name) if self.tokens else []
        return next((g for g in grams if g in self.tokens), None)


def mention_keys(
    names: Iterable[tuple[str, int]],
    min_chars: int = BARE_MENTION_MIN_CHARS,
    min_words: int = BARE_MENTION_MIN_WORDS,
) -> dict[str, int]:
    """Normalised watched names and aliases -> group, and each name without its legal form
    when the bare name has at least ``min_chars`` characters and ``min_words`` words. The
    first group given for a key keeps it."""
    keys: dict[str, int] = {}
    normalised = ((normalize_name(name), group) for name, group in names)
    for norm, group in ((n, g) for n, g in normalised if n):
        keys.setdefault(norm, group)
        bare, form = base_and_form(norm)
        long_enough = len(bare) >= min_chars and len(bare.split()) >= min_words
        if form and long_enough:
            keys.setdefault(bare, group)
    return keys


def _mention(acts: list[tuple[str, str]], index: GroupIndex) -> Candidate | None:
    """A watched company named, whole, in a relation slot of one of the acts."""
    for act_type, detail in acts:
        found = tuple(holdings(detail)) if act_type in OFFICER_ACTS else ()
        named = (
            (kind, normalize_name(name)) for kind, name in relation_names(act_type, detail, found)
        )
        hit = next(((k, n) for k, n in named if n in index.mention_names), None)
        if hit is not None:
            return Candidate(index.mention_names[hit[1]], "mention", f'{hit[0]} "{hit[1]}"')
    return None


def _address(acts: list[tuple[str, str]], index: GroupIndex) -> Candidate | None:
    """An act registers the company at a watched site that is not crowded."""
    for act_type, detail in acts:
        seen = registered_site(act_type, detail)
        hit = index.watched_site(seen) if seen else None
        if hit is not None and not index.crowded(hit[0]):
            return Candidate(hit[1], "address", f"at {hit[0].label}")
    return None


def _person(pairs: Iterable[str], index: GroupIndex, personal: bool) -> Candidate | None:
    if not index.persons or personal:
        return None
    pair = min((p for p in pairs if p in index.persons), default=None)
    # The pair itself is personal data: it stays in the local database.
    return (
        None if pair is None else Candidate(index.persons[pair], "person", "officer surname pair")
    )


def signals(
    company_name: str,
    acts: Iterable[tuple[str, str]],
    index: GroupIndex,
    person_pairs: Iterable[str] = (),
    personal_holding: bool = False,
) -> list[Candidate]:
    """Every signal that fires for one announcement, in ``SIGNAL_ORDER``.

    ``person_pairs`` are the surname pairs found in its acts. ``personal_holding`` (its
    sole shareholder is a natural person) switches the person signal off: the pairs of
    that person would only lead to the companies the person owns.
    """
    acts = list(acts)
    token = index.token_in(company_name)
    fired = (
        _mention(acts, index),
        _address(acts, index),
        Candidate(index.tokens[token], "token", f'token "{token}"') if token else None,
        _person(person_pairs, index, personal_holding),
    )
    return [candidate for candidate in fired if candidate is not None]


def detect(
    company_name: str,
    acts: Iterable[tuple[str, str]],
    index: GroupIndex,
    person_pairs: Iterable[str] = (),
    personal_holding: bool = False,
) -> Candidate | None:
    """The candidate of the most reliable signal that fires, or None."""
    found = signals(company_name, acts, index, person_pairs, personal_holding)
    return found[0] if found else None


# ------------------------------------------------------------------ brand tokens


def is_valid_token_shape(token: str, excluded: frozenset[str] = frozenset()) -> str | None:
    """Why a proposed brand token can never be used, or None if its shape is valid.

    A token has one or two words and no legal-form word; a single word needs four letters
    or more and must not be in ``excluded`` (the calibration passes the municipalities of
    the registered addresses it read).
    """
    words = plain(token).split()
    if len(words) not in (1, 2):
        return "a token has one or two words"
    if not LEGAL_FORM_TOKENS.isdisjoint(words):
        return "legal-form word"
    single = words[0] if len(words) == 1 else None
    if single is not None and single in excluded:
        return "generic or geographic word"
    if single is not None and len(single) < 4:
        return "shorter than 4 letters"
    return None


def load_tokens(path: Path | None) -> dict[str, int | str]:
    """The tokens of an approved JSON file (token -> group), each in plain form. No file
    means no tokens: a list nobody approved is never read."""
    if path is None or not path.is_file():
        return {}
    approved = json.loads(path.read_text(encoding="utf-8"))
    return {plain(token): group for token, group in approved.items()}
