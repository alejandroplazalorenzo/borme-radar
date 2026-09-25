"""One pass over the cached gazette, counting only what is asked.

Calibrating a signal means asking the corpus one question: **in how many unrelated
companies does this appear?** If a brand token, an address or a surname pair appears in
many companies unrelated to the watched groups, it tells nothing apart.

Rules of every measurement here:

* **one pass** over the cached XML of a date range, no network, no database writes;
* **only what is asked is counted** (the tokens proposed, the watched addresses, a
  sample of names): memory grows with the question, not with the corpus. The first
  idea, an inverted index of every word, grows with the corpus (``memory_comparison``
  measures both);
* companies are counted **distinct, by normalised name**: the same company files its
  accounts every year, with the year glued to its name;
* **watched companies and companies with an explicit relation to a watched group are
  excluded** (the relations are collected in the same pass): the public watchlist holds
  only the parent companies, so without this a group's own subsidiaries would count as
  noise against its brand;
* a measurement cut short with ``limit_documents`` is **partial**: it is reported as
  such and never writes a proposal.
"""

from __future__ import annotations

import hashlib
import re
import time
import tracemalloc
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Protocol

from rapidfuzz import fuzz, process

from borme_radar.cache import DiskCache
from borme_radar.discovery import (
    AddressKey,
    GroupIndex,
    name_grams,
    registered_address,
    signals,
)
from borme_radar.download import days_between
from borme_radar.httpclient import NotCached, PoliteClient
from borme_radar.matching import Matcher
from borme_radar.models import Announcement, Document
from borme_radar.normalize import base_and_form, normalize_name, plain
from borme_radar.officers import OFFICER_ACTS, Holding, canonical_role, holdings
from borme_radar.parser import parse_document
from borme_radar.relations import Relation, relations_of_announcement
from borme_radar.source import BormeSource

_START_OF_OPERATIONS_RE = re.compile(r"Comienzo de operaciones\s*:\s*(\d{1,2})\.(\d{1,2})\.(\d{2})")


class Seen:
    """One announcement as the collectors see it; expensive parts are computed once."""

    __slots__ = ("__dict__", "ann", "doc", "norm", "watched")

    def __init__(self, doc: Document, ann: Announcement, norm: str, watched: int | None):
        self.doc = doc
        self.ann = ann
        self.norm = norm
        self.watched = watched

    @property
    def fact_date(self) -> date:
        return self.ann.inscription_date or self.doc.pub_date

    @cached_property
    def holdings(self) -> dict[int, list[Holding]]:
        return {
            act.seq: holdings(act.detail) for act in self.ann.acts if act.act_type in OFFICER_ACTS
        }

    @cached_property
    def relations(self) -> list[Relation]:
        acts = [(a.act_type, a.detail, tuple(self.holdings.get(a.seq, ()))) for a in self.ann.acts]
        return relations_of_announcement(acts, self.ann.other_companies, self.norm)


class Collector(Protocol):
    def observe(self, seen: Seen) -> None: ...


@dataclass(slots=True)
class ScanInfo:
    start: date
    end: date
    days: int = 0
    documents: int = 0
    announcements: int = 0
    watched_announcements: int = 0
    missing_documents: int = 0
    partial: bool = False
    seconds: float = 0.0
    companies: set[str] = field(default_factory=set)  # distinct subjects (normalised)


def scan(
    cache_dir: Path,
    start: date,
    end: date,
    matcher: Matcher,
    collectors: Iterable[Collector],
    limit_documents: int | None = None,
) -> ScanInfo:
    """Feed every cached announcement of the range to the collectors (offline)."""
    collectors = list(collectors)
    info = ScanInfo(start, end)
    started = time.perf_counter()
    with PoliteClient(cache=DiskCache(cache_dir), offline=True) as client:
        source = BormeSource(client)
        for day in days_between(start, end):
            try:
                refs = source.documents_for(day)
            except NotCached:
                continue
            if refs is None:
                continue
            info.days += 1
            for ref in refs:
                try:
                    payload = source.document_xml(ref)
                except NotCached:
                    info.missing_documents += 1
                    continue
                doc = parse_document(payload)
                info.documents += 1
                for ann in doc.announcements:
                    norm = normalize_name(ann.company_name)
                    watched = matcher.watched(ann.company_name, doc.province, ann.sheet)
                    info.announcements += 1
                    info.watched_announcements += watched is not None
                    info.companies.add(norm)
                    seen = Seen(doc, ann, norm, watched)
                    for collector in collectors:
                        collector.observe(seen)
                if limit_documents and info.documents >= limit_documents:
                    info.partial = True
                    info.seconds = time.perf_counter() - started
                    return info
    info.seconds = time.perf_counter() - started
    return info


# ---------------------------------------------------------------- relations / truth

# How group membership flows along an explicit relation: from the other company to the
# announced one (it is owned or directed by a member), from the announced one to the
# other (a member absorbs it, or transfers business to it), or both.
_FLOW = {
    "owned_by": "to_subject",
    "directed_by": "to_subject",
    "absorbs": "both",
    "transfers_to": "to_other",
    "co_listed": "both",
}


class Relations:
    """Explicit company-to-company relations of the whole range (legal persons only)."""

    def __init__(self) -> None:
        self.edges: list[tuple[str, str, str]] = []  # (subject, kind, other)
        self.names: dict[str, str] = {}
        self.subjects: set[str] = set()  # companies with an announcement of their own

    def observe(self, seen: Seen) -> None:
        self.subjects.add(seen.norm)
        if seen.relations:
            self.names.setdefault(seen.norm, seen.ann.company_name)
        for relation in seen.relations:
            self.edges.append((seen.norm, relation.kind, relation.other_norm))
            self.names.setdefault(relation.other_norm, relation.other)

    def closure(
        self, seeds: dict[str, int], max_depth: int | None = None
    ) -> dict[str, tuple[int, int]]:
        """Companies reachable from the watched ones: norm -> (group_id, depth)."""
        forward: dict[str, list[str]] = defaultdict(list)
        for subject, kind, other in self.edges:
            flow = _FLOW[kind]
            if flow in ("to_subject", "both"):
                forward[other].append(subject)
            if flow in ("to_other", "both"):
                forward[subject].append(other)
        found: dict[str, tuple[int, int]] = {norm: (group, 0) for norm, group in seeds.items()}
        frontier = list(seeds)
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            depth += 1
            nxt: list[str] = []
            for node in frontier:
                group = found[node][0]
                for neighbour in forward.get(node, ()):
                    if neighbour not in found:
                        found[neighbour] = (group, depth)
                        nxt.append(neighbour)
            frontier = nxt
        return {norm: value for norm, value in found.items() if value[1] > 0}

    def kinds(self) -> Counter[str]:
        return Counter(kind for _, kind, _ in self.edges)


# Two officers with the same full name as officers of a watched company: the second
# kind of group evidence. One shared name is common (homonyms, a shared law firm).
SHARED_OFFICERS_MIN = 2


class SharedOfficers:
    """Companies whose officers include at least ``minimum`` natural persons who are also
    officers of a watched company (LOCAL ONLY: it reads names; only counts leave it).

    Explicit relations published within a year reach few of a large group's companies:
    most subsidiaries declared their sole shareholder years ago. Their officers, though,
    are the group's people. The names asked are those of the watched companies' officers.
    """

    def __init__(self, names: dict[str, set[int]], minimum: int = SHARED_OFFICERS_MIN):
        self.names = names  # plain full name -> groups it is an officer in
        self.minimum = minimum
        self.shared: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))

    def observe(self, seen: Seen) -> None:
        if seen.watched is not None:
            return
        for items in seen.holdings.values():
            for h in items:
                if h.is_company:
                    continue
                name = " ".join(plain(h.holder).split())
                for group in self.names.get(name, ()):
                    self.shared[seen.norm][group].add(name)

    def members(self) -> dict[str, tuple[int, int]]:
        """norm -> (group, 1) for companies sharing enough officers with one group."""
        out: dict[str, tuple[int, int]] = {}
        for norm, by_group in self.shared.items():
            group, names = max(by_group.items(), key=lambda kv: len(kv[1]))
            if len(names) >= self.minimum:
                out[norm] = (group, 1)
        return out

    def histogram(self) -> Counter[int]:
        """Companies by the most officers shared with one watched group."""
        return Counter(max(len(v) for v in g.values()) for g in self.shared.values())


# ---------------------------------------------------------------- noise counters


class TokenNoise:
    """token -> distinct companies whose name carries it (as one or two whole words)."""

    def __init__(self, tokens: Iterable[str]) -> None:
        self.tokens = sorted({" ".join(plain(t).split()) for t in tokens if t})
        self.asked = frozenset(self.tokens)
        self.hits: dict[str, set[str]] = defaultdict(set)

    def observe(self, seen: Seen) -> None:
        if seen.watched is not None:
            return
        for gram in self.asked.intersection(name_grams(seen.ann.company_name)):
            self.hits[gram].add(seen.norm)

    def unrelated(self, exclude: set[str]) -> dict[str, set[str]]:
        return {t: self.hits.get(t, set()) - exclude for t in self.tokens}


class AddressNoise:
    """watched registered address -> distinct companies registered there."""

    def __init__(self, addresses: Iterable[tuple[AddressKey, int]]) -> None:
        self.index = GroupIndex(groups={}, mention_names={}, hqs=list(addresses), tokens={})
        self.hits: dict[str, set[str]] = defaultdict(set)
        self.labels = {key.label for key, _ in self.index.hqs}

    def observe(self, seen: Seen) -> None:
        if seen.watched is not None:
            return
        for act in seen.ann.acts:
            key = registered_address(act.act_type, act.detail)
            hit = self.index.hq_for(key) if key else None
            if hit:
                self.hits[hit[0].label].add(seen.norm)

    def unrelated(self, exclude: set[str]) -> dict[str, set[str]]:
        return {label: self.hits.get(label, set()) - exclude for label in self.labels}


class RegisteredAddresses:
    """Every registered address stated in the range: company -> address labels, and
    municipality -> companies (the geographic words a brand token cannot be)."""

    def __init__(self) -> None:
        self.by_company: dict[str, set[str]] = defaultdict(set)
        self.keys: dict[str, AddressKey] = {}
        self.cities: dict[str, set[str]] = defaultdict(set)

    def observe(self, seen: Seen) -> None:
        for act in seen.ann.acts:
            key = registered_address(act.act_type, act.detail)
            if key is None:
                continue
            self.by_company[seen.norm].add(key.label)
            self.keys.setdefault(key.label, key)
            self.cities[key.city].add(seen.norm)

    def group_addresses(self, members: dict[str, tuple[int, int]]) -> dict[int, Counter[str]]:
        """group -> address label -> number of member companies registered there."""
        out: dict[int, Counter[str]] = defaultdict(Counter)
        for norm, (group, _depth) in members.items():
            for label in self.by_company.get(norm, ()):
                out[group][label] += 1
        return out


def _bucket(text: str, buckets: int) -> bool:
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") % buckets == 0


class BareNameCollisions:
    """Does a company name without its legal form equal a natural person's name?

    Deterministic hash sampling (1 in ``buckets``) on both sides, so equal strings are
    always sampled together and memory stays a fraction of the corpus.
    """

    def __init__(self, buckets: int = 10) -> None:
        self.buckets = buckets
        self.bare: set[str] = set()
        self.persons: dict[str, set[str]] = defaultdict(set)

    def observe(self, seen: Seen) -> None:
        bare, form = base_and_form(seen.norm)
        if form and bare and _bucket(bare, self.buckets):
            self.bare.add(bare)
        for items in seen.holdings.values():
            for h in items:
                if h.is_company:
                    continue
                person = " ".join(plain(h.holder).split())
                if _bucket(person, self.buckets):
                    self.persons[person].add(seen.norm)

    def table(self) -> list[tuple[int, int, int, int]]:
        """(chars bucket, words, bare names, bare names equal to a person's name)."""
        rows: Counter[tuple[int, int]] = Counter()
        hits: Counter[tuple[int, int]] = Counter()
        for bare in self.bare:
            key = (min(len(bare) // 4 * 4, 32), min(len(bare.split()), 5))
            rows[key] += 1
            if bare in self.persons:
                hits[key] += 1
        return [(c, w, rows[(c, w)], hits[(c, w)]) for c, w in sorted(rows)]


class FuzzyScores:
    """Best similarity of each non-exact name to the watched names, with the truth
    taken from the registry sheet: same sheet as the watched company = same company."""

    def __init__(self, matcher: Matcher, floor: float = 70.0) -> None:
        self.matcher = matcher
        self.floor = floor
        self.bands: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def observe(self, seen: Seen) -> None:
        index = self.matcher.index
        if seen.norm in index.by_name or not index.base_list:
            return
        base = base_and_form(seen.norm)[0]
        best = process.extractOne(base, index.base_list, scorer=fuzz.ratio, score_cutoff=self.floor)
        if best is None:
            return
        suggested = index.by_name[index.bases[best[0]][0]]
        sheet_owner = index.by_sheet.get((seen.doc.province, seen.ann.sheet or ""))
        truth = "same_company" if sheet_owner == suggested else "different"
        self.bands[int(best[1]) // 5 * 5][truth].add(seen.norm)


class NameLengths:
    def __init__(self) -> None:
        self.section_a: Counter[int] = Counter()
        self.section_b_lists: Counter[int] = Counter()  # companies listed per B entry

    def observe(self, seen: Seen) -> None:
        if seen.doc.section == "A":
            self.section_a[len(seen.ann.company_name)] += 1
        else:
            self.section_b_lists[1 + len(seen.ann.other_companies)] += 1


class Roles:
    def __init__(self) -> None:
        self.raw: Counter[str] = Counter()

    def observe(self, seen: Seen) -> None:
        for items in seen.holdings.values():
            for h in items:
                self.raw[h.role_raw] += 1

    def coverage(self) -> tuple[int, int]:
        total = sum(self.raw.values())
        known = sum(n for raw, n in self.raw.items() if canonical_role(raw) != "other")
        return known, total


class IncorporationLag:
    """Days between "Comienzo de operaciones" and the inscription of incorporations."""

    def __init__(self) -> None:
        self.inscription_minus_start: Counter[int] = Counter()

    def observe(self, seen: Seen) -> None:
        if seen.ann.inscription_date is None:
            return
        for act in seen.ann.acts:
            if act.act_type != "incorporation":
                continue
            match = _START_OF_OPERATIONS_RE.search(act.detail)
            if not match:
                continue
            d, m, y = (int(g) for g in match.groups())
            try:
                started = date(2000 + y, m, d)
            except ValueError:
                continue
            self.inscription_minus_start[(seen.ann.inscription_date - started).days] += 1


class SheetStability:
    """Is the registry sheet a stable key? Renames, and acts after an extinction."""

    def __init__(self) -> None:
        # (province, sheet) -> [first norm, renamed, extinct, dissolved]
        self.state: dict[tuple[str, str], list] = {}
        self.c: Counter[str] = Counter()

    def observe(self, seen: Seen) -> None:
        if seen.doc.section != "A":
            return
        self.c["announcements"] += 1
        if not seen.ann.sheet:
            return
        self.c["with_sheet"] += 1
        key = (seen.doc.province, seen.ann.sheet)
        state = self.state.get(key)
        if state is None:
            state = self.state[key] = [seen.norm, False, False, False]
        else:
            if state[1]:
                self.c["after_rename"] += 1
                self.c["after_rename_other_name"] += seen.norm != state[0]
            if state[2]:
                self.c["after_extinction"] += 1
            if state[3]:
                self.c["after_dissolution"] += 1
            if not state[1] and seen.norm != state[0]:
                self.c["name_differs_without_rename"] += 1
        types = {a.act_type for a in seen.ann.acts}
        if "name_change" in types and not state[1]:
            state[1] = True
            self.c["renamed_sheets"] += 1
        if "extinction" in types and not state[2]:
            state[2] = True
            self.c["extinct_sheets"] += 1
        if "dissolution" in types and not state[3]:
            state[3] = True
            self.c["dissolved_sheets"] += 1

    def summary(self) -> dict[str, int]:
        return dict(self.c) | {"distinct_sheets": len(self.state)}


class SignalHits:
    """Every discovery signal fired on every non-watched announcement (evaluation)."""

    def __init__(self, index: GroupIndex, pairs_of: object | None = None) -> None:
        self.index = index
        self.pairs_of = pairs_of
        # reason -> norm -> group ; "detector" = the first signal only
        self.hits: dict[str, dict[str, int]] = defaultdict(dict)

    def observe(self, seen: Seen) -> None:
        if seen.watched is not None:
            return
        acts = [(a.act_type, a.detail) for a in seen.ann.acts]
        pairs = self.pairs_of(" ".join(d for _, d in acts)) if callable(self.pairs_of) else ()
        found = signals(seen.ann.company_name, acts, self.index, pairs)
        for candidate in found:
            self.hits[candidate.reason].setdefault(seen.norm, candidate.group_id)
        if found:
            self.hits["detector"].setdefault(seen.norm, found[0].group_id)


class PairCounts:
    """Surname-pair rarity for the person signal (local only: these are names)."""

    def __init__(self, words: set[str], pairs: set[str]) -> None:
        self.words = words
        self.pairs = pairs
        self.word_counts: Counter[str] = Counter()
        self.pair_hits: dict[str, set[str]] = defaultdict(set)

    def observe(self, seen: Seen) -> None:
        if seen.watched is not None:
            return
        from borme_radar.persons import name_words, pair_key

        found = {w for a in seen.ann.acts for w in name_words(a.detail) if w in self.words}
        for word in found:
            self.word_counts[word] += 1
        ordered = sorted(found)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                key = pair_key(a, b)
                if key in self.pairs:
                    self.pair_hits[key].add(seen.norm)


# ---------------------------------------------------------------- memory comparison


def memory_comparison(
    cache_dir: Path, start: date, end: date, matcher: Matcher, tokens: Iterable[str]
) -> dict[str, float]:
    """Peak memory of a full inverted index (every word -> companies) versus the
    targeted count (only the asked tokens), over the same documents."""

    class FullIndex:
        def __init__(self) -> None:
            self.index: dict[str, set[str]] = defaultdict(set)

        def observe(self, seen: Seen) -> None:
            if seen.watched is not None:
                return
            words = set(plain(seen.ann.company_name).split())
            for act in seen.ann.acts:
                words.update(plain(act.detail).split())
            for word in words:
                self.index[word].add(seen.norm)

    out: dict[str, float] = {}
    for name, collector in (("full_index", FullIndex()), ("targeted", TokenNoise(tokens))):
        tracemalloc.start()
        info = scan(cache_dir, start, end, matcher, [collector])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        out[f"{name}_peak_mb"] = round(peak / 2**20, 1)
        out[f"{name}_keys"] = float(
            len(collector.index) if isinstance(collector, FullIndex) else len(collector.hits)
        )
        out["documents"] = float(info.documents)
    return out


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
