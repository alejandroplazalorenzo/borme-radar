# The full history: January 2009 to November 2026

Version 0.3.0 over every BORME issue published in XML: from 2 January 2009 (the first
issue of the open-data API) to 18 November 2026. Windows 11, Python 3.13.7, httpx
0.28.1, rapidfuzz 3.14.6. The download ran on 18 November 2026 with the downloader of
version 0.2.0, the code published in September (it is not in this repository's history):
the cache holds the BOE's bytes whatever the version, and the 12 months downloaded again
with 0.3.0 on 20 November came out byte-identical to it (README). Every other run is of
20 November 2026 with the code of this version, each series of runs from an empty data
directory that reads the same cache. The watchlist is `examples/watchlist.csv` (the
parent companies of 32 listed groups), the brand tokens
`examples/brand_tokens.approved.json` and the address counts
`examples/address_noise.json` (the train window of [calibration.md](calibration.md)).
Every figure comes from the command shown next to it. Names of natural persons are never
printed; company names are public data about legal persons. The samples of `formats`
that this page quotes or counts (they stay local: they quote names) were read with the
AI assistant used for this rebuild, not by a person; each place that cites one says so
again.

## 1. Download

### Why everything was downloaded again

My production system keeps its own copy of the same gazettes. It was not used as a
seed: the cache is trusted forever, so only unmodified BOE responses may go into it.
One-off check of 18 November 2026 (the script is not part of the repository): 51 files
of that copy (2 XML documents per year 2009-2025 and one daily summary per year, picked
at random) downloaded again with this tool's client.

| | Files |
|---|---:|
| Byte-identical to the BOE response | 0 / 51 |
| Identical after turning CRLF line ends into LF | 51 / 51 |
| BOE responses with CRLF line ends | 0 / 51 |

Same content, other bytes: the copy had been saved with Windows line ends. The BOE
itself is stable: 6 documents this tool downloaded in September 2026 were byte-identical
when downloaded again on 18 November (same one-off check). So the whole history was
downloaded again.

### The backfill

`borme-radar fetch --from YYYY-01-01 --to YYYY-12-31 --workers 4 --download-only`, one
year at a time from 2026 back to 2009, then the whole range once more
(`--from 2009-01-01 --to 2026-11-18`) to recover anything missing.

| Measure | Value |
|---|---:|
| Days checked (1 Jan 2009 - 18 Nov 2026) | 6,531 |
| Days with gazette / without (404) | 4,446 / 2,085 |
| Documents (Sections A and B) | 146,610, plus 4,446 daily summaries |
| Cache on disk | 151,056 files, 4.5 GB |
| Failed days | 0 |
| Retries | 1 (a transient error in 2012) |
| Median request latency | 0.036-0.048 s depending on the year |
| Wall time of a full year (4 workers, 0.5 s between requests of each worker) | 1,040-1,500 s |
| Second pass over the whole range | 0 documents missing; only the 2,085 days without gazette were asked again |

2025 and 2026 were downloaded with the default pause of 1 s per worker (the 12 months of
September 2026 were already in the cache); from 2024 back, with 0.5 s, still at most 4
workers. A year of 2009-2011 has about 10,200 documents and 1.6 million announcements
(the deposits of annual accounts were then published one by one in Section A); a
recent year, about 7,700 documents and 550,000 announcements.

### Parsing every document

All 146,610 documents parse. Version 0.2 stopped with a parse error on three of them
(found with the same kind of one-off check): one announcement of 2011 printed without
its number (" - EJEMPLO SA.") and two errata of 2013 under a bare " - " header. They are
kept now, with a negative number unique inside their document.

`borme-radar stats` after the sweep below:

| Measure | Value |
|---|---:|
| Announcements (Section A / B) | 9,703,853 / 3,305,963 |
| Acts | 21,355,018 |
| Section A announcements with a registry sheet | 9,703,472 (99.996%) |
| Announcements with text before the first known heading | 2,378 |
| Announcements without any recognised act | 545 |
| Inscription to publication (Section A), median (p10 / p90) | 8 days (7 / 14), n = 9,702,871 |
| Officer acts by scope: board / attorney / auditor / other | 73.5% / 21.0% / 5.0% / 0.5% of 8,931,638 |

## 2. The sweep: most recent year first

`borme-radar rebuild --from 2009-01-02 --to 2026-11-18 --watchlist
examples/watchlist.csv --tokens examples/brand_tokens.approved.json --address-noise
examples/address_noise.json` (recent first, at most 3 passes), and the same with
`--order chronological --passes 5`, each in an empty data directory. Pass 1 matches and
discovers; the later passes look only at exact matches (sheet, name, alias) and at the
sheets of leads found by mention. The two runs shared the machine with each other,
with `formats`, with the runs of section 7 and with other work, so the seconds compare
between the two orders only.

What each pass adds (sheets learned, aliases from renames, announcements of watched
companies, announcements stored with a followed lead):

| Order | Pass | Seconds | Sheets | Aliases | Watched announcements | Lead announcements |
|---|---:|---:|---:|---:|---:|---:|
| Most recent first | 1 (full) | 5,855 | +45 | +18 | +22,480 | +23,039 |
|  | 2 (exact only) | 4,094 | 0 | +1 | +249 | +8,580 |
|  | 3 (exact only) | 4,062 | 0 | 0 | 0 | +519 |
| Chronological | 1 (full) | 5,835 | +44 | +2 | +21,677 | +20,542 |
|  | 2 (exact only) | 4,088 | 0 | +17 | +825 | +11,077 |
|  | 3 (exact only) | 4,036 | +1 | 0 | +227 | +21 |
|  | 4 (exact only) | 3,547 | 0 | 0 | 0 | +498 |
|  | 5 (exact only) | 3,339 | 0 | 0 | 0 | 0 |

Both orders end with the same 45 sheets, 19 aliases, 22,729 announcements of watched
companies and 32,138 announcements of leads. Most recent first gets 98.9% of the watched
ones in the first pass and all of them in two; in date order the first pass gets 95.4%,
and the third pass is still learning a sheet and linking 227 announcements. The aliases
show why: in date order the first pass learns 2 renames, most recent first learns 18,
because the announcement of a rename is published under the old name and is only
recognised once the sheet is known. Of the 22,729, 22,449 are linked by their sheet and
280 by name (`stats`). The 280 are Section B entries without a sheet: Section B entries
can print one (15,763 do in the 18 years), these do not. `stats` does not print the
section; the database gives it:

```sql
SELECT d.section, n.sheet IS NULL AS without_sheet, COUNT(*)
FROM notices n JOIN gazette_docs d USING (doc_id)
WHERE n.via = 'name' GROUP BY 1, 2;                -- B | 1 | 280
SELECT SUM(t.n) FROM doc_tally t JOIN gazette_docs d USING (doc_id)
WHERE d.section = 'B' AND t.what = 'with_sheet';   -- 15763
```

The lead side does not settle as fast in either order: judging a lead again on all its
stored acts can turn an address or token lead into a mention lead, whose sheet is then
followed in the next pass. Most recent first stopped at its limit of 3 passes still
adding 519 lead announcements; in date order the fifth pass added nothing.

Kept in the database after the sweep (most recent first): 55,282 announcements of
13,009,816 read (0.42%): 22,729 of watched companies, 415 in the review queue and 32,138
of leads; 2,166 open leads (1,242 by address, 644 by mention, 280 by brand token; 2,160
in date order), and 2 more that turned out to be watched companies.

`borme-radar rebuild --history-only` then derives again everything that comes from the
stored acts: 22,729 announcements read, 604 observations and 269,090 officer events in
14.6 seconds (38,691 positions held).

## 3. Format quirks and the constants measured on them

`borme-radar formats --from 2009-01-02 --to 2026-11-18` (two passes over the cache,
14,316 seconds on a machine running both sweeps at the same time): how often each quirk
the parser and the record handle appears, and the counts behind the constants of
sections 3.6 and 3.7. The derived lists (`company_words.txt`, `street_filler.txt`,
`role_labels.txt`, `generic_street_words.txt`) are written next to the JSON report; the
package ships them in `src/borme_radar/lists/`.

### 3.1 Act headings without a separator

| Heading | Occurrences | Announcements |
|---|---:|---:|
| Glued to the capital letter that ends a name ("...PERSONA UNONombramientos.") | 5,362 | |
| After a full stop and a dash (".- Cambio de domicilio social.", ". - ...") | 15,223 | |
| Either | 20,585 | 19,088 |

Out of 9,703,853 Section A announcements; `acts.openings` says what stands before each
heading that opens an act, and `formats` counts them. In the 25 samples of each that
`formats` keeps (locally: they quote names), read with the AI assistant used for this
rebuild: the 25 glued headings are all acts (appointments after a list of removals,
"Otros conceptos", by-laws changes); of the 25 after a full stop and a dash, 24 open an
act after the text of another (most often the end of a by-laws change, or the name of
the company a merger or a split is with), and 1 sits inside the text of a corporate
purpose ("...año siguiente.- Ampliacion del objeto social. D) ...").

### 3.2 Two numbers after the registry sheet

6,836 Section A announcements print "H X n,m". To know which number is the sheet, the
second pass of `formats` looks for the same company (same normalised name and province)
printing a single sheet elsewhere in the 18 years:

| Second number | First is the sheet | Second is the sheet | Neither / both | No other announcement |
|---|---:|---:|---:|---:|
| 1 digit | 1,575 | 0 | 157 | 3,639 |
| 2 digits | 140 | 0 | 0 | 96 |
| 3 digits | 107 | 0 | 11 | 130 |
| 4 digits | 9 | 8 | 2 | 41 |
| 5 digits | 31 | 36 | 15 | 63 |
| 6 digits or more | 15 | 9 | 0 | 41 |
| Repeats the first | 190 (same number) | | 2 | 519 |

Up to 3 digits the second number is never the sheet: the first is taken. From 4 digits
it is a coin toss, so the sheet is left unknown and the announcement is matched by name
(production uses 2 digits; this cut was re-derived here). With this rule, 9,703,472
Section A announcements carry a sheet (99.996%).

### 3.3 Officer holders

In officer acts: 2,896 labels glued to the previous holder were split off; 1,853 holders
were cut where lowercase text starts; 26 pieces with two lowercase words or more and 207
pieces carrying an ID number and no company mark are not holders. That leaves 17,864,221
holders, of which 1,199,521 are legal persons. Against the test of version 0.2, 36,504
holders that were taken for people are companies and 1,539 that were taken for companies
are people. In the 25 samples of each that `formats` keeps (read with the AI assistant),
the new companies are names cut inside their legal form, fund managers ("SGIIC"),
branches of foreign companies, foreign forms, foundations and institutes in Catalan or
Spanish, and a person whose name carries a measured company word (ERNST: see below); of
the new people, 21 carry "SAS", "SPA" or "AG" inside a person's name and 4 are names of
banks with "BANK" and no legal form: the word is in 82 different names, fewer than the
100 the company words ask, and a bank is not one of the types of entity of section 3.7.

Where the abbreviations of a foreign or professional legal form appear in holders that
carry no other company word (in the samples read with the AI assistant, the cases are as
described):

| Abbreviation | At the end | Elsewhere | Elsewhere, in the sample read |
|---|---:|---:|---|
| SAS | 1,682 | 775 | surnames ("SAS APELLIDO NOMBRE", "DE SAS ...") |
| SPA | 241 | 124 | surnames |
| AG | 381 | 426 | initials of a person, and branches of foreign banks ("... AG SUCURSAL") |
| SAP | 3,455 | 4 | a first name |
| SAT | 80 | 445 | agricultural transformation companies ("SAT N ... EJEMPLO") |
| CB, SC | 147, 333 | 10, 6 | community of goods and civil partnerships, with the form in front |
| LTD, PLC, INC, LLC | 815, 61, 683, 402 | 43, 32, 12, 2 | companies |
| BV, NV, SARL, LTDA, SRC | 2,421, 344, 1,839, 61, 19 | 15, 7, 27, 7, 1 | companies |
| SRL | (a Spanish legal form at the end) | 9 | companies |

So SAS, SPA, AG and SAP count only at the end of a holder's name; the others anywhere.

### 3.4 Auditor labels

505,445 auditor mentions in 18 years, by class (`officers.auditor_class`): individual
442,996 ("Auditor" 366,197, "AUDIT.CUENT" 68,550, then "AUDIT.INDIV", "AUDIT.ART265",
"AUDIT.CONJUN", "Auditor Titu", "AUDITOR TIT", "Aud.Manc", "Auditor Grup"), consolidated
34,647 ("Aud.C.Con" 27,983, "AUDT.CTS.CON" 6,451, "AUD.C.C.CONJ", "AUDIT.CC.TIT") and
substitute 27,802 ("Aud.Supl" 24,031, "AUDITOR SUP" 2,642, "Aud.Supl.C.C", "Auditor
Sup", "Aud.Su.C.Con", "AUDIT.CC.SUP", "Aud.Supl.CC", "Aud.Sup.Grup"). The rule was
written from the 15 labels of September's 12 months; every label seen in 18 years gets a
class. 32 mentions carry part of the holder's name glued to the label ("... SL.
Auditor"), and a few rare labels fall to individual by default ("Auditor Grup" 69,
"Aud.Tit.Grup" 8, "Auditor Cons" 1): whether they audit individual accounts is not
known.

### 3.5 Record

| Measure | Value |
|---|---:|
| Incorporations | 1,763,336 |
| ... with the capital written "Capital suscrito:" (not paid up in full) | 8,386 (0.48%) |
| ... with no capital at all (pension funds: "Patrimonio del fondo") | 1,066 |
| Changes of corporate purpose / extensions of it | 229,720 / 142,349 |
| "Otros conceptos" mentioning the registered address | 19,433 |
| ... of them, a street renamed or renumbered by the city council, with a house number | 660 |

38% of the acts about the corporate purpose are extensions, whose text is only the
activity added: version 0.2 stored them as the purpose. The wordings that announce the
new address of a renamed street were written from the "Otros conceptos" texts about the
street of the domicile (its name, its numbering, the council's register of street names:
`record.street_cues`); corrections of an earlier inscription, someone else's domicile,
and new names given without a house number (a street name alone would replace the
address with less than it had) are not read. `formats` counts how often each wording is
used (`street_cue_wordings`) and keeps samples of the texts about a street that use
none: of 1,669 texts about the street of the domicile, 944 use one of the wordings (104
different ones, 1,004 uses: "pasa a ser" 90, "pasando a denominarse" 75, "pasando a ser"
72, "quedando establecido en" 68, "ahora" 64...) and 725 none. In the 25 samples of the
texts with none (read with the AI assistant), most give the new address in a way the
list lacks ("actualmente", "estableciéndose en", "se sitúa en", "en adelante será") and
the rest give no address or are about something else (a branch abroad, the article of
the by-laws on the domicile): the wordings reach 944 of the 1,669 texts about the street
(57%). 660 new addresses come out, and among the 25 samples of them (read the same way)
every one is an address; some carry the town or a business park around the street.

### 3.6 Constants measured here

The run behind these figures had in its code the lists the package ships, and it
derived the same cut lengths, the same 133 company words, the same 120 street words, the
same 209 generic street words and the same 213 role labels (the files it writes are
those of `lists/`, byte for byte): the holder counts above are those of the shipped
lists.

**Where the Registro cuts a holder and a street.** The counts by length fall with every
extra character until the values cut at the maximum pile up just below it
(`formats.cut_length`: the cap is the steepest drop of the counts; the cut values start
one length above the lowest point of the run that rises towards it, a rise of more than
5% per character):

| Length | 44 | 45 | 46 | 47 | 48 | 49 | 50 | 51 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Holders | 17,555 | 13,756 | 13,256 | 11,331 | 9,732 | 15,844 | 64,222 | 3 |
| Registered streets | 27,545 | 25,476 | 25,124 | 27,320 | 39,974 | 80,444 | 149,665 | 0 |

So a holder of 49 characters or more may be cut (`officers.TRUNCATED_LENGTH`), and a
street of 47 or more (`record.REGISTRY_STREET_CUT`): the trailing punctuation stripped
after the cut leaves some cut values a character or two short of 50.

**Company words.** A word makes a holder a legal person wherever it is when, over the 18
years, it is in 1,000 holders or more and in 100 different names or more, and 80% or
more of those holders carry a legal form (at the end, or a form abbreviation anywhere:
"X BANK PLC SUCURSAL EN ESPAÑA"); the words of the legal forms themselves have their own
rules and are left out. 133 words pass (`lists/company_words.txt`), from ABOGADOS,
ASESORES, AUDITORES, CONSULTORES and INVERSIONES to the English words of company names
(GROUP, HOLDINGS, MANAGEMENT, SERVICES, THE, AND). Asking for 100 different names keeps
out the brand of a single firm (DELOITTE: 62,747 holders, 32 names); a few names of
large audit firms pass (ERNST 490 names, YOUNG 306, GRANT 328) and would also catch a
person with that surname. The table shows the words of version 0.2: the measure finds
those of company names, and not the legal persons that are not companies, which carry no
legal form (those are the types of entity of section 3.7).

| Version 0.2 word | Holders | With a legal form | Names |
|---|---:|---:|---:|
| AUDITORES | 240,779 | 97.1% | 3,445 |
| SOCIEDAD | 154,691 | 81.4% | 24,020 |
| INVERSIONES | 32,456 | 92.8% | 5,524 |
| AUDITORIA | 18,276 | 93.2% | 424 |
| HOLDING | 9,307 | 95.9% | 1,965 |
| CORPORACION | 8,456 | 88.8% | 703 |
| BANCO | 8,190 | 96.2% | 208 |
| CAJA | 7,326 | 12.5% | 530 |
| BANK | 1,919 | 86.8% | 82 |
| FUNDACION | 1,741 | 0.1% | 405 |
| ASOCIACION | 1,574 | 1.3% | 520 |
| AYUNTAMIENTO | 1,388 | 0.0% | 363 |
| AUDITING | 1,105 | 99.9% | 33 |
| UNIVERSIDAD | 228 | 0.9% | 48 |

**Street words that tell no place apart.** A word of the registered streets is a street
type or a word of the entrance when, over the 18 years, it is in 1,000 streets or more,
in 50% or more of them it opens the street or comes after the house number, and it is
not a word of a municipality of 100 addresses or more (a town written after the number
can also name a street). Only words of three letters or more are looked at: shorter ones
never tell two addresses apart in the comparison. 120 words pass
(`lists/street_filler.txt`): the Registro's street types (AVDA, CTRA, CMNO, PASEO,
PLAZA, CALLE...) and the words of the entrance and the building (PISO, PLANTA, LOCAL,
PUERTA, BAJO, NAVE, OFICINA, EDIF...). The rule also lets through a few names of
localities that are not municipalities and are written after the number (AGUADULCE,
MUTILVA), and leaves out EDIFICIO, which 107 addresses carry where the municipality
goes. Being right before a number is counted but cannot decide: NUM is there in 99% of
its uses, and so is the last word of a street name ("CALLE GOYA 5"). Two lists are
written in `record.py` instead (`WRITTEN_IN_FULL`): the street types in full, which the
Registro mostly abbreviates (AVENIDA in 6,124 streets against AVDA in 291,288) and
rarely opens a street with, so the counts cannot select them although another source
writes them; and the number words (NUM in 337,035 streets, NUMERO in 110,181), which
stand right before the number like the last word of a street name. The articles of three
or four letters (DEL, LAS, LOS...) are a matter of grammar.

**Prose and ID numbers.** Pieces by their standalone lowercase words: 0 words
17,864,420; 1 word 8; 2 words 9; 3 or more 17. The rule takes two or more as a sentence
(26 pieces). In the samples (read with the AI assistant), the pieces with one such word
are names followed by glued text or with a given name in lowercase; those with two or
more are mostly text glued after a name, but also names written all in lowercase: the
rule costs a few names in 18 years. Longest run of digits in the pieces:

| Digits in a row | 0 | 1-3 | 4 | 5 | 6 | 7 | 8 | 9 or more |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Legal persons | 1,139,987 | 32,275 | 26,904 | 149 | 8 | 19 | 10 | 169 |
| Others | 16,663,743 | 852 | 105 | 6 | 2 | 28 | 162 | 9 |

A person's name carries no run of 5 digits or more except an ID card or a reference
glued to it; a company name can (a year has 4). The 207 other pieces with 5 digits or
more are the ones left out above.

### 3.7 Other word lists: measured, or why not

**Role labels.** 17,864,454 label mentions in 18 years (one per holder); the 667 labels
(by their letters) used 10 times or more have 17,861,100 of them, and the 292 used 100
times or more cover 99.91% of all mentions. The role is read from the letters of a label
by rules written from those labels (`officers.role_of_label`: "ADM...UNI" is a sole
director, "APO" an attorney, a member, chair or secretary of a committee is not the
board, "ADMMANCOM" is a joint director and not a committee); 213 of the 292 get a role
and are listed with it in `lists/role_labels.txt`, most used first: 99.2% of the
mentions. The other 79 (managers, depositaries, management entities, partners, members
of other bodies) stay without a canonical role, as do the labels used fewer than 100
times unless a rule reads them.

**Words of the street names.** The address signal of discovery leaves out the street
types and entrance words of section 3.6, and does not take two streets for one when they
share only words found in many different street names: 209 words are in 500 different
street names or more (the words before the house number) and in 1,000 streets or more,
from INDUSTRIAL, SAN, PARCELA, JUAN and JOSE to DOCTOR, GENERAL, GRAN and REAL
(`lists/generic_street_words.txt`). A rule without the floor of 1,000 streets that
the other street words have gives 214: five street types more (four abbreviations and
DISEMINADO).

**Lowercase links of names.** None: in 18 years only "del" (16 times) and "conceptos"
(10) are found 10 times or more as standalone lowercase words of a holder, and always
next to other lowercase words, in prose. The Registro writes names in capitals, so every
lowercase word counts towards prose.

**Types of legal person without a legal form.** Not measured. The words that name the
type of entity open the holder's name and almost never close it:

| Word | Holders | First word | Last word |
|---|---:|---:|---:|
| FUNDACION | 1,741 | 97.8% | 0.2% |
| ASOCIACION | 1,574 | 91.9% | 0.0% |
| INSTITUTO | 1,613 | 93.1% | 0.0% |
| AYUNTAMIENTO | 1,388 | 72.4% | 0.0% |
| CAJA | 7,326 | 77.4% | 3.3% |

but so do surnames, because people are written "SURNAME SURNAME NAME": of the 145 words
of five letters or more in 500 holders and 100 names or more that open at least 70% of
their holders, close at most 5% and are not company words, the types of entity are a
handful and the rest are surnames and names of firms (counted on `holder_word_places` of
the `formats` report).

So the list (`officers.LEGAL_PERSON_WORDS`) is the types of legal person the law sets,
which carry no legal form: foundation, association, city council, university, savings
bank, institute, federation, confederation, consortium and agency, and the Catalan names
of the first five (FUNDACIO, ASSOCIACIO, AJUNTAMENT, UNIVERSITAT, CAIXA) with the
Galician CONCELLO.

**Particles of names.** The person key and the person signal leave out the articles of
`normalize.ARTICLES` (DEL, LAS, LOS...) and the particles of foreign surnames (VAN, VON,
DER, DEN): a matter of grammar.

## 4. Record and `sync`

`borme-radar stats`: 604 registry observations of the 32 watched parents (capital 502,
address 30, municipality 30, purpose 22, name 18, status 2). The record starts from
`examples/watchlist.csv` only (names and registered addresses; no capital, purpose or
status), and `borme-radar sync` lists 55 differences: capital 30, corporate purpose 15,
address 5, municipality 4, name 1. They are moves of registered office inscribed in
2013-2026, capital operations, the purposes the watchlist did not carry and one rename.
12 more addresses or municipalities are the same place written otherwise (`sync` prints
how many) and are not listed. `sync --apply` wrote the 55 and logged them; the next
`sync` lists none.

Two of the 15 purposes are not the whole purpose: the Registro published them under
"Cambio de objeto social", but one only adds activities ("SE AMPLIA EN: ...", 2018) and
the other changes one paragraph of the article (2015). The rule reads a purpose from the
type of act, not from its text, so both are proposed as the purpose; `sync` shows them
before it writes (README, "Limitations").

The full history found one case the record rules did not cover. A watched parent took,
in a 2016 merger, the name of the company it absorbed; the absorbed company's older
announcements under that name taught a second sheet, and its last announcement
(dissolution and extinction by merger, the same day) made the watched parent
"dissolved". The extinction was already ignored because the company kept publishing;
now a dissolution inscribed together with an extinction is ignored too.

## 5. Positions held

269,090 officer events of the 32 parents (attorneys are 260,833 of them, 97%: large
companies give and revoke powers by the thousand). `borme-radar stats`:

| Positions held | Last event per name as published and role (version 0.2) | `positions.py` |
|---|---:|---:|
| All | 39,076 | 38,691 |
| Attorneys | 37,953 | 37,661 |
| Auditors | 82 | 35 |
| Board and other roles | 1,040 | 995 |
| Sole directors | 1 | 0 |

Inside one company and one role, the person key makes 931 persons of 1,883 of the
119,763 names as published: the same words in another order. Auditors fall by more
than half because only the latest of each kind of accounts holds the seat. The one sole
director the rule of version 0.2 keeps was appointed in 2024, and board members have
been appointed since (from `role_changes`; `stats` does not print it): the form of body
in force ends that position. There is no public list of the officers of these companies
to check the 38,691 against.

## 6. Year each company joined its group

`borme-radar joined` over the database of the sweep. The companies of each group are its
watched parent and the 609 leads tied to it by ownership (a group company is their sole
shareholder, they absorb one, or one is their sole director); 641 in all. The first rule
of `joining.RULES` that applies dates each one:

| Rule | Evidence of the year | Confirmed by an inscribed act | Companies |
|---|---|---|---:|
| `head`, incorporated 2009 or later | incorporation | yes | 6 |
| `head`, incorporated before 2009 | (no year) | | 26 |
| `founded`: the group within a year of the incorporation | owner (a group company as sole shareholder) | yes | 177 |
| | officers (group officers, or a group company as director) | no | 52 |
| `acquired`: the group after signs of another owner | owner | yes | 121 |
| | officers | no | 102 |
| `already_in`: the group's from its first act | (no year) | | 156 |
| `untied`: no act shows the group | (no year) | | 1 |

So 298 years rest on an owner act, 154 on officers alone and 6 on an incorporation; 183
companies get none. No company without a year has an incorporation date (neither an
incorporation act in the gazette window nor one in the example watchlist), so the
fallback to the incorporation year never applies here. `joined --apply` wrote the year
of the 6 parents that have one (the leads have no row in the record); a second `--apply`
wrote nothing.

### What can and cannot be checked

No public source gives the date on which each company entered a listed group: the
groups' annual accounts list their subsidiaries, not when each one arrived. So there is
no accuracy here. What the gazette allows is to set one kind of evidence against the
other: the 298 entries an owner act confirms are dated again with the sole shareholders
left out, from the officers alone (`officers_against_owner` in `joining.json`).

| Confirmed entry | Officers give no year | Same year | A later year |
|---|---:|---:|---:|
| `founded` (177) | 116 | 51 | 10 |
| `acquired` (121) | 106 | 13 | 2 |

Where the officers give a year, it is the same in 64 of 76 cases (median gap 0 days, p90
600 days later). But for 222 of the 298 they give none: the people who run a subsidiary
are not the parent's board, which is all this watchlist knows of each group. The
officers are the weaker evidence, and here also the rarer one; the two are kept apart
for that reason.

### The windows

Both windows are values of the production system, kept; `joined` prints the days behind
them (`window_checks` in `joining.json`), which check them but would not choose them.

Days from the incorporation to the first sign of the group, for the 277 companies with
both in the gazette: 0-30 days 197, 31-90 13, 91-180 9, 181-365 10, 366-730 8, 731-1,825
24, more 16. A company set up by the group shows it within a month in most cases; the
window of a year (`joining.BORN_WINDOW`) also takes the slow ones, and the counts are
flat after it.

Days between the first sign of the group and the nearest outsider leaving the body,
when one leaves: 0-7 days 178, 8-30 21, 31-90 28, 91-365 96, more 167. A handover is a
matter of days; the window of 30 days (`joining.HANDOVER_WINDOW`) takes the first two
bands, whose last edge is the window itself.

## 7. The 12 months to 23 September 2026

The commands of [calibration.md](calibration.md), in an empty data directory: the 12
months processed once without address counts (the calibration needs their officers),
the train window calibrated, and the 12 months processed again with the address counts
it wrote, then `borme-radar stats`:

| Measure | Without address counts | With them |
|---|---:|---:|
| Announcements of watched companies (by sheet / by name) | 1,326 (1,325 / 1) | 1,326 (1,325 / 1) |
| Officer events / positions held | 8,090 / 3,319 | 8,090 / 3,319 |
| Registry observations | 31 | 31 |
| Kept announcements | 2,311 | 1,990 |
| Open leads | 451 | 130 (21 by address, 28 by mention, 81 by token) |
| Section A announcements with a registry sheet | 597,149 | 597,149 |
| Inscription to publication, median (p10 / p90) | 7 days (7 / 10) | 7 days (7 / 10) |

The address counts change only discovery: without them no address is crowded, and the
office buildings where many unrelated companies are registered bring in 451 leads
instead of 130. The one announcement matched by name is a Section B entry without a sheet. 17 Section A announcements print a second number of four digits or more after
the sheet and are left without one (section 3.2). Every lead found by mention has its
sheet followed in the next passes: 42 more announcements kept in pass 2.

## 8. What public data cannot measure here

| Value | Where | Why it is not measured |
|---|---|---|
| A person on the governing body of 3 group companies or more is a group officer | `joining.GROUP_OFFICER_MIN` | Kept from the production system; the example watchlist has one company per group, so no person can be in 3 of them and the value cannot be measured here. |
| The windows of a year (founded by the group) and of 30 days (a handover of the body) | `joining.BORN_WINDOW`, `joining.HANDOVER_WINDOW` | Kept from the production system; `joined` prints the days behind both (section 6), which check them but would not choose them. |
| The noise cut of the brand tokens (5) and the similarity cut of the review queue (90) | `discovery.TOKEN_NOISE_MAX`, `matching.FUZZY_CUTOFF` | Kept from the production system; `calibrate` prints the precision at every cut and the names in every band ([calibration.md](calibration.md)). |
| The thresholds of the rules that derive the lists: 1,000 holders, 100 names and 80% for a company word; 500 street names and 1,000 streets for a generic street word; 50% and 100 addresses for a street word; 100 uses for a role label; 5% per character for a cut length | `formats.py` | Chosen; the lists they give are measured. |
| The street types in full and the number words (AVENIDA, NUM...) | `record.WRITTEN_IN_FULL` | The Registro mostly abbreviates the first and writes the second before the number, like the last word of a street name, so the counts cannot select them (section 3.6). |
| The articles of three or four letters (DEL, LAS, LOS...) and the particles of foreign surnames (VAN, VON...) | `normalize.ARTICLES`, `persons.PARTICLES` | A matter of grammar, not of counts. |
| The words of a passing status (en liquidación, en concurso, unipersonal) | `normalize.STATUS_WORDS` | Set by the law and the Registro's practice. |
| The types of legal person that carry no legal form (foundation, association, city council...) | `officers.LEGAL_PERSON_WORDS` | Set by law; by their place in a name they cannot be told from surnames (section 3.7). |
| How a role label is read (which letters mean which role) | `officers.role_of_label` | Written from the labels; the labels themselves and their uses are counted (section 3.7). |
| The half-life and the tier shares of the triage score | `triage.HALF_LIFE_DAYS`, `triage.TIER_SHARES` | Choices: no truth says which lead a person should read first. The precision of each signal is measured. |
| How much of an act the detailed report quotes, and how many leads it lists per group | `report.QUOTE_MAX`, `report.LEADS_SHOWN` | Choices about reading, not about data. |
| Whether a year of joining is right | `joining.py` | No public list dates the entry of each company into a listed group (section 6). |
| Whether a position is still held | `positions.py` | No public list of current officers (section 5). |
