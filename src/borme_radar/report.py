"""The Markdown report of a date range, one part per watched group.

It is written for the person who follows the groups. A table first gives, for each
group, how much happened in the window; then each group with anything to show has a
part of its own, in this order: the acts to read first (insolvency, dissolution,
mergers, capital...), what the record should take from the Registro, the possible new
companies, the officers who came and went, one person given or stripped of powers in
several of its companies on the same day, and its other acts. The names that only
resemble a watched one, and what the window covered, close the report. Every act links
to the PDF of its document, the edition with legal effect.

``details=False`` gives the version that may be passed around: no text of the acts and
no names of natural persons, so the officers and the powers are counted instead.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from borme_radar.acts import ACT_TYPES
from borme_radar.record import Proposal
from borme_radar.triage import Scored

# How much of an act's text the detailed report quotes: its first sentence, cut at this
# many characters (a choice: about three lines).
QUOTE_MAX = 240
# Leads shown per group, best first (a choice); the rest are only counted.
LEADS_SHOWN = 20


@dataclass
class _Part:
    """What the report says of one group."""

    first: list[str] = field(default_factory=list)
    record: list[str] = field(default_factory=list)
    leads: list[str] = field(default_factory=list)
    leads_total: int = 0
    officers: list[str] = field(default_factory=list)
    powers: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)
    acts: int = 0


def _quote(text: str | None) -> str:
    """The first sentence of a text, cut at ``QUOTE_MAX`` characters."""
    sentence = (text or "").split(". ", 1)[0].strip()
    return sentence if len(sentence) <= QUOTE_MAX else sentence[: QUOTE_MAX - 1].rstrip() + "…"


# What a value cannot carry into one line of Markdown or a cell of a table.
_ONE_LINE = str.maketrans({"|": "/", "\n": " "})


def _plain(value: object) -> str:
    return "" if value is None else str(value).translate(_ONE_LINE)


def _link(url: str | None) -> str:
    return f" ([BOE PDF]({url}))" if url else ""


def _kind(act_type: str) -> str:
    known = ACT_TYPES.get(act_type)
    return known.label if known else act_type


def _grid(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    """A Markdown table; columns of numbers are right-aligned."""
    numeric = [all(isinstance(row[i], int) for row in rows) for i in range(len(header))]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join("---:" if n else "---" for n in numeric) + "|")
    lines += ["| " + " | ".join(_plain(v) for v in row) + " |" for row in rows]
    return lines


# The kept announcements published in the window, which every part of the report reads.
_PUBLISHED = "WITH published AS (SELECT * FROM notices WHERE day >= :first AND day <= :last)"


class _Window:
    """The rows of the database a report of one window reads."""

    def __init__(self, conn: sqlite3.Connection, start: date, end: date) -> None:
        span = {"first": start.isoformat(), "last": end.isoformat()}
        self.company: dict[int, tuple[str, str]] = {
            cid: (name, label)
            for cid, name, label in conn.execute(
                "SELECT company_id, name, label FROM watched JOIN watch_groups USING (group_id)"
            )
        }
        self.pdf = dict(conn.execute("SELECT doc_id, pdf_url FROM gazette_docs").fetchall())
        self.acts = conn.execute(
            f"""{_PUBLISHED}
            SELECT published.day, company_id, doc_id, seq, act_type, scope, priority, detail
            FROM published JOIN notice_acts USING (doc_id, num)
            WHERE company_id > 0
            ORDER BY published.day DESC, doc_id, num, seq
            """,
            span,
        ).fetchall()
        # Officer changes by the day their announcement was published, like the acts.
        self.changes = conn.execute(
            f"""{_PUBLISHED}
            SELECT published.day, role_changes.company_id, scope, role, kind, holder, holder_key
            FROM role_changes JOIN published USING (doc_id, num)
            ORDER BY published.day DESC, change_id
            """,
            span,
        ).fetchall()
        self.similar = conn.execute(
            f"""{_PUBLISHED}
            SELECT day, firm, province, near_company, near_score, near_reason FROM published
            WHERE near_company > 0 ORDER BY near_score DESC, day DESC, firm
            """,
            span,
        ).fetchall()


def _acts(window: _Window, parts: dict[str, _Part], details: bool) -> None:
    for day, cid, doc_id, _seq, act_type, scope, level, detail in window.acts:
        name, group = window.company[cid]
        part = parts[group]
        part.acts += 1
        if level == "high" and scope in ("board", "company"):
            shown = part.first
        elif level == "medium" and scope == "company":
            shown = part.other
        else:
            continue
        shown.append(f"- {day} · {name} · {_kind(act_type)}{_link(window.pdf.get(doc_id))}")
        if details and detail:
            shown.append(f"  > {_plain(_quote(detail))}")


def _officers(window: _Window, parts: dict[str, _Part], details: bool) -> None:
    board = [c for c in window.changes if c["scope"] == "board"]
    if details:
        for c in board:
            name, group = window.company[c["company_id"]]
            parts[group].officers.append(
                f"- {c['day']} · {name} · {c['kind']} of {_plain(c['holder'])} as {c['role']}"
            )
        return
    counted = Counter((window.company[c["company_id"]], c["role"], c["kind"]) for c in board)
    for ((name, group), role, kind), n in sorted(counted.items()):
        parts[group].officers.append(f"- {name}: {role}, {kind} of {n}")


def _powers(window: _Window, parts: dict[str, _Part], details: bool) -> None:
    """Changes of powers that one person takes or loses in two companies or more of one
    group on one day: the case a reader should see together."""
    spread: dict[tuple[str, str, str, str], dict[int, str]] = defaultdict(dict)
    for c in (c for c in window.changes if c["scope"] == "attorney"):
        group = window.company[c["company_id"]][1]
        key = (group, c["day"], c["kind"], c["holder_key"])
        spread[key].setdefault(c["company_id"], c["holder"])
    wide = {key: held for key, held in spread.items() if len(held) >= 2}
    if details:
        for (group, day, kind, _key), held in sorted(
            wide.items(), key=lambda kv: kv[0][1], reverse=True
        ):
            names = ", ".join(sorted(window.company[cid][0] for cid in held))
            person = _plain(next(iter(held.values())))
            parts[group].powers.append(f"- {day} · {kind} of {person} in {len(held)}: {names}")
        return
    people: Counter[tuple[str, str, str]] = Counter()
    seats: Counter[tuple[str, str, str]] = Counter()
    for (group, day, kind, _key), held in wide.items():
        people[group, day, kind] += 1
        seats[group, day, kind] += len(held)
    for (group, day, kind), n in sorted(people.items(), key=lambda kv: kv[0][1], reverse=True):
        companies = seats[group, day, kind]
        parts[group].powers.append(f"- {day} · {kind}: {n} people, {companies} company-positions")


def _record(proposals: Sequence[Proposal], window: _Window, parts: dict[str, _Part]) -> None:
    for p in proposals:
        group = window.company[p.company_id][1]
        if p.why == "unbacked":
            back = _plain(p.proposed) or "empty"
            said = f"back to {back}, as the act behind the value written is gone"
        else:
            said = f"{_plain(_quote(p.proposed))}, inscribed {p.day}"
        held = _plain(_quote(p.held)) if p.held else "empty"
        link = _link(window.pdf.get(p.doc_id or ""))
        parts[group].record.append(f"- {p.company}, {p.field}: {said} (record: {held}){link}")


def _leads(leads: Sequence[Scored], parts: dict[str, _Part]) -> None:
    for lead in leads:
        part = parts[lead.group]
        part.leads_total += 1
        if len(part.leads) < LEADS_SHOWN:
            n = lead.n_announcements
            seen = (
                f"1 announcement, {lead.first_date}"
                if n == 1
                else (f"{n} announcements, {lead.first_date} to {lead.last_date}")
            )
            part.leads.append(
                f"- {lead.company_name} ({lead.province}) · {lead.reason}: {_plain(lead.evidence)}"
                f" · {seen} · tier {lead.tier}"
            )


_HEADINGS = (
    ("first", "Read first"),
    ("record", "The record against the Registro (`sync --apply` writes it)"),
    ("leads", "Possible new companies (never added by themselves)"),
    ("officers", "Officers"),
    ("powers", "One person, several companies"),
    ("other", "Other acts"),
)


def render(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    proposals: Sequence[Proposal],
    leads: Sequence[Scored],
    coverage: dict[str, object],
    details: bool = True,
) -> str:
    window = _Window(conn, start, end)
    parts: dict[str, _Part] = defaultdict(_Part)
    _acts(window, parts, details)
    _officers(window, parts, details)
    _powers(window, parts, details)
    _record(proposals, window, parts)
    _leads(leads, parts)

    out = [
        f"# Watched groups in the BORME, {start.isoformat()} to {end.isoformat()}",
        "",
        "Built from the BORME that the Agencia Estatal Boletín Oficial del Estado publishes "
        "as open data (https://www.boe.es). Not an official text: each act links to the "
        "PDF, the edition with legal effect.",
        "",
    ]
    if not details:
        out += ["Shareable version: the acts are not quoted and no natural person is named.", ""]
    groups = sorted(parts)
    out += ["## The window by group", ""]
    out += _grid(
        ["Group", "Acts", "Read first", "Officer changes", "Record fields", "Leads"],
        [
            [
                g,
                parts[g].acts,
                sum(line.startswith("- ") for line in parts[g].first),
                len(parts[g].officers),
                len(parts[g].record),
                parts[g].leads_total,
            ]
            for g in groups
        ]
        or [["(none)", 0, 0, 0, 0, 0]],
    )
    for g in (g for g in groups if any(getattr(parts[g], attr) for attr, _ in _HEADINGS)):
        out += ["", f"## {g}"]
        for attr, title in _HEADINGS:
            lines: list[str] = getattr(parts[g], attr)
            if not lines:
                continue
            out += ["", f"### {title}", "", *lines]
            if attr == "leads" and parts[g].leads_total > len(lines):
                out.append(
                    f"- … and {parts[g].leads_total - len(lines)} more (`borme-radar triage`)"
                )
    out += ["", "## Names that resemble a watched one (never matched)", ""]
    if window.similar:
        out += _grid(
            ["Published", "Name in the BORME", "Province", "Resembles", "Score", "Why"],
            [
                [day, firm, province, window.company[near][0], f"{score:.1f}", why]
                for day, firm, province, near, score, why in window.similar
            ],
        )
    else:
        out.append("None in this window.")
    out += ["", "## What this window covered", ""]
    out += _grid(["", "Count"], [[k, v] for k, v in coverage.items()])
    out += [
        "",
        "How to read the counts: acts and officer changes are those published in the window; "
        "the leads and the record fields come from the whole database, whatever the day they "
        "were found; `borme-radar triage` lists every lead, and `borme-radar sync` the fields "
        "with their acts.",
        "",
    ]
    return "\n".join(out)
