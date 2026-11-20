"""The watchlist: a CSV of watched companies and the group each one belongs to."""

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
    activity: str | None = None
    sheet: str | None = None
    province: str | None = None
    incorporated: date | None = None
    head: bool = False
    joined: int | None = None


def _clean(row: dict[str, str | None], key: str) -> str | None:
    """The cell stripped of spaces, or None when the column is missing or the cell blank."""
    return (row.get(key) or "").strip() or None


def _capital(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(" ", ""))
    except ValueError as exc:
        raise ValueError(f"capital must be a plain number, got {value!r}") from exc


def _year(value: str | None) -> int | None:
    if value is not None and not (len(value) == 4 and value.isdigit()):
        raise ValueError(f"joined must be a year, got {value!r}")
    return None if value is None else int(value)


def _entry(row: dict[str, str | None], name: str) -> WatchedCompany:
    cell = {key: _clean(row, key) for key in row if key}
    incorporated = cell.get("incorporated")
    return WatchedCompany(
        group=cell.get("group") or name,
        name=name,
        norm=normalize_name(name),
        address=cell.get("address"),
        city=cell.get("city"),
        capital=_capital(cell.get("capital")),
        status=cell.get("status"),
        purpose=cell.get("purpose"),
        activity=cell.get("activity"),
        sheet=cell.get("sheet"),
        province=cell.get("province"),
        incorporated=date.fromisoformat(incorporated) if incorporated else None,
        head=(cell.get("head") or "").lower() in ("1", "yes", "true"),
        joined=_year(cell.get("joined")),
    )


def load_watchlist(path: Path) -> list[WatchedCompany]:
    """Read the watchlist CSV; only the ``name`` column is required.

    Rows without a name are skipped, and of several names that normalise to the same
    key only the first row is kept. A company without a ``group`` is a group of its own.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if "name" not in (reader.fieldnames or ()):
            raise ValueError(f"{path}: the watchlist needs a 'name' column")
        rows = [(row, _clean(row, "name")) for row in reader]
    by_norm: dict[str, WatchedCompany] = {}
    for row, name in rows:
        if name and normalize_name(name) not in by_norm:
            by_norm[normalize_name(name)] = _entry(row, name)
    return list(by_norm.values())
