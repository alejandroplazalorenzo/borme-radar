"""Count the quirks of the published format, and measure the thresholds that cope with them.

Every rule that deals with how the Registro writes (a heading glued to a name, a second
number after the registry sheet, a role glued to the previous holder, a foreign legal
form, a street renamed by the city council...) is checked here against the cached
corpus: how often it fires, and what the previous rule did instead. The same pass
measures what some constants of the parser are made of, so that they come from the
public gazette and not from a guess:

* the length at which the Registro cuts holders and streets (:func:`cut_length`);
* the words that, in holders, almost only appear in names with a legal form
  (:func:`company_words`), which make a holder a legal person anywhere in its name;
* the words of registered streets that are street types or parts of the entrance
  (:func:`street_filler`), which do not tell two addresses apart, and the words that
  many different street names share (:func:`generic_street_words`);
* the role labels used most, with the role ``officers.role_of_label`` reads in each
  (:func:`role_labels`);
* how many lowercase words a holder has, and its longest run of digits, for the prose
  and ID-number rules of ``officers.py``; where words sit in the holders' names, and the
  short words of people's names, as evidence for the lists that are not measured.

Same rules as the calibration (``corpus.py``): no database writes, and what is kept is
counts (the samples quote the gazette and stay local).

Two passes. The first counts the quirks and the material of the derived values, none of
which depends on the company test; the second looks for the other announcements of the
companies that print two numbers after the "H" (which of the two is the sheet they use
elsewhere) and counts legal persons and people among the holders with the values the
first pass derived (:class:`CompanySplit`).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from borme_radar import officers
from borme_radar.acts import openings
from borme_radar.corpus import Seen
from borme_radar.normalize import (
    LEGAL_FORM_TOKENS,
    has_legal_form,
    normalize_name,
    plain,
    word_set,
)
from borme_radar.officers import (
    _LOWERCASE_WORD_RE,
    ID_DIGITS,
    OFFICER_ACTS,
    auditor_class,
    canonical_role,
    cut_holder,
    glued_labels,
    is_company_name,
    is_prose,
    label_pieces,
    longest_digit_run,
    readable,
)
from borme_radar.parser import sheet_parts
from borme_radar.record import DOMICILE_RE, renamed_street, street_and_city, street_cues

_SAMPLES = 25


_CAPITAL_PAID_RE = re.compile(r"\bCapital\s+suscrito\s*:", re.I)
_CAPITAL_PLAIN_RE = re.compile(r"\bCapital\s*:", re.I)
_DOMICILE_RE = re.compile(r"domicilio", re.I)

# The company test of version 0.2, kept to count what the new one changes.
PREVIOUS_COMPANY_WORDS = frozenset(
    {"SOCIEDAD", "LIMITED", "LTD", "GMBH", "INC", "LLC", "LLP", "BV", "NV", "AG", "SARL"}
    | {"SAS", "SPA", "SRL", "AUDITORES", "AUDITORIA", "AUDITING", "BANCO", "BANK", "CAJA"}
    | {"FUNDACION", "ASOCIACION", "AYUNTAMIENTO", "UNIVERSIDAD", "HOLDING", "INVERSIONES"}
    | {"CORPORACION"}
)
# Abbreviations that are legal forms abroad and could also be surnames, names or
# initials here: where each one is found decides where it counts (docs/history.md).
AMBIGUOUS_FORMS = frozenset(
    {"AG", "BV", "CB", "INC", "LLC", "LTD", "LTDA", "NV", "PLC", "SAP", "SARL", "SAS", "SAT"}
    | {"SC", "SPA", "SRC", "SRL"}
)
# Rules applied to the counts below; the values chosen are in docs/history.md.
COMPANY_WORD_MIN_HOLDERS = 1000
COMPANY_WORD_MIN_SHARE = 0.8
COMPANY_WORD_MIN_NAMES = 100
STREET_WORD_MIN_STREETS = 1000
STREET_WORD_MIN_SHARE = 0.5
STREET_CITY_MIN = 100
# Thresholds of the word lists derived below (their values are in docs/history.md).
ROLE_LABEL_MIN = 100
GENERIC_STREET_MIN_NAMES = 500
# Below the cap of a field, lengths piled up by the cut have 5% more values than the one
# before (the curve of the uncut values falls with length).
CUT_RISE = 0.05


def previous_is_company(holder: str) -> bool:
    if has_legal_form(holder):
        return True
    return not PREVIOUS_COMPANY_WORDS.isdisjoint(plain(holder).split())


def _keep(bucket: list, value: object) -> None:
    if len(bucket) < _SAMPLES:
        bucket.append(value)


# ------------------------------------------------------------------ derived values

# Legal forms counted anywhere in a holder for the company words: the canonical
# Spanish forms and the foreign and professional ones ("X BANK PLC SUCURSAL EN ESPAÑA"
# carries one, though not at the end).
_FORM_TOKENS = (
    word_set("SA SAL SAD SL SLL SLP SLNE COOP AIE")
    | officers.FORMS_AT_END
    | officers.FORMS_ANYWHERE
)


def carries_form(holder: str) -> bool:
    """A legal form at the end of the name, or a form token anywhere in it."""
    return has_legal_form(holder) or not _FORM_TOKENS.isdisjoint(normalize_name(holder).split())


def cut_length(by_length: Mapping[int, int], rise: float = CUT_RISE) -> int | None:
    """Where a field is cut, from its counts by length.

    The cap is the length after which almost nothing is longer (the steepest drop of the
    counts). The values cut there pile up just below it: going down from the cap, each
    length has fewer values than the next by more than ``rise``, until the curve of the
    uncut values is reached. The cut values start one length above that point."""
    if not by_length:
        return None
    cap = max(by_length, key=lambda n: by_length[n] / max(1, by_length.get(n + 1, 0)))
    low = cap
    while low - 1 in by_length and by_length[low - 1] * (1 + rise) < by_length[low]:
        low -= 1
    return low + 1 if low < cap else None


def company_words(
    with_form: Mapping[str, int],
    total: Mapping[str, int],
    names: Mapping[str, int],
    min_holders: int = COMPANY_WORD_MIN_HOLDERS,
    min_share: float = COMPANY_WORD_MIN_SHARE,
    min_names: int = COMPANY_WORD_MIN_NAMES,
) -> list[str]:
    """Words found in at least ``min_holders`` holders and in ``min_names`` different
    names, at least ``min_share`` of those holders names that carry a legal form; the
    words of the legal forms themselves are left out (they have rules of their own).
    Alphabetical."""
    return sorted(
        w
        for w, n in total.items()
        if n >= min_holders
        and names.get(w, 0) >= min_names
        and with_form.get(w, 0) >= min_share * n
        and w not in _FORM_TOKENS
        and w not in LEGAL_FORM_TOKENS
    )


def street_filler(
    words: Mapping[str, tuple[int, ...]],
    city_words: Mapping[str, int],
    min_streets: int = STREET_WORD_MIN_STREETS,
    min_share: float = STREET_WORD_MIN_SHARE,
    city_min: int = STREET_CITY_MIN,
) -> list[str]:
    """Street words (three letters or more) seen in at least ``min_streets`` streets that,
    in at least ``min_share`` of their uses, open the street (its type) or come after the
    house number (floor, door, unit), and that are not a word of a municipality (in the
    municipality of ``city_min`` addresses or more): a town written after the number can
    also name a street. Standing right before a number is counted too, but does not
    decide: the last word of a street name stands there as well. Alphabetical."""
    return sorted(
        w
        for w, (n, opening, _before_number, after_number) in words.items()
        if len(w) >= 3
        and n >= min_streets
        and max(opening, after_number) >= min_share * n
        and city_words.get(w, 0) < city_min
    )


def role_labels(keys: Mapping[str, int], min_uses: int = ROLE_LABEL_MIN) -> list[tuple[str, str]]:
    """(label key, role) of every label used ``min_uses`` times or more whose role can be
    read, the most used first."""
    used = sorted(((n, k) for k, n in keys.items() if n >= min_uses), key=lambda x: (-x[0], x[1]))
    return [(k, role) for _n, k in used if (role := officers.role_of_label(k)) is not None]


def generic_street_words(
    counts: Mapping[str, tuple[int, ...]],
    not_names: frozenset[str],
    min_names: int = GENERIC_STREET_MIN_NAMES,
    min_streets: int = STREET_WORD_MIN_STREETS,
) -> list[str]:
    """Words of three letters or more found in ``min_names`` different street names or
    more (the words before the house number) and in ``min_streets`` streets, leaving out
    the street types and entrance words in ``not_names``: sharing only these does not make
    two streets one. ``counts`` maps a word to (streets, opening, before number, after
    number, town addresses, different street names). Alphabetical."""
    return sorted(
        w
        for w, c in counts.items()
        if len(w) >= 3 and c[0] >= min_streets and c[5] >= min_names and w not in not_names
    )


# ------------------------------------------------------------------ act boundaries


@dataclass(slots=True)
class Boundaries:
    """Headings stuck to the last letter of a name, or after ". -": acts a split at
    a full stop and a space would have missed."""

    counts: Counter[str] = field(default_factory=Counter)
    samples: dict[str, list] = field(default_factory=lambda: {"glued": [], "dash": []})

    def observe(self, seen: Seen) -> None:
        if seen.doc.section != "A":
            return
        body = seen.ann.raw_text
        self.counts["announcements"] += 1
        hits: dict[str, list[tuple[int, int]]] = {"glued": [], "dash": []}
        for opening in openings(body):
            kind = {"glued": "glued", "stop_dash": "dash"}.get(opening.after)
            if kind is not None:
                hits[kind].append((opening.cut, opening.found.end()))
        self.counts["glued_to_capital"] += len(hits["glued"])
        self.counts["after_dot_space_dash"] += len(hits["dash"])
        self.counts["announcements_affected"] += any(hits.values())
        for kind, spans in hits.items():
            for start, end in spans:
                _keep(self.samples[kind], body[max(0, start - 15) : end + 15])


# ------------------------------------------------------------------ registry sheet


@dataclass(slots=True)
class SheetPairs:
    """First pass: the announcements whose registry data give the sheet as "H X n,m",
    kept with the name and province to look them up again."""

    counts: Counter[str] = field(default_factory=Counter)
    cases: list[tuple[str, str, str, str, str]] = field(default_factory=list)

    def observe(self, seen: Seen) -> None:
        if seen.doc.section != "A" or not seen.ann.registry_data:
            return
        self.counts["announcements"] += 1
        parts = sheet_parts(seen.ann.registry_data)
        if parts is None or parts[2] is None:
            return
        letters, first, second = parts
        self.counts["two_numbers"] += 1
        self.cases.append((seen.norm, seen.doc.province, letters, first, second))

    def keys(self) -> set[tuple[str, str]]:
        return {(norm, province) for norm, province, *_ in self.cases}


@dataclass(slots=True)
class SheetSightings:
    """Second pass: for the companies of the first pass, the sheet they print alone in
    any other announcement."""

    keys: set[tuple[str, str]]
    seen_sheets: dict[tuple[str, str], Counter[str]] = field(default_factory=dict)

    def observe(self, seen: Seen) -> None:
        key = (seen.norm, seen.doc.province)
        if key not in self.keys or not seen.ann.registry_data:
            return
        parts = sheet_parts(seen.ann.registry_data)
        if parts is not None and parts[2] is None:
            letters, first, _ = parts
            sheets = self.seen_sheets.setdefault(key, Counter())
            sheets[f"{letters} {int(first)}"] += 1


def sheet_verdicts(cases: list[tuple], sightings: SheetSightings) -> dict[str, Counter[str]]:
    """For each length of the second number, how often the first, the second, both or
    neither turn out to be the sheet the company prints alone elsewhere."""
    out: dict[str, Counter[str]] = {}
    for norm, province, letters, first, second in cases:
        if int(second) == int(first):
            kind = "repeats the first"
        else:
            kind = f"{min(len(second), 6)} digits" + ("+" if len(second) >= 6 else "")
        elsewhere = sightings.seen_sheets.get((norm, province), Counter())
        a, b = f"{letters} {int(first)}", f"{letters} {int(second)}"
        if not elsewhere:
            verdict = "no other announcement"
        elif a in elsewhere and b not in elsewhere:
            verdict = "first"
        elif b in elsewhere and a not in elsewhere:
            verdict = "second"
        elif a in elsewhere:
            verdict = "both"
        else:
            verdict = "neither"
        out.setdefault(kind, Counter())[verdict] += 1
    return out


# ------------------------------------------------------------------ officer holders


def _holders_of(seen: Seen) -> Iterator[tuple[str, str, bool]]:
    """(label, holder, cut after it) of every readable piece of the officer acts."""
    for act in seen.ann.acts:
        if act.act_type not in OFFICER_ACTS:
            continue
        for label, piece in label_pieces(act.detail):
            holder, cut = cut_holder(piece)
            if readable(holder):
                yield label, holder, cut


@dataclass(slots=True)
class Holders:
    """Pass 1: officer holders as the parser reads them, and what the holder rules of
    ``officers.py`` are measured on. Nothing here depends on the company test, so the
    values derived from it (company words, cut length) do not depend on themselves."""

    counts: Counter[str] = field(default_factory=Counter)
    ambiguous: Counter[tuple[str, str]] = field(default_factory=Counter)
    auditor_labels: Counter[str] = field(default_factory=Counter)
    lengths: Counter[int] = field(default_factory=Counter)  # names (pieces that are not prose)
    lowercase_words: Counter[int] = field(default_factory=Counter)
    words_total: Counter[str] = field(default_factory=Counter)
    words_with_form: Counter[str] = field(default_factory=Counter)
    words_names: Counter[str] = field(default_factory=Counter)  # distinct names per word
    # Role labels by their letters ("Adm. Unico" -> ADMUNICO), one count per holder.
    label_keys: Counter[str] = field(default_factory=Counter)
    # Standalone lowercase words, with how many such words the piece has (1, 2, 3+).
    lower_words: Counter[tuple[str, int]] = field(default_factory=Counter)
    # Words that open a holder's name, and words that close it.
    words_first: Counter[str] = field(default_factory=Counter)
    words_last: Counter[str] = field(default_factory=Counter)
    samples: dict[str, list] = field(default_factory=dict)
    _names_seen: set[int] = field(default_factory=set)

    def observe(self, seen: Seen) -> None:
        for act in seen.ann.acts:
            if act.act_type in OFFICER_ACTS:
                self.counts["glued_roles_split"] += glued_labels(act.detail)
        for label, holder, cut in _holders_of(seen):
            self.counts["pieces"] += 1
            self.counts["cut_at_lowercase_text"] += cut
            self.label_keys["".join(ch for ch in plain(label) if "A" <= ch <= "Z")] += 1
            every_lower = _LOWERCASE_WORD_RE.findall(holder)
            for word in every_lower:
                self.lower_words[word, min(len(every_lower), 3)] += 1
            lower = _LOWERCASE_WORD_RE.findall(holder)
            self.lowercase_words[min(len(lower), 3)] += 1
            if len(lower) == 1:
                _keep(self.samples.setdefault("one_lowercase_word", []), holder[:80])
            if is_prose(holder):
                self.counts["rejected_prose"] += 1
                _keep(self.samples.setdefault("prose", []), holder[:80])
            else:
                self._name(label, holder)

    def _name(self, label: str, holder: str) -> None:
        words = plain(holder).split()
        self.lengths[min(len(holder), 60)] += 1
        with_form = has_legal_form(holder)
        marked = carries_form(holder)
        first_time = hash(" ".join(words)) not in self._names_seen
        if first_time:
            self._names_seen.add(hash(" ".join(words)))
        if words:
            self.words_first[words[0]] += 1
            self.words_last[words[-1]] += 1
        for word in {w for w in words if w.isalpha() and len(w) >= 3}:
            self.words_total[word] += 1
            self.words_with_form[word] += marked
            self.words_names[word] += first_time
        loose = [w for w in words if w in AMBIGUOUS_FORMS]
        others = " ".join(w for w in words if w not in AMBIGUOUS_FORMS)
        if loose and not (with_form or previous_is_company(others)):
            last = len(words) - 1
            for i, w in ((i, w) for i, w in enumerate(words) if w in AMBIGUOUS_FORMS):
                place = "end" if i == last else "elsewhere"
                self.ambiguous[(w, place)] += 1
                _keep(self.samples.setdefault(f"{w} {place}", []), holder[:80])
        if canonical_role(label) == "auditor":
            self.auditor_labels[label] += 1

    def derived_company_words(self) -> list[str]:
        return company_words(self.words_with_form, self.words_total, self.words_names)


@dataclass(slots=True)
class CompanySplit:
    """Pass 2: legal persons and people among the holders, with the company words and
    the cut length given (those derived in pass 1), and what changes against the test
    of version 0.2."""

    words: frozenset[str]
    cut_length: int
    counts: Counter[str] = field(default_factory=Counter)
    digit_runs: Counter[tuple[str, int]] = field(default_factory=Counter)
    # Short words (four letters or fewer) of the holders taken for people.
    person_short_words: Counter[str] = field(default_factory=Counter)
    samples: dict[str, list] = field(default_factory=dict)

    def observe(self, seen: Seen) -> None:
        for _label, holder, _cut in _holders_of(seen):
            if is_prose(holder):
                continue
            company = is_company_name(holder, self.words, self.cut_length)
            digits = longest_digit_run(holder)
            self.digit_runs[("company" if company else "other", min(digits, 9))] += 1
            if digits >= ID_DIGITS and not company:
                self.counts["rejected_id_number"] += 1
                continue
            self.counts["holders"] += 1
            self.counts["company" if company else "person"] += 1
            if not company:
                self.person_short_words.update({w for w in plain(holder).split() if len(w) <= 4})
            if company != previous_is_company(holder):
                change = "person_to_company" if company else "company_to_person"
                self.counts[change] += 1
                _keep(self.samples.setdefault(change, []), holder[:80])


# ------------------------------------------------------------------ record


@dataclass(slots=True)
class RegistryStreets:
    """The street part of the registered addresses (incorporations and changes of
    address): length, and where each word sits in it."""

    lengths: Counter[int] = field(default_factory=Counter)
    # word -> [streets, opening it, right before a number, after the house number]
    words: dict[str, list[int]] = field(default_factory=dict)
    city_words: Counter[str] = field(default_factory=Counter)  # addresses per town word
    # Different street names (the words before the house number) each word is part of.
    names_per_word: Counter[str] = field(default_factory=Counter)
    _street_names: set[int] = field(default_factory=set)

    def observe(self, seen: Seen) -> None:
        for act in seen.ann.acts:
            if act.act_type == "address_change":
                self._street(act.detail)
            elif act.act_type == "incorporation":
                domicile = DOMICILE_RE.search(act.detail or "")
                if domicile:
                    self._street(domicile.group(1))

    def _street(self, text: str) -> None:
        street, city = street_and_city(text)
        self.city_words.update(w for w in set(plain(city or "").split()) if len(w) >= 3)
        if not street:
            return
        self.lengths[min(len(street), 80)] += 1
        tokens = plain(street).split()
        numeric = [t[0].isdigit() for t in tokens]
        house = numeric.index(True) if True in numeric else len(tokens)
        name = tokens[:house]
        if hash(" ".join(name)) not in self._street_names:
            self._street_names.add(hash(" ".join(name)))
            self.names_per_word.update({t for t in name if t.isalpha() and len(t) >= 3})
        for i, token in enumerate(tokens):
            if token.isalpha():
                counts = self.words.setdefault(token, [0, 0, 0, 0])
                counts[0] += 1
                counts[1] += i == 0
                counts[2] += i + 1 < len(tokens) and numeric[i + 1]
                counts[3] += i > house

    def derived_filler(self) -> list[str]:
        return street_filler({w: tuple(c) for w, c in self.words.items()}, self.city_words)


@dataclass(slots=True)
class RecordQuirks:
    """What the record reads from the acts: how incorporations state the capital, purpose
    changes against extensions, and addresses renamed by a city council."""

    counts: Counter[str] = field(default_factory=Counter)
    samples: dict[str, list] = field(default_factory=dict)
    cue_wordings: Counter[str] = field(default_factory=Counter)

    def observe(self, seen: Seen) -> None:
        for act in seen.ann.acts:
            detail = act.detail or ""
            kind = act.act_type
            if kind == "incorporation":
                self.counts["incorporations"] += 1
                if _CAPITAL_PAID_RE.search(detail):
                    self.counts["capital_subscribed_wording"] += 1
                elif _CAPITAL_PLAIN_RE.search(detail):
                    self.counts["capital_plain_wording"] += 1
                else:
                    self.counts["capital_absent"] += 1
            elif kind in ("business_purpose_change", "business_purpose_extension"):
                self.counts[kind.replace("business_", "")] += 1
            elif kind == "other" and _DOMICILE_RE.search(detail):
                self.counts["other_mentioning_domicile"] += 1
                self._street_texts(detail)

    def _street_texts(self, detail: str) -> None:
        """Texts about the street of the domicile: the cue wordings they use, the texts
        with no cue (what the cues miss, sampled), and the new addresses read."""
        found = street_cues(detail)
        if found is None:
            return
        text, cues = found
        self.counts["about_the_street"] += 1
        if not cues:
            self.counts["about_the_street_no_cue"] += 1
            _keep(self.samples.setdefault("street_text_no_cue", []), text[:160])
        for cue in cues:
            self.cue_wordings[" ".join(cue.group(0).lower().split())] += 1
        street = renamed_street(detail)
        if street:
            self.counts["street_renamed"] += 1
            _keep(self.samples.setdefault("street_renamed", []), street[:80])


def auditor_classes(labels: Counter[str]) -> dict[str, dict[str, int]]:
    """Auditor labels grouped by the class ``officers.auditor_class`` gives them."""
    grouped: dict[str, dict[str, int]] = {}
    for label, n in sorted(labels.items(), key=lambda kv: (-kv[1], kv[0])):
        grouped.setdefault(auditor_class(label), {})[label] = n
    return grouped
