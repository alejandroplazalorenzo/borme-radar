"""The year each company joined its group, read from its acts in the gazette.

The members of a group are its watched companies and the open leads found by mention
that the group holds: a group company owns them, they absorb one, or one is their sole
director (:func:`tie_to_group`). The acts of each member are reduced to a
:class:`Timeline` of dated facts: its incorporation, each sole shareholder, and each
holder who joins or leaves its governing body in a control role. A fact is the group's
when the shareholder or the corporate holder is a group company, or when the person is
a group officer (:func:`group_officers`).

The entry is given by the first rule of ``RULES`` that applies to the member:

``head``
    the head of the group joined it when it was incorporated;
``founded``
    the group shows up within ``BORN_WINDOW`` of the incorporation: dated on the
    incorporation;
``acquired``
    the timeline shows the company was someone else's when the group's first fact came
    (:meth:`Timeline.earlier_hands`, where ``HANDOVER_WINDOW`` bounds the sign about the
    body): dated on that first fact;
``untied``
    no fact of the group at all;
``listed``
    the company was the group's from its first act, and the watchlist gives an
    incorporation inside the years the gazette covers: dated on it;
``already_in``
    the company was the group's from its first act: no date.

Each entry also says which kind of fact its day rests on (``owner``, ``officers`` or
``incorporation``), whether an inscribed act states the tie (``confirmed``: a group
company as sole shareholder, or the head's own incorporation in the gazette), and the
evidence as short codes (:data:`CODES`) for a person to check. A company the rules leave
without a day keeps the year of its incorporation, if one is known, as evidence
``incorporation`` and not confirmed. A year typed in the watchlist is never overwritten.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from typing import Literal

from borme_radar.discovery import BARE_MENTION_MIN_CHARS
from borme_radar.normalize import base_and_form, normalize_name, plain
from borme_radar.officers import BOARD_ROLES, EVENT_BY_ACT, Holding, holdings
from borme_radar.relations import relation_names

# A sign of the group within a year of the incorporation means the group set the company
# up. The value comes from the production system and was kept; `joined` prints the days
# from the incorporation to the first sign of the group, to check it (docs/history.md).
BORN_WINDOW = timedelta(days=365)
# Outsiders leaving the body this close to the group's arrival make it a purchase. Kept
# from the production system as well, and checked against the days `joined` prints.
HANDOVER_WINDOW = timedelta(days=30)
# A person appointed to the bodies of this many watched companies is a group officer.
# Kept from the production system; with one watched company per group it cannot be
# measured here.
GROUP_OFFICER_MIN = 3

# The roles of the governing body that decide who controls a company: every board role
# but those who keep its minutes, sit on one of its committees, wind it up, represent a
# corporate director or are put there by a court.
_NO_CONTROL = frozenset(
    {
        "committee_member",
        "insolvency_administrator",
        "liquidator",
        "representative",
        "secretary",
        "vice_secretary",
    }
)
CONTROL_ROLES = BOARD_ROLES - _NO_CONTROL

Evidence = Literal["incorporation", "officers", "owner"]
Act = tuple[date, str, str]  # (fact date, act type, detail)

# What each evidence code of an entry carries after its ":", in the terms of the timeline.
CODES = {
    "before_gazette": "(nothing) a head with no incorporation day in either source",
    "founded": "day: the member's first `incorporated` fact",
    "founded_before": "day: an `incorporated` fact earlier than `Timeline.group_sign`",
    "group_from": "day: `Timeline.group_sign`, when no rule before `already_in` applied",
    "handover": "days: from `group_sign` to the nearest `body_out` fact not of the group",
    "head": "(nothing) `Member.is_head`",
    "listed_founded": "day: `Member.incorporated`, the watchlist's date",
    "no_group_act": "(nothing) no fact of the timeline has `by_group`",
    "officer": "day and role label: a `body_in` fact of the group",
    "outside_owner": "day: an `owner` fact not of the group, earlier than `group_sign`",
    "owner": "day and name: an `owner` fact of the group",
}


@dataclass(slots=True)
class Member:
    key: str  # "company:<id>" or "candidate:<id>"
    name: str
    group_id: int
    origin: Literal["watchlist", "candidate"]
    company_id: int | None = None
    is_head: bool = False
    incorporated: date | None = None  # from the watchlist
    stored: tuple[int | None, str | None, bool] = (None, None, False)  # year, evidence, confirmed
    manual: bool = False  # the year was typed in the watchlist
    names: set[str] = field(default_factory=set)  # normalised names and aliases
    acts: list[Act] = field(default_factory=list)
    tie: str = ""  # for candidates: why they are in the group (a code)


@dataclass(slots=True)
class Group:
    group_id: int
    members: list[Member]
    officers: set[str] = field(default_factory=set)
    _by_name: dict[str, Member] = field(default_factory=dict)

    def index(self, *, without_candidates: bool = False) -> None:
        """Index the members by name and alias, also without legal form when long enough.

        ``without_candidates`` leaves the candidates out, to decide which of them belong.
        """
        members = [m for m in self.members if not (without_candidates and m.origin == "candidate")]
        full = [(norm, m) for m in members for norm in m.names]
        bare = [(base_and_form(norm)[0], m) for norm, m in full]
        self._by_name = {}
        for name, m in full + [(b, m) for b, m in bare if len(b) >= BARE_MENTION_MIN_CHARS]:
            self._by_name.setdefault(name, m)

    def find(self, name: str) -> Member | None:
        norm = normalize_name(name)
        if not norm:
            return None
        return self._by_name.get(norm) or self._by_name.get(base_and_form(norm)[0])


# ------------------------------------------------------------------ the timeline


@dataclass(frozen=True, slots=True)
class Fact:
    """One dated fact of a member: incorporated, a sole shareholder, a holder of a control
    role joining or leaving the body. ``note`` is the shareholder or the role label."""

    day: date
    kind: Literal["incorporated", "owner", "body_in", "body_out"]
    by_group: bool
    note: str


@dataclass(frozen=True, slots=True)
class Timeline:
    facts: tuple[Fact, ...]  # by day

    def _first(self, test: Callable[[Fact], bool]) -> Fact | None:
        return next(filter(test, self.facts), None)

    @property
    def founded(self) -> Fact | None:
        return self._first(lambda f: f.kind == "incorporated")

    @property
    def group_owner(self) -> Fact | None:
        return self._first(lambda f: f.kind == "owner" and f.by_group)

    @property
    def group_sign(self) -> Fact | None:
        """The first fact of the group: a group shareholder or the group joining the body."""
        return self._first(lambda f: f.by_group and f.kind in ("owner", "body_in"))

    def handover_days(self, sign: Fact) -> int | None:
        """Days from ``sign`` to the nearest outsider leaving the body, before or after."""
        outside = (f for f in self.facts if f.kind == "body_out" and not f.by_group)
        return min((abs((f.day - sign.day).days) for f in outside), default=None)

    def earlier_hands(self, sign: Fact) -> list[str]:
        """The codes of every sign that the company was someone else's before ``sign``."""
        before = [f for f in self.facts if f.day < sign.day]
        codes = [f"outside_owner:{f.day}" for f in before if f.kind == "owner" and not f.by_group]
        codes += [f"founded_before:{f.day}" for f in before if f.kind == "incorporated"][:1]
        gap = self.handover_days(sign)
        if gap is not None and gap <= HANDOVER_WINDOW.days:
            codes.append(f"handover:{gap}d")
        return codes


def _from_group(h: Holding, member: Member, group: Group) -> bool:
    if h.is_company:
        other = group.find(h.holder)
        return other is not None and other is not member
    return plain(h.holder) in group.officers


def timeline_of(member: Member, group: Group, *, with_owners: bool = True) -> Timeline:
    """The facts of a member, read from its acts. ``with_owners=False`` leaves the sole
    shareholders out, to see what the officers alone say."""
    facts: list[Fact] = []
    for day, act_type, detail in member.acts:
        event = EVENT_BY_ACT.get(act_type)
        if act_type == "incorporation":
            facts.append(Fact(day, "incorporated", False, "incorporation"))
        elif act_type in ("sole_shareholder_change", "sole_shareholder_declared"):
            owners = [n for k, n in relation_names(act_type, detail) if k == "owned_by"]
            for owner in owners if with_owners else ():
                other = group.find(owner)
                if other is not member:
                    facts.append(Fact(day, "owner", other is not None, owner))
        elif event is not None:
            kind: Literal["body_in", "body_out"] = (
                "body_in" if event in ("appointment", "reelection") else "body_out"
            )
            facts += [
                Fact(day, kind, _from_group(h, member, group), h.role_raw)
                for h in holdings(detail)
                if h.role in CONTROL_ROLES
            ]
    return Timeline(tuple(sorted(facts, key=lambda f: f.day)))


# ------------------------------------------------------------------ the entry


@dataclass(frozen=True, slots=True)
class Entry:
    day: date | None
    rule: str
    evidence: Evidence | None
    confirmed: bool
    codes: tuple[str, ...]
    from_watchlist: bool = False  # the day is the incorporation the watchlist gives


def _code(fact: Fact) -> str:
    """The evidence code of a fact of the group."""
    return (
        f"owner:{fact.day}:{fact.note}"
        if fact.kind == "owner"
        else f"officer:{fact.day}:{fact.note}"
    )


def _head(member: Member, line: Timeline, _first: date) -> Entry | None:
    if not member.is_head:
        return None
    founded = line.founded
    if member.incorporated is not None:
        day = member.incorporated
        return Entry(day, "head", "incorporation", False, ("head", f"listed_founded:{day}"), True)
    if founded is not None:
        return Entry(founded.day, "head", "incorporation", True, ("head", f"founded:{founded.day}"))
    return Entry(None, "head", None, False, ("head", "before_gazette"))


def _founded(_member: Member, line: Timeline, _first: date) -> Entry | None:
    founded, sign = line.founded, line.group_sign
    if founded is None or sign is None or sign.day - founded.day > BORN_WINDOW:
        return None
    owner = line.group_owner
    by_owner = owner is not None and owner.day - founded.day <= BORN_WINDOW
    proof = owner if by_owner and owner is not None else sign
    evidence: Evidence = "owner" if by_owner else "officers"
    return Entry(
        founded.day, "founded", evidence, by_owner, (f"founded:{founded.day}", _code(proof))
    )


def _acquired(_member: Member, line: Timeline, _first: date) -> Entry | None:
    sign = line.group_sign
    hands = line.earlier_hands(sign) if sign is not None else []
    if sign is None or not hands:
        return None
    by_owner = sign.kind == "owner"
    evidence: Evidence = "owner" if by_owner else "officers"
    return Entry(sign.day, "acquired", evidence, by_owner, (_code(sign), *hands))


def _untied(_member: Member, line: Timeline, _first: date) -> Entry | None:
    return (
        Entry(None, "untied", None, False, ("no_group_act",)) if line.group_sign is None else None
    )


def _listed(member: Member, line: Timeline, first: date) -> Entry | None:
    day, sign = member.incorporated, line.group_sign
    if day is None or day < first or sign is None:
        return None
    return Entry(
        day, "listed", "incorporation", False, (f"listed_founded:{day}", _code(sign)), True
    )


def _already_in(_member: Member, line: Timeline, _first: date) -> Entry | None:
    sign = line.group_sign
    return Entry(None, "already_in", None, False, (f"group_from:{sign.day}",)) if sign else None


# The rules of an entry, in the order they are tried: the first that applies dates it.
RULES: tuple[Callable[[Member, Timeline, date], Entry | None], ...] = (
    _head,
    _founded,
    _acquired,
    _untied,
    _listed,
    _already_in,
)


def date_entry(member: Member, line: Timeline, first_gazette: date) -> Entry:
    """When the member entered its group, by the first rule of ``RULES`` that applies
    (``untied`` and ``already_in`` between them cover every timeline)."""
    tried = (rule(member, line, first_gazette) for rule in RULES)
    return next(entry for entry in tried if entry is not None)


def to_store(
    entry: Entry, member: Member, line: Timeline
) -> tuple[int | None, Evidence | None, bool]:
    """Year, evidence and confirmation to store: the entry's when it has a day, else the
    year of the incorporation, if known, as unconfirmed ``incorporation``."""
    if entry.day is not None and entry.evidence is not None:
        return entry.day.year, entry.evidence, entry.confirmed
    founded = member.incorporated or (line.founded.day if line.founded else None)
    return (founded.year, "incorporation", False) if founded else (None, None, False)


# ------------------------------------------------------------------ membership


def tie_to_group(member: Member, group: Group) -> tuple[str | None, str]:
    """How a candidate is tied to the group, with the evidence as a code, or ``(None, "")``.

    ``owned_by``, ``absorbs`` and ``run_by`` (a group company appointed sole director) are
    returned at once; ``on_body`` (a group company on the body) only if none of them is.
    """
    seat = ""
    for day, act_type, detail in member.acts:
        slots = relation_names(act_type, detail)
        owner = next((n for k, n in slots if k == "owned_by" and group.find(n)), None)
        if owner:
            return "owned_by", f"owned_by:{day}:{owner}"
        if act_type in ("merger", "merger_project"):
            absorbed = next((n for k, n in slots if k == "absorbs" and group.find(n)), None)
            if absorbed:
                return "absorbs", f"absorbs:{day}:{absorbed}"
        event = EVENT_BY_ACT.get(act_type)
        if event is None:
            continue
        for h in holdings(detail):
            if not (h.is_company and h.role in CONTROL_ROLES and group.find(h.holder)):
                continue
            if h.role == "sole_director" and event in ("appointment", "reelection"):
                return "run_by", f"run_by:{day}:{h.holder}"
            seat = seat or f"on_body:{day}:{h.holder}"
    return ("on_body", seat) if seat else (None, "")


def _people(member: Member) -> Iterator[tuple[str, bool]]:
    """The natural persons in a control role in the officer acts of a member, each with
    whether that act brings them in (an appointment or a re-election)."""
    for _day, act_type, detail in member.acts:
        event = EVENT_BY_ACT.get(act_type)
        if event is None:
            continue
        brings_in = event in ("appointment", "reelection")
        for h in holdings(detail):
            if h.role in CONTROL_ROLES and not h.is_company:
                yield plain(h.holder), brings_in


def group_officers(group: Group) -> set[str]:
    """Plain names of the group's officers: natural persons in a control role of the head
    of the group, or brought into the bodies of ``GROUP_OFFICER_MIN`` watched companies."""
    listed = [m for m in group.members if m.origin == "watchlist"]
    officers = {person for m in listed if m.is_head for person, _ in _people(m)}
    bodies = Counter(
        person for m in listed for person in {p for p, brings_in in _people(m) if brings_in}
    )
    return officers | {person for person, n in bodies.items() if n >= GROUP_OFFICER_MIN}


# ------------------------------------------------------------------ one group


@dataclass(frozen=True, slots=True)
class Dated:
    member: Member
    entry: Entry
    year: int | None
    evidence: Evidence | None
    confirmed: bool
    officers_only: Entry  # the entry read without the sole shareholders
    timeline: Timeline


def _belongs(member: Member, group: Group) -> bool:
    """Does the member belong to the group? Watched companies always do; a candidate when
    the group owns it, it absorbs a group company, or one runs it. Its tie is kept."""
    if member.origin != "candidate":
        return True
    kind, member.tie = tie_to_group(member, group)
    return kind in ("owned_by", "absorbs", "run_by")


def analyse(group: Group, first_gazette: date) -> list[Dated]:
    """Date the entry of every member of a group, after dropping the candidates the group
    does not hold. Each entry is also read from the officers alone (``officers_only``),
    to set the two kinds of evidence against each other."""
    group.index(without_candidates=True)
    group.members = [m for m in group.members if _belongs(m, group)]
    group.index()
    group.officers = group_officers(group)
    dated: list[Dated] = []
    for m in group.members:
        line = timeline_of(m, group)
        entry = date_entry(m, line, first_gazette)
        alone = date_entry(m, timeline_of(m, group, with_owners=False), first_gazette)
        dated.append(Dated(m, entry, *to_store(entry, m, line), alone, line))
    return dated


# ------------------------------------------------------------------ measurements


def officers_against_owner(dated: list[Dated]) -> dict[str, object]:
    """For the entries an owner act confirms (founded or acquired): the year the officers
    alone give, by rule, and the days between the two dates (officers minus owner)."""
    by_rule: dict[str, Counter[str]] = defaultdict(Counter)
    gaps: list[int] = []
    for d in dated:
        if not d.entry.confirmed or d.entry.rule not in ("acquired", "founded"):
            continue
        owner_day, alone = d.entry.day, d.officers_only
        assert owner_day is not None
        if alone.day is None or alone.evidence is None:
            by_rule[d.entry.rule]["officers give no year"] += 1
            continue
        gaps.append((alone.day - owner_day).days)
        years = (alone.day.year > owner_day.year) - (alone.day.year < owner_day.year)
        verdict = {
            0: "officers, same year",
            1: "officers, later year",
            -1: "officers, earlier year",
        }
        by_rule[d.entry.rule][verdict[years]] += 1
    gaps.sort()
    at = {q: gaps[min(len(gaps) - 1, q * len(gaps) // 100)] for q in (10, 50, 90)} if gaps else {}
    return {
        "by_rule": {k: dict(v) for k, v in sorted(by_rule.items())},
        "officers_minus_owner_days": {"n": len(gaps), "quantiles": at},
    }


_FOUNDING_BANDS = (0, 31, 91, 181, 366, 731, 1826)
_HANDOVER_BANDS = (0, 8, 31, 91, 366)


def _band(days: int, edges: tuple[int, ...]) -> str:
    for low, high in pairwise(edges):
        if low <= days < high:
            return f"{low}-{high - 1}"
    return f">={edges[-1]}"


def window_checks(dated: list[Dated]) -> dict[str, dict[str, int]]:
    """The days behind the two windows, in bands; heads are left out: from the
    incorporation to the first sign of the group, and from that sign to the nearest
    outsider leaving the body."""
    founding: Counter[str] = Counter()
    handover: Counter[str] = Counter()
    for d in (d for d in dated if not d.member.is_head):
        sign, founded = d.timeline.group_sign, d.timeline.founded
        if sign is None:
            continue
        if founded is not None:
            founding[_band((sign.day - founded.day).days, _FOUNDING_BANDS)] += 1
        gap = d.timeline.handover_days(sign)
        if gap is not None:
            handover[_band(gap, _HANDOVER_BANDS)] += 1
    return {
        "incorporation_to_first_group_sign_days": dict(founding),
        "first_group_sign_to_nearest_outsider_leaving_days": dict(handover),
    }


# ------------------------------------------------------------------ database


def first_gazette_day(conn: sqlite3.Connection) -> date | None:
    (first,) = conn.execute("SELECT MIN(day) FROM gazette_docs").fetchone()
    return date.fromisoformat(first) if first else None


def _acts_by(conn: sqlite3.Connection, owner: str) -> dict[int, list[Act]]:
    """Acts of the kept announcements by their ``owner`` column, oldest first."""
    acts: dict[int, list[Act]] = defaultdict(list)
    query = f"""
        SELECT n.{owner}, n.fact_day, a.act_type, a.detail
        FROM notices n JOIN notice_acts a ON a.doc_id = n.doc_id AND a.num = n.num
        WHERE n.{owner} IS NOT NULL
        ORDER BY n.fact_day, n.doc_id, n.num, a.seq
    """
    for key, day, act_type, detail in conn.execute(query):
        acts[key].append((date.fromisoformat(day), act_type, detail))
    return acts


def load_groups(conn: sqlite3.Connection) -> list[Group]:
    """The groups in the database, with their members and the acts of each.

    Watched companies come with their aliases; a group with one watched company has it as
    head. The candidates are the open ones found by mention.
    """
    company_acts = _acts_by(conn, "company_id")
    lead_acts = _acts_by(conn, "lead_id")
    aliases: dict[int, set[str]] = defaultdict(set)
    for name_key, company_id in conn.execute("SELECT name_key, company_id FROM other_names"):
        aliases[company_id].add(name_key)
    groups: dict[int, Group] = {}
    rows = conn.execute(
        "SELECT company_id, group_id, name, name_key, heads_group, incorporated, joined_year,"
        " joined_evidence, joined_confirmed, joined_manual FROM watched ORDER BY company_id"
    )
    for cid, gid, name, key, head, born, year, evidence, confirmed, manual in rows:
        groups.setdefault(gid, Group(gid, [])).members.append(
            Member(
                key=f"company:{cid}",
                name=name,
                group_id=gid,
                origin="watchlist",
                company_id=cid,
                is_head=bool(head),
                incorporated=date.fromisoformat(born) if born else None,
                stored=(year, evidence, bool(confirmed)),
                manual=bool(manual),
                names={key, *aliases[cid]},
                acts=company_acts.get(cid, []),
            )
        )
    for group in groups.values():
        listed = [m for m in group.members if m.origin == "watchlist"]
        if len(listed) == 1:
            listed[0].is_head = True
    leads = conn.execute(
        "SELECT lead_id, group_id, firm, firm_key FROM leads"
        " WHERE found_by = 'mention' AND state = 'open' ORDER BY lead_id"
    )
    for lead_id, gid, firm, firm_key in leads:
        if gid in groups:
            groups[gid].members.append(
                Member(
                    key=f"candidate:{lead_id}",
                    name=firm,
                    group_id=gid,
                    origin="candidate",
                    names={firm_key},
                    acts=lead_acts.get(lead_id, []),
                )
            )
    return list(groups.values())


def changes(dated: list[Dated]) -> list[dict[str, object]]:
    """The watched companies whose stored year, evidence or confirmation would change,
    leaving out those whose year a person typed."""
    return [
        {
            "year": d.year,
            "evidence": d.evidence,
            "confirmed": int(d.confirmed),
            "id": d.member.company_id,
        }
        for d in dated
        if d.member.company_id is not None
        and not d.member.manual
        and (d.year, d.evidence, d.confirmed) != d.member.stored
    ]


def apply(conn: sqlite3.Connection, dated: list[Dated]) -> int:
    """Store the :func:`changes`; returns how many were written."""
    found = changes(dated)
    with conn:
        conn.executemany(
            "UPDATE watched SET joined_year = :year, joined_evidence = :evidence,"
            " joined_confirmed = :confirmed WHERE company_id = :id",
            found,
        )
    return len(found)
