"""Discover NEW companies of the watched groups: companies not in the watchlist.

A new subsidiary has no known registry sheet and no watched name, so the matcher cannot
see it. Signals, tried from the most to the least reliable; at most one candidate per
announcement:

1. **mention**: an act of the unknown company names a watched company in a relation
   slot: as its sole shareholder, as a company it absorbs, as the beneficiary of its
   split, as its sole or joint administrator, or listed with it in a Section B merger
   deposit. It is an explicit corporate relation, not a resemblance. A watched company
   merely sitting on a board, holding powers of attorney or acting as a depositary is
   not a mention.
2. **address**: its registered address (incorporation or change of address) is the
   registered address of a watched group: same street words, same number (or the same
   kilometre, decimals included, on a road) and same municipality. An address shared by
   many unrelated companies (office towers, business centres, law firms) is *degraded*:
   it no longer decides alone and the other signals still run.
3. **token**: its name contains a brand token of 1-2 words from a list a person
   approved. Generic, geographic and legal-form words never qualify, and every token is
   measured against the corpus before approval (``calibrate --tokens``).
4. **person** (local only, opt-in): an officer of the watched group, identified by a
   rare pair of surnames, appears in the act. It finds companies *of that person*, so
   personal holding companies (sole shareholder is a natural person) are skipped.

Rejected signal: any single word of the watched names. Geographic words drag in
hundreds of unrelated companies.

Candidates are never added to the watchlist automatically (the BORME publishes no tax
ID) and are re-judged with the current rules on every run.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from borme_radar.normalize import (
    LEGAL_FORM_TOKENS,
    base_and_form,
    normalize_name,
    plain,
    strip_annotations,
)
from borme_radar.officers import OFFICER_ACTS, holdings
from borme_radar.record import street_and_city
from borme_radar.relations import relation_names

Reason = Literal["mention", "address", "token", "person"]
SIGNAL_ORDER: tuple[Reason, ...] = ("mention", "address", "token", "person")

# Thresholds derived from public gazettes with ``borme-radar calibrate`` on the train
# window (24 Sep 2025 - 23 Mar 2026; histograms in docs/calibration.md). They are
# defaults; the CLI can override them.
# A registered address with this many unrelated companies or more stops deciding alone.
# The 30 watched addresses split with a gap: 26 have 0-5 unrelated companies, none has
# 6-15, and 4 office buildings have 16, 18, 23 and 83.
ADDRESS_NOISE_MAX = 6
# A brand token found in more unrelated company names than this is a word, not a brand.
# Combined precision of the kept tokens (against relations or shared officers): 68% at
# a cut of 1 (9 tokens), 47% at 3 and at 5 (14 and 18 tokens), 34% at 7: 5 is the
# largest cut before it drops.
TOKEN_NOISE_MAX = 5
# A watched name looked for WITHOUT its legal form ("Socio único: BANCO EJEMPLO") must
# have at least this many characters and words. Measured over 12 months: a company name
# without legal form equals a natural person's name in at most 0.20% of cases in any
# length and word bucket (hash sample of 41,729 names), and mentions are matched as
# whole relation slots, never as substrings; the floor only drops 1-3 letter names,
# of which the sample had too few to measure.
BARE_MENTION_MIN_CHARS = 4
BARE_MENTION_MIN_WORDS = 1


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_ROAD_KM_RE = re.compile(r"\bK\.?\s?M\.?\s*(\d{1,4})(?:\s*[.,]\s*(\d{1,3}))?", re.I)
_HOUSE_NUMBER_RE = re.compile(r"\b(\d{1,4})\b")
_ROAD_ID_RE = re.compile(r"\b([A-Z]{1,3})\s*-\s*(\d{1,4})\b")
_STREET_TYPES = _words(
    "C CL CALLE CALLEJON AV AVDA AVENIDA PS PASEO PZ PZA PLAZA CTRA CARRETERA POL "
    "POLIGONO RONDA RAMBLA CAMI CAMINO TRAVESIA TRAV URB URBANIZACION NUM N NO GTA "
    "GLORIETA PG PGNO PARQUE PQ BARRIO BO CP"
)
# Floor, door and other details of the entrance: not part of the street name.
_ENTRANCE_WORDS = _words(
    "BAJO BJ BIS DUPLICADO DUP PORTAL PTL LOCAL LC OFICINA OFIC OF DESPACHO DESP "
    "EDIFICIO EDIF PLANTA PLTA PL PISO PTA PUERTA ESCALERA ESC MODULO MOD IZQUIERDA "
    "IZQDA IZQ DERECHA DCHA DER CENTRO INTERIOR EXTERIOR SN SIN NUMERO APARTAMENTO "
    "APTO ENTREPLANTA ENTLO ATICO SOTANO NAVE PARCELA BLOQUE BL MANZANA MZ KM"
)
# Words that appear in many different street names: sharing only these is not sharing
# an address ("GENERAL X 9" and "GENERAL Y 9" are different streets).
_GENERIC_STREET_WORDS = _words(
    "GENERAL SAN SANTA SANTO NUESTRA SENORA DOCTOR DON DONA GRAN VIA NUEVA "
    "MAYOR REAL NACIONAL INDUSTRIAL SUR NORTE ESTE OESTE PRIMERA SEGUNDA "
    "TERCERA CUARTA DEL DE LA LAS LOS EL AVENIDA"
)


@dataclass(frozen=True, slots=True)
class AddressKey:
    street: frozenset[str]
    number: str  # house number, or "KM17.4" on a road
    city: str

    @property
    def label(self) -> str:
        return f"{' '.join(sorted(self.street))}|{self.number}|{self.city}"


def address_key(street: str | None, city: str | None) -> AddressKey | None:
    """Comparable key of a registered address, or None when it cannot distinguish.

    Without a number there is no signal: half a town would match "C/ MAYOR S/N".
    """
    if not street or not city:
        return None
    raw = street.upper()
    km = _ROAD_KM_RE.search(raw)
    words: set[str] = set()
    if km:
        decimals = (km.group(2) or "").rstrip("0")
        number = f"KM{int(km.group(1))}" + (f".{decimals}" if decimals else "")
        raw = raw[: km.start()] + " " + raw[km.end() :]
        # On a road the road id is the street: "N-120" -> "N120".
        words = {f"{a}{b}" for a, b in _ROAD_ID_RE.findall(raw)}
    else:
        numbers = _HOUSE_NUMBER_RE.findall(plain(raw))
        if not numbers:
            return None
        number = str(int(numbers[0]))
    words |= {
        w
        for w in plain(raw).split()
        if len(w) >= 3 and not w.isdigit() and w not in _STREET_TYPES and w not in _ENTRANCE_WORDS
    }
    if not words and not km:
        return None
    return AddressKey(frozenset(words), number, plain(city))


def same_address(a: AddressKey, b: AddressKey) -> bool:
    """Same number, same municipality and a recognisably equal street."""
    if a.number != b.number or a.city != b.city:
        return False
    common = a.street & b.street
    # A kilometre point locates the site by itself; roads share generic words, and a
    # road may be written without any usable word ("CTRA KM 12").
    if a.number.startswith("KM"):
        return bool(common) or not a.street or not b.street
    if not common or len(common) < min(len(a.street), len(b.street)) / 2:
        return False
    return bool(common - _GENERIC_STREET_WORDS)


def registered_address(act_type: str, detail: str) -> AddressKey | None:
    """Registered address stated by an incorporation or change-of-address act."""
    if act_type == "address_change":
        return address_key(*street_and_city(detail))
    if act_type == "incorporation":
        match = re.search(
            r"Domicilio\s*:\s*(.+?)(?=\.\s*(?:Capital|Patrimonio)[^:]*:|$)", detail, re.S
        )
        if match:
            return address_key(*street_and_city(match.group(1)))
    return None


def name_grams(name: str) -> list[str]:
    """Two-word then one-word sequences of a name: where brand tokens are looked for."""
    words = plain(strip_annotations(name)).split()
    pairs = [f"{a} {b}" for a, b in itertools.pairwise(words)]
    return pairs + words


@dataclass(frozen=True, slots=True)
class Candidate:
    group_id: int
    reason: Reason
    evidence: str


@dataclass(slots=True)
class GroupIndex:
    """Everything the detector needs, built once per run."""

    groups: dict[int, str]
    mention_names: dict[str, int]  # normalised watched name (full or bare) -> group
    hqs: list[tuple[AddressKey, int]]  # registered addresses of the watched groups
    tokens: dict[str, int]  # approved brand token -> group
    hq_noise: dict[str, int] = field(default_factory=dict)  # address label -> unrelated
    persons: dict[str, int] = field(default_factory=dict)  # surname pair -> group
    address_noise_max: int = ADDRESS_NOISE_MAX
    _hq_by_site: dict[tuple[str, str], list[tuple[AddressKey, int]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key, group_id in self.hqs:
            self._hq_by_site.setdefault((key.number, key.city), []).append((key, group_id))

    def hq_for(self, key: AddressKey) -> tuple[AddressKey, int] | None:
        for hq, group_id in self._hq_by_site.get((key.number, key.city), ()):
            if same_address(key, hq):
                return hq, group_id
        return None

    def degraded(self, hq: AddressKey) -> bool:
        return self.hq_noise.get(hq.label, 0) >= self.address_noise_max

    def token_in(self, name: str) -> str | None:
        """The approved token in a company name, two-word tokens first."""
        if not self.tokens:
            return None
        for gram in name_grams(name):
            if gram in self.tokens:
                return gram
        return None


def mention_keys(
    names: Iterable[tuple[str, int]],
    min_chars: int = BARE_MENTION_MIN_CHARS,
    min_words: int = BARE_MENTION_MIN_WORDS,
) -> dict[str, int]:
    """Watched names as mention keys: always with legal form; bare only when long."""
    keys: dict[str, int] = {}
    for name, group_id in names:
        norm = normalize_name(name)
        if not norm:
            continue
        keys.setdefault(norm, group_id)
        bare, form = base_and_form(norm)
        if form and len(bare) >= min_chars and len(bare.split()) >= min_words:
            keys.setdefault(bare, group_id)
    return keys


def _mentioned(
    acts: list[tuple[str, str]], names: Mapping[str, int]
) -> tuple[str, str, int] | None:
    """(relation, watched name, group) of the first relation slot naming a watched one."""
    for act_type, detail in acts:
        found = holdings(detail) if act_type in OFFICER_ACTS else []
        for kind, name in relation_names(act_type, detail, tuple(found)):
            norm = normalize_name(name)
            if norm in names:
                return kind, norm, names[norm]
    return None


def signals(
    company_name: str,
    acts: Iterable[tuple[str, str]],
    index: GroupIndex,
    person_pairs: Iterable[str] = (),
    personal_holding: bool = False,
) -> list[Candidate]:
    """Every signal that fires for one announcement, in :data:`SIGNAL_ORDER`.

    ``acts`` are ``(act_type, detail)`` pairs. The detector keeps the first; the
    evaluation looks at all of them.
    """
    acts = list(acts)
    found: list[Candidate] = []
    mention = _mentioned(acts, index.mention_names)
    if mention:
        found.append(Candidate(mention[2], "mention", f'{mention[0]} "{mention[1]}"'))
    for act_type, detail in acts:
        key = registered_address(act_type, detail)
        hit = index.hq_for(key) if key else None
        if hit and not index.degraded(hit[0]):
            found.append(Candidate(hit[1], "address", f"registered at {hit[0].label}"))
            break
    token = index.token_in(company_name)
    if token:
        found.append(Candidate(index.tokens[token], "token", f'token "{token}"'))
    if index.persons and not personal_holding:
        for pair in person_pairs:
            if pair in index.persons:
                # The pair itself is personal data: it stays in the local database.
                found.append(Candidate(index.persons[pair], "person", "officer surname pair"))
                break
    return found


def detect(
    company_name: str,
    acts: Iterable[tuple[str, str]],
    index: GroupIndex,
    person_pairs: Iterable[str] = (),
    personal_holding: bool = False,
) -> Candidate | None:
    """The most reliable signal for an unknown company, or None."""
    found = signals(company_name, acts, index, person_pairs, personal_holding)
    return found[0] if found else None


# ------------------------------------------------------------------ brand tokens


def is_valid_token_shape(token: str, excluded: frozenset[str] = frozenset()) -> str | None:
    """Why a proposed token can never be a brand, or None if its shape is acceptable."""
    words = plain(token).split()
    if not 1 <= len(words) <= 2:
        return "a token has one or two words"
    if any(w in LEGAL_FORM_TOKENS for w in words):
        return "legal-form word"
    if len(words) == 1 and words[0] in excluded:
        return "generic or geographic word"
    if len(words) == 1 and len(words[0]) < 4:
        return "shorter than 4 letters"
    return None


def load_tokens(path: Path | None) -> dict[str, int | str]:
    """Approved token file: ``{"TOKEN": "Group name", ...}``. Missing file -> no tokens.

    Deliberate: until a person approves a list, the token signal is off. An automatic
    list brings in geographic words and floods the candidates.
    """
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {" ".join(plain(token).split()): group for token, group in data.items()}
