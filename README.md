# borme-radar

Keeps a company record in sync with Spain's Official Gazette of the Companies Register
(BORME, Sections A and B) and discovers **new companies of watched groups**. It
downloads the daily gazette from the BOE open-data API, parses every announcement into
typed acts, learns the **registry sheet** of each watched company from its first exact
name match and follows it from then on, keeps the company record as **dated
observations** (name, address, capital, status, corporate purpose), keeps the history of
**officer appointments and removals**, and flags companies that appear to belong to a
watched group but are not in the watchlist. Nothing is written to the record or added to
the watchlist without a person: `sync` shows the changes and writes only with `--apply`,
and discovery candidates are ordered, never accepted.

## Why

A company record fed from annual accounts is stale by design: the Spanish Companies Act
allows six months after year-end to approve the accounts (art. 164 LSC) and one more
month to deposit them (art. 279 LSC), so they describe the company as it was at least
seven months earlier (statutory deadlines, not measured here). The Registro publishes
renames, changes of address, capital operations, dissolutions and new directors much
sooner: over the last 12 months the median gap between the inscription date printed in
each announcement and its publication was **7 days** (p10 7, p90 10; 63.0% of
announcements exactly 7 days; n = 597,157, measured below).

## How it works

```
 BOE open-data API: daily summary (JSON) + one XML per province, Sections A and B
        | up to 4 parallel downloads, permanent atomic cache, a failed day is skipped and listed
        v
 parser: acts from a closed catalogue of headings (accents optional), Section B tables,
         registry sheet ("H M 786863"), scope of each act: board / attorney / auditor / company
        v
 matcher: 1. registry sheet   2. identical normalised name or alias (learns the sheet)
          3. similar name >= 90 -> review queue, never matched
        v
 pipeline (per announcement)
   watched  -> stored; record observations; officer events; aliases on rename; orphans re-linked
   similar  -> stored in the review queue
   unknown  -> discovery signals: mention > address > brand token > (person, local, opt-in)
               hit -> stored as a candidate (re-judged every run)
   anything else -> only counted (acts by type and scope, publication lag)
        v
 SQLite: record + observations (current_values view), officer_events (current_officers view),
         candidates, review queue, per-document counters
        v
 sync (--apply) | report (7 sections, --no-details) | triage | calibrate | evaluate | rebuild
```

Modules in `src/borme_radar/`: `httpclient`, `cache`, `source`, `download`, `runner`,
`parser`, `acts`, `officers`, `relations`, `normalize`, `watchlist`, `matching`,
`pipeline`, `record`, `discovery`, `persons`, `store` (+ `sql/`), `corpus`, `evaluate`,
`triage`, `report`, `cli`. Runtime dependencies: `httpx`, `rapidfuzz`, `truststore`.

## Decisions from my production system (no internal figures)

This is a rebuild on public data of the BORME radar I built at work. These design
decisions come from that system; every threshold was re-derived here from public
gazettes ([docs/calibration.md](docs/calibration.md)) and every figure in the next
sections was re-measured.

| Decision | Why |
|---|---|
| **The registry sheet is the key**; the name only starts the match. The sheet is learned from the first announcement that matches by identical normalised name; stored announcements already carrying that sheet are re-linked. | The BORME publishes no tax ID. Names change and are written in many ways; the sheet does not change with them. |
| Similar names are **never matched**: they go to a review queue. | Filing a foreign act under a watched company is worse than missing one: the first is shown as true, the second appears on the next run once the sheet is known. |
| A sheet is **not learned from an announcement that closes it** (extinction, sheet closure). | In a merger the absorbing company can take the absorbed company's name, and both announcements of that day then match the same record by name. |
| **Homonym guard**: acts published long before the company's incorporation are not attributed to it. | Otherwise an earlier company with the same name hands its whole history (including its extinction) to the new one. |
| Old and new names are kept as **aliases** after a rename. | The company keeps matching by name whether or not the rename has been applied to the record. |
| Normalisation drops the registry notes the Registro appends: `(R.M. ...)`, the financial year of accounts filings `(2008)`, and status suffixes `EN LIQUIDACION`, `EN CONCURSO`, `UNIPERSONAL`. | With them a known company looks new, and its own filings inflate the noise of its brand. |
| Each act has a **scope**: board, attorney, auditor or company; **attorney and auditor acts never rise above low priority**. | Giving and taking signing powers is maintenance; a new director is governance. Attorney acts were drowning the alerts. |
| **Officer events** (appointment, removal, revocation, re-election) with the role **as published and canonical**, a flag for **company holders**, and **names never reordered**. | There is no official list of role abbreviations, so the raw label is kept. Reordering words would merge two different people; a duplicated person is a small problem, a position attributed to the wrong person is not. |
| The **record is resolved from dated observations**, not edited act by act. The Registro beats the other source whatever the dates; the latest fact wins; on the same day the most advanced lifecycle status wins; an extinction followed by more acts is ignored. Capital is the **"Resultante Suscrito"**, never the amount of the change. | Applying each act against the record renames a company backwards when it was renamed twice, and updates generated field by field can leave an old capital. The date of a non-registry value is when it was downloaded, not when it was true. |
| **`sync` shows by default and writes only with `--apply`**, logging the value it overwrote; an applied change whose evidence disappears later is **reverted**. | The record feeds other processes; a wrong write breaks them silently. |
| **Discovery of new group companies** with signals tried in order of reliability, at most one candidate per announcement: mention of a watched company (as sole shareholder, absorbed company, beneficiary, administrator), registered address (street words, number or road kilometre with decimals, municipality), brand token of 1-2 words from a list a person approved, and optionally rare surname pairs of the group's officers. | A new subsidiary has no known sheet and no watched name. Single words of the watched names were rejected as a signal: geographic words bring in hundreds of unrelated companies. |
| A registered address shared by many unrelated companies is **degraded, not discarded**: it stops deciding alone and the other signals still run. | Office buildings and business centres host hundreds of unrelated companies, but a group can open a new company in its own building. |
| **Proposing** a brand token (a person) and **measuring** it against the corpus (the code) are separate steps; nothing uses a list that was not approved. | A naive rule proposes common words; only the measurement shows it. |
| **Candidates are never added automatically** and are **re-judged on every run** with the current rules, including candidates without stored acts. | The BORME publishes no tax ID; and a candidate the current rules would not propose, or that is already watched, is the worst noise in the list. |
| A **triage score** (signal, activity, recency, brand in the name) orders the candidates in tiers. It decides nothing. | A list nobody can review is ignored. |
| **Calibration against the corpus**: one pass, counting only what is asked, companies deduplicated by normalised name, watched groups excluded; **a partial measurement never writes a proposal**. | An inverted index of every word grows with the corpus; the question is small. A measurement on a fraction of the corpus approves every token. |
| **Data minimisation**: only the watched companies' announcements, the review queue and the candidates are stored; the rest is counted and stays in the cache (`--store-all` and `prune` switch it). | Announcements name directors and attorneys; there is no reason to keep hundreds of thousands of them. |
| **Section B** is read (deposits of merger and split projects, sheet closures and reopenings), with a length cap that splits a header line listing several companies. | The deposit of a merger project is the earliest public sign of the operation. |
| **Up to 4 parallel downloads**; a **day that fails is skipped and listed**, and running the same range again recovers it from the cache. | A backfill must not stop for one bad day, and the public service must not be hammered. |
| **`rebuild`** regenerates every derived table offline from the cache; **loads are upserts that never lose a match**. | Every improvement of the parser or the matcher has to reach the whole history; re-processing a document before its sheet is known must not undo a match found later. |
| A shareable report has **no act text and no personal names**; clusters of powers (the same person in two or more companies of a group on the same day) are aggregated. | The gazette names natural persons. |

Decisions of this rebuild, not of the production system:

| Decision | Why |
|---|---|
| Retries only for 429, 5xx, network errors and truncated or invalid payloads; `Retry-After` honoured; a payload is validated before it is cached. | Permanent errors cannot be fixed by retrying. (The production system retried every error except 404.) |
| TLS against the operating-system trust store (`truststore`). | certifi's bundle failed on the development machine; verification is never disabled. |
| The publication lag and the parser coverage are measured on every run. | The lag is usually assumed; here it is counted. |
| Mentions are matched in **relation slots** (sole shareholder, absorbed, beneficiary, administrator, Section B co-listing), never as substrings of the text. | A watched company sitting on a board or holding powers of attorney is not a group relation. |
| Only an **extinction** followed by later acts is ignored; a dissolution is not. | Measured: 1,019 announcements followed a dissolution on the same sheet within the year (liquidators, the extinction itself), against 147 after an extinction. |
| Discovery is evaluated against **explicit company-to-company relations published in the BORME**, with thresholds chosen on a train window and measured on a later test window. | A ground truth that is public and about legal persons only. |

## Re-measured here on public data

All figures come from runs on 23-24 September 2026 (Windows 11, Python 3.13.7, httpx
0.28.1, rapidfuzz 3.14.6) over the gazettes of **24 Sep 2025 to 23 Sep 2026**. The
watchlist is `examples/watchlist.csv`: the parent companies of 32 Spanish listed groups
(mostly IBEX-35), with registered addresses typed by hand from public information.

### Download (backfill of 12 months)

| Measure | Value |
|---|---:|
| Command | `borme-radar fetch --from 2025-09-24 --to 2026-09-23 --workers 4 --download-only` |
| Days with gazette / without | 252 / 113 |
| Documents | 8,026 (Section A 7,611, Section B 415) + 252 daily summaries |
| Network requests / retries / failed days | 7,793 / 0 / 0 |
| Wall time (4 workers, 1 s between requests of each worker) | 2,123.7 s (35 min) |
| Median request latency | 0.052 s |
| Cache on disk | 8,278 files, 262 MB |

Serial against 4 workers, same range, cold caches, same 1 s delay per worker:

| Week | 4 workers | Serial | Speed-up |
|---|---:|---:|---:|
| 31 Aug - 4 Sep 2026 (4 workers ran first) | 40.1 s | 163.8 s | 4.1x |
| 24 - 28 Aug 2026 (serial ran first) | 38.5 s | 161.3 s | 4.2x |

The BOE answered in about 45 ms per request in both orders, so the delay between
requests, not the server, set the pace.

### Processing (offline rebuild of the 12 months)

`borme-radar rebuild --from 2025-09-24 --to 2026-09-23 --watchlist examples/watchlist.csv
--tokens examples/brand_tokens.approved.json`: **293 s** for 597,984 announcements and
1,165,592 acts. Kept in the database: **1,882 announcements (0.31%)**: 1,326 of watched
companies, 5 in the review queue, 551 of discovery candidates. No day failed.
Re-processing three days with `fetch --reprocess` and then `rebuild --history-only` left
every table count identical.

### Parser and publication lag

| Measure | Value |
|---|---:|
| Announcements (Section A / B) | 597,166 / 818 |
| Section A announcements with a registry sheet | 597,166 (100.00%) |
| Announcements with text before the first known heading | 36 |
| Announcements without any recognised act | 5 |
| Inscription to publication (Section A), median (p10 / p90) | 7 days (7 / 10) |
| Longest Section A company name | 191 characters (p99.9: 88) |
| Section B: merger / split / global transfer / transfer abroad projects deposited | 338 / 93 / 14 / 16 |

### Registry sheet and renames

- All 32 watched parents were matched; their 32 sheets were learned from exact names,
  and **1,293 of their 1,326 announcements matched by sheet** (33 by name, the first
  ones).
- One watched company was renamed in the window (Inmobiliaria Colonial to COLONIAL SFL
  SOCIMI SA, inscription dated 1 Oct 2025): 13 of its 14 announcements matched by sheet,
  and the new name is kept as an alias.
- Over the whole gazette, 9,704 sheets had a rename in the window; 5,544 of the 5,603
  later announcements on those sheets (98.9%) carry a name other than the first one
  seen: lost by name-only matching, kept by the sheet.

### Scope of officer acts

Of the 575,337 officer acts of the year (appointments, removals, revocations,
re-elections, ex officio cancellations): **board 71.6%, attorney 22.8%, auditor 5.0%**,
other 0.6%. For the watched parents it is the other way round: of their 1,224 officer
acts, **1,034 (84.5%) concern attorneys** and 152 (12.4%) the board; only the board ones
reach the alerts. Role labels: 1,048,141 mentions, 857 distinct labels, 98.96% mapped to
a canonical role.

### Officer history and record

- **8,089 officer events** for the watched companies: 4,068 appointments, 3,734
  revocations, 179 re-elections and 108 removals; 136 of them held by a company; 3,319
  positions current (`current_officers`). Names stay in the local database.
- 31 registry observations for the watched companies. `sync` listed **16 differences**
  (15 capitals the record did not have, 1 rename); `sync --apply` wrote them and logged
  16 changes with the values they overwrote; the next `sync` reported no difference.

### Discovery of new group companies

Pending candidates after the rebuild: **139** (30 by address, 28 by mention, 81 by
brand token). Triage: 59 "review first", 52 "worth a look", 28 "long tail". The token
list used is the calibration proposal copied as is (`examples/brand_tokens.approved.json`);
in use, a person reviews it first.

Evaluation (`borme-radar evaluate`) on the **test window, 24 Mar - 23 Sep 2026** (4,052
documents, 281,291 announcements), with the tokens and the address threshold chosen on
the train window. Two ground truths: **explicit company-to-company relations** published
in the window, propagated along the relations (17 companies, 16 with an announcement of
their own); and the same **or at least two officers in common** with a watched company
(211 companies, 210 with announcements). Precision is a lower bound (a real subsidiary
with no published evidence in the window counts as wrong); recall is over the companies
of each truth that published something.

| Signal | Flagged | Precision vs relations | Recall vs relations | Precision vs relations or shared officers | Recall vs relations or shared officers |
|---|---:|---:|---:|---:|---:|
| mention | 13 | 84.6% | 68.8% | 92.3% | 5.7% |
| address | 11 | 0.0% | 0.0% | 9.1% | 0.5% |
| brand token | 68 | 1.5% | 6.2% | 54.4% | 17.6% |
| combined detector | 91 | 13.2% | 75.0% | 53.8% | 23.3% |
| person (local, opt-in) | 619 | 0.5% | 18.8% | 24.4% | 71.9% |

How to read it: the mention signal reads the same relation slots as the first truth, so
its precision there checks the matching rather than an independent source. Tokens and
addresses use evidence neither truth uses. Against relations alone they look useless,
because a year of gazettes publishes relations for few companies of a large group;
against the broader truth, half of the token candidates are group companies. The
address signal is weak for listed groups: a few subsidiaries are registered at the
parent's address, but so are many unrelated tenants of the same office buildings. The
person signal is not independent of the shared-officers truth (both use officers) and
flags 619 companies: the watched parents have thousands of officers, most of them
attorneys who also act for other companies. It is off by default and its keys never
leave the local database.

### Calibration

[docs/calibration.md](docs/calibration.md) has the histograms and the derivation of
every threshold: similar-name cut-off (90), homonym guard (93 days), Section B name cap
(191 characters), minimum length of a bare mention (4 characters), brand-token noise (at
most 5 unrelated companies), address noise (degraded from 6), surname pairs (never seen
outside the group), and the memory comparison: a full inverted index took 41.3 MB for
312 documents and doubles with the corpus, while the targeted count took 3.7 MB.
Measured on 3 months instead of 12, the same token list passes 23 tokens instead of 19.

## How to run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"      # Windows; .venv/bin/python elsewhere

# download and process (at most 4 workers, 1 s between requests of each worker)
borme-radar fetch --from 2026-09-01 --to 2026-09-23 --watchlist examples/watchlist.csv \
    --tokens examples/brand_tokens.approved.json
borme-radar stats
borme-radar sync                     # shows the differences; --apply writes and logs them
borme-radar triage --tokens examples/brand_tokens.approved.json
borme-radar report --from 2026-09-01 --to 2026-09-23 --no-details --out reports/radar.md

# offline
borme-radar rebuild --from 2025-09-24 --to 2026-09-23   # every derived table from the cache
borme-radar rebuild --history-only                       # observations and officer events only
borme-radar relink [--apply]                             # orphans of known sheets
borme-radar prune                                        # drop what is no longer relevant
borme-radar calibrate --from 2025-09-24 --to 2026-03-23 \
    --tokens examples/brand_tokens.proposed.json --addresses --hq
borme-radar evaluate --from 2026-03-24 --to 2026-09-23 \
    --tokens examples/brand_tokens.approved.json
```

The watchlist is a CSV with `group` and `name` columns, plus optional `address`, `city`,
`capital`, `status`, `purpose`, `sheet`, `province` and `incorporated`. Everything is
written under `data/` (`--data-dir` to change it): the cache, the SQLite database and the
calibration outputs; `data/` and `reports/` are gitignored. Nothing is scheduled: the
radar is run by hand. `examples/report_2026-09_no-details.md` is a shareable report
generated with `--no-details`.

## Privacy

Announcements name directors, attorneys, sole shareholders and judges. They stay in the
local database (`officer_events`, stored acts, `person_keys`), which is gitignored. The
committed Section A fixtures are real documents with natural-person names replaced by
`PERSONA N` (`scripts/make_fixture.py`); the Section B fixture names only companies.
Shared reports use `--no-details`, and the person signal publishes only aggregate
metrics. The BOE reuse terms require processing personal data under the GDPR and the
LOPDGDD.

## Tests

```bash
pytest            # 207 passed on 24 Sep 2026 (Windows 11, Python 3.13.7)
ruff check . && ruff format --check .
```

The suite runs offline (`tests/conftest.py` makes any socket connection fail). It covers
the parser on real Section A and B fixtures, the role parser, relations, normalisation,
matching and its guards, sheet learning and orphan re-linking, upserts that never lose a
match, aliases, minimisation, candidate re-judging (including candidates without stored
acts), the record rules and `sync` (apply, log, revert), the downloader with a fake
server (parallel downloads, a failed day skipped and recovered, offline runs), the
calibration and evaluation collectors, the report without names, and the CLI end to end
from a pre-filled cache. GitHub Actions runs ruff and pytest on Python 3.11 and 3.12.

## Limitations and next steps

- **Section C** (legal notices: general meetings, creditor notices) is free prose and is
  not parsed, and competitors are not detected by corporate purpose. Both were left out
  on purpose in the production system too.
- **Clusters of powers need several companies per group** in the watchlist. With one
  parent per group, as here, the section is empty by construction; it is covered by
  tests only.
- The registered addresses in the watchlist were typed by hand; at 17 of the 30 no
  company at all registered in the train window, and the address signal found nothing
  that explicit relations confirm.
- The triage score uses the measured precision of each signal, but its activity,
  recency and brand weights and the tier cut-offs are ordering heuristics.
- Retries under real throttling were not observed (0 retries in 7,793 requests); the
  policy is tested with a fake transport.
- This rebuild measured 12 months; the production system covers a much longer history.

## Data source and licence

- Source: Boletín Oficial del Registro Mercantil (BORME), published by the Agencia
  Estatal Boletín Oficial del Estado through its open-data API
  (https://www.boe.es/datosabiertos/). **Basado en datos de la Agencia Estatal Boletín
  Oficial del Estado** (https://www.boe.es).
- Reuse terms (https://www.boe.es/informacion/aviso_legal/index.php#reutilizacion,
  standard licence approved by BOE resolution of 27 June 2024, checked on 23 Sep 2026):
  commercial and non-commercial reuse is allowed if the source is cited with a link to
  https://www.boe.es, the meaning of the information is not distorted, the date of last
  update is mentioned when the document includes it, modifications are identified as
  such, reuse does not suggest official status or endorsement by the BOE, and personal
  data is processed in compliance with the GDPR and Spanish data protection law
  (LOPDGDD).
- Only the electronic edition of the BORME is official and authentic; reports link to
  the official PDF of each document. The output of this tool is derived and unofficial.
- Code: MIT licence, see [LICENSE](LICENSE).

## About

Rebuild on public data of the BORME radar I built at work and run on demand. No
proprietary code or data. Built with AI-assisted development; the design decisions come
from my production system and every figure here was re-measured on public data.

Alejandro Plaza Lorenzo - alejandroplaza.dev@gmail.com
