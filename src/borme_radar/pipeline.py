"""Process parsed documents: match, keep what matters, derive the history, discover.

For every announcement of every document (Sections A and B):

1. match it to a watched company (sheet, exact name or alias; see ``matching.py``);
2. if matched: store it, learn its sheet when matched by name (and re-link the stored
   orphans of that sheet), keep old and new names as aliases on a rename, and derive
   the record observations and the officer events;
3. if it only resembles a watched company: store it in the review queue;
4. otherwise run the discovery signals; a hit is stored as a candidate;
5. everything else is only counted (``store_all`` keeps it, as version 1 did).

Counters of every document (acts by type and scope, publication lag) are stored even
when the announcements are not, so the coverage statistics need no personal data.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from borme_radar import store
from borme_radar.discovery import GroupIndex, detect
from borme_radar.matching import Matcher
from borme_radar.models import Announcement, Document, DocumentRef
from borme_radar.normalize import normalize_name, plain
from borme_radar.officers import EVENT_BY_ACT, SCOPE_OF_ROLE, holdings
from borme_radar.record import observations_of_act
from borme_radar.relations import ends_as_company

log = logging.getLogger("borme_radar")

PairsOf = Callable[[str], set[str]]


@dataclass(slots=True)
class RunCounters:
    values: Counter[str] = field(default_factory=Counter)

    def add(self, other: Counter[str] | dict[str, int]) -> None:
        self.values.update(other)

    def __getitem__(self, key: str) -> int:
        return self.values.get(key, 0)


def _fact_date(doc: Document, ann: Announcement) -> str:
    return (ann.inscription_date or doc.pub_date).isoformat()


def _personal_holding(ann: Announcement) -> bool:
    """Sole shareholder is a natural person: the company is someone's own vehicle."""
    for act in ann.acts:
        if act.act_type == "sole_shareholder_declared" and ":" in act.detail:
            owner = act.detail.split(":", 1)[1].strip()
            return bool(owner) and not ends_as_company(owner)
        if act.act_type == "sole_shareholder_change":
            return bool(act.detail) and not ends_as_company(act.detail)
    return False


def derive_rows(
    company_id: int,
    document_id: str,
    number: int,
    fact_date: str,
    acts: Iterable[tuple[int, str, str]],
) -> tuple[list[tuple], list[tuple]]:
    """Observation rows and officer-event rows of one watched announcement.

    ``acts`` are ``(seq, act_type, detail)``.
    """
    observations: list[tuple] = []
    events: list[tuple] = []
    for seq, act_type, detail in acts:
        for obs in observations_of_act(act_type, detail):
            observations.append((company_id, obs.field, obs.value, fact_date, document_id, number))
        event = EVENT_BY_ACT.get(act_type)
        if event is None:
            continue
        for h in holdings(detail):
            events.append(
                (
                    company_id,
                    h.holder,
                    plain(h.holder),
                    h.role,
                    h.role_raw,
                    event,
                    SCOPE_OF_ROLE.get(h.role, "company"),
                    fact_date,
                    int(h.is_company),
                    document_id,
                    number,
                    seq,
                )
            )
    return observations, events


class Pipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        matcher: Matcher,
        groups: GroupIndex,
        *,
        store_all: bool = False,
        pairs_of: PairsOf | None = None,
    ) -> None:
        self.conn = conn
        self.matcher = matcher
        self.groups = groups
        self.store_all = store_all
        self.pairs_of = pairs_of
        self._extra: Counter[str] = Counter()

    # ------------------------------------------------------------------ documents

    def process_document(self, ref: DocumentRef, doc: Document) -> Counter[str]:
        """Process one document in one transaction; returns its counters."""
        c: Counter[str] = Counter()
        act_counts: Counter[tuple[str, str]] = Counter()
        lag_counts: Counter[int] = Counter()
        with self.conn:
            # The document row goes first (announcements reference it); its counters
            # are rewritten at the end with the final numbers.
            store.upsert_document(self.conn, ref, doc, self._doc_counts(doc, 0), {}, {})
            for ann in doc.announcements:
                for act in ann.acts:
                    act_counts[(act.act_type, act.scope)] += 1
                if doc.section == "A" and ann.inscription_date:
                    lag_counts[(doc.pub_date - ann.inscription_date).days] += 1
                c[self._announcement(doc, ann)] += 1
            stored = c["watched"] + c["review"] + c["candidate"] + c["all"]
            store.upsert_document(
                self.conn, ref, doc, self._doc_counts(doc, stored), act_counts, lag_counts
            )
        c["documents"] += 1
        c["announcements"] += len(doc.announcements)
        c["acts"] += sum(len(a.acts) for a in doc.announcements)
        c["discarded"] = c.pop("discard", 0)
        c.update(self._extra)
        self._extra = Counter()
        return c

    @staticmethod
    def _doc_counts(doc: Document, stored: int) -> dict[str, int]:
        return {
            "announcements": len(doc.announcements),
            "acts": sum(len(a.acts) for a in doc.announcements),
            "with_sheet": sum(1 for a in doc.announcements if a.sheet),
            "unparsed_prefix": sum(1 for a in doc.announcements if a.unparsed_prefix),
            "without_acts": sum(1 for a in doc.announcements if not a.acts),
            "stored": stored,
        }

    def _announcement(self, doc: Document, ann: Announcement) -> str:
        """Match and store one announcement; returns how it was kept."""
        fact_date = _fact_date(doc, ann)
        result = self.matcher.match(
            ann.company_name, doc.province, ann.sheet, date.fromisoformat(fact_date)
        )
        norm = normalize_name(ann.company_name)
        row: dict[str, object] = {
            "document_id": doc.document_id,
            "number": ann.number,
            "pub_date": doc.pub_date.isoformat(),
            "fact_date": fact_date,
            "province": doc.province,
            "company_name": ann.company_name,
            "company_norm": norm,
            "sheet": ann.sheet,
            "registry_data": ann.registry_data,
            "company_id": result.company_id,
            "match_via": result.via if result.company_id else None,
        }
        acts = [(a.seq, a.act_type, a.heading, a.detail, a.scope, a.priority) for a in ann.acts]

        if result.company_id is not None:
            self._store(row, "watched", acts)
            self._watched(doc, ann, result.company_id, result.via, fact_date)
            return "watched"
        if result.suggestion is not None:
            self._store(row, "review", acts)
            store.upsert_review(
                self.conn,
                doc.document_id,
                ann.number,
                result.suggestion,
                result.score,
                result.reason,
            )
            return "review"
        pairs = self.pairs_of(" ".join(a.detail for a in ann.acts)) if self.pairs_of else ()
        candidate = detect(
            ann.company_name,
            [(a.act_type, a.detail) for a in ann.acts],
            self.groups,
            pairs,
            _personal_holding(ann) if pairs else False,
        )
        if candidate is not None:
            self._store(row, "candidate", acts)
            store.upsert_candidate(
                self.conn,
                company_norm=norm,
                province=doc.province,
                company_name=ann.company_name,
                sheet=ann.sheet,
                group_id=candidate.group_id,
                reason=candidate.reason,
                evidence=candidate.evidence,
                fact_date=fact_date,
                document_id=doc.document_id,
                number=ann.number,
            )
            self._extra[f"candidate_{candidate.reason}"] += 1
            return "candidate"
        if self.store_all:
            self._store(row, "all", acts)
            return "all"
        return "discard"

    def _store(self, row: dict[str, object], kept_as: str, acts: list[tuple]) -> None:
        document_id, number = str(row["document_id"]), int(str(row["number"]))
        store.upsert_announcement(self.conn, {**row, "kept_as": kept_as})
        store.upsert_acts(self.conn, document_id, number, acts)
        if kept_as == "watched":  # it may have been in review or a candidate before
            for table in ("review_queue", "candidate_announcements"):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE document_id = ? AND number = ?",
                    (document_id, number),
                )

    def _watched(
        self, doc: Document, ann: Announcement, company_id: int, via: str, fact_date: str
    ) -> None:
        act_types = [a.act_type for a in ann.acts]
        if via == "name" and self.matcher.sheet_learnable(doc.province, ann.sheet, act_types):
            assert ann.sheet is not None
            if store.learn_sheet(self.conn, doc.province, ann.sheet, company_id, doc.document_id):
                self.matcher.learn_sheet(doc.province, ann.sheet, company_id)
                self._extra["sheets_learned"] += 1
                self._extra["relinked"] += self.relink(doc.province, ann.sheet, company_id)
        for act in ann.acts:
            if act.act_type == "name_change" and act.detail:
                # Old and new names both keep matching, whether or not the rename has
                # been applied to the record yet.
                for name in (ann.company_name, act.detail):
                    if store.add_alias(self.conn, name, company_id, "name_change", doc.document_id):
                        self.matcher.index.add_name(normalize_name(name), company_id)
                        self._extra["aliases"] += 1
        self._derive(
            company_id,
            doc.document_id,
            ann.number,
            fact_date,
            [(a.seq, a.act_type, a.detail) for a in ann.acts],
        )

    def _derive(
        self,
        company_id: int,
        document_id: str,
        number: int,
        fact_date: str,
        acts: list[tuple[int, str, str]],
    ) -> None:
        observations, events = derive_rows(company_id, document_id, number, fact_date, acts)
        self._extra["observations"] += store.insert_observations(self.conn, observations)
        self._extra["officer_events"] += store.insert_officer_events(self.conn, events)

    def relink(self, province: str, sheet: str, company_id: int) -> int:
        """Attach the stored orphans of a sheet that has just been learned."""
        orphans = store.orphans_of_sheet(self.conn, province, sheet)
        for document_id, number in orphans:
            store.link(self.conn, document_id, number, company_id)
            (fact_date,) = self.conn.execute(
                "SELECT fact_date FROM announcements WHERE document_id = ? AND number = ?",
                (document_id, number),
            ).fetchone()
            acts = self.conn.execute(
                "SELECT seq, act_type, detail FROM acts WHERE document_id = ? AND number = ?"
                " ORDER BY seq",
                (document_id, number),
            ).fetchall()
            self._derive(company_id, document_id, number, fact_date, [tuple(a) for a in acts])
        return len(orphans)

    # ------------------------------------------------------------------ candidates

    def rejudge(self) -> Counter[str]:
        """Judge every pending (and machine-dismissed) candidate with today's rules.

        Two reasons to drop one: it now matches a watched company (a better match, a
        new alias or sheet), or today's signals no longer fire on it. Proposing to add
        something that is already there, or that the rules no longer support, is the
        worst noise this list can have. A dismissed candidate comes back if the rules
        fire again; nothing a person decided is touched.
        """
        verdicts: Counter[str] = Counter()
        rows = store.candidates_to_judge(self.conn)
        with self.conn:
            for row in rows:
                if self.matcher.watched(row["company_name"], row["province"], row["sheet"]):
                    store.set_candidate_verdict(self.conn, row["candidate_id"], "incorporated")
                    verdicts["incorporated"] += 1
                    continue
                acts = [
                    tuple(part.split("\x1f", 1))
                    for part in (row["acts"] or "").split("\x1e")
                    if "\x1f" in part
                ]
                text = " ".join(detail for _, detail in acts)
                pairs = self.pairs_of(text) if self.pairs_of else ()
                candidate = detect(row["company_name"], acts, self.groups, pairs)
                if candidate is None:
                    if row["status"] != "dismissed":
                        verdicts["dismissed"] += 1
                    store.set_candidate_verdict(self.conn, row["candidate_id"], "dismissed")
                else:
                    if row["status"] == "dismissed":
                        verdicts["revived"] += 1
                    store.set_candidate_verdict(
                        self.conn,
                        row["candidate_id"],
                        "pending",
                        candidate.group_id,
                        candidate.reason,
                        candidate.evidence,
                    )
        store.refresh_candidate_aggregates(self.conn)
        return verdicts
