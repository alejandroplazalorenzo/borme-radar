"""Plain data containers shared across modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """One province document listed in a daily summary (Section A or B)."""

    document_id: str  # e.g. BORME-A-2026-183-28
    province: str  # e.g. MADRID
    url_xml: str
    section: str = "A"
    url_pdf: str | None = None  # the official, authentic edition


@dataclass(frozen=True, slots=True)
class Act:
    """One registered act inside an announcement, e.g. an appointment."""

    seq: int  # position inside the announcement, starting at 1
    act_type: str  # stable English code, e.g. "appointment"
    heading: str  # heading exactly as published, e.g. "Nombramientos"
    detail: str  # text between this heading and the next one
    scope: str = "company"  # who the act is about: board | attorney | auditor | company
    priority: str = "low"  # high | medium | low, decided by type and scope


@dataclass(frozen=True, slots=True)
class Announcement:
    """One announcement: all acts inscribed for one company in one entry."""

    number: int  # announcement number printed before the company name
    company_name: str
    acts: tuple[Act, ...]
    raw_text: str
    registry_data: str | None = None  # "Datos registrales" block
    inscription_date: date | None = None  # date between brackets in registry_data
    unparsed_prefix: str = ""  # text before the first recognised heading, if any
    sheet: str | None = None  # registry sheet, e.g. "M 786863" (stable company key)
    other_companies: tuple[str, ...] = ()  # Section B: the other companies listed


@dataclass(frozen=True, slots=True)
class Document:
    """One parsed province document of Section A or B."""

    document_id: str
    pub_date: date
    gazette_number: int
    province: str
    announcements: tuple[Announcement, ...] = field(default_factory=tuple)
    section: str = "A"
