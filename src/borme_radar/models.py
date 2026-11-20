"""What is read from the gazette: references from the daily summary, parsed documents.

A document is one province's part of one issue, in Section A (the acts inscribed in the
Registro) or Section B (other entries, such as deposits of merger projects). It holds
numbered announcements, one per company, and each announcement holds its acts in the
order they were published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """A document listed in a daily summary: what it is and where to download it."""

    document_id: str  # the BOE identifier: BORME-A-2026-183-28
    province: str  # as the summary titles the document: MADRID
    url_xml: str
    section: str = "A"
    url_pdf: str | None = None  # the PDF, which is the legally valid text


@dataclass(frozen=True, slots=True)
class Act:
    """One act of an announcement: a heading of the catalogue and the text after it."""

    seq: int  # 1 for the first act of the announcement, then 2, 3...
    act_type: str  # code of acts.ACT_TYPES ("appointment")
    heading: str  # as printed ("Nombramientos")
    detail: str  # what follows the heading, up to the next heading
    scope: str = "company"  # board, attorney, auditor or company (officers.act_scope)
    priority: str = "low"  # high, medium or low (acts.priority)


@dataclass(frozen=True, slots=True)
class Announcement:
    """One announcement: a company and the acts published about it, in order.

    In Section B, ``inscription_date`` holds the deposit date printed in the header.
    """

    number: int  # the number printed ahead of the company's name
    company_name: str
    acts: tuple[Act, ...]
    raw_text: str
    registry_data: str | None = None  # the closing "Datos registrales" text
    inscription_date: date | None = None  # read off registry_data
    unparsed_prefix: str = ""  # any text ahead of the first known heading
    sheet: str | None = None  # "M 786863": the key that survives renames
    other_companies: tuple[str, ...] = ()  # Section B lists several at once


@dataclass(frozen=True, slots=True)
class Document:
    """A parsed province document of one issue, with its announcements in order."""

    document_id: str
    pub_date: date
    gazette_number: int
    province: str
    announcements: tuple[Announcement, ...] = field(default_factory=tuple)
    section: str = "A"
