"""Corpus scans: ground-truth closure, noise counts, evaluation and triage."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from borme_radar.cache import DiskCache
from borme_radar.corpus import (
    BareNameCollisions,
    Relations,
    SharedOfficers,
    SheetStability,
    SignalHits,
    TokenNoise,
    scan,
)
from borme_radar.discovery import GroupIndex
from borme_radar.evaluate import score, union
from borme_radar.matching import Matcher, WatchIndex
from borme_radar.source import SUMMARY_URL
from borme_radar.triage import score_one, signal_weights, tier_of
from conftest import announcement, document


class Fake:
    """Feed synthetic announcements to collectors the way ``scan`` does."""

    def __init__(self, watched: dict[str, int]) -> None:
        index = WatchIndex()
        for norm, company_id in watched.items():
            index.add_name(norm, company_id)
        self.matcher = Matcher(index)

    def feed(self, collectors: list[object], *anns) -> None:  # type: ignore[no-untyped-def]
        from borme_radar.corpus import Seen
        from borme_radar.normalize import normalize_name

        _, doc = document("D", date(2026, 3, 2), *anns)
        for ann in doc.announcements:
            watched = self.matcher.watched(ann.company_name, doc.province, ann.sheet)
            seen = Seen(doc, ann, normalize_name(ann.company_name), watched)
            for collector in collectors:
                collector.observe(seen)  # type: ignore[attr-defined]


def test_closure_follows_the_direction_of_each_relation() -> None:
    relations = Relations()
    Fake({"MATRIZ SA": 1}).feed(
        [relations],
        announcement(1, "HIJA SL", ("sole_shareholder_declared", "Socio único: MATRIZ SA")),
        announcement(2, "NIETA SL", ("sole_shareholder_declared", "Socio único: HIJA SL")),
        announcement(3, "MATRIZ SA", ("merger", "Sociedades absorbidas: ABSORBIDA SL")),
        # Upward ownership never propagates: the owner of a group company's buyer...
        announcement(4, "HIJA SL", ("sole_shareholder_change", "COMPRADOR AJENO SA")),
        announcement(5, "SPINOFF SL", ("split", "Beneficiarios de la segregación: HIJA SL")),
    )
    truth = relations.closure({"MATRIZ SA": 7})
    assert truth == {
        "HIJA SL": (7, 1),
        "ABSORBIDA SL": (7, 1),
        "NIETA SL": (7, 2),
    }
    assert "COMPRADOR AJENO SA" not in truth  # HIJA is owned BY it: no downward flow
    assert "SPINOFF SL" not in truth  # it transferred business TO a group company


def test_token_noise_excludes_watched_and_counts_companies_once() -> None:
    noise = TokenNoise(["MARCA", "MARCA NUEVA"])
    Fake({"MARCA SA": 1}).feed(
        [noise],
        announcement(1, "MARCA SA", ("website", "x")),
        announcement(2, "MARCA NUEVA SL", ("website", "x")),
        announcement(3, "MARCA NUEVA SL(2008)", ("website", "x")),
        announcement(4, "OTRA MARCA SL", ("website", "x")),
    )
    assert noise.unrelated(set()) == {
        "MARCA NUEVA": {"MARCA NUEVA SL"},
        "MARCA": {"MARCA NUEVA SL", "OTRA MARCA SL"},
    }
    assert noise.unrelated({"OTRA MARCA SL"})["MARCA"] == {"MARCA NUEVA SL"}


def test_bare_name_collisions_use_the_same_sample_on_both_sides() -> None:
    collisions = BareNameCollisions(buckets=1)  # sample everything
    Fake({}).feed(
        [collisions],
        announcement(1, "PERSONA UNO SL", ("website", "x")),
        announcement(2, "OTRA SL", ("appointment", "Adm. Unico: PERSONA UNO")),
    )
    table = {(c, w): (n, h) for c, w, n, h in collisions.table()}
    assert table[(8, 2)] == (1, 1)  # "PERSONA UNO": 11 chars -> bucket 8, 2 words, 1 hit


def test_sheet_stability_counts_renames_and_acts_after_extinction() -> None:
    stability = SheetStability()
    Fake({}).feed(
        [stability],
        announcement(1, "VIEJA SL", ("name_change", "NUEVA SL"), sheet="M 1"),
        announcement(2, "NUEVA SL", ("website", "x"), sheet="M 1"),
        announcement(3, "MUERTA SL", ("extinction", ""), sheet="M 2"),
        announcement(4, "MUERTA SL", ("website", "x"), sheet="M 2"),
    )
    s = stability.summary()
    assert (s["renamed_sheets"], s["after_rename"], s["after_rename_other_name"]) == (1, 1, 1)
    assert (s["extinct_sheets"], s["after_extinction"]) == (1, 1)


def test_evaluation_precision_and_recall() -> None:
    relations = Relations()
    index = GroupIndex({1: "G"}, {"MATRIZ SA": 1}, [], {"MARCA": 1})
    hits = SignalHits(index)
    Fake({"MATRIZ SA": 1}).feed(
        [relations, hits],
        announcement(1, "HIJA SL", ("sole_shareholder_declared", "Socio único: MATRIZ SA")),
        announcement(2, "MARCA DOS SL", ("sole_shareholder_declared", "Socio único: HIJA SL")),
        announcement(3, "MARCA AJENA SL", ("website", "x")),
    )
    truth = relations.closure({"MATRIZ SA": 1})
    by = {s.reason: s for s in score("relations", truth, relations.subjects, hits)}
    assert set(truth) == {"HIJA SL", "MARCA DOS SL"}
    assert (by["mention"].flagged, by["mention"].true_positive) == (1, 1)
    assert (by["token"].flagged, by["token"].true_positive, by["token"].precision) == (2, 1, 0.5)
    assert by["detector"].recall == 1.0
    assert union(truth, {"MARCA AJENA SL": (1, 1)})["MARCA AJENA SL"] == (1, 1)


def test_shared_officers_need_two_people() -> None:
    shared = SharedOfficers({"PERSONA UNO": {1}, "PERSONA DOS": {1}}, minimum=2)
    Fake({}).feed(
        [shared],
        announcement(1, "FILIAL SL", ("appointment", "Apoderado: PERSONA UNO;PERSONA DOS")),
        announcement(2, "AJENA SL", ("appointment", "Apoderado: PERSONA UNO")),
    )
    assert shared.members() == {"FILIAL SL": (1, 1)}
    assert shared.histogram() == {2: 1, 1: 1}


def test_scan_reads_only_the_cache_and_stops_when_partial(fixtures: Path, tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache")
    day = date(2026, 9, 22)
    cache.put(SUMMARY_URL.format(day=day), (fixtures / "sumario_20260922.json").read_bytes())
    for doc_id in ("BORME-A-2026-183-07", "BORME-A-2026-183-28"):
        url = f"https://www.boe.es/diario_borme/xml.php?id={doc_id}"
        cache.put(url, (fixtures / f"{doc_id}.xml").read_bytes())
    full = scan(tmp_path / "cache", day, day, Matcher(WatchIndex()), [])
    assert (full.documents, full.announcements, full.partial) == (2, 19, False)
    part = scan(tmp_path / "cache", day, day, Matcher(WatchIndex()), [], limit_documents=1)
    assert (part.documents, part.partial) == (1, True)


def test_triage_orders_but_does_not_decide() -> None:
    weights = {"mention": 50.0, "token": 10.0}
    today = date(2026, 9, 23)
    strong = score_one("mention", 3, date(2026, 9, 1), today, True, weights)
    weak = score_one("token", 1, date(2024, 1, 1), today, False, weights)
    assert strong > weak
    assert tier_of(strong) == "review first"
    assert tier_of(weak) == "long tail"


def test_triage_weights_come_from_the_broader_ground_truth(tmp_path: Path) -> None:
    rows = [
        {"truth": "relations", "reason": "token", "precision": 0.1},
        {"truth": "relations_or_shared_officers", "reason": "token", "precision": 0.5},
    ]
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps({"signals": rows}), encoding="utf-8")
    assert signal_weights(path)["token"] == 25.0
    assert signal_weights(tmp_path / "missing.json")["mention"] == 50.0
