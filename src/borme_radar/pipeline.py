"""What happens to each announcement of a parsed document.

An announcement goes through these steps and stops at the first that applies:

1. the registry sheet of a watched company, or
2. its identical normalised name or an alias: stored as ``watched``. The observations
   of the record and the officer events are derived from its acts, both names of a
   rename become aliases, and a match by name teaches the sheet and re-links the stored
   announcements that carry it;
3. a similar name: stored as ``review``, with the watched company it resembles;
4. the registry sheet of a candidate found by mention: stored with that candidate;
5. a discovery signal (:func:`discovery.detect`): stored as ``candidate``;
6. anything else is only counted, unless every announcement is kept (``store_all``).

Every document also leaves its tallies: acts by type and scope, and the days from
inscription to publication. The later passes of a sweep (``known_only``) run only steps
1, 2 and 4. :meth:`Pipeline.rejudge` judges every candidate again with the current rules.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import KW_ONLY, dataclass, field
from datetime import date

from borme_radar import store
from borme_radar.discovery import Candidate, GroupIndex, detect
from borme_radar.matching import Matcher
from borme_radar.models import Announcement, Document, DocumentRef
from borme_radar.normalize import normalize_name, plain
from borme_radar.officers import EVENT_BY_ACT, SCOPE_OF_ROLE, Holding, holdings
from borme_radar.record import observations_of_act
from borme_radar.relations import ends_as_company

log = logging.getLogger("borme_radar")

PairsOf = Callable[[str], set[str]]
Kept = str  # how an announcement was kept: watched | review | candidate | all | discard


def _personal_holding(ann: Announcement) -> bool:
    """Does one of the acts name a natural person as the sole shareholder? Read from a
    declaration or a change of sole shareholder; the first of them decides."""
    for act in ann.acts:
        if act.act_type == "sole_shareholder_change":
            owner = act.detail
        elif act.act_type == "sole_shareholder_declared" and ":" in act.detail:
            owner = act.detail.partition(":")[2].strip()
        else:
            continue
        return bool(owner) and not ends_as_company(owner)
    return False


def _event_row(
    company_id: int, h: Holding, what: str, where: tuple[str, str, int], seq: int
) -> tuple:
    fact_date, document_id, number = where
    scope = SCOPE_OF_ROLE.get(h.role, "company")
    who = (h.holder, plain(h.holder), h.role, h.role_raw)
    return (company_id, *who, what, scope, fact_date, int(h.is_company), document_id, number, seq)


def derive_rows(
    company_id: int,
    document_id: str,
    number: int,
    fact_date: str,
    acts: Iterable[tuple[int, str, str]],
) -> tuple[list[tuple], list[tuple]]:
    """Record observations and officer events of one announcement of a watched company.

    ``acts`` are ``(seq, act_type, detail)``; the rows are ready for
    :func:`store.insert_observations` and :func:`store.insert_officer_events`.
    """
    where = (fact_date, document_id, number)
    acts = list(acts)
    observations = [
        (company_id, o.field, o.value, *where)
        for _seq, act_type, detail in acts
        for o in observations_of_act(act_type, detail)
    ]
    events = [
        _event_row(company_id, h, what, where, seq)
        for seq, act_type, detail in acts
        if (what := EVENT_BY_ACT.get(act_type)) is not None
        for h in holdings(detail)
    ]
    return observations, events


def _document_counts(doc: Document, stored: int) -> dict[str, int]:
    anns = doc.announcements
    return {
        "announcements": len(anns),
        "acts": sum(len(a.acts) for a in anns),
        "with_sheet": sum(bool(a.sheet) for a in anns),
        "unparsed_prefix": sum(bool(a.unparsed_prefix) for a in anns),
        "without_acts": sum(not a.acts for a in anns),
        "stored": stored,
    }


@dataclass
class Pipeline:
    """Processes parsed documents against the watchlist and the discovery index.

    ``known_only`` (the later passes of a sweep): exact matches and followed sheets only.
    ``store_all`` keeps every announcement; ``pairs_of`` turns the person signal on.
    """

    conn: sqlite3.Connection
    matcher: Matcher
    groups: GroupIndex
    _: KW_ONLY
    store_all: bool = False
    pairs_of: PairsOf | None = None
    known_only: bool = False
    # Registry sheet of each candidate found by mention -> candidate (step 4).
    followed: dict[tuple[str, str], int] = field(init=False)
    _extra: Counter[str] = field(init=False, default_factory=Counter)

    def __post_init__(self) -> None:
        self.followed = store.followed_sheets(self.conn)

    # ------------------------------------------------------------------ documents

    def process_document(self, ref: DocumentRef, doc: Document) -> Counter[str]:
        """Store what is kept of one document, with its tallies, and return its counters."""
        acts = Counter((a.act_type, a.scope) for ann in doc.announcements for a in ann.acts)
        lags = Counter(
            (doc.pub_date - ann.inscription_date).days
            for ann in doc.announcements
            if doc.section == "A" and ann.inscription_date
        )
        # A known-only pass over a document read before leaves its tallies as they are: only
        # the number of kept announcements can change.
        recount = not (self.known_only and store.has_document(self.conn, doc.document_id))
        with self.conn:
            if recount:  # announcements reference the document: its row goes first
                store.upsert_document(self.conn, ref, doc, _document_counts(doc, 0), {}, {})
            kept = Counter(self._announcement(doc, ann) for ann in doc.announcements)
            stored = sum(kept[k] for k in ("watched", "review", "candidate", "all"))
            if recount:
                counts = _document_counts(doc, stored)
                store.upsert_document(self.conn, ref, doc, counts, acts, lags)
            elif stored:
                store.refresh_stored_count(self.conn, doc.document_id)
        kept["discarded"] = kept.pop("discard", 0)
        kept.update(
            documents=1,
            announcements=len(doc.announcements),
            acts=sum(acts.values()),
        )
        kept.update(self._extra)
        self._extra = Counter()
        return kept

    def _announcement(self, doc: Document, ann: Announcement) -> Kept:
        """Match one announcement, keep it if it matters and say under which heading it was kept."""
        fact_date = (ann.inscription_date or doc.pub_date).isoformat()
        find = self.matcher.exact if self.known_only else self.matcher.match
        found = find(ann.company_name, doc.province, ann.sheet, date.fromisoformat(fact_date))
        row: dict[str, object] = {
            "document_id": doc.document_id,
            "number": ann.number,
            "pub_date": doc.pub_date.isoformat(),
            "fact_date": fact_date,
            "province": doc.province,
            "company_name": ann.company_name,
            "company_norm": normalize_name(ann.company_name),
            "sheet": ann.sheet,
            "registry_data": ann.registry_data,
            "company_id": found.company_id,
            "match_via": found.via if found.company_id else None,
        }
        acts = [(a.seq, a.act_type, a.heading, a.detail, a.scope, a.priority) for a in ann.acts]
        company = found.company_id
        if company is not None:
            self._store(row, "watched", acts)
            if found.via == "name":
                self._learn_sheet(doc, ann, company)
            self._keep_names(doc, ann, company)
            stated = [(a.seq, a.act_type, a.detail) for a in ann.acts]
            self._derive(company, doc.document_id, ann.number, fact_date, stated)
            return "watched"
        if found.suggestion is not None and not self.known_only:
            self._store(row, "review", acts)
            store.change_notice(
                self.conn, "near", doc.document_id, ann.number,
                company=found.suggestion, score=found.score, reason=found.reason,
            )  # fmt: skip
            return "review"
        followed = self.followed.get((doc.province, ann.sheet)) if ann.sheet else None
        if followed is not None:
            self._store(row, "candidate", acts)
            store.change_notice(self.conn, "followed", doc.document_id, ann.number, lead=followed)
            self._extra["followed"] += 1
            return "candidate"
        if self.known_only:
            return "discard"
        if self._discovered(doc, ann, row, acts):
            return "candidate"
        if self.store_all:
            self._store(row, "all", acts)
            return "all"
        return "discard"

    def _discovered(
        self, doc: Document, ann: Announcement, row: dict[str, object], acts: list[tuple]
    ) -> bool:
        """Step 5: store the announcement with a candidate if a discovery signal fires.

        A candidate found by mention has its sheet followed from then on (step 4).
        """
        pairs = self.pairs_of(" ".join(a.detail for a in ann.acts)) if self.pairs_of else ()
        candidate = detect(
            ann.company_name,
            [(a.act_type, a.detail) for a in ann.acts],
            self.groups,
            pairs,
            bool(pairs) and _personal_holding(ann),
        )
        if candidate is None:
            return False
        self._store(row, "candidate", acts)
        fact_date = str(row["fact_date"])
        candidate_id = store.upsert_candidate(
            self.conn,
            company_norm=str(row["company_norm"]),
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
        if candidate.reason == "mention" and ann.sheet:
            self.followed.setdefault((doc.province, ann.sheet), candidate_id)
        self._extra[f"candidate_{candidate.reason}"] += 1
        return True

    def _store(self, row: dict[str, object], kept_as: str, acts: list[tuple]) -> None:
        document_id, number = str(row["document_id"]), int(str(row["number"]))
        store.upsert_announcement(self.conn, {**row, "kept_as": kept_as})
        store.upsert_acts(self.conn, document_id, number, acts)
        if kept_as == "watched":
            store.change_notice(self.conn, "untied", document_id, number)

    def _keep_names(self, doc: Document, ann: Announcement, company_id: int) -> None:
        """Make both names of a rename known: the one this announcement is published under
        and the new one its act gives become aliases of the company, in the database and
        in the matcher at once."""
        renamed = [a.detail for a in ann.acts if a.act_type == "name_change" and a.detail]
        names = [n for new in renamed for n in (ann.company_name, new)]
        origin = (company_id, "name_change", doc.document_id)
        added = [n for n in names if store.add_alias(self.conn, n, *origin)]
        for name in added:
            self.matcher.index.add_name(normalize_name(name), company_id)
        if added:
            self._extra["aliases"] += len(added)

    def _learn_sheet(self, doc: Document, ann: Announcement, company_id: int) -> None:
        kinds = [a.act_type for a in ann.acts]
        sheet = ann.sheet
        if sheet is None or not self.matcher.can_learn(doc.province, sheet, kinds):
            return
        if store.learn_sheet(self.conn, doc.province, sheet, company_id, doc.document_id):
            self.matcher.learn_sheet(doc.province, sheet, company_id)
            self._extra["sheets_learned"] += 1
            self._extra["relinked"] += self.relink(doc.province, sheet, company_id)

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
        """Link the unmatched stored announcements of a sheet just learned to its company.

        Their observations and officer events are derived as well. Returns how many.
        """
        orphans = store.orphans_of_sheet(self.conn, province, sheet)
        for document_id, number in orphans:
            store.change_notice(self.conn, "watched", document_id, number, company=company_id)
            key = (document_id, number)
            (fact_date,) = self.conn.execute(
                "SELECT fact_day FROM notices WHERE doc_id = ? AND num = ?", key
            ).fetchone()
            acts = self.conn.execute(
                "SELECT seq, act_type, detail FROM notice_acts WHERE doc_id = ? AND num = ?"
                " ORDER BY seq",
                key,
            ).fetchall()
            self._derive(company_id, document_id, number, fact_date, [tuple(a) for a in acts])
        return len(orphans)

    # ------------------------------------------------------------------ candidates

    def _verdict(self, row: sqlite3.Row) -> tuple[str, Candidate | None]:
        """Judge a stored candidate again, on its stored acts and with the current rules.

        ``watched`` when it is in the watchlist now, ``open`` when a signal still fires,
        ``gone`` otherwise.
        """
        if self.matcher.watched(row["firm"], row["province"], row["sheet"]):
            return "watched", None
        parts = (part.split("\x1f", 1) for part in (row["acts"] or "").split("\x1e"))
        acts = [(kind, text) for kind, text in (p for p in parts if len(p) == 2)]
        text = " ".join(detail for _, detail in acts)
        pairs = self.pairs_of(text) if self.pairs_of else ()
        candidate = detect(row["firm"], acts, self.groups, pairs)
        return ("open" if candidate else "gone"), candidate

    def rejudge(self) -> Counter[str]:
        """Judge every candidate again and refresh their counts and dates.

        Returns how many changed state, by the state they changed to (``watched``,
        ``gone``, ``open`` again).
        """
        verdicts: Counter[str] = Counter()
        with self.conn:
            for row in store.candidates_to_judge(self.conn):
                status, candidate = self._verdict(row)
                if status != row["state"]:
                    verdicts[status] += 1
                found = (
                    (candidate.group_id, candidate.reason, candidate.evidence) if candidate else ()
                )
                store.set_candidate_verdict(self.conn, row["lead_id"], status, *found)
        store.refresh_candidate_aggregates(self.conn)
        return verdicts
