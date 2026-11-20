"""The record of a watched company, kept as dated values, and its sync with the Registro.

Six fields make the record (``FIELDS``): address, capital, city (the municipality), name,
purpose and status. Nothing writes them act by act. Each value a source states is stored
with its day in ``seen_values``: the gazette's from the acts of the company's
announcements (the inscription day, or the publication day when none is printed), the
watchlist's from the import. The view ``record_now`` picks one value per field:

* any value of the gazette before any value of the watchlist: the watchlist is where the
  record starts, and the first act that states a field takes it over;
* among the gazette's, the one of the latest day;
* on the same day, for the status, the one furthest along ``LIFECYCLE``;
* and last, the row stored last.

A status the company outlived is left out (view ``outlived``): an extinction followed by
more announcements of the same company, which went on, and a dissolution inscribed on
the day of such an extinction.

Which acts state which fields is in ``_READERS``. The purpose is taken only where an act
states it whole (an incorporation, a change of purpose); the capital only as the
subscribed amount after the operation ("Resultante Suscrito"), never the amount of the
operation; and a free text of "Otros conceptos" gives a new address when the city council
renamed or renumbered the street (:func:`renamed_street`).

Two addresses, or two municipalities, can be one place written two ways: they are
compared as places (:func:`same_address`, :func:`same_city`), not as text.

:func:`proposals` lists what ``sync`` would write: the fields where the record holds
something other than ``record_now``, and the earlier writes of ``sync`` whose observation
is no longer there (they go back to the value they replaced). :func:`sync` writes them
only with ``apply`` and logs each write in ``sync_log``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from operator import attrgetter
from typing import Any, NamedTuple

from borme_radar.normalize import (
    ARTICLES,
    normalize_name,
    plain,
    strip_accents,
    strip_annotations,
    word_list,
    word_set,
)

FIELDS = ("address", "capital", "city", "name", "purpose", "status")
STATUS_BY_ACT = {
    "dissolution": "dissolved",
    "extinction": "extinct",
    "insolvency": "insolvency",
    "reactivation": "active",
}
# The stages of a company, earliest first. Two statuses inscribed on one day resolve to
# the later stage; the table `status_stages` of the schema holds the same order.
LIFECYCLE = ("active", "insolvency", "dissolved", "extinct")

_PURPOSE_RE = re.compile(r"Objeto\s+social\s*:\s*(.+?)(?=\.\s*Domicilio\s*:|$)", re.S | re.I)
DOMICILE_RE = re.compile(
    r"Domicilio\s*:\s*(.+?)(?=\.\s*(?:Capital|Patrimonio)[^:]*:|$)", re.S | re.I
)
# "STREET (MUNICIPALITY)". The municipality may carry a bracket of its own, the postponed
# article ("... 24 (EJIDO (EL))" in BORME-A-2026-91-04), and the last closing bracket is
# sometimes missing.
_STREET_CITY_RE = re.compile(r"^(?P<street>.*?)\s*\((?P<city>[^()]*(?:\([^()]*\)?)?)\)?\s*$")
# The articles the Registro writes after a municipality, in brackets (Spanish, Catalan and
# Galician); they go back in front.
POSTPONED_ARTICLES = frozenset({"A", "AS", "EL", "L'", "LA", "LAS", "LOS", "O", "OS"})
# An amount in euros as the gazette prints it: dots between thousands, a comma before the
# cents, then the currency. Older acts give some amounts in pesetas, which are not read.
_EUROS_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s*(?=eur)", re.I)


@dataclass(frozen=True, slots=True)
class Observation:
    field: str
    value: str


def amount_after(detail: str, label: str) -> int | None:
    """The first amount in euros printed after ``label`` and a colon, in cents, or None.

    The label is matched in any case and spacing, and up to two words may stand between
    it and the colon ("Capital suscrito:" is found for "Capital").
    """
    words = r"\s+".join(map(re.escape, label.split()))
    for found in re.finditer(rf"\b{words}(?:\s+[^\W\d_]+){{0,2}}\s*:\s*", detail or "", re.I):
        amount = _EUROS_RE.match(detail, found.end())
        if amount is not None:
            whole, cents = amount.groups()
            return int(whole.replace(".", "")) * 100 + int((cents or "0").ljust(2, "0"))
    return None


def as_text(cents: int) -> str:
    """Cents as the record stores an amount: "200493.60"."""
    return f"{cents // 100}.{cents % 100:02d}"


def _front_article(city: str) -> str:
    """A municipality with its postponed article put back in front."""
    name, bracket, tail = city.rpartition("(")
    article = tail.strip().rstrip(")").strip().upper()
    if bracket and name.strip() and article in POSTPONED_ARTICLES:
        return f"{article} {name.strip()}"
    return city


def street_and_city(text: str) -> tuple[str | None, str | None]:
    """A published domicile split into street and municipality.

    Registry notes are dropped and a postponed article goes back in front ("EJIDO (EL)"
    -> "EL EJIDO"). Without a municipality in brackets at the end, the whole text is the
    street.
    """
    text = " ".join(strip_annotations(text or "").split()).strip(" .,;")
    match = _STREET_CITY_RE.match(text)
    if not match or not match.group("city").strip():
        return (text or None), None
    street = match.group("street").strip(" .,-")
    city = _front_article(match.group("city").strip())
    return (street or None), city.strip() or None


# ------------------------------------------------------------------ renamed streets

# Street words, singular or plural; "domicilio" too ("la numeración del domicilio").
_STREET = (
    r"(?<![a-záéíóúñ])(?:calle|c/|v[ií]a|avenida|avinguda|avda|plaza|pla[cç]a|paseo|passeig"
    r"|carrer|rambla|ronda|carretera|ctra|camino|cam[ií]|traves[ií]a|glorieta|pol[ií]gono"
    r"|urbanizaci[oó]n|r[uú]a|inmuebles?|edificios?|parcelas?|portal(?:es)?|domicilio)s?"
    r"(?![a-záéíóúñ])"
)
# The text is about the street itself: its name, numbering or signs, a street "renumbered"
# or "renamed", or the city's register of street names ("nomenclátor", "nomenclatura").
_ABOUT_THE_STREET_RE = re.compile(
    r"(?:nombre|denominaci[oó]n|numeraci[oó]n|rotulaci[oó]n|n[uú]mero\s+de\s+polic[ií]a)"
    rf"[^.;]{{0,60}}?{_STREET}"
    rf"|(?:re)?(?:numerad|denominad)[oa]\s+(?:la|el|los|las)\s+{_STREET}"
    r"|nomenclat",
    re.I,
)
# Not read: a correction of an earlier inscription, by-law articles or shares renumbered,
# and the domicile of an officer or a shareholder.
_NOT_THE_STREET_RE = re.compile(
    r"subsan|rectific|err[oó]ne|\berror\b"
    r"|numera\w*\s+(?:de\s+)?(?:los\s+|las\s+)?(?:art[ií]culos|acciones|participaciones)"
    r"|domicilio\s+(?:del?|de\s+los)\s+(?:administrador|socio|apoderado|consejero|liquidador)",
    re.I,
)
# The words that introduce the new address ("pasa a ser", "quedando establecido en", "siendo
# la actual", "ahora"...), written from the texts of "Otros conceptos" about a street;
# `formats` counts how often each wording is used and how many texts have none.
_NEW_ADDRESS_RE = re.compile(
    r"(?:pas(?:a|ó|ará|ando|ar)|ha\s+pasado)\s+(?:esta\s+|este\s+|éste\s+|el\s+domicilio\s+)?"
    r"(?:a|ha)\s+(?:ser|deno\w*|llamarse|constar\s+como"
    r"|(?:estar|establecerse|radicar|situarse|ubicarse)(?:\s+(?:ubicad|situad)[oa])?\s+en)"
    r"|(?:quedando|queda|qued[oó]|quedar)(?:le)?\s+(?:el\s+mismo\s+|este\s+|esta\s+)?"
    r"(?:establecido|situado|fijado|radicado|ubicado)\s+(?:en|como)"
    r"|atribuida\s+la\s+siguiente\s+direcci[oó]n"
    r"|siendo\s+(?:el|la|este|esta|su)?\s*(?:actual(?:mente)?|hoy|en\s+adelante|nuevo"
    r"|en\s+la\s+actualidad|nueva\s+denominaci[oó]n(?:\s+la\s+de)?|el\s+domicilio(?:\s+social)?)"
    r"|(?:denomin[aá]ndose|es|son)\s+en\s+la\s+actualidad|en\s+la\s+actualidad\s+(?:es|son)"
    r"|figura\s+en\s+la\s+actualidad(?:\s+con)?|actual(?:es|mente)?\s+(?:es|son)"
    r"|estando\s+(?:el\s+actual\s+)?en|el\s+sito\s+en|se\s+corresponde\s+(?:a|con)"
    r"|hall[aá]ndose\s+(?:el\s+mismo\s+)?(?:ubicado\s+|situado\s+)?en"
    r"|\ben\s+adelante|\ben\s+lo\s+sucesivo|a\s+partir\s+de\s+(?:este\s+momento|ahora)\s+ser[aá]"
    r"|\bahora|domicilio(?:\s+social)?(?:\s+de\s+la\s+(?:sociedad|entidad|compa[ñn][ií]a))?\s+"
    r"(?:es|est[aá]\s+en|se\s+encuentra(?:\s+ubicado|\s+situado)?\s+en)"
    r"|nuevo\s+nombre\s+es(?:\s+el\s+de)?|nueva\s+denominaci[oó]n[^.;]{0,60}?\bes\b"
    r"|nueva\s+direcci[oó]n|\bpor\s+el\s+de|identificad[oa]\s+como"
    r"|(?:las?|los?|el)\s+siguientes?\s*:|nuevo\s+domicilio\s+social\s+en",
    re.I,
)
# Where the address starts after the cue: an opening quote or a street word ("Gran Vía" as a
# whole, not from "Vía"); words before it, such as an industrial estate, are skipped.
_STREET_START_RE = re.compile(rf"[\"«“]|(?<![a-záéíóúñ])gran\s+v[ií]a|{_STREET}", re.I)
# Where it ends: a full stop that closes a sentence (not the dot of "Avda." or "NUM."), a
# ";" or ":", or what follows the address: the old one, the reason, the council, the date.
_ADDRESS_END_RE = re.compile(
    r"(?:(?<=[A-Za-zÁÉÍÓÚÑáéíóúñ]{5})|(?<=\d))\.\s+(?=[A-ZÁÉÍÓÚÑ]?[a-záéíóúñ])"
    r"|\.-|[;:]|\s-?antes\b|,?\s+en\s+lugar\s+de\b"
    r"|,?\s+(?:por|en\s+virtud|debido|como\s+consecuencia|seg[uú]n|de\s+fecha"
    r"|y\s+(?:en\s+su\s+|como\s+)?consecuencia|en\s+consecuencia|y\s+se)\b"
    r"|,?\s+del\s+(?:excmo\.\s+)?(?:ayuntamiento|concello|ajuntament)\b"
    r"|\s+cambio\s+de",
    re.I,
)
_QUOTES = "\"«»“”'"
# An address that stands alone has a name, and its house number or says it has none.
_HAS_NUMBER_RE = re.compile(r"\d|\bS\s*/\s*N\b|\bSIN\s+N[UÚ]MERO\b", re.I)


def _street_after(text: str) -> str:
    """The address at the start of the text after a cue: from its street word or opening
    quote (unless a number comes first), to the closing quote or where the address ends."""
    text = text.lstrip(" :,")
    start = _STREET_START_RE.search(text, 0, 60)
    if start is not None and not any(ch.isdigit() for ch in text[: start.start()]):
        text = text[start.start() :]
    if text[:1] in _QUOTES:
        body = text[1:]
        closing = next((i for i, ch in enumerate(body) if ch in _QUOTES), None)
        if closing is not None:
            return body[:closing]
        text = body
    end = _ADDRESS_END_RE.search(text, 1)
    return text[: end.start()] if end else text


def street_cues(detail: str | None) -> tuple[str, list[re.Match[str]]] | None:
    """A free text single-spaced, with the cues that announce a new address in it, when it
    is about the street of the domicile itself (renamed, renumbered, its signs or the
    council's register of street names) and not a correction or someone else's domicile;
    else None. ``formats`` counts the wordings of the cues the corpus uses."""
    text = " ".join((detail or "").split())
    if not _ABOUT_THE_STREET_RE.search(text) or _NOT_THE_STREET_RE.search(text):
        return None
    return text, list(_NEW_ADDRESS_RE.finditer(text))


def renamed_street(detail: str) -> str | None:
    """The new address given by a free text about a street renamed or renumbered by the
    city council, or None.

    It is read after the last cue, since the old address, when given, comes first. It must
    have two words or more, a name word and a house number (or "s/n"): a name or a number
    alone would overwrite the address with less than it had.
    """
    found = street_cues(detail)
    if found is None or not found[1]:
        return None
    text, cues = found
    street = _street_after(text[cues[-1].end() :]).strip(" .,;:-" + _QUOTES)
    words = plain(street).split()
    named = any(
        w.isalpha() and len(w) >= 3 and w not in STREET_FILLER and w not in ARTICLES for w in words
    )
    return street if len(words) >= 2 and named and _HAS_NUMBER_RE.search(street) else None


# ------------------------------------------------------------------ what an act says

Reader = Callable[[str], Iterable[tuple[str, str | None]]]


def _capital(cents: int | None) -> Iterable[tuple[str, str | None]]:
    return () if cents is None else (("capital", as_text(cents)),)


def _incorporation(detail: str) -> Iterable[tuple[str, str | None]]:
    purpose = _PURPOSE_RE.search(detail)
    if purpose:
        yield "purpose", purpose.group(1)
    domicile = DOMICILE_RE.search(detail)
    if domicile:
        street, city = street_and_city(domicile.group(1))
        yield "address", street
        yield "city", city
    yield from _capital(amount_after(detail, "Capital"))


def _new_address(detail: str) -> Iterable[tuple[str, str | None]]:
    street, city = street_and_city(detail)
    return (("address", street), ("city", city))


def _after_operation(detail: str) -> Iterable[tuple[str, str | None]]:
    return _capital(amount_after(detail, "Resultante Suscrito"))


# Act type -> the fields read from its text; the status comes from STATUS_BY_ACT. Only the
# acts that state a purpose whole are read for it (`business_purpose_extension` is not).
_READERS: dict[str, Reader] = {
    "address_change": _new_address,
    "business_purpose_change": lambda detail: (("purpose", detail),),
    "capital_increase": _after_operation,
    "capital_reduction": _after_operation,
    "incorporation": _incorporation,
    "name_change": lambda detail: (("name", strip_annotations(detail)),),
    "other": lambda detail: (("address", renamed_street(detail)),),
}


def observations_of_act(act_type: str, detail: str) -> list[Observation]:
    """The record values one act gives: the fields read from its text and the status its
    type implies, single-spaced and trimmed of dots; empty values are left out."""
    reader = _READERS.get(act_type)
    pairs = list(reader(detail or "")) if reader else []
    if act_type in STATUS_BY_ACT:
        pairs.append(("status", STATUS_BY_ACT[act_type]))
    tidy = ((field, " ".join((value or "").split()).strip(" .")) for field, value in pairs)
    return [Observation(field, value) for field, value in tidy if value]


# ------------------------------------------------------------------ same place

# Street types and entrance words (floor, door, unit...), which do not tell two addresses
# apart: measured on 18 years of registered streets by `borme-radar formats` (docs/history.md).
MEASURED_STREET_FILLER = word_list("street_filler.txt")
# Street types in full and the words for "number": the Registro mostly abbreviates them, so
# the measured list can miss them, but other sources write them out. A design choice.
WRITTEN_IN_FULL = word_set(
    "AVENIDA CALLE CAMINO CARRETERA GLORIETA NRO NUM NUMERO PASAJE PASEO PLAZA POLIGONO"
    " RAMBLA TRAVESIA URBANIZACION"
)
STREET_FILLER = MEASURED_STREET_FILLER | WRITTEN_IN_FULL
# The Registro cuts long streets: from this length the last word may be cut short. Measured
# on 18 years of registered streets by `borme-radar formats` (docs/history.md).
REGISTRY_STREET_CUT = 47
# A kilometre point, written either way: "KM 28,400" is "KM 28.4".
_ROAD_MARKS = frozenset({"KM", "KILOMETRO", "KMS"})
_ROAD_ID_RE = re.compile(r"\b([A-Z]{1,3})\s*-\s*(\d{1,4})\b")
_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[A-Z][A-Z0-9]*")


class Place(NamedTuple):
    """What is compared of an address: its number (the kilometre point on a road, else the
    first number), the other numbers, the distinctive words, the last word when the
    Registro may have cut it, and whether the number is a kilometre point."""

    portal: str | None
    numbers: frozenset[str]
    words: frozenset[str]
    cut_word: str | None
    on_road: bool = False


def _numbers(token: str, kilometre: bool) -> list[str]:
    whole, _, tail = token.replace(",", ".").partition(".")
    if kilometre and tail:
        return [f"{int(whole)}.{tail.rstrip('0') or '0'}"]
    return [str(int(part)) for part in (whole, tail) if part]


def place(text: str | None, *, may_be_cut: bool = False) -> Place:
    """The parts of an address that are compared.

    Road ids are joined ("N-340" -> "N340"); a kilometre point keeps its decimals ("KM
    28,400" -> "28.4") and is the number of the address; other numbers lose their leading
    zeros; street types, entrance words, articles and words under three letters are left
    out. With ``may_be_cut``, the last word of a street long enough to have been cut is
    kept apart.
    """
    flat = _ROAD_ID_RE.sub(r"\1\2", strip_accents((text or "").upper()))
    tokens = _TOKEN_RE.findall(flat)
    cut_word = None
    if may_be_cut and len((text or "").strip()) >= REGISTRY_STREET_CUT and tokens:
        last = tokens[-1]
        if last.isalpha():
            tokens.pop()
            # A cut street type or entrance word ("PLAN" of "PLANTA") tells nothing.
            if not any(word.startswith(last) for word in STREET_FILLER):
                cut_word = last
    points: list[str] = []
    numbers: list[str] = []
    words: set[str] = set()
    kilometre = False
    for token in tokens:
        if token[0].isdigit():
            (points if kilometre else numbers).extend(_numbers(token, kilometre))
            kilometre = False
        elif token in _ROAD_MARKS:
            kilometre = True
        elif len(token) >= 3 and token not in STREET_FILLER and token not in ARTICLES:
            words.add(token)
    ordered = points + numbers
    portal = ordered[0] if ordered else None
    return Place(portal, frozenset(ordered[1:]), frozenset(words), cut_word, bool(points))


def _inside(small: frozenset[str], large: frozenset[str]) -> bool:
    return not small - large


def _words_fit(current: Place, registry: Place) -> bool:
    """The words of one address are all in the other. A word the Registro may have cut
    stands for any word it starts, and when the Registro's words are the ones inside, it
    must start one of the current address."""
    cut = registry.cut_word
    completes = (lambda w: w.startswith(cut)) if cut else (lambda w: False)
    current_inside = all(w in registry.words or completes(w) for w in current.words)
    registry_inside = _inside(registry.words, current.words) and (
        cut is None or any(map(completes, current.words))
    )
    return current_inside or registry_inside


def same_place(current: Place, registry: Place) -> bool:
    """Is ``registry`` (as the Registro writes it, maybe cut) the place ``current`` is?
    The number must be the same; then the other numbers of one must all be in the other,
    and so must the words, each in either direction."""
    if not (current.portal or current.words) or not (registry.portal or registry.words):
        return False
    if current.portal != registry.portal:
        return False
    numbers_fit = _inside(current.numbers, registry.numbers) or _inside(
        registry.numbers, current.numbers
    )
    return numbers_fit and _words_fit(current, registry)


def same_address(current: str | None, registry: str) -> bool:
    """Are the record's address and the Registro's the same place (:func:`same_place`)?"""
    return bool(current) and same_place(place(current), place(registry, may_be_cut=True))


def city_forms(text: str) -> frozenset[str]:
    """Each official name of a municipality ("DONOSTIA/SAN SEBASTIAN") as its words of three
    letters or more, sorted, so that the order of the words does not count."""
    forms = (plain(part).split() for part in text.split("/"))
    return frozenset(" ".join(sorted(w for w in words if len(w) >= 3)) for words in forms) - {""}


def same_city(current: str | None, registry: str) -> bool:
    """Do two municipalities share one of their official names?"""
    return bool(city_forms(current or "") & city_forms(registry or ""))


def _cents(value: str | None) -> int | None:
    """A stored amount ("61000.0", "61000.00") in cents, or None."""
    try:
        return round(float(value or "") * 100)
    except (ValueError, OverflowError):
        return None


def _same_text(held: str | None, value: str | None) -> bool:
    return plain(held or "") == plain(value or "")


# How each field tells that the record already holds what the Registro says: an amount to
# the cent, a name by its key, a place as a place, anything else as plain text.
_HOLDS: dict[str, Callable[[str | None, str | None], bool]] = {
    "address": lambda a, b: _same_text(a, b) or same_address(a, b or ""),
    "capital": lambda a, b: _cents(a) == _cents(b),
    "city": lambda a, b: _same_text(a, b) or same_city(a, b or ""),
    "name": lambda a, b: normalize_name(a or "") == normalize_name(b or ""),
}


def differs(field: str, held: str | None, value: str | None) -> bool:
    """Does the record's value say something other than ``value`` for this field?"""
    return not _HOLDS.get(field, _same_text)(held, value)


# ------------------------------------------------------------------------------ sync


@dataclass(frozen=True, slots=True)
class Proposal:
    """One field ``sync`` would write.

    ``why`` is ``registry`` when ``record_now`` gives another value of the Registro, or
    ``unbacked`` when a value ``sync`` wrote earlier has no observation of the Registro
    left behind it and goes back to the value it replaced (``proposed`` is "" to empty
    the field).
    """

    why: str
    company_id: int
    company: str
    field: str
    held: str | None
    proposed: str
    day: str | None
    doc_id: str | None
    num: int | None


# The record's current value of a row's field, as text.
_HELD = (
    "CASE {row}.field " + " ".join(f"WHEN '{f}' THEN CAST(w.{f} AS TEXT)" for f in FIELDS) + " END"
)
# Both kinds of proposal in one query; comparing the values is left to `differs`.
_PROPOSALS = f"""
    WITH last_write AS (
        SELECT MAX(entry_id) AS entry_id FROM sync_log WHERE state = 'applied'
        GROUP BY company_id, field
    )
    SELECT 'registry' AS why, r.company_id, w.name, r.field, {_HELD.format(row="r")} AS held,
           r.value AS proposed, NULL AS written, r.day, r.doc_id, r.num
    FROM record_now r JOIN watched w ON w.company_id = r.company_id
    WHERE r.source = 'registry'
    UNION ALL
    SELECT 'unbacked', l.company_id, w.name, l.field, {_HELD.format(row="l")},
           IFNULL(l.replaced, ''), l.written, l.fact_day, l.doc_id, l.num
    FROM last_write JOIN sync_log l USING (entry_id)
    JOIN watched w ON w.company_id = l.company_id
    WHERE NOT EXISTS (SELECT 1 FROM record_now r WHERE r.company_id = l.company_id
                      AND r.field = l.field AND r.source = 'registry')
"""
_PROPOSAL_COLUMNS = (
    "why", "company_id", "name", "field", "held", "proposed", "day", "doc_id", "num"
)  # fmt: skip


def _rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = conn.execute(_PROPOSALS)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def proposals(conn: sqlite3.Connection) -> list[Proposal]:
    """What ``sync`` would write, by company, field and reason (see :class:`Proposal`).

    A value of the Registro is proposed when the record says something else; an unbacked
    write only while the record still holds what was written (a value changed since by
    other means is left alone).
    """
    found = []
    for row in _rows(conn):
        if row["why"] == "registry":
            wanted = differs(row["field"], row["held"], row["proposed"])
        else:
            wanted = not differs(row["field"], row["held"], row["written"])
        if wanted:
            found.append(Proposal(*(row[k] for k in _PROPOSAL_COLUMNS)))
    return sorted(found, key=attrgetter("company", "field", "why"))


def spelled_otherwise(conn: sqlite3.Connection) -> int:
    """How many addresses and municipalities of the record are written otherwise than the
    Registro's but are the same place: not proposals, only counted."""
    return sum(
        row["why"] == "registry"
        and row["field"] in ("address", "city")
        and bool(row["held"])
        and not _same_text(row["held"], row["proposed"])
        and not differs(row["field"], row["held"], row["proposed"])
        for row in _rows(conn)
    )


def _typed(field: str, value: str) -> object:
    """A proposed value as the column stores it: None for "", the capital as a number."""
    if not value:
        return None
    cents = _cents(value) if field == "capital" else None
    return value if cents is None else cents / 100


def sync(conn: sqlite3.Connection, apply: bool = False) -> list[Proposal]:
    """The proposals, written in one transaction when ``apply`` is set.

    A ``registry`` write adds an ``applied`` entry to ``sync_log`` with the value it
    replaced; an ``unbacked`` one marks the applied entries of its field as ``reverted``.
    """
    found = proposals(conn)
    if not (apply and found):
        return found
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    column = {f: f for f in FIELDS}  # a column name never comes from the data
    with conn:
        for p in found:
            conn.execute(
                f"UPDATE watched SET {column[p.field]} = ?, updated_at = ? WHERE company_id = ?",
                (_typed(p.field, p.proposed), stamp, p.company_id),
            )
            if p.why == "unbacked":
                conn.execute(
                    "UPDATE sync_log SET state = 'reverted', undone_at = ?"
                    " WHERE company_id = ? AND field = ? AND state = 'applied'",
                    (stamp, p.company_id, p.field),
                )
                continue
            conn.execute(
                "INSERT INTO sync_log (company_id, field, replaced, written, state, applied_at,"
                " fact_day, doc_id, num) VALUES (?, ?, ?, ?, 'applied', ?, ?, ?, ?)",
                (p.company_id, p.field, p.held, p.proposed, stamp, p.day, p.doc_id, p.num),
            )
    return found
