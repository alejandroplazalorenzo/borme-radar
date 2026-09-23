"""Markdown rendering of watchlist alerts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from borme_radar.acts import ACT_TYPES
from borme_radar.matching import Match, WatchEntry
from borme_radar.store import StoredAnnouncement

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_DETAIL_MAX = 160


def _priority(act_type: str) -> str:
    known = ACT_TYPES.get(act_type)
    return known.priority if known else "low"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _short(text: str) -> str:
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1].rstrip() + "…"


def _top_priority(items: Sequence[StoredAnnouncement]) -> str:
    priorities = [_priority(act.act_type) for a in items for act in a.acts]
    return min(priorities, key=_PRIORITY_ORDER.__getitem__) if priorities else "-"


def _act_table(items: Sequence[StoredAnnouncement], details: bool) -> list[str]:
    header = "| Published | Province | Announcement | Act | Priority |"
    rule = "|---|---|---|---|---|"
    if details:
        header += " Detail |"
        rule += "---|"
    lines = [header, rule]
    for ann in items:
        for act in ann.acts:
            label = ACT_TYPES[act.act_type].label if act.act_type in ACT_TYPES else act.act_type
            row = (
                f"| {ann.pub_date} | {_cell(ann.province)} | {ann.document_id} #{ann.number} "
                f"| {label} | {_priority(act.act_type)} |"
            )
            if details:
                row += f" {_cell(_short(act.detail))} |"
            lines.append(row)
    return lines


def render_alerts(
    watchlist: Sequence[WatchEntry],
    matches: Sequence[Match],
    announcements: Mapping[str, Sequence[StoredAnnouncement]],
    *,
    since: date | None,
    threshold: float,
    details: bool = True,
) -> str:
    """Render confirmed alerts, then fuzzy candidates that need a human decision."""
    confirmed = [m for m in matches if m.status == "confirmed"]
    review = [m for m in matches if m.status == "needs_review"]
    hit_names = {m.watch.norm for m in matches}

    out: list[str] = ["# BORME watchlist alerts", ""]
    out.append(
        f"Watchlist: {len(watchlist)} companies. Window: "
        f"{'since ' + since.isoformat() if since else 'all stored gazettes'}. "
        f"Fuzzy threshold: {threshold:g}."
    )
    out.append("")
    out.append(
        f"- Confirmed (exact normalised name): {len(confirmed)} companies\n"
        f"- Needs review (similar name, not confirmed): {len(review)} candidates\n"
        f"- Watchlist entries without any hit: {len(watchlist) - len(hit_names)}"
    )

    out += ["", "## Confirmed alerts", ""]
    if not confirmed:
        out.append("No exact matches in this window.")
    for m in confirmed:
        items = announcements.get(m.company_norm, [])
        out += [
            f"### {m.company_name}",
            "",
            f"Watchlist entry: {m.watch.name} | announcements: {len(items)} "
            f"| highest priority: {_top_priority(items)}",
            "",
            *_act_table(items, details),
            "",
        ]

    out += ["## Needs review (fuzzy, NOT confirmed)", ""]
    if not review:
        out.append("No similar names above the threshold.")
    else:
        out += [
            "| Watchlist entry | Name in BORME | Score | Reason | Announcements |",
            "|---|---|---|---|---|",
        ]
        for m in review:
            n_items = len(announcements.get(m.company_norm, []))
            out.append(
                f"| {_cell(m.watch.name)} | {_cell(m.company_name)} | {m.score:.1f} "
                f"| {_cell(m.reason)} | {n_items} |"
            )

    missing = [w.name for w in watchlist if w.norm not in hit_names]
    out += ["", "## Watchlist entries without hits", ""]
    out += [f"- {name}" for name in missing] or ["(none)"]
    out.append("")
    return "\n".join(out)
