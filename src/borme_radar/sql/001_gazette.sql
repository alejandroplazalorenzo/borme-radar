-- What was read from the BOE: the days asked for, the province documents of each issue
-- and the tallies each document leaves. Tallies hold numbers only, so they can be kept
-- for every document while most announcements are dropped.

CREATE TABLE issue_days (
    day       TEXT NOT NULL PRIMARY KEY,
    asked_at  TEXT NOT NULL,
    outcome   TEXT NOT NULL,
    docs      INTEGER NOT NULL DEFAULT 0,
    failure   TEXT,
    CHECK (outcome IN ('published', 'no_gazette', 'failed')),
    CHECK (docs >= 0)
) WITHOUT ROWID, STRICT;

CREATE TABLE gazette_docs (
    doc_id    TEXT NOT NULL PRIMARY KEY,
    section   TEXT NOT NULL,
    province  TEXT NOT NULL,
    day       TEXT NOT NULL,
    issue_no  INTEGER NOT NULL,
    xml_url   TEXT NOT NULL,
    pdf_url   TEXT,
    read_at   TEXT NOT NULL,
    CHECK (section IN ('A', 'B'))
) WITHOUT ROWID, STRICT;
CREATE INDEX gazette_docs_by_section ON gazette_docs (section, day);

-- One row per document and measure: announcements, acts, with_sheet (a registry sheet
-- was read), prefix (text before the first known heading), no_acts and kept.
CREATE TABLE doc_tally (
    doc_id  TEXT NOT NULL REFERENCES gazette_docs ON DELETE CASCADE,
    what    TEXT NOT NULL,
    n       INTEGER NOT NULL,
    PRIMARY KEY (doc_id, what),
    CHECK (what IN ('announcements', 'acts', 'with_sheet', 'prefix', 'no_acts', 'kept'))
) WITHOUT ROWID, STRICT;

-- Acts of each document by scope and type.
CREATE TABLE act_tally (
    doc_id    TEXT NOT NULL REFERENCES gazette_docs ON DELETE CASCADE,
    scope     TEXT NOT NULL,
    act_type  TEXT NOT NULL,
    n         INTEGER NOT NULL CHECK (n > 0),
    PRIMARY KEY (doc_id, scope, act_type)
) WITHOUT ROWID, STRICT;

-- Section A announcements of each document by days between inscription and issue.
CREATE TABLE lag_tally (
    doc_id  TEXT NOT NULL REFERENCES gazette_docs ON DELETE CASCADE,
    days    INTEGER NOT NULL,
    n       INTEGER NOT NULL CHECK (n > 0),
    PRIMARY KEY (doc_id, days)
) WITHOUT ROWID, STRICT;
