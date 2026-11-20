# borme-radar

Keeps a company record in sync with Spain's Official Gazette of the Companies Register
(BORME, Sections A and B) and discovers **new companies of watched groups**. It
downloads the daily gazette from the BOE open-data API, parses every announcement into
typed acts, learns the **registry sheet** of each watched company from its first exact
name match and follows it from then on, keeps the company record as **dated values**
(name, address, capital, status, corporate purpose), keeps the history of **officer
appointments and removals**, dates **the year each company joined its group**, and
flags companies that appear to belong to a watched group but are not in the watchlist.
It runs over the whole history published in XML, **January 2009 to today**, processed
again most recent year first. Nothing is written to the record or added to the
watchlist without a person: `sync` and `joined` show the changes and write only with
`--apply`, and discovery leads are ordered, never accepted.

## Why

A company record fed from annual accounts is stale by design: the Spanish Companies Act
allows six months after year-end to approve the accounts (art. 164 LSC) and one more
month to deposit them (art. 279 LSC), so they describe the company as it was at least
seven months earlier (statutory deadlines, not measured here). The Registro publishes
renames, changes of address, capital operations, dissolutions and new directors much
sooner: the median gap between the inscription date printed in each announcement and
its publication was **7 days** over the 12 months to 23 September 2026 (p10 7, p90 10;
n = 597,157) and **8 days** over 2009-2026 (p10 7, p90 14; n = 9,702,871), measured
below.

## How it works

```
 BOE open-data API: daily summary (JSON) + one XML per province, Sections A and B
        | up to 4 parallel downloads, permanent atomic cache, a failed day is skipped and listed
        v
 parser: acts from a closed catalogue of headings (accents optional, also when glued to a
         name), Section B tables, registry sheet ("H M 786863"; two long numbers: none),
         scope of each act: board / attorney / auditor / company
        v
 matcher: 1. registry sheet   2. identical normalised name or alias (learns the sheet)
          3. similar name >= 90 -> review queue, never matched
        v
 pipeline (per announcement)
   watched  -> stored; dated values of the record; officer changes; aliases; orphans re-linked
   similar  -> stored in the review queue
   sheet of a lead found by mention -> stored with that lead
   unknown  -> discovery signals: mention > address > brand token > (person, local, opt-in)
               hit -> stored as a lead (judged again every run)
   anything else -> only counted (acts by type and scope, publication lag)
        v
 SQLite: the record and its dated values (view record_now), officer changes (role_changes)
         and the positions still open (view officers_now), leads, similar names waiting
         for a person, per-document tallies, year each company joined its group
        v
 sync (--apply) | joined (--apply) | report (one part per group, --no-details) | triage
 rebuild: the whole cache again, most recent year first, in passes (or --history-only)
 calibrate | formats | evaluate: read a window of the cache once, keep only the counts asked
```

Modules in `src/borme_radar/`: `httpclient`, `cache`, `source`, `download`, `runner`,
`parser`, `acts`, `officers`, `relations`, `normalize`, `watchlist`, `matching`,
`pipeline`, `record`, `positions`, `discovery`, `persons`, `joining`, `store` (+
`sql/`), `corpus`, `formats` (+ `lists/`, the word lists it measures), `evaluate`,
`triage`, `report`, `cli`. Runtime dependencies: `httpx`, `rapidfuzz`, `truststore`.

## Decisions from my production system (no internal figures)

This is a rebuild on public data of the BORME radar I built at work. The decisions in
the first table come from that system; the reasons given for them are measurements of
this rebuild on the public gazette, with the command that gives each one. A few values
were kept from production as they were and are said to be so where they are used
(`TOKEN_NOISE_MAX`, `FUZZY_CUTOFF`, `GROUP_OFFICER_MIN` and the two windows of
`joining.py`); the counts that check them are in
[docs/calibration.md](docs/calibration.md) and [docs/history.md](docs/history.md), which
also list the values that are choices.

| Decision | Why (measured here) |
|---|---|
| **The registry sheet is the key**; a name match only starts it. The sheet is learned from the first announcement that matches by identical normalised name, and the stored announcements already carrying it are linked to the company then. A sheet printed with two different long numbers ("H V 10888,194956") is not read, and that announcement is matched by name. | The BORME prints no tax ID, and a company's name is written many ways over the years. Of the 22,729 announcements of the watched parents in the full history, 22,449 were linked by their sheet and 280 by name (`stats`); those 280 are Section B entries without a sheet (a query of the database, [docs/history.md](docs/history.md) section 2). |
| A long history is **processed most recent year first, in passes**; the later passes look only at exact matches (sheet, name, alias). | A rename is published under the old name and the sheet is learned under the current one. Most recent first, the first pass linked 98.9% of the parents' announcements and found 18 renames; in date order, 95.4% and 2. |
| Similar names are **never matched**: they wait in a review queue. | In a year of gazettes, no name scoring 70 or more against a watched name was the watched company (its sheet says so): every match by similarity would have been another company (`calibrate --fuzzy`). |
| A sheet is **not learned from an announcement that ends it** (extinction, sheet closure). | Nothing is gained from a sheet that is over: in a year, 147 announcements followed the 36,145 extinctions on their sheet (`calibrate --renames`). |
| **Homonym guard**: an act dated long before the company's incorporation is not the company's. | 99.9% of a year's incorporations are inscribed no more than 93 days before the start of operations they declare (`calibrate --lag`); an older act under the same name is an earlier company's, with its own history. |
| Both names of a rename become **aliases**. | The announcement of a rename goes out under the old name and the next ones under the new: with both known, every one of them matches by name. |
| Normalisation drops what the Registro or the law adds to a name: registry notes `(R.M. ...)`, a year in brackets at the end, the words of a passing status (en liquidación, en concurso, unipersonal). | They come and go, and each one gives the same company another key: 826 announcements of a year carry a name other than the first one of their sheet without any rename act (`calibrate --renames`). |
| Text the Registro **prints without a separator** is cut where it starts: an act heading glued to the last name ("...PERSONA UNONombramientos."), a role glued to the last holder ("...SLRepresentan:"). | `formats` counts 5,362 headings glued to a name and 2,896 roles glued to a holder in 18 years; left whole, each is read as part of the act or the holder before it. |
| Each act has a **scope** (board, attorney, auditor, company); **acts of attorneys and auditors stay at low priority**. | 97% of the 269,090 officer events of the watched parents are about attorneys; as alerts they would bury the board. |
| **Officer events** keep the role label **as published and a canonical role**; a holder is a **legal person by what its name carries** (a legal form, a word only company names use), never by what it lacks, and forms that are also names or initials ("SAS", "SPA") count only at the end. Names are **never reordered to tell people apart**. | No official list of the abbreviations exists: the 213 labels used 100 times or more hold 99.2% of the 17.9 million mentions (`formats`), and the rest keep their label. `formats` finds SAS, SPA, AG and SAP inside people's names. Reordering across companies would make one person of two. |
| **Positions held today** are worked out from the changes, the only thing the Registro publishes: inside one company and one role the same words in another order are one person, an appointment to a seat with one holder (the sole director, the auditor of each kind of accounts) ends the earlier holder, and so does an appointment to another form of governing body. | Taking the last event of each name as published leaves 39,076 positions open in the full history, against 38,691 with these rules; for auditors 82 against 35 (`stats`). |
| The **record is chosen from dated values**, not edited act by act: any value of the Registro before the watchlist's, then the latest day, then on one day the latest stage of the status; a status the company outlived is left out. The capital is the subscribed amount after the operation ("Resultante Suscrito"; "Capital suscrito" when an incorporation is not paid up in full), never the amount of the operation. The purpose is read only where an act states it whole: an incorporation or a change of purpose. A street renamed or renumbered by the city council is a new address although the company did not move. | A rule over all the values gives the same record in whatever order the acts were read, and a rebuild reads them in many. 38% of the acts about the purpose are extensions (`formats`), whose text is only the activity added. |
| The **registered corporate purpose** and the **activity description** of another source are different fields; nothing from the Registro writes the second. | One is what the by-laws allow, the other what someone says the company does; one written over the other loses the second. |
| **`sync` shows by default and writes only with `--apply`**, logging the value it replaced, and undoes a write whose observation is gone. Addresses and municipalities are compared **as places**. | In the full history the record and the Registro wrote 12 addresses or municipalities differently that were the same place; as changes they would crowd the 55 real ones (`sync`). |
| The **year each company joined its group** is read from its acts and stored with the kind of fact it rests on (a group company as sole shareholder, the group's officers, the incorporation only), with whether an inscribed act confirms the tie, and with whether a person typed it; `joined` writes only with `--apply` and never over a typed year. | The kinds do not agree enough to be merged: of the 298 entries an owner act confirms, the officers alone date 76 (`joined`). |
| **Discovery** tries its signals from the most reliable and keeps one lead per announcement: a watched company in a relation slot (sole shareholder, absorbed, beneficiary, director), the registered address of a watched company, an approved brand token of one or two words, and, local and opt-in, rare pairs of name words of the groups' officers. | On the test window, against the broader ground truth: mentions 92% precise, tokens 54%, addresses 14% (`evaluate`). |
| An address where calibration counted many unrelated companies is **not used**; the other signals still run. | At the watched addresses of the train window, headquarters had at most 5 unrelated companies and office buildings 14 or more (`calibrate --addresses`). |
| **Proposing** brand tokens and **measuring** them are separate steps, and nothing reads a list that was not approved. | Of the same 39 proposed tokens, 23 pass on 3 months and 19 on 12 (`calibrate --tokens`): a list judged by eye, or on a short window, lets common words through. |
| **Leads are never added by themselves** and are **judged again on every run** with the current rules, those without stored acts included. | The BORME gives no tax ID to confirm one; and as names and sheets are learned a lead can turn out to be a watched company: judged again at the end of the full history, 2 were. |
| A **triage score** orders the open leads in tiers and decides nothing. | The full history leaves 2,166 open leads. |
| **Calibration reads the cached corpus once** and counts only what each question needs, companies deduplicated by normalised name and the watched groups left out; a partial reading saves no proposal. | A full index of every word doubles with the corpus (21.4 to 41.4 MB for twice the documents) while the targeted count stays at 2.9 to 4.0 MB (`calibrate --memory`); counts over part of the corpus are lower than the real ones and would let any token pass. |
| **Data minimisation**: only the announcements of the watched companies, the review queue and the leads are stored; the rest is counted and stays in the cache (`--store-all` and `prune` switch it). | 55,282 of 13.0 million announcements are kept (0.42%); the others name people the radar has no reason to hold. |
| **Section B** is read (deposits of merger and split projects, sheet closures and reopenings), with a length cap that splits a header listing several companies. | A merger project is deposited and published there before the merger is inscribed: 338 merger projects in 12 months. |
| **Up to 4 parallel downloads**; a **day that fails is skipped and listed**, and running the same range again does it. | Four workers download a week 4.1 to 4.2 times faster than one with the same pause per request; a backfill of 6,531 days should not stop for one. |
| **`rebuild`** regenerates every derived table offline from the cache and **`rebuild --history-only`** the dated values and officer changes from the stored acts, deleting what was derived before; **loads are upserts that never lose a match**. | A fix of the parser has to reach 18 years: `--history-only` derives the 269,090 officer events again in 15 s, and an upsert keeps the match a later pass found when an earlier document is read again. |
| A shareable report has **no act text and no names of natural persons**; one person given or stripped of powers in several companies of a group on one day is counted, not named. | The gazette names natural persons: 16.7 million of the 17.9 million holders of 18 years are people (`formats`). |

Decisions of this rebuild, not of the production system:

| Decision | Why |
|---|---|
| Retries only for 429, 5xx, network errors and truncated or invalid payloads; `Retry-After` honoured; a payload is validated before it is cached. | Permanent errors cannot be fixed by retrying. (The production system retried every error except 404.) |
| TLS against the operating-system trust store (`truststore`). | certifi's bundle failed on the development machine; verification is never disabled. |
| The cache holds **only byte-identical BOE responses**: the copy of the same gazettes kept by my production system was not used as a seed. | Checked on a sample: same content, other bytes (it had been saved with Windows line ends). A cache is trusted forever. |
| The publication lag and the parser coverage are measured on every run; `formats` counts how often each format quirk appears in the corpus. | The lag is usually assumed and the quirks are guessed from a few cases; here both are counted. |
| Mentions are matched in **relation slots** (sole shareholder, absorbed, beneficiary, administrator, Section B co-listing), never as substrings of the text. | A watched company sitting on a board or holding powers of attorney is not a group relation. |
| An **extinction** followed by later acts is ignored, and so is a dissolution inscribed the same day as an extinction (a merger); a dissolution alone is not. Production ignores both whenever acts follow. | In a year, 1,018 announcements followed a dissolution on the same sheet (liquidators, the extinction itself), against 147 after an extinction (`calibrate --renames`). The full history added the merger case: a parent that took the name of the company it absorbed inherited its dissolution. |
| Discovery is evaluated against **explicit company-to-company relations published in the BORME**, with thresholds chosen on a train window and measured on a later test window. | A ground truth that is public and about legal persons only. |
| Announcements carrying the sheet of a **lead found by mention are stored with it**, during the sweep. | Its incorporation, earlier owners and board date when it joined the group. Production gets them because the group's companies are in its record, and dated the rest with a separate search by name, then by sheet. |
| The passes of a sweep **stop when one adds nothing** (no sheet, alias or announcement), or at `--passes`. | Production ran two or three passes by hand. |
| Abbreviations of a legal form counted **anywhere** in a holder's name: LTD, LLC, INC, PLC, BV, NV, SARL, SRL, LTDA, SAT, CB, SC, SRC, and also LIMITED, LLP, GMBH, COOP and SRCP; **only at the end**: SAS, SPA, AG, SAP. | The first 17 were re-derived from where each one appears in 18 years of holders ([docs/history.md](docs/history.md)): only SAS, SPA, AG and SAP were found inside people's names in the samples read (with the AI assistant used for this rebuild). The other five were not measured: none is a Spanish name or word. Production counts the first 13 except SAT only at the end, and SAT and LLP not at all. |
| A second number after the sheet of **up to 3 digits** is noise and the first number is the sheet; from 4 digits the sheet is left unknown. | Measured on 18 years: where the company prints a single sheet elsewhere, a second number of up to 3 digits is never the sheet; from 4 digits it is as often as the first. Production cuts at 2 digits. |
| A holder written in **mixed case is still a name**; only two or more lowercase words make a sentence. An ID number (5 or more digits in a row) only fits inside a company name. | The gazette has names in title case, which production drops with any lowercase; production also drops any digit. The counts behind both rules are in [docs/history.md](docs/history.md). |
| The **auditor class** is read from the published label with rules derived from the labels counted in the gazette (15 in 12 months). Every label seen in 18 years gets a class; about 30 mentions carry part of a holder's name and a few rare labels fall to individual by default. | A substring rule on "Supl" and "Con" files "AUDITOR SUP" as individual and "AUDIT.CONJUN" (joint) as consolidated. |
| Holders of the form of body in force **appointed before the last appointment of another form** are not held either. | A board, then a sole director, then a board again: the first board is over. The production view leaves that case open. |
| **The positions held are worked out in Python** from the changes, company by company, and stored (`open_roles`, read through the view `officers_now`): each holder's last change says whether it opened the position, and a later day that fills its seat or appoints the body under another form closes it; nothing closed reopens. | It is the result of applying every change in date order (the tests compare the two), and an auditor replaced and then removed does not bring back the one before. |
| Words that make a holder a legal person **wherever they are**: the long words of the Spanish legal forms, the foreign and professional forms above, the types of legal person that carry no legal form (foundation, association, city council, university, savings bank, institute, federation, consortium, agency, also in Catalan and Galician), and 133 words **measured on 18 years of holders**: each in 1,000 holders or more and in 100 different names or more, at least 80% of those holders with a legal form ([`lists/company_words.txt`](src/borme_radar/lists/company_words.txt)). | Production keeps its own list. A word that only company names use catches the names cut before their legal form and those that carry none; asking for 100 different names keeps out the brand of a single firm. The types of legal person are not measured: by their place in a name they cannot be told from surnames ([docs/history.md](docs/history.md)). |
| The **role of a label** is read from its letters by rules written from the labels of 18 years: a member, chair or secretary of a committee is not the board, and "Adm. Mancom." is a joint director. The 213 labels used 100 times or more are listed with their role ([`lists/role_labels.txt`](src/borme_radar/lists/role_labels.txt)): 99.2% of the 17.9 million label mentions. | There is no official list of the abbreviations; the labels the Registro uses most are counted, not assumed, and the rules that read them are written. |
| An **address is a site** for discovery: the municipality (any of its official names), the number (the kilometre point on a road) and the words that place it, compared with the place rule of the record below; two sites are one only with a word in common that is not one of the **209 words found in 500 different street names or more** (and 1,000 streets: SAN, GENERAL, DOCTOR, JUAN...: [`lists/generic_street_words.txt`](src/borme_radar/lists/generic_street_words.txt)), or the same kilometre point. An address with generic words only places nothing. | A word shared by many streets is not a street; counted on 18 years of registered addresses instead of listed by hand. The cost: 5 of the 30 watched addresses (GRAN VIA, EUROPA...) give no address signal ([docs/calibration.md](docs/calibration.md)). |
| A holder of **49 characters or more** whose last word starts a legal-form word is a company. | The Registro cuts holders at 50 characters, and 49 is where the counts of holders by length rise again after the trailing punctuation is stripped ([docs/history.md](docs/history.md)). Production also recognises names already in its database; this rebuild has no such base. |
| Two addresses are **the same place** when their number (the kilometre point on a road) is the same and what else one says is in the other: the other numbers, and the distinctive words. The street types and entrance words left out are measured on 18 years of registered streets ([`lists/street_filler.txt`](src/borme_radar/lists/street_filler.txt)), plus the street types in full and the number words the Registro mostly abbreviates, and the articles of three or four letters; the last word of a street of 47 characters or more may be cut and only has to start a word of the other address. | Production compares words and numbers each in either direction and drops the last word of a long street; here the number decides by itself, and a cut word still has to match. |
| A **street renamed or renumbered** by the city council is read with the wordings written from the "Otros conceptos" texts about a street; `formats` counts how often each wording is used and how many such texts have none ([docs/history.md](docs/history.md)). A correction of an earlier inscription, the domicile of a person and an address without a house number are not read. | A street name without its number would overwrite the address with less than it had. |
| An **amount** is read only in euros, after its label: older acts give some capitals in pesetas, which are left unread. | Read as euros, 6.000.000 pesetas would be a capital of six million euros. |
| A group company is recognised by its name **without the legal form from 4 characters**, the floor measured for bare mentions in discovery. | The same question as a bare mention ([docs/calibration.md](docs/calibration.md)). |
| The head of a group is the company marked `head` in the watchlist, or its only watched company; "before the gazette window" means before the first processed day. | The watchlist has no ownership data. |
| The **triage score** multiplies the measured precision of the signal (combined with a brand token in the name as a second clue), a freshness that halves every 180 days from the last day the lead was published (not the inscription day an announcement prints, misprinted in a few: one says 2097) and log2(1 + announcements); the tiers are the first fifth, the next three tenths and the rest (a lead scored 0 is always in the rest). | The precisions are measured ([docs/calibration.md](docs/calibration.md)); the half-life and the shares are choices, since no truth says which lead a person should read first. |

## Re-measured here on public data

Version 0.2.0 was published in September 2026; the history of this repository starts at
0.3.0, and every figure below was measured with 0.3.0 on 20 November 2026 (Windows 11,
Python 3.13.7, httpx 0.28.1, rapidfuzz 3.14.6), each series of runs from an empty data
directory that reads the same cache. The one exception is the download of the full
history, made on 18 November with the downloader of version 0.2.0: the cache holds the
BOE's bytes whatever the version, and the 12 months downloaded again with 0.3.0 came out
byte-identical to it. The watchlist is always `examples/watchlist.csv`: the parent
companies of 32 Spanish listed groups (mostly IBEX-35), with registered addresses
written from public information; the brand tokens are
`examples/brand_tokens.approved.json` and the address counts
`examples/address_noise.json` ([docs/calibration.md](docs/calibration.md)).

### The full history, 2009-2026

Every issue published in XML, **2 January 2009 to 18 November 2026**. Details and
commands in [docs/history.md](docs/history.md).

| Measure | Value |
|---|---:|
| Days with gazette / failed | 4,446 / 0 |
| Documents / announcements / acts | 146,610 / 13,009,816 / 21,355,018 |
| Cache | 151,056 files, 4.5 GB, downloaded with 4 workers (1 retry, 0 failed days) |
| Section A announcements with a registry sheet | 99.996% of 9,703,853 |
| Inscription to publication, median (p10 / p90) | 8 days (7 / 14), n = 9,702,871 |
| Kept in the database | 55,282 announcements (0.42%) |

**The order of the sweep.** Both orders end with the same 45 sheets, 19 aliases and
22,729 announcements of the watched parents. Most recent first finds 98.9% of them in
the first pass and all of them in two; in date order the first pass finds 95.4%, and a
third pass is still learning a sheet and linking 227 announcements. In the first pass,
most recent first learns 18 renames against 2: the announcement of a rename is published
under the old name, and it is only recognised once the sheet is known. Of the 22,729,
22,449 are linked by their sheet and 280 by name: all 280 are Section B entries without
a sheet (a query of the database in [docs/history.md](docs/history.md) section 2; 15,763
Section B entries do carry one). (The announcements of leads followed by their sheet
settle more slowly in both orders.)

| Order | Pass 1 | Pass 2 | Pass 3 |
|---|---:|---:|---:|
| Most recent first: watched announcements linked | 22,480 | +249 | +0 |
| In date order: watched announcements linked | 21,677 | +825 | +227 |

**Format quirks** (`borme-radar formats`, 18 years): 5,362 act headings glued to a name
and 15,223 after a full stop and a dash; 6,836 sheets printed with two numbers (where
the company prints a single sheet elsewhere, a second number of up to 3 digits is never
the sheet, from 4 digits it is as often as the first); 2,896 roles glued to a holder;
1,199,521 of 17,864,221 holders are legal persons (36,504 that version 0.2 took for
people, and 1,539 the other way: mostly "SAS", "SPA" or "AG" inside a person's name);
8,386 incorporations with "Capital suscrito" and 660 streets renamed or renumbered by a
city council. **38% of the acts about the corporate purpose are extensions** (142,349 of
372,069), which version 0.2 stored as the purpose. The same run measures the constants
of the parser: holders are cut from 49 characters and streets from 47, 133 company
words, 120 street words, 209 words shared by many street names and the roles of 213
labels, all of them equal to the lists the package ships.

**Record.** 604 registry observations of the 32 parents (capital 502, address 30,
municipality 30, purpose 22, name 18, status 2). From a record filled only from
`examples/watchlist.csv`, `sync` listed 55 differences (capital 30, purpose 15, address
5, municipality 4, name 1: moves of registered office inscribed in 2013-2026, capital
operations, purposes the watchlist did not carry and a rename); 12 more addresses or
municipalities were the same place written otherwise and are not listed. `--apply` wrote
them; the next `sync` listed none. The full history also found a case the rules did not
cover: a parent that took, in a merger, the name of the company it absorbed inherited
that company's dissolution. Fixed (a dissolution inscribed with an extinction is ignored
when the company keeps publishing) and measured again.

**Positions held.** 269,090 officer events (97% about attorneys). Positions held
today, worked out from the events: 38,691, against 39,076 with the last event per
name as published; auditors 35 against 82. The person key makes 931 persons of
1,883 of the 119,763 names as published (per company and role). There is no public
list to check them against.

**Year each company joined its group** (`borme-radar joined`): 641 companies (the 32
parents and the 609 leads tied to them by ownership). 298 years rest on an owner act (a
group company as sole shareholder), 154 on the group's officers alone and 6 on the
incorporation of a parent; 183 companies get no year (26 parents incorporated before
2009, and 157 companies already in the group the first time the gazette shows them or
never tied to it). There is **no public ground truth** for these dates, so there is no
accuracy here. What can be checked is one kind of evidence against the other: of the 298
entries an owner act confirms, dated again from the officers alone, 76 get a year and 64
the same one; for 222 the officers give none, because the people who run a subsidiary
are not the parent's board, which is all this watchlist knows of each group.

### The 12 months to 23 September 2026

The gazettes of **24 Sep 2025 to 23 Sep 2026**, where discovery is calibrated (train
window, to 23 March) and evaluated (test window, from 24 March).

#### Download

| Measure | Value |
|---|---:|
| Command | `borme-radar fetch --from 2025-09-24 --to 2026-09-23 --workers 4 --download-only` |
| Days with gazette / without | 252 / 113 |
| Documents | 8,026 (Section A 7,611, Section B 415) + 252 daily summaries |
| Network requests / retries / failed days | 8,395 / 4 / 0 |
| Wall time (4 workers, 1 s between requests of each worker) | 2,253.5 s (38 min) |
| Median request latency | 0.049 s |
| Cache on disk | 8,278 files, 252.8 MB, byte-identical to the cache of the full history |

Serial against 4 workers, same range, cold caches, same 1 s delay per worker:

| Week | 4 workers | Serial | Speed-up |
|---|---:|---:|---:|
| 31 Aug - 4 Sep 2026 (4 workers ran first) | 39.7 s | 163.0 s | 4.1x |
| 24 - 28 Aug 2026 (serial ran first) | 38.5 s | 160.1 s | 4.2x |

The BOE answered in 42-53 ms per request (median) in both orders, so the delay between
requests, not the server, set the pace.

#### Processing

`borme-radar rebuild --from 2025-09-24 --to 2026-09-23 --watchlist
examples/watchlist.csv --tokens examples/brand_tokens.approved.json --address-noise
examples/address_noise.json`: three passes over 597,984 announcements and 1,165,894
acts, 913 s (365 + 273 + 275, on a machine running the full-history sweeps at the same
time). Kept in the database: **1,990 announcements (0.33%)**: 1,326 of watched
companies, 5 in the review queue, 659 of discovery leads. No day failed.

#### Parser and publication lag

| Measure | Value |
|---|---:|
| Announcements (Section A / B) | 597,166 / 818 |
| Section A announcements with a registry sheet | 597,149 (100.00%) |
| Announcements with text before the first known heading | 36 |
| Announcements without any recognised act | 5 |
| Inscription to publication (Section A), median (p10 / p90) | 7 days (7 / 10); 63.0% exactly 7 |
| Longest Section A company name | 191 characters (p99.9: 88) |
| Section B: merger / split / global transfer / transfer abroad projects deposited | 338 / 93 / 14 / 16 |

#### Registry sheet and renames

- All 32 watched parents were matched; their 32 sheets were learned from exact names,
  and **1,325 of their 1,326 announcements matched by sheet** (1 by name: a Section B
  entry without a sheet).
- One watched company was renamed in the window (Inmobiliaria Colonial to COLONIAL SFL
  SOCIMI SA, inscription dated 1 Oct 2025); the new name is kept as an alias.
- Over the whole gazette, 9,703 sheets had a rename in the window; 5,544 of the 5,603
  later announcements on those sheets (98.9%) carry a name other than the first one
  seen: lost by name-only matching, kept by the sheet.

#### Scope of officer acts

Of the 575,515 officer acts of the year (appointments, removals, revocations,
re-elections, ex officio cancellations): **board 71.6%, attorney 22.8%, auditor 5.0%**,
other 0.6%. For the watched parents it is the other way round: of their 1,224 officer
acts, **1,034 (84.5%) concern attorneys** and 166 (13.6%) the board; only the board ones
reach the alerts. Role labels: 1,048,130 mentions, 613 distinct labels, 99.21% mapped to
a canonical role.

#### Officer history and record

- **8,090 officer events** for the watched companies: 4,070 appointments, 3,734
  revocations, 178 re-elections and 108 removals; 139 of them held by a company; 3,319
  positions held. Names stay in the local database.
- 31 registry observations for the watched companies. `sync` lists **16 differences**
  (15 capitals the record did not have, 1 rename).

#### Discovery of new group companies

Open leads after the rebuild: **130** (21 by address, 28 by mention, 81 by brand token).
Triage with the precisions of the evaluation below: 26 "first", 39 "next", 65 "rest".
The token list used is the calibration proposal copied as is
(`examples/brand_tokens.approved.json`); in use, a person reviews it first.

Evaluation (`borme-radar evaluate`) on the **test window, 24 Mar - 23 Sep 2026** (4,052
documents, 281,291 announcements), with the tokens and the address counts of the train
window. Two ground truths: **explicit company-to-company relations** published in the
window, propagated along the relations (17 companies, 16 with an announcement of their
own); and the same **or at least two officers in common** with a watched company (211
companies, 210 with announcements). Precision is a lower bound (a real subsidiary with
no published evidence in the window counts as wrong); recall is over the companies of
each truth that published something.

| Signal | Flagged | Precision vs relations | Recall vs relations | Precision vs relations or shared officers | Recall vs relations or shared officers |
|---|---:|---:|---:|---:|---:|
| mention | 13 | 84.6% | 68.8% | 92.3% | 5.7% |
| address | 7 | 0.0% | 0.0% | 14.3% | 0.5% |
| brand token | 68 | 1.5% | 6.2% | 54.4% | 17.6% |
| combined detector | 87 | 13.8% | 75.0% | 56.3% | 23.3% |
| person (local, opt-in; `--persons`) | 762 | 0.4% | 18.8% | 19.8% | 71.9% |

How to read it: the mention signal reads the same relation slots as the first truth, so
its precision there checks the matching rather than an independent source. Tokens and
addresses use evidence neither truth uses. Against relations alone they look useless,
because a year of gazettes publishes relations for few companies of a large group;
against the broader truth, half of the token leads are group companies. The address
signal is weak for listed groups: a few subsidiaries are registered at the parent's
address, but so are many unrelated tenants of the same office buildings, and 5 of the 30
watched addresses place nothing. The person signal is not independent of the
shared-officers truth (both use officers) and flags 762 companies: the watched parents
have thousands of officers, most of them attorneys who also act for other companies. It
is off by default and its keys never leave the local database.

#### Calibration

[docs/calibration.md](docs/calibration.md) has the commands, the histograms and where
each threshold comes from: similar-name cut-off (90, kept from production), homonym
guard (93 days), Section B name cap (191 characters), minimum length of a bare mention
(4 characters), brand-token noise (at most 5 unrelated companies, kept from production),
address noise (not used from 6), surname pairs (never seen outside the group), and the
memory comparison: a full inverted index took 41.4 MB for 312 documents and doubles with
the corpus, while the targeted count took 4.0 MB. Measured on 3 months instead of 12,
the same token list passes 23 tokens instead of 19.

## How to run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"      # Windows; .venv/bin/python elsewhere

# download and process (at most 4 workers, 1 s between requests of each worker by default)
borme-radar fetch --from 2026-09-01 --to 2026-09-23 --watchlist examples/watchlist.csv \
    --tokens examples/brand_tokens.approved.json --address-noise examples/address_noise.json
borme-radar stats
borme-radar sync                     # shows what differs; --apply writes and logs it
borme-radar joined                   # year each company joined its group; --apply stores it
borme-radar triage --tokens examples/brand_tokens.approved.json
borme-radar report --from 2026-09-01 --to 2026-09-23 --no-details --out reports/radar.md

# the whole history: fill the cache, then process it offline, most recent year first
borme-radar fetch --from 2009-01-01 --to 2026-11-18 --download-only
borme-radar rebuild --from 2009-01-02 --to 2026-11-18 --watchlist examples/watchlist.csv \
    --tokens examples/brand_tokens.approved.json \
    --address-noise examples/address_noise.json          # --order chronological, --passes N

# offline
borme-radar rebuild --history-only                       # dated values and officer changes only
borme-radar relink [--apply]                             # orphans of known sheets
borme-radar prune                                        # drop what is no longer relevant
borme-radar formats --from 2009-01-02 --to 2026-11-18     # format quirks of the corpus
borme-radar calibrate --from 2025-09-24 --to 2026-03-23 \
    --tokens examples/brand_tokens.proposed.json --addresses --hq
borme-radar evaluate --from 2026-03-24 --to 2026-09-23 \
    --tokens examples/brand_tokens.approved.json
```

`--address-noise` names the counts of `calibrate --addresses` that discovery reads to
leave crowded addresses out; without it, `fetch`, `rebuild` and `evaluate` look for
`calibration/address_noise.json` in the data directory, and with neither no address is
left out. `examples/address_noise.json` is the count of the train window
([docs/calibration.md](docs/calibration.md)); `calibrate` needs the officers of the
watched companies, so it runs on a database that has processed its window first.

The watchlist is a CSV with `group` and `name` columns, plus optional `address`, `city`,
`capital`, `status`, `purpose` (the corporate purpose as registered), `activity` (a
description from another source, never synced), `sheet`, `province`, `incorporated`,
`head` (`1` for the head of its group) and `joined` (a year decided by a person; never
overwritten). Everything is written under `data/` (`--data-dir` to change it): the
cache, the SQLite database and the measurement outputs (`calibration/`: the reports of
`calibrate`, `formats`, `evaluate` and of each sweep, the token proposal, the address
counts, `joining.json`); `data/` and `reports/` are gitignored. Nothing is scheduled:
the radar is run on demand. `examples/report_2026-09_no-details.md` is a shareable
report of 1-23 September 2026, written with `--no-details` by version 0.3.0 from the
12-month run above.

## Privacy

Announcements name directors, attorneys, sole shareholders and judges. They stay in the
local database (`role_changes`, stored acts, `surname_pairs`), which is gitignored. The
committed Section A fixtures are real documents with natural-person names replaced by
`PERSONA N` (`scripts/make_fixture.py`; names it missed because a role or a heading
was glued to them were replaced in a separate edit); the Section B fixture names only
companies. Shared reports use `--no-details`, and the person signal, the year of joining
a group and the format counts publish only aggregate figures (the `formats` samples
quote the gazette and stay local). The BOE reuse terms require processing personal data
under the GDPR and the LOPDGDD.

## Tests

```bash
pytest            # 359 passed on 20 Nov 2026 (Windows 11, Python 3.13.7; also 3.11.16)
ruff check . && ruff format --check .
```

The suite runs offline (`tests/conftest.py` makes any socket connection fail). It covers
the parser on real Section A and B fixtures (2026 documents with glued headings and
roles, two-number sheets, a purpose extension, subscribed capital, a street renamed by
the city council, foreign and professional legal forms), the role parser, relations,
normalisation, matching and its guards, sheet learning and orphan re-linking, upserts
that never lose a match, aliases, minimisation, leads judged again (including leads
without stored acts), the record rules and `sync` (apply, log, revert, addresses
compared as places), the positions held (against a replay of the events), the schema
and the refusal of a database of an earlier layout, the history sweep in both orders
and its passes, followed leads, the year of joining a group, the downloader with a fake
server (parallel downloads, a failed day skipped and recovered, offline runs), the
calibration, format and evaluation collectors and the values `formats` derives (cut
lengths, company words, street words, role labels), the triage, the report without
names, and the CLI end to end from a pre-filled cache. GitHub Actions runs ruff and
pytest on Python 3.11, 3.12 and 3.13.

## Limitations and next steps

- **Section C** (legal notices: general meetings, creditor notices) is free prose and is
  not parsed, and competitors are not detected by corporate purpose. Both were left out
  on purpose in the production system too.
- **One company per group in the example watchlist.** One person given powers in several
  companies of a group, and group officers appointed in several companies, need more;
  with one parent per group the first part is empty and the second rule never applies
  (both are covered by tests). The year of joining is therefore dated for the leads tied
  by ownership, and 26 of the 32 parents, incorporated before 2009, get no year. Those
  609 leads were not reviewed by a person: their years are dated as if each one were a
  group company, which a published tie of ownership suggests but does not prove.
- **The year of joining has no public ground truth** here: no public list gives the
  date each company entered a listed group. What is checked is how the officers alone
  agree with the owner act where both exist; the exits from a group are not dated.
- A **change of purpose** that the Registro files under "Cambio de objeto social" but
  that only extends or amends part of the purpose is read as the whole purpose (2 of the
  15 purposes `sync` proposed in the full history); `sync` shows it before it writes.
- The registered addresses in the watchlist were written from public information; at 15
  of the 25 sites no company at all registered in the train window, 5 of the 30
  addresses place nothing (generic words only), and on the test window the address
  signal found 1 company of the broader truth and none of the explicit relations.
- Legal persons that carry no legal form are recognised by the types of entity listed
  in `officers.LEGAL_PERSON_WORDS` (Spanish, Catalan and Galician). A type outside the
  list, or written in Basque, is taken for a person, and so is a bank written "... BANK"
  without a legal form (the word is in 82 names, below the 100 the company words ask).
  The other way round, ERNST, YOUNG and GRANT are measured company words (names of audit
  firms) and also surnames.
- The triage score orders by the measured precision of each signal; its half-life and
  the shares of the tiers are choices.
- Retries under real throttling were rare: 1 in the 142,086 requests logged for the full
  history (the yearly runs and the recheck pass), 4 in the 8,395 of the 12 months; the
  policy is tested with a fake transport.
- Discovery was calibrated and evaluated on the 12 months to September 2026; the brand
  tokens were not measured again on the full history.

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
from my production system and every figure here was measured on public data.

Alejandro Plaza Lorenzo - alejandroplaza.dev@gmail.com
