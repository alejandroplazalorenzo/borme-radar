"""Watchlist: the watched groups and their known companies (the company record).

CSV columns (UTF-8, header required):

* ``group`` - the group the company belongs to (default: the company itself);
* ``name`` - registered name, as the Registro writes it (required);
* ``address``, ``city`` - registered address; the address signal uses it to recognise
  new companies domiciled at the group's address;
* ``capital``, ``status``, ``purpose`` - the record as known from other sources;
* ``sheet``, ``province`` - registry sheet, if known (otherwise it is learned from the
  first exact name match);
* ``incorporated`` - incorporation date (ISO), used by the homonym guard.

Only published facts about legal persons belong here; no personal data.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from borme_radar.normalize import normalize_name


@dataclass(frozen=True, slots=True)
class WatchedCompany:
    group: str
    name: str
    norm: str
    address: str | None = None
    city: str | None = None
    capital: float | None = None
    status: str | None = None
    purpose: str | None = None
    sheet: str | None = None
    province: str | None = None
    incorporated: date | None = None


def _clean(row: dict[str, str | None], key: str) -> str | None:
    value = (row.get(key) or "").strip()
    return value or None


def _capital(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(" ", ""))
    except ValueError as exc:
        raise ValueError(f"capital must be a plain number, got {value!r}") from exc


def load_watchlist(path: Path) -> list[WatchedCompany]:
    """Read the watchlist CSV; duplicates (after normalisation) are dropped."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "name" not in reader.fieldnames:
            raise ValueError(f"{path}: the watchlist needs a 'name' column")
        entries: dict[str, WatchedCompany] = {}
        for row in reader:
            name = _clean(row, "name")
            if not name:
                continue
            norm = normalize_name(name)
            incorporated = _clean(row, "incorporated")
            entries.setdefault(
                norm,
                WatchedCompany(
                    group=_clean(row, "group") or name,
                    name=name,
                    norm=norm,
                    address=_clean(row, "address"),
                    city=_clean(row, "city"),
                    capital=_capital(_clean(row, "capital")),
                    status=_clean(row, "status"),
                    purpose=_clean(row, "purpose"),
                    sheet=_clean(row, "sheet"),
                    province=_clean(row, "province"),
                    incorporated=date.fromisoformat(incorporated) if incorporated else None,
                ),
            )
    return list(entries.values())
