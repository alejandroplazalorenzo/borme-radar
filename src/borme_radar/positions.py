"""Who holds each position today, worked out from each company's officer events.

The gazette publishes changes, never the body as it stands: the positions held today are
what the changes leave when they are applied in date order. A position is one holder in
one role of one company, the holder told by ``officers.holder_key`` (inside a company and
a role, the same words in another order are the same person). It is held while its last
event opened it, an appointment or a re-election, and no later day closed it. A later
appointment closes it without any removal being published in two cases:

* it fills the same seat. A seat has one holder at a time: the sole director, and the
  auditor of each kind of accounts (``officers.auditor_class``);
* it is an appointment to the governing body under another form (``BODY_FORM``): the
  body is that form from then on, and the posts of the earlier form are over.

Appointments of the same day never close each other, and nothing reopens a position once
it is closed. :func:`refresh` keeps the result in ``open_roles``, read through the view
``officers_now``; the tests set it against a replay of every event in order.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from itertools import groupby
from operator import attrgetter
from typing import NamedTuple

from borme_radar.officers import ROLES, auditor_class, holder_key

# The forms a governing body takes and the roles that make each: three with a role of
# their own, and the board with its posts. Roles outside every form (attorneys, auditors,
# liquidators, a plain "Adm.") do not change with the form of the body.
_OWN_FORM = {
    "joint_director": "joint",
    "joint_several_director": "joint_several",
    "sole_director": "sole",
}
_BOARD_POSTS = frozenset(
    {"board_member", "chair", "managing_director", "secretary", "vice_chair", "vice_secretary"}
)
BODY_FORM = {
    role: _OWN_FORM.get(role, "board")
    for role in ROLES
    if role in _OWN_FORM or role in _BOARD_POSTS
}
_FORMS = frozenset(BODY_FORM.values())
_OPENS = frozenset({"appointment", "reelection"})


class Event(NamedTuple):
    """A row of ``role_changes``, in the order the query below reads it."""

    company_id: int
    day: str  # ISO date
    event_id: int
    holder: str
    role: str
    role_raw: str
    kind: str

    @property
    def opens(self) -> bool:
        return self.kind in _OPENS

    @property
    def seat(self) -> str:
        """The seat only one appointment at a time can fill ("" when the role has none)."""
        if self.role == "auditor":
            return "auditor: " + auditor_class(self.role_raw)
        return "sole director" if self.role == "sole_director" else ""


_EVENTS = """
    SELECT company_id, day, change_id, holder, role, label, kind
    FROM role_changes ORDER BY company_id, day, change_id
"""


def _still_open(timeline: list[Event]) -> Iterator[int]:
    """Events that opened the current positions of one company (``timeline`` in order)."""
    persons: dict[str, str] = {}
    last_event: dict[tuple[str, str], Event] = {}  # (person, role) -> its last event
    seat_taken: dict[str, str] = {}  # seat -> last day an appointment filled it
    body_forms: dict[str, set[str]] = {}  # day -> forms of the body appointed that day
    for ev in timeline:
        if ev.holder not in persons:
            persons[ev.holder] = holder_key(ev.holder)
        last_event[persons[ev.holder], ev.role] = ev
        if ev.opens:
            if ev.seat:
                seat_taken[ev.seat] = ev.day
            if ev.role in BODY_FORM:
                body_forms.setdefault(ev.day, set()).add(BODY_FORM[ev.role])
    # The last day each form was left out of the appointments to the body.
    form_left_out = {form: day for day, forms in body_forms.items() for form in _FORMS - forms}
    for ev in last_event.values():
        ended_by_seat = seat_taken.get(ev.seat, "") > ev.day
        ended_by_form = form_left_out.get(BODY_FORM.get(ev.role, ""), "") > ev.day
        if ev.opens and not ended_by_seat and not ended_by_form:
            yield ev.event_id


def current_event_ids(events: Iterable[Event]) -> list[int]:
    """Events that opened a current position; ``events`` sorted by company, day and id."""
    by_company = groupby(events, attrgetter("company_id"))
    return sorted(i for _, timeline in by_company for i in _still_open(list(timeline)))


def refresh(conn: sqlite3.Connection) -> int:
    """Rebuild ``open_roles`` from ``role_changes``; returns the positions."""
    held = current_event_ids(Event._make(row) for row in conn.execute(_EVENTS))
    with conn:
        conn.execute("DELETE FROM open_roles")
        conn.executemany("INSERT INTO open_roles (change_id) VALUES (?)", ((i,) for i in held))
    return len(held)


def reordered_names(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """How much the person key changes: (names as published per company and role, persons
    with two names or more, the names those persons have).

    Two names are one person when they belong to the same company and role and have the
    same words in another order. Each name is taken as first published (SQLite returns
    the other columns of the row that gives the ``MIN``).
    """
    first_spelling = conn.execute(
        "SELECT company_id, role, holder, MIN(change_id) FROM role_changes"
        " GROUP BY company_id, role, holder_key"
    ).fetchall()
    persons = Counter(
        (company, role, holder_key(holder)) for company, role, holder, _ in first_spelling
    )
    merged = [n for n in persons.values() if n > 1]
    return len(first_spelling), len(merged), sum(merged)
