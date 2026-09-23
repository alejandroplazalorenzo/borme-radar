# borme-radar

Monitor of Spain's Official Gazette of the Companies Register (BORME, Section A
"Actos inscritos"). It downloads each daily gazette from the BOE open-data API, parses
every announcement into typed acts (incorporations, director appointments and
resignations, address changes, mergers, capital changes, dissolutions, insolvency...),
stores them in SQLite and reports the acts of a watchlist of companies.

## Why

Registry events are public within days: in the run below, the median gap between the
inscription date printed in each announcement and its publication in the BORME was
**7 calendar days** (measured, see [Results](#results)). Annual accounts arrive much
later: the Spanish Companies Act allows six months after year-end to approve them
(art. 164 LSC) and one more month to deposit them (art. 279 LSC), so even on time they
describe the company as it was at least seven months earlier (statutory deadlines, not
measured here). A change of directors, an insolvency filing or a dissolution shows up
in the registry long before it shows up in any balance sheet.

## Architecture

```
                 BOE open-data API (https://www.boe.es)
 /datosabiertos/api/borme/sumario/YYYYMMDD      /diario_borme/xml.php?id=BORME-A-...
            | JSON (daily summary)                   | XML (one document per province)
            v                                        v
 +-----------------------------------------------------------------------------+
 | httpclient.PoliteClient  timeouts, retries only for 429/5xx/network/        |
 |                          truncated payloads, Retry-After, 1 request/s,      |
 |                          User-Agent with contact, OS trust store            |
 | cache.DiskCache          permanent + atomic, successful responses only      |
 +-----------------------------------------------------------------------------+
            |                                        |
            v                                        v
 source.section_a_documents               parser.parse_document
 (404 = no gazette that day;               + acts.split_acts (closed catalogue
  alphabetical index skipped)                of act headings; raw text kept)
                                                     |
                                                     v
                    store: SQLite, schema from sql/001_initial.sql
                    days | documents | announcements | acts   (idempotent loads)
                                                     |
 examples/watchlist.csv -> normalize (names, legal forms) -> matching
                                  exact normalised name  -> confirmed
                                  fuzz.ratio >= 90       -> needs review (never confirmed)
                                                     |
                                                     v
                                  report.md (alerts)   /   stats
```

Modules live in `src/borme_radar/`: `httpclient.py`, `cache.py`, `source.py`,
`parser.py`, `acts.py`, `store.py` (+ `sql/`), `normalize.py`, `matching.py`,
`report.py`, `cli.py`. Runtime dependencies: `httpx`, `rapidfuzz`, `truststore`.

## Design decisions

| Decision | Alternative rejected | Why |
|---|---|---|
| Parse the per-province **XML** from the open-data API. | Parse the PDF gazette. | The XML already separates announcements (`<p class="articulo">` / `<p class="parrafo">`); PDF text extraction depends on layout and breaks across pages and columns. |
| Split acts with a **closed catalogue of headings** (`acts.py`), matched only after a full stop and before `.` or `:`, case-sensitive and accent-tolerant. | A generic "capitalised phrase followed by a period" rule. | Announcements contain free text (by-law articles, court decisions) full of heading-like sentences. Unknown headings are not silently lost: the text lands in `unparsed_prefix` or in the previous act's detail, and `stats` counts it. Three headings were added this way after the first real run (see Results). |
| Only an **exact match on the normalised name is confirmed**; fuzzy similarity is reported as **"needs review"**. | Auto-confirm above a similarity threshold. | A registry alert triggers work (calling a client, blocking credit, reviewing a supplier), so false positives are expensive. On the real run every fuzzy candidate between 75 and 90 was a different company (see Results). |
| Fuzzy score = `fuzz.ratio` on the **base name without legal form**; same name with another legal form is flagged for review. | `token_set_ratio` / partial ratios. | Token-set scorers rate `TELEFONICA SA` vs `TELEFONICA DE ESPANA SA` as 100, so every subsidiary would match its parent. A legal-form mismatch can be a transformation (21 "Transformación de sociedad" acts in the run) or a typo, so a human decides. |
| Normalisation maps `S.A.U.`/`S.L.U.` to `SA`/`SL` and drops `EN LIQUIDACION` and `(R.M. ...)` notes. | Keep them as part of the name. | Otherwise a watched company stops matching exactly when it becomes single-member or enters liquidation, which is when the alert matters most. |
| Load = **delete + insert per document in one transaction**; key `(document_id, number)`. | `INSERT OR IGNORE` / append-only. | Re-running a day never duplicates rows, and an improved parser re-applied from the cache replaces old rows instead of leaving them stale. |
| **Permanent cache** of successful responses; 404s are not cached. | TTL / HTTP cache headers. | A published issue never changes (corrections are published as new announcements). Not caching 404s means a day queried before publication is retried next time. |
| Retries with exponential backoff **only** for 429, 5xx, connection errors and truncated or invalid payloads; `Retry-After` honoured, and the client gives up rather than retry earlier than a server asks. | Retry every error / library defaults. | Permanent errors (other 4xx, TLS certificate failures) cannot be fixed by retrying, and hammering a public service is not acceptable. |
| TLS verified against the **operating system trust store** (`truststore`). | `verify=False`. | On the development machine, certifi's CA bundle failed to verify www.boe.es (`CERTIFICATE_VERIFY_FAILED`) while the Windows trust store succeeded; verification stays on either way. |
| Personal data stays local: raw text in the gitignored SQLite file, **pseudonymised fixtures**, `--no-details` for shareable reports. | Commit real fixtures and full reports. | BORME announcements name directors, attorneys and judges; the BOE reuse conditions require GDPR compliance. |
| Standard library `sqlite3`, `argparse`, `xml.etree`. | SQLAlchemy, Typer, lxml. | Three runtime dependencies in total; the schema is small and explicit SQL. |

## Results

All numbers below come from runs on **23 September 2026** (Windows 11, Python 3.13.7,
httpx 0.28.1, rapidfuzz 3.14.6) over the gazettes of **10-23 September 2026**
(10 business days).

### Fetch and cache

| Run | Command | Network requests | Cache hits | Retries | 404 (no gazette) | Wall time |
|---|---|---:|---:|---:|---:|---:|
| 1, cold cache | `borme-radar fetch --from 2026-09-10 --to 2026-09-23` | 296 | 0 | 0 | 4 | 317.6 s |
| final, warm cache | same command | 4 | 292 | 0 | 4 | 11.7 s |

296 requests = 14 daily summaries + 282 province documents. On the warm run only the
four weekend days (12, 13, 19, 20 Sep) went to the network, because 404s are not cached
on purpose. The cache holds 292 files (7,485,997 bytes); the SQLite database is 16.6 MB.

Idempotency on real data: the same date range was loaded three times into the same
database (cold run, a warm re-run after the parser fix below, and the final warm run)
and the database holds 17,958 announcements, the count of a single run.

### What was parsed

| Measure | Value |
|---|---:|
| Days checked / with gazette | 14 / 10 |
| Province documents (Section A, index excluded) | 282 |
| Announcements | 17,958 |
| Distinct companies (normalised names) | 16,526 |
| Acts | 33,502 |
| Announcements with more than one act | 8,783 |
| Most acts in one announcement | 12 |
| Announcements with text before the first known heading | 0 |
| Announcements without any recognised act | 0 |
| Inscription to publication, median (p10 / p90) | 7 days (7 / 8), n = 17,958 |
| Share published exactly 7 days after inscription | 69.9 % |

Parser coverage was measured, not assumed. After the cold run, SQL queries on the
database found 7 announcements with text before the first known heading, 6 without any
recognised act and 36 without registry data. The fix: three headings added ("Articulo
378.5 del Reglamento del Registro Mercantil", "Suspensión de pagos", "Crédito
incobrable"), "Datos registrales" located on its own (it is not always preceded by a
full stop), plus one registry's garbled date format. After re-parsing from the cache all
of these counters are 0.

| Date | Documents | Announcements | Acts |
|---|---:|---:|---:|
| 2026-09-10 | 28 | 1,755 | 3,237 |
| 2026-09-11 | 28 | 1,313 | 2,636 |
| 2026-09-14 | 27 | 1,726 | 3,115 |
| 2026-09-15 | 24 | 1,817 | 3,198 |
| 2026-09-16 | 27 | 1,591 | 2,998 |
| 2026-09-17 | 31 | 1,663 | 3,359 |
| 2026-09-18 | 31 | 2,861 | 4,647 |
| 2026-09-21 | 27 | 1,350 | 2,608 |
| 2026-09-22 | 29 | 1,823 | 3,605 |
| 2026-09-23 | 30 | 2,059 | 4,099 |

### Acts by type (`borme-radar stats`)

| Act type | Code | Count |
|---|---|---:|
| Appointment (directors, auditors, attorneys) | `appointment` | 10,047 |
| Incorporation | `incorporation` | 3,881 |
| Removal / resignation of officers | `officer_removal` | 3,206 |
| Registry sheet closed | `registry_sheet_closure` | 2,756 |
| Sole shareholder declared | `sole_shareholder_declared` | 2,295 |
| Other entries (free text) | `other` | 2,281 |
| Change of registered address | `address_change` | 1,268 |
| Revocation (mainly powers of attorney) | `revocation` | 1,240 |
| By-laws amendment | `bylaws_amendment` | 945 |
| Capital increase | `capital_increase` | 850 |
| Extinction (company struck off) | `extinction` | 720 |
| Change of corporate purpose | `business_purpose_change` | 714 |
| Dissolution | `dissolution` | 642 |
| Re-election | `reelection` | 540 |
| Sole-shareholder company | `sole_shareholder` | 308 |
| Change of sole shareholder | `sole_shareholder_change` | 308 |
| Change of company name | `name_change` | 303 |
| Sole-shareholder status lost | `sole_shareholder_lost` | 263 |
| Insolvency proceedings (concurso) | `insolvency` | 237 |
| Capital reduction | `capital_reduction` | 204 |
| Erratum | `erratum` | 174 |
| Merger | `merger` | 60 |
| Appointments cancelled ex officio | `ex_officio_cancellation` | 57 |
| Registry sheet reopened | `registry_sheet_reopening` | 54 |
| Corporate website | `website` | 42 |
| Preventive annotation (e.g. debtor declared insolvent) | `preventive_annotation` | 21 |
| Split / spin-off | `split` | 21 |
| Transformation (change of legal form) | `transformation` | 21 |
| Annual accounts not approved (art. 378.5 RRM) | `accounts_not_approved` | 14 |
| Branch opened | `branch_opening` | 11 |
| Unpaid capital paid up | `capital_call_paid` | 4 |
| Change of duration | `duration_change` | 4 |
| Bond issue | `bond_issue` | 3 |
| Statutory adaptation to a new law | `statutory_adaptation` | 3 |
| Change of powers | `powers_change` | 2 |
| Sole trader entry | `sole_trader` | 2 |
| Debt declared uncollectible | `uncollectible_debt` | 1 |

Two things stand out. 2,661 of the 2,756 registry-sheet closures are the same entry,
"Cierre provisional hoja registral por baja en el índice de Entidades Jurídicas". And of
the 237 insolvency acts, 56 announcements contain an order declaring insolvency ("Auto de
declaración de concurso") and 59 contain an order closing the proceedings ("Auto de
conclusión del concurso").

### Example of a parsed announcement

BORME-A-2026-183-28 (Madrid, published 2026-09-22), announcement 423948,
`OCAMPO GONZALEZ SL`. Registry data `S 8 , H M 258820, I/A 6 (15.09.26)`, so the
inscription date is 2026-09-15:

| seq | act_type | heading | detail |
|---:|---|---|---|
| 1 | `capital_reduction` | Reducción de capital | Importe reducción: 22.088,00 Euros. Resultante Suscrito: 68.062,00 Euros |
| 2 | `address_change` | Cambio de domicilio social | AVDA DE ARGANZUELA S/N - NAVE B10 MERCAMADRID (MADRID) |
| 3 | `business_purpose_change` | Cambio de objeto social | la compra venta y arrendamiento de todo tipo de bienes inmuebles |
| 4 | `split` | Escisión parcial | Sociedades beneficiarias de la escisión: OCAMPO PESCADOS Y MARISCOS SL |

### Watchlist alerts

Command: `borme-radar alerts --watchlist examples/watchlist.csv --no-details --out ...`
(18 IBEX-35 parent companies, default threshold 90). Result: **4 confirmed, 0 needs
review, 14 without hits.**

| Company (as published) | Province | Announcements | Acts | Highest priority |
|---|---|---:|---|---|
| BANCO SANTANDER, S.A | Cantabria | 12 | 1 capital increase, 1 capital reduction, 9 appointments, 1 revocation | high |
| REPSOL SA | Madrid | 3 | 2 appointments, 1 revocation | medium |
| ENAGAS SA | Madrid | 1 | 1 appointment, 1 revocation | medium |
| BANCO BILBAO VIZCAYA ARGENTARIA SOCIEDAD ANONIMA | Bizkaia | 1 | 1 appointment | medium |

The published spellings differ ("BANCO SANTANDER, S.A" without the final dot, "...
SOCIEDAD ANONIMA" in full, "ENAGAS" without the accent) and all normalise to the
watchlist entries. Santander's material events were a capital increase of
164,923,219.00 EUR (resulting capital 7,509,582,970.00 EUR, published 10 Sep) and a
capital reduction of 231,341,569.50 EUR (resulting capital 7,278,241,400.50 EUR,
published 18 Sep). Every appointment and revocation for these four companies concerned
powers of attorney (roles `Apoderado`, `Apo.Sol.`, `Apo.Manc.`, `Apo.Man.Soli`), not
directors.

The same watchlist at lower thresholds, to show what fuzzy matching would bring in:

| Threshold | Needs review | Candidates |
|---:|---:|---|
| 90 (default) | 0 | - |
| 85 | 1 | `IDASA SISTEMAS SL` for Indra Sistemas (85.7) |
| 80 | 2 | + `IBEROA SL` for Iberdrola (80.0) |
| 75 | 7 | + four more `... SISTEMAS SL` names for Indra Sistemas, `DOLADO CORPORACION SL` for Redeia Corporación |

None of the seven candidates is the watched company: they share a generic word
("SISTEMAS", "CORPORACION") or a few letters, not the distinctive name. That is why
fuzzy candidates are never auto-confirmed.

## How to run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows; use .venv/bin/python elsewhere

borme-radar fetch --from 2026-09-10 --to 2026-09-23  # download, parse, store (1 request/s)
borme-radar stats                                    # coverage, lag and acts by type
borme-radar alerts --watchlist examples/watchlist.csv --since 2026-09-10 --out reports/alerts.md
borme-radar alerts --watchlist examples/watchlist.csv --no-details   # no personal names
```

The watchlist is a CSV with a `name` column. Everything is written under `data/`
(`--data-dir` to change it): `data/cache/` for the downloaded documents and
`data/borme.sqlite` for the database. Both are gitignored, as is `reports/`.
`python -m borme_radar` works too. Scheduling (cron, Task Scheduler) is left to the
user.

## Tests

```bash
pytest            # 96 passed on 23 Sep 2026 (Windows 11, Python 3.13.7)
ruff check . && ruff format --check .
```

The suite runs offline: `tests/conftest.py` makes any socket connection fail the
test. It covers the parser on real XML fixtures, act splitting, name normalisation,
matching thresholds (exact vs needs review), idempotent loads, the HTTP retry policy
with a fake transport (`httpx.MockTransport`, no real sleeps), and an end-to-end CLI run
served from a pre-filled cache.

The fixtures in `tests/fixtures/` are real documents of the 22 Sep 2026 gazette
(summary JSON and two province XML files) **modified** for the repository: trimmed to 19
announcements, with natural-person names replaced by `PERSONA N` by
`scripts/make_fixture.py`. Company data and `fecha_actualizacion` are kept as
published. Each fixture is under 8 KB.

GitHub Actions (`.github/workflows/ci.yml`) runs ruff and pytest on Python 3.11 and
3.12. Status on GitHub: pending (the repository has not been pushed yet).

## Limitations and next steps

- **Heading catalogue.** New or rare headings need adding to `acts.py`; `stats`
  reports coverage (0 unparsed announcements here, but only 10 days were observed).
  A heading-like sentence inside free text can still create an extra act: seen once in
  this run, where "3.- Cierre provisional art. 485 TRLC." inside a court decision added
  a second closure act to an announcement that already had one.
- **Details are raw text.** Roles, people, amounts and court data are not structured
  yet. Next: role-aware extraction, so that a director resignation ranks above a
  routine power-of-attorney change (all appointments for the four matched companies
  were attorneys).
- **Name-only matching.** Section A announcements do not carry the tax ID (NIF), so
  the name is the key. A renamed company needs its old name in the watchlist; the
  "Cambio de denominación social" act could update it automatically. Subsidiaries are
  deliberately not matched; group perimeters are a next step.
- **Section C** (legal notices: general meetings, merger announcements, capital
  reductions) is not parsed.
- **Retries under real throttling** were not observed (0 retries in this run); the
  policy is tested with a fake transport only.
- **Planned: insolvency early-warning features combining registry events.** Per-company
  signals such as preventive annotations (debtor declared insolvent), debts declared
  uncollectible, accounts not approved (art. 378.5 RRM), provisional closures of the
  registry sheet, auditor changes and director turnover, evaluated against later
  "Situación concursal" declarations. Predictive performance: pending.

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
- Only the electronic edition of the BORME is official and authentic. The output of
  this tool is derived and unofficial.
- Code: MIT licence, see [LICENSE](LICENSE).

## About

Rebuild on public data of a system I designed and ran in production at work
(automotive sector). It contains no proprietary code or data. Built with AI-assisted
development; design decisions, evaluation and review are mine.

Alejandro Plaza Lorenzo - alejandroplaza.dev@gmail.com
