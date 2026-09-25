"""Company record as dated observations, and the ``sync`` that keeps it up to date.

Every act of a watched company leaves **observations**: "this field had this value on
this date, according to this source". An act that confirms what was already known is
stored too, because it dates the value. The current value of each field is *resolved*
from the observations (view ``current_values``), never typed in:

* the Registro wins over the watchlist whatever the dates, because the date of a
  watchlist value is the day it was imported, not the day it was true;
* among registry observations the latest fact date wins;
* on the same day, the most advanced lifecycle status wins (extinct > dissolved >
  insolvency > active): a company can have its insolvency, dissolution and extinction
  inscribed together, and the order of print inside the announcement means nothing;
* an extinction is ignored if the company keeps publishing acts after it.

Comparing each act against the record instead renames companies backwards: a company
renamed twice produces a stale proposal for its first rename. Resolving the full history
and keeping the last value does not.

``sync`` shows where the record differs from the resolved value and writes only with
``apply=True``. Each write is logged with the value it overwrote, and a value applied
from evidence that later disappeared (e.g. after a matching fix and a ``rebuild``) is
reverted to what it was before.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from borme_radar.normalize import normalize_name, plain, strip_annotations

FIELDS = ("name", "address", "city", "capital", "status", "purpose")
STATUS_BY_ACT = {
    "dissolution": "dissolved",
    "extinction": "extinct",
    "insolvency": "insolvency",
    "reactivation": "active",
}
# Lifecycle order used only to break ties between statuses inscribed on the same day.
STATUS_SEVERITY = {"extinct": 4, "dissolved": 3, "insolvency": 1, "active": 0}
FINAL_STATUS = "extinct"

_RESULTING_CAPITAL_RE = re.compile(r"Resultante\s+Suscrito\s*:\s*([\d.]+,\d{2})", re.I)
_CAPITAL_RE = re.compile(r"\bCapital\s*:\s*([\d.]+,\d{2})", re.I)
_PURPOSE_RE = re.compile(r"Objeto\s+social\s*:\s*(.+?)(?=\.\s*Domicilio\s*:|$)", re.S | re.I)
_ADDRESS_RE = re.compile(
    r"Domicilio\s*:\s*(.+?)(?=\.\s*(?:Capital|Patrimonio)[^:]*:|$)", re.S | re.I
)
# "CL MAYOR 5 (ROZAS DE MADRID (LAS)": the municipality in brackets, with the
# article the official list postpones, sometimes with the bracket left open.
_STREET_CITY_RE = re.compile(r"^(?P<street>.*?)\s*\((?P<city>[^()]*(?:\([^()]*\)?)?)\)?\s*$")
_POSTPONED_ARTICLE_RE = re.compile(r"\s*\((LAS|LOS|EL|LA|AS|OS|A|O|L')\)?\s*$", re.I)


@dataclass(frozen=True, slots=True)
class Observation:
    field: str
    value: str


def euros(text: str) -> float | None:
    """'1.646.608,00' -> 1646608.0 (Spanish number format)."""
    try:
        return float(text.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def resulting_capital(detail: str) -> float | None:
    """Capital after an increase or reduction: the "Resultante Suscrito", never the
    amount of the change ("Capital:" in an increase is the increment)."""
    match = _RESULTING_CAPITAL_RE.search(detail or "")
    return euros(match.group(1)) if match else None


def street_and_city(text: str) -> tuple[str | None, str | None]:
    """Split "C/ ROMERO, 38 06240 (FUENTE DE CANTOS)" into street and municipality."""
    text = " ".join(strip_annotations(text or "").split()).strip(" .,;")
    match = _STREET_CITY_RE.match(text)
    if not match or not match.group("city").strip():
        return (text or None), None
    street = match.group("street").strip(" .,-")
    city = match.group("city").strip()
    article = _POSTPONED_ARTICLE_RE.search(city)
    if article:
        city = f"{article.group(1).upper()} {city[: article.start()].strip()}"
    return (street or None), city.strip() or None


def _obs(field: str, value: str | None) -> list[Observation]:
    value = " ".join((value or "").split()).strip(" .")
    return [Observation(field, value)] if value else []


def observations_of_act(act_type: str, detail: str) -> list[Observation]:
    """What one act says about the record fields (no comparison with anything)."""
    detail = detail or ""
    found: list[Observation] = []
    if act_type == "name_change":
        found += _obs("name", strip_annotations(detail))
    elif act_type == "address_change":
        street, city = street_and_city(detail)
        found += _obs("address", street) + _obs("city", city)
    elif act_type in ("capital_increase", "capital_reduction"):
        capital = resulting_capital(detail)
        if capital is not None:
            found.append(Observation("capital", f"{capital:.2f}"))
    elif act_type == "business_purpose_change":
        found += _obs("purpose", detail)
    elif act_type == "incorporation":
        purpose = _PURPOSE_RE.search(detail)
        if purpose:
            found += _obs("purpose", purpose.group(1))
        address = _ADDRESS_RE.search(detail)
        if address:
            street, city = street_and_city(address.group(1))
            found += _obs("address", street) + _obs("city", city)
        capital = _CAPITAL_RE.search(detail)
        if capital and (amount := euros(capital.group(1))) is not None:
            found.append(Observation("capital", f"{amount:.2f}"))
    if act_type in STATUS_BY_ACT:
        found.append(Observation("status", STATUS_BY_ACT[act_type]))
    return found


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def differs(field: str, current: str | None, resolved: str | None) -> bool:
    """Does the record really differ from the resolved value?

    Capital compares as a number, the name with the matching normaliser (legal-form
    spelling is not a rename), everything else without accents, case or extra spaces.
    """
    if field == "capital":
        a, b = _number(current), _number(resolved)
        return a != b if a is None or b is None else abs(a - b) >= 0.01
    if field == "name":
        return normalize_name(current or "") != normalize_name(resolved or "")
    return plain(current or "") != plain(resolved or "")


# ------------------------------------------------------------------------------ sync


@dataclass(frozen=True, slots=True)
class Difference:
    company_id: int
    company: str
    field: str
    record: str | None  # what the record holds now
    registry: str  # what it should hold (resolved), or the value to restore
    fact_date: str | None
    document_id: str | None
    number: int | None
    kind: str  # "update" (registry says otherwise) | "revert" (evidence disappeared)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def differences(conn: sqlite3.Connection) -> list[Difference]:
    """Fields where the record differs from its resolved registry value."""
    out: list[Difference] = []
    rows = conn.execute(
        """
        SELECT c.company_id, c.name AS company, v.field, v.value, v.obs_date,
               v.document_id, v.number, c.name, c.address, c.city, c.capital, c.status,
               c.purpose
        FROM current_values v JOIN companies c USING (company_id)
        WHERE v.source = 'registry'
        ORDER BY c.name, v.field
        """
    ).fetchall()
    for row in rows:
        record = dict(zip(FIELDS, row[7:], strict=True))
        current = record[row["field"]]
        current_text = None if current is None else str(current)
        if differs(row["field"], current_text, row["value"]):
            out.append(
                Difference(
                    row["company_id"],
                    row["company"],
                    row["field"],
                    current_text,
                    row["value"],
                    row["obs_date"],
                    row["document_id"],
                    row["number"],
                    "update",
                )
            )
    return out


def retired(conn: sqlite3.Connection) -> list[Difference]:
    """Applied changes that the Registro no longer backs: restore the previous value.

    Only when the record still holds what was applied; if a person changed it since,
    their value stays.
    """
    out: list[Difference] = []
    rows = conn.execute(
        """
        SELECT r.company_id, c.name AS company, r.field, r.old_value, r.new_value,
               r.fact_date, r.document_id, r.number, c.name, c.address, c.city,
               c.capital, c.status, c.purpose
        FROM record_changes r JOIN companies c USING (company_id)
        WHERE r.status = 'applied'
          AND NOT EXISTS (SELECT 1 FROM current_values v
                          WHERE v.company_id = r.company_id AND v.field = r.field
                            AND v.source = 'registry')
        ORDER BY r.company_id, r.field, r.applied_at DESC
        """
    ).fetchall()
    seen: set[tuple[int, str]] = set()
    for row in rows:
        key = (row["company_id"], row["field"])
        if key in seen:
            continue
        seen.add(key)
        record = dict(zip(FIELDS, row[8:], strict=True))
        holds = record[row["field"]]
        if differs(row["field"], None if holds is None else str(holds), row["new_value"]):
            continue
        out.append(
            Difference(
                row["company_id"],
                row["company"],
                row["field"],
                None if holds is None else str(holds),
                row["old_value"] or "",
                row["fact_date"],
                row["document_id"],
                row["number"],
                "revert",
            )
        )
    return out


def _stored(field: str, value: str | None) -> object:
    if not value:
        return None
    return _number(value) if field == "capital" else value


def sync(conn: sqlite3.Connection, apply: bool = False) -> list[Difference]:
    """Differences between the record and the Registro; written only with ``apply``."""
    found = differences(conn) + retired(conn)
    if not apply or not found:
        return found
    now = _now()
    with conn:
        for d in found:
            if d.field not in FIELDS:  # column names are never taken from outside
                raise ValueError(f"unknown record field {d.field!r}")
            conn.execute(
                f"UPDATE companies SET {d.field} = ?, updated_at = ? WHERE company_id = ?",
                (_stored(d.field, d.registry), now, d.company_id),
            )
            if d.kind == "revert":
                conn.execute(
                    """
                    UPDATE record_changes SET status = 'reverted', reverted_at = ?
                    WHERE company_id = ? AND field = ? AND status = 'applied'
                    """,
                    (now, d.company_id, d.field),
                )
                continue
            conn.execute(
                """
                INSERT INTO record_changes (company_id, field, old_value, new_value,
                    fact_date, document_id, number, status, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'applied', ?)
                """,
                (
                    d.company_id,
                    d.field,
                    d.record,
                    d.registry,
                    d.fact_date,
                    d.document_id,
                    d.number,
                    now,
                ),
            )
    return found
