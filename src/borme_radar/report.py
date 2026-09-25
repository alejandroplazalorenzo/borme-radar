"""The run report: seven sections, the review queue and coverage.

1. The record against the Registro (what ``sync`` would change).
2. New companies of the watched groups (candidates, triaged).
3. Alerts: insolvency, dissolution, extinction, mergers, capital reductions...
4. Changes in the board.
5. Other corporate changes.
6. Clusters: the same person gaining or losing powers in two or more companies of the
   same group on the same day (one attorney is paperwork; four at once means someone
   left).
7. Who joins and who leaves the boards.

With ``details=False`` the report carries no act text and no personal names: sections
6 and 7 are aggregated to counts. That is the only form in which reports are shared.
Every act links to the official PDF, the authentic edition.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import date

from borme_radar.acts import ACT_TYPES
from borme_radar.record import Difference
from borme_radar.triage import Scored

_DETAIL_MAX = 150
_LIST_MAX = 60


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _short(text: str | None, limit: int = _DETAIL_MAX) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    if not rows:
        return ["_Nothing in this window._"]
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return out


def _label(act_type: str) -> str:
    return ACT_TYPES[act_type].label if act_type in ACT_TYPES else act_type


def _pdf(url: str | None) -> str:
    return f"[PDF]({url})" if url else ""


def _acts(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    priority: str,
    scopes: tuple[str, ...],
    details: bool,
) -> list[list[object]]:
    marks = ",".join("?" for _ in scopes)
    rows = conn.execute(
        f"""
        SELECT a.pub_date, g.name AS grp, c.name AS company, t.act_type, t.detail, d.url_pdf
        FROM acts t
        JOIN announcements a USING (document_id, number)
        JOIN companies c ON c.company_id = a.company_id
        JOIN groups g ON g.group_id = c.group_id
        JOIN documents d ON d.document_id = a.document_id
        WHERE a.pub_date BETWEEN ? AND ? AND t.priority = ? AND t.scope IN ({marks})
        ORDER BY a.pub_date DESC, c.name, t.seq
        """,
        (start.isoformat(), end.isoformat(), priority, *scopes),
    ).fetchall()
    out: list[list[object]] = []
    for row in rows:
        line: list[object] = [row["pub_date"], row["grp"], row["company"], _label(row["act_type"])]
        if details:
            line.append(_short(row["detail"]))
        line.append(_pdf(row["url_pdf"]))
        out.append(line)
    return out


def _clusters(
    conn: sqlite3.Connection, start: date, end: date, details: bool
) -> tuple[list[str], list[list[object]]]:
    rows = conn.execute(
        """
        SELECT e.event_date, g.name AS grp, e.holder, e.holder_norm, e.event,
               COUNT(DISTINCT e.company_id) AS n,
               group_concat(DISTINCT c.name) AS companies
        FROM officer_events e
        JOIN companies c ON c.company_id = e.company_id
        JOIN groups g ON g.group_id = c.group_id
        WHERE e.scope = 'attorney' AND e.event_date BETWEEN ? AND ?
        GROUP BY e.event_date, g.group_id, e.holder_norm, e.event
        HAVING COUNT(DISTINCT e.company_id) >= 2
        ORDER BY n DESC, e.event_date DESC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if details:
        headers = ["Date", "Group", "Person", "Event", "Companies", "Which"]
        return headers, [
            [r["event_date"], r["grp"], r["holder"], r["event"], r["n"], _short(r["companies"], 80)]
            for r in rows
        ]
    agg: Counter[tuple[str, str, str]] = Counter()
    companies: Counter[tuple[str, str, str]] = Counter()
    for r in rows:
        agg[(r["event_date"], r["grp"], r["event"])] += 1
        companies[(r["event_date"], r["grp"], r["event"])] += r["n"]
    headers = ["Date", "Group", "Event", "People in a cluster", "Company-positions"]
    return headers, [[*k, n, companies[k]] for k, n in sorted(agg.items(), reverse=True)]


def _movements(
    conn: sqlite3.Connection, start: date, end: date, details: bool
) -> tuple[list[str], list[list[object]]]:
    rows = conn.execute(
        """
        SELECT e.event_date, g.name AS grp, c.name AS company, e.role, e.event, e.holder,
               e.is_company
        FROM officer_events e
        JOIN companies c ON c.company_id = e.company_id
        JOIN groups g ON g.group_id = c.group_id
        WHERE e.scope = 'board' AND e.event_date BETWEEN ? AND ?
        ORDER BY e.event_date DESC, c.name, e.role
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if details:
        headers = ["Date", "Group", "Company", "Role", "Event", "Holder"]
        return headers, [
            [r["event_date"], r["grp"], r["company"], r["role"], r["event"], r["holder"]]
            for r in rows
        ]
    agg: Counter[tuple[str, str, str, str]] = Counter(
        (r["grp"], r["company"], r["role"], r["event"]) for r in rows
    )
    headers = ["Group", "Company", "Role", "Event", "Holders"]
    return headers, [[*k, n] for k, n in sorted(agg.items())]


def render(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    differences: Sequence[Difference],
    candidates: Sequence[Scored],
    coverage: dict[str, object],
    details: bool = True,
) -> str:
    out: list[str] = [
        f"# BORME radar: {start.isoformat()} to {end.isoformat()}",
        "",
        "> Watched groups against the Registro Mercantil. Source: BORME, open data of "
        "the Agencia Estatal Boletín Oficial del Estado (https://www.boe.es). Derived and "
        "unofficial output; the linked PDF is the authentic edition.",
        "",
    ]
    if not details:
        out += ["> Shareable version: no act text and no personal names.", ""]

    pdfs = dict(conn.execute("SELECT document_id, url_pdf FROM documents").fetchall())
    out += ["## 1. The record against the Registro", ""]
    out += [
        "Fields where the record is behind the resolved registry value (last value "
        "inscribed; a revert means the evidence of an applied change disappeared). "
        "Written only with `borme-radar sync --apply`. **Nothing is applied by itself.**",
        "",
    ]
    out += _table(
        ["Company", "Field", "Record", "Registro", "Since", "Kind", "Official"],
        [
            [
                d.company,
                d.field,
                _short(d.record, 60) or "(empty)",
                _short(d.registry, 60),
                d.fact_date,
                d.kind,
                _pdf(pdfs.get(d.document_id or "")),
            ]
            for d in differences
        ],
    )

    tiers = Counter(c.tier for c in candidates)
    reasons = Counter(c.reason for c in candidates)
    out += ["", "## 2. New companies of the watched groups", ""]
    by_signal = ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items())) or "none"
    out += [
        f"{len(candidates)} pending candidates ({by_signal}). "
        f"Tiers: review first {tiers['review first']}, worth a look {tiers['worth a look']}, "
        f"long tail {tiers['long tail']}. They are never added by themselves: the BORME "
        "publishes no tax ID, and the order is a triage, not a decision.",
        "",
    ]
    shown = [c for c in candidates if c.tier != "long tail"][:_LIST_MAX]
    out += _table(
        [
            "Tier",
            "Score",
            "Group",
            "Company",
            "Province",
            "Signal",
            "Evidence",
            "Announcements",
            "Period",
        ],
        [
            [
                c.tier,
                c.score,
                c.group,
                c.company_name,
                c.province,
                c.reason,
                _short(c.evidence, 60),
                c.n_announcements,
                f"{c.first_date} to {c.last_date}",
            ]
            for c in shown
        ],
    )

    act_headers = (
        ["Published", "Group", "Company", "Act"] + (["Detail"] if details else []) + ["Official"]
    )
    out += ["", "## 3. Alerts: insolvency, dissolution, mergers, capital", ""]
    out += _table(act_headers, _acts(conn, start, end, "high", ("company", "board"), details))
    out += ["", "## 4. Changes in the board", ""]
    out += _table(act_headers, _acts(conn, start, end, "medium", ("board",), details))
    out += ["", "## 5. Other corporate changes", ""]
    out += _table(act_headers, _acts(conn, start, end, "medium", ("company",), details))

    headers, rows = _clusters(conn, start, end, details)
    out += ["", "## 6. Clusters of powers", ""]
    out += [
        "Single attorneys are not reported. These are: the same person gaining or losing "
        "powers in two or more companies of the same group on the same day.",
        "",
    ]
    out += _table(headers, rows)

    headers, rows = _movements(conn, start, end, details)
    out += ["", "## 7. Who joins and who leaves", ""]
    out += [
        "Board appointments, removals, revocations and re-elections, dated by inscription. "
        "The full history is in `officer_events`; who holds each position today, in the "
        "`current_officers` view (local database only).",
        "",
    ]
    out += _table(headers, rows)

    review = conn.execute(
        """
        SELECT a.pub_date, a.company_name, a.province, c.name AS watched, r.score, r.reason
        FROM review_queue r
        JOIN announcements a USING (document_id, number)
        JOIN companies c ON c.company_id = r.company_id
        WHERE a.pub_date BETWEEN ? AND ?
        ORDER BY r.score DESC, a.pub_date DESC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out += ["", "## Review queue: similar names, never matched", ""]
    out += _table(
        ["Published", "Name in BORME", "Province", "Resembles", "Score", "Reason"],
        [[r[0], r[1], r[2], r[3], f"{r[4]:.1f}", r[5]] for r in review],
    )

    out += ["", "## Coverage of this window", ""]
    out += [f"- {key}: **{value}**" for key, value in coverage.items()]
    out += [
        "",
        "## Not covered",
        "",
        "- Section C (legal notices: general meetings, creditor notices): free prose, "
        "another parser.",
        "- Competitors: new companies of the sector by corporate purpose.",
        "- The BORME publishes no tax ID and no deal values.",
        "",
    ]
    return "\n".join(out)
