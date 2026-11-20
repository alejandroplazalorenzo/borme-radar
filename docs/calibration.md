# Calibration: where every threshold comes from

The thresholds of matching and discovery are checked on public gazettes with
`borme-radar calibrate`, version 0.3.0, runs of 20 November 2026 (Windows 11, Python
3.13.7), over the cached BORME of **24 Sep 2025 to 23 Sep 2026** (252 gazette days,
8,026 documents, 597,984 announcements, 423,774 distinct companies by normalised name).
The format rules of the parser and the record (sheets with two numbers, legal forms and
company words of holders, the cut lengths of holders and streets, the street words that
do not tell two addresses apart, the year of joining a group) are measured on the full
history, 2009-2026: see [history.md](history.md), which also lists the values that are
choices.

Thresholds that decide discovery (tokens, addresses, surname pairs) are measured on the
**train window, 24 Sep 2025 to 23 Mar 2026** (3,974 documents, 316,693 announcements),
so that the evaluation on the **test window, 24 Mar to 23 Sep 2026** is not measured on
the data that chose them. Structural thresholds (name length, similarity bands,
incorporation dates) use the whole year.

## How the runs were made

Every run started from an empty data directory that reads the same cache. `calibrate`
needs the officers of the watched companies (for the shared-officers ground truth), so
the 12 months are processed first:

```bash
D="--data-dir data/w12"
borme-radar $D rebuild --from 2025-09-24 --to 2026-09-23 --watchlist examples/watchlist.csv \
    --tokens examples/brand_tokens.approved.json
borme-radar $D calibrate --from 2025-09-24 --to 2026-03-23 \
    --tokens examples/brand_tokens.proposed.json --addresses --hq --person-pairs
# calibration/address_noise.json of the train window: also examples/address_noise.json
borme-radar $D rebuild --from 2025-09-24 --to 2026-09-23 --watchlist examples/watchlist.csv \
    --tokens examples/brand_tokens.approved.json   # reads calibration/address_noise.json
borme-radar $D calibrate --from 2025-09-24 --to 2026-09-23 --fuzzy --names --lag \
    --collisions --renames --roles --tokens examples/brand_tokens.proposed.json
borme-radar $D calibrate --from 2025-09-24 --to 2025-12-23 --tokens examples/brand_tokens.proposed.json
borme-radar $D calibrate --from 2025-09-24 --to 2025-10-07 --memory --memory-days 6 13
borme-radar $D evaluate --from 2026-03-24 --to 2026-09-23 --tokens examples/brand_tokens.approved.json
```

Each `calibrate` writes `calibration/calibration_<from>_<to>.md` and `.json`; the train
window also writes the token proposal (`tokens.proposal.json`), the address counts
(`address_noise.json`), the address proposal (`hq.proposal.json`) and, into the local
database only, the surname pairs. The tables below come from those files.

"Unrelated" always means: not a watched company, and not tied to a watched group by
published evidence in the window (an explicit company-to-company relation, see
`relations.py`, or at least two officers in common with a watched company). The
watchlist holds only the listed parents; without this exclusion a group's own
subsidiaries count as noise against its brand. In the train window that excluded 273
companies (22 by relations, 259 by shared officers); in the whole year, 432 (41 and
403).

Everything below is aggregated. Names of natural persons are never printed; company
names are public data about legal persons.

## Fuzzy cut-off for the review queue: 90

`calibrate --fuzzy`, whole year. Best `fuzz.ratio` of each non-exact company name
against the watched names (without legal form); truth from the registry sheet (same
sheet as the watched company it resembles = the same company written differently).

| Similarity band | Same company | Different company (distinct names) |
|---|---:|---:|
| 90-94 | 0 | 2 |
| 85-89 | 0 | 12 |
| 80-84 | 0 | 95 |
| 75-79 | 0 | 253 |
| 70-74 | 0 | 642 |

No true variant in any band: normalisation and the sheet absorb them all, and a similar
name is never matched whatever its score. The cut of 90 is the value of the production
system, kept: it only sets how many names wait for a person, 2 a year at 90 against 12
at 85, 95 at 80 and 642 at 70, all of them other companies.

## Homonym guard: acts more than 93 days before the incorporation date are not attributed

`calibrate --lag`, whole year, 135,798 incorporations with a "Comienzo de operaciones"
date. Inscription date minus declared start of operations, in days:

| p0.1 | p1 | p5 | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|---:|
| -93 | -40 | -7 | 15 | 74 | 190 |

10.02% of incorporations are inscribed before the start of operations they declare
(future start dates). With `incorporated` = the declared start of operations, a window
of 93 days keeps 99.9% of companies' own first acts; an older act under the same name
belongs to a homonym.

## Longest name before a Section B header is treated as a list: 191 characters

`calibrate --names`, whole year: Section A company names, n = 597,166; maximum 191
characters, p99.9 88. Section B header lines longer than the longest Section A name
cannot be one name and are split into companies (817 of the 818 Section B entries of the
year named one company; one named three).

## Mentions without legal form: at least 4 characters

`calibrate --collisions`, whole year, deterministic 1-in-10 hash sample on both sides:
41,729 company names without legal form, 48,124 names of natural persons in officer
lists. A bare company name equal to a person's full name:

| Characters | 1 word | 2 words | 3 words | 4 words | 5+ words |
|---|---:|---:|---:|---:|---:|
| 1-3 | 0 / 9 | 0 / 2 | | | |
| 4-7 | 0 / 1,415 | 0 / 167 | 0 / 6 | | |
| 8-11 | 0 / 2,993 | 0 / 3,121 | 0 / 192 | 0 / 11 | |
| 12-15 | 0 / 483 | 2 / 6,668 | 0 / 1,373 | 0 / 93 | 0 / 4 |
| 16-19 | 0 / 66 | 0 / 4,705 | 4 / 3,262 | 0 / 443 | 0 / 29 |
| 20-23 | 0 / 16 | 0 / 1,562 | 7 / 3,571 | 0 / 952 | 0 / 125 |
| 24+ | 0 / 1 | 0 / 231 | 1 / 3,576 | 1 / 3,848 | 0 / 2,805 |

At most 0.20% in any bucket. Mentions are matched as whole relation slots (sole
shareholder, absorbed company, beneficiary, administrator), never as substrings, so
length does not protect against much here; the floor of 4 characters only excludes 1-3
letter names, too few in the sample to measure (11).

## Brand tokens: at most 5 unrelated companies

`calibrate --tokens examples/brand_tokens.proposed.json`, train window. 39 tokens
written from the groups' public brands (a proposal, measured below). Four never qualify
by shape: SANTANDER, SABADELL and PUIG are municipalities in the registered addresses of
the window (3,543 municipalities seen), ACS has fewer than 4 letters.

| Unrelated companies | Tokens |
|---|---:|
| 0 | 2 |
| 1-2 | 10 |
| 3-5 | 6 |
| 6-10 | 8 |
| 11-20 | 4 |
| 21-50 | 7 |
| 51-200 | 1 |
| 201+ | 1 (IBERIA: 1,289) |

Precision of the tokens kept at each cut, pooling their hits (related / all), as
`calibrate` prints it:

| Cut | Tokens kept | Related | Unrelated | Precision |
|---:|---:|---:|---:|---:|
| 1 | 9 | 15 | 7 | 68.2% |
| 2 | 12 | 17 | 13 | 56.7% |
| 3 | 14 | 17 | 19 | 47.2% |
| 4 | 16 | 22 | 27 | 44.9% |
| 5 | 18 | 33 | 37 | 47.1% |
| 6 | 19 | 33 | 43 | 43.4% |
| 7 | 24 | 40 | 78 | 33.9% |
| 12 | 28 | 41 | 118 | 25.8% |
| 44 | 34 | 66 | 290 | 18.5% |

The cut of 5 is the value of the production system, kept (`discovery.TOKEN_NOISE_MAX`).
The table agrees with it without deciding it: up to 5 the pooled precision stays at
45-47% from 3 on, and from 7 it falls to a third. The 18 tokens kept at 5 are
`examples/brand_tokens.approved.json`, identical to the proposal of this run. In this
run the approval step was mechanical (the proposal copied as is); in use, a person reads
the proposal before saving it as the approved list, and nothing reads a list that was
not approved.

**More corpus changes the verdict.** The same 39 tokens measured on 3 months (24 Sep -
23 Dec 2025) pass 23; on 12 months, 19. MERLIN goes from 4 unrelated companies on 3
months to 11 on 12 (LEROY MERLIN), ROVI from 4 to 8, SOLARIA from 2 to 10, GRIFOLS from
5 to 8, UNICAJA from 3 to 6. A list calibrated on a short window lets words through.

## Registered addresses: not used from 6 unrelated companies

`calibrate --addresses`, train window, over the sites of the watched companies:
`discovery.site` reduces an address to its municipality, its number (the kilometre point
on a road) and the words that place it, and the label of a site is those words, sorted,
the number and the municipality ("CASTELLANA 257 (MADRID)"). 30 of the 32 watched
parents have an address in the watchlist (Inditex and IAG none); 5 of the 30 give no
site, because every word of the street is one that many street names share (GRAN VIA,
EUROPA twice, ANDALUCIA, JESUS Y MARIA: `lists/generic_street_words.txt`), so there is
no address signal for those groups either. That leaves 25 sites:

| Unrelated companies at the site | Sites |
|---|---:|
| 0 | 16 |
| 1-2 | 4 |
| 3-5 | 1 |
| 6-10 | 0 |
| 11-20 | 3 |
| 21-50 | 0 |
| 51-200 | 1 |

The gap is between 5 and 14: office buildings and business centres (84, 18, 16 and 14
unrelated companies) against headquarters with at most 5. From 6 the site is crowded and
discovery does not use it (`discovery.ADDRESS_NOISE_MAX`); the other signals still run.
The counts are `examples/address_noise.json`, the file `fetch`, `rebuild` and `evaluate`
read with `--address-noise` (or from `calibration/address_noise.json` of the data
directory). Without it no site is crowded: on the 12 months the same rebuild proposes
451 leads instead of 130.

`calibrate --hq` proposes sites where at least two related companies are registered. In
the train window it found only one, so the addresses in `examples/watchlist.csv` were
written from public information on each group's registered office (not checked against
the companies' own filings in this rebuild) and then measured against the corpus: at 15
of the 25 sites no company at all registered in the train window.

## Surname pairs (person signal, local only): 0 unrelated companies

`calibrate --person-pairs`, train window, 6,691 natural persons who are officers of
watched companies (board and attorneys). For each, the pair of name words whose words
are rarest; unrelated companies carrying it:

| 0 | 1 | 2 | 3 | 4-5 | 6-10 | 11-20 | 21+ |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 3,307 | 648 | 311 | 195 | 308 | 400 | 369 | 1,151 |

Only pairs never seen outside the group are kept (3,273 distinct pairs). They stay in
the local database; this table is all that is published.

## Ground truth by shared officers: at least 2

Companies (not watched) by the most natural-person officers they share with one watched
company, whole year: 1 officer 1,112; 2 officers 188; 3 officers 38; 4-10 officers
83; 11 or more 94. A single shared name is common (homonyms, a shared law firm or
auditor partner), so the evaluation's second ground truth requires two.

## Registry sheet stability

`calibrate --renames`, whole year:

- Section A announcements with a registry sheet: 597,149 of 597,166 (17 print a second
  number of four digits or more after the sheet and are left without one).
- Sheets with a name change in the window: 9,703. Later announcements on those sheets:
  5,603, of which 5,544 (98.9%) carry a name other than the first one seen. Matching by
  name alone loses them unless the old and new names are kept as aliases; the sheet
  keeps all of them.
- Announcements whose name differs from the sheet's first name without any rename act:
  826 (the Registro's spelling changes, legal-form changes).
- Sheets with an extinction: 36,145; announcements after it on the same sheet: 147.
  Sheets with a dissolution: 30,779; announcements after it: 1,018. A dissolved company
  keeps publishing (liquidators, the extinction itself), so only an extinction followed
  by more acts is ignored.

## Officer role labels

`calibrate --roles`, whole year: 1,048,130 role mentions with 613 distinct labels as
published; 99.21% map to a canonical role (the shipped table of
`lists/role_labels.txt`, then the rules of `officers.role_of_label`). The rest stay
`other` with the raw label kept.

## Memory: full inverted index against the targeted count

`calibrate --memory --memory-days 6 13`: peak Python memory (tracemalloc) of an index of
every word of every unrelated announcement against the count of only the asked tokens,
over the same documents.

| Window | Documents | Full index: keys | Full index: peak MB | Targeted: peak MB |
|---|---:|---:|---:|---:|
| 24-30 Sep 2025 | 158 | 28,153 | 21.4 | 2.9 |
| 24 Sep - 7 Oct 2025 | 312 | 45,620 | 41.4 | 4.0 |

Doubling the documents doubles the full index (21.4 to 41.4 MB); the targeted count
barely moves (most of its 2.9-4.0 MB is parsing one document at a time). A linear
extrapolation of the full index to the 8,026 documents of the year gives about 1 GB
(extrapolated, not measured). Longer windows were not run: this measurement only needs
to show which of the two grows with the corpus, and the machine was running the
full-history sweeps at the same time.
