-- The announcements that are kept (a watched company's, those waiting for a person and
-- those of discovery leads) with their acts, and the leads themselves.

-- A company discovery points at: `open` while a signal still fires for it, `gone` when
-- none does any more, `watched` once it is in the watchlist.
CREATE TABLE leads (
    lead_id       INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES watch_groups,
    found_by      TEXT NOT NULL CHECK (found_by IN ('address', 'mention', 'person', 'token')),
    evidence      TEXT NOT NULL,
    firm          TEXT NOT NULL,
    firm_key      TEXT NOT NULL,
    province      TEXT NOT NULL,
    sheet         TEXT,
    notice_count  INTEGER NOT NULL,
    first_day     TEXT NOT NULL,
    last_day      TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'gone', 'watched')),
    judged_at     TEXT NOT NULL,
    UNIQUE (firm_key, province)
) STRICT;
CREATE INDEX leads_followed ON leads (province, sheet) WHERE found_by = 'mention';

-- One row per kept announcement. A similar name waiting for a person carries the
-- watched company it resembles (`near_*`); an announcement of a lead carries `lead_id`.
CREATE TABLE notices (
    doc_id         TEXT NOT NULL REFERENCES gazette_docs ON DELETE CASCADE,
    num            INTEGER NOT NULL,
    province       TEXT NOT NULL,
    day            TEXT NOT NULL,
    fact_day       TEXT NOT NULL,
    firm           TEXT NOT NULL,
    firm_key       TEXT NOT NULL,
    sheet          TEXT,
    registry_data  TEXT,
    kept_as        TEXT NOT NULL,
    company_id     INTEGER REFERENCES watched,
    via            TEXT,
    near_company   INTEGER REFERENCES watched,
    near_score     REAL,
    near_reason    TEXT,
    lead_id        INTEGER REFERENCES leads ON DELETE SET NULL,
    PRIMARY KEY (doc_id, num),
    CHECK (kept_as IN ('watched', 'review', 'candidate', 'all')),
    CHECK (via IN ('sheet', 'name'))
) WITHOUT ROWID, STRICT;
CREATE INDEX notices_of_company ON notices (company_id, fact_day) WHERE company_id IS NOT NULL;
CREATE INDEX notices_by_sheet ON notices (sheet, province);
CREATE INDEX notices_by_firm ON notices (firm_key);
CREATE INDEX notices_of_lead ON notices (lead_id) WHERE lead_id IS NOT NULL;

CREATE TABLE notice_acts (
    doc_id    TEXT NOT NULL,
    num       INTEGER NOT NULL,
    seq       INTEGER NOT NULL,
    act_type  TEXT NOT NULL,
    scope     TEXT NOT NULL,
    priority  TEXT NOT NULL,
    heading   TEXT NOT NULL,
    detail    TEXT NOT NULL,
    PRIMARY KEY (doc_id, num, seq),
    FOREIGN KEY (doc_id, num) REFERENCES notices ON DELETE CASCADE,
    CHECK (scope IN ('board', 'attorney', 'auditor', 'company')),
    CHECK (priority IN ('high', 'medium', 'low'))
) WITHOUT ROWID, STRICT;

-- Personal data, local only: rare pairs of words of the groups' officers' names.
CREATE TABLE surname_pairs (
    pair         TEXT NOT NULL PRIMARY KEY,
    group_id     INTEGER NOT NULL REFERENCES watch_groups,
    outside      INTEGER NOT NULL,
    measured_at  TEXT NOT NULL
) WITHOUT ROWID, STRICT;
