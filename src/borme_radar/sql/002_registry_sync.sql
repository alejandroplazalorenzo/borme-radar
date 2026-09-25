-- Schema version 2: from watchlist alert monitor to registry synchroniser.
--
-- Version 1 stored every announcement of the gazette with its raw text. Everything in
-- it was derived from the downloaded documents, which stay in the permanent cache, so
-- this migration drops those tables; `borme-radar rebuild` regenerates the derived data
-- offline.
--
-- What is stored now (data minimisation): the announcements of watched companies, of
-- the review queue and of discovery candidates, plus per-document counters for the
-- coverage statistics. `--store-all` keeps every announcement, as version 1 did.

DROP TABLE IF EXISTS acts;
DROP TABLE IF EXISTS announcements;
DROP TABLE IF EXISTS documents;
DROP TABLE IF EXISTS days;

CREATE TABLE days (
    pub_date     TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('published', 'no_gazette', 'failed')),
    n_documents  INTEGER NOT NULL DEFAULT 0,
    error        TEXT,                              -- why the day failed, if it did
    checked_at   TEXT NOT NULL
);

CREATE TABLE documents (
    document_id        TEXT PRIMARY KEY,            -- e.g. BORME-A-2026-183-28
    pub_date           TEXT NOT NULL,
    section            TEXT NOT NULL CHECK (section IN ('A', 'B')),
    gazette_number     INTEGER NOT NULL,
    province           TEXT NOT NULL,
    url_xml            TEXT NOT NULL,
    url_pdf            TEXT,                        -- the official, authentic edition
    n_announcements    INTEGER NOT NULL,
    n_acts             INTEGER NOT NULL,
    n_with_sheet       INTEGER NOT NULL,            -- announcements with a registry sheet
    n_unparsed_prefix  INTEGER NOT NULL,            -- text before the first known heading
    n_without_acts     INTEGER NOT NULL,
    n_stored           INTEGER NOT NULL,            -- announcements kept in this database
    loaded_at          TEXT NOT NULL
);
CREATE INDEX idx_documents_pub_date ON documents (pub_date);

-- Counters of every document, stored or not: acts by type and scope, and the days
-- from inscription to publication. No names, no text.
CREATE TABLE document_act_counts (
    document_id  TEXT NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    act_type     TEXT NOT NULL,
    scope        TEXT NOT NULL,
    n            INTEGER NOT NULL,
    PRIMARY KEY (document_id, act_type, scope)
);
CREATE TABLE document_lag_counts (
    document_id  TEXT NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    lag_days     INTEGER NOT NULL,
    n            INTEGER NOT NULL,
    PRIMARY KEY (document_id, lag_days)
);

-- ---------------------------------------------------------------- watched groups
CREATE TABLE groups (
    group_id  INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE
);

-- The company record. Written by the watchlist import (only fields the Registro has
-- said nothing about) and by `sync --apply`; never edited by hand in normal use.
CREATE TABLE companies (
    company_id    INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES groups (group_id),
    name          TEXT NOT NULL,
    name_norm     TEXT NOT NULL UNIQUE,
    address       TEXT,
    city          TEXT,
    capital       REAL,
    status        TEXT,
    purpose       TEXT,
    incorporated  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Registry sheet -> company. The key is (province, sheet): a sheet belongs to one
-- company, so two watched companies can never claim the same one.
CREATE TABLE company_sheets (
    province     TEXT NOT NULL,
    sheet        TEXT NOT NULL,
    company_id   INTEGER NOT NULL REFERENCES companies (company_id),
    origin       TEXT NOT NULL CHECK (origin IN ('exact_name', 'watchlist')),
    document_id  TEXT,                              -- announcement it was learned from
    learned_at   TEXT NOT NULL,
    PRIMARY KEY (province, sheet)
);

-- Other names of a company (old and new names after a rename).
CREATE TABLE company_aliases (
    alias_norm   TEXT PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies (company_id),
    alias        TEXT NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('watchlist', 'name_change')),
    document_id  TEXT,
    created_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------- announcements
CREATE TABLE announcements (
    document_id    TEXT NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    number         INTEGER NOT NULL,
    pub_date       TEXT NOT NULL,
    fact_date      TEXT NOT NULL,                  -- inscription (A) / deposit (B) date
    province       TEXT NOT NULL,
    company_name   TEXT NOT NULL,                  -- as published
    company_norm   TEXT NOT NULL,
    sheet          TEXT,
    registry_data  TEXT,
    company_id     INTEGER REFERENCES companies (company_id),
    match_via      TEXT CHECK (match_via IS NULL OR match_via IN ('sheet', 'name')),
    kept_as        TEXT NOT NULL CHECK (kept_as IN ('watched', 'review', 'candidate', 'all')),
    PRIMARY KEY (document_id, number)
);
CREATE INDEX idx_announcements_company ON announcements (company_id, fact_date);
CREATE INDEX idx_announcements_sheet ON announcements (province, sheet);
CREATE INDEX idx_announcements_norm ON announcements (company_norm, province);

CREATE TABLE acts (
    document_id  TEXT NOT NULL,
    number       INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    act_type     TEXT NOT NULL,
    heading      TEXT NOT NULL,
    detail       TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('board', 'attorney', 'auditor', 'company')),
    priority     TEXT NOT NULL CHECK (priority IN ('high', 'medium', 'low')),
    PRIMARY KEY (document_id, number, seq),
    FOREIGN KEY (document_id, number)
        REFERENCES announcements (document_id, number) ON DELETE CASCADE
);
CREATE INDEX idx_acts_type ON acts (act_type);

-- Similar names: never matched, waiting for a person.
CREATE TABLE review_queue (
    document_id  TEXT NOT NULL,
    number       INTEGER NOT NULL,
    company_id   INTEGER NOT NULL REFERENCES companies (company_id),
    score        REAL NOT NULL,
    reason       TEXT NOT NULL,
    PRIMARY KEY (document_id, number),
    FOREIGN KEY (document_id, number)
        REFERENCES announcements (document_id, number) ON DELETE CASCADE
);

-- ---------------------------------------------------------------- discovery
-- Companies that look like part of a watched group and are not in the watchlist.
-- Never added automatically (the BORME publishes no tax ID); re-judged every run.
CREATE TABLE candidates (
    candidate_id     INTEGER PRIMARY KEY,
    company_norm     TEXT NOT NULL,
    province         TEXT NOT NULL,
    company_name     TEXT NOT NULL,
    sheet            TEXT,
    group_id         INTEGER NOT NULL REFERENCES groups (group_id),
    reason           TEXT NOT NULL CHECK (reason IN ('mention', 'address', 'token', 'person')),
    evidence         TEXT NOT NULL,
    first_date       TEXT NOT NULL,
    last_date        TEXT NOT NULL,
    n_announcements  INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'dismissed', 'incorporated')),
    judged_at        TEXT NOT NULL,
    UNIQUE (company_norm, province)
);
CREATE TABLE candidate_announcements (
    document_id   TEXT NOT NULL,
    number        INTEGER NOT NULL,
    candidate_id  INTEGER NOT NULL REFERENCES candidates (candidate_id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, number),
    FOREIGN KEY (document_id, number)
        REFERENCES announcements (document_id, number) ON DELETE CASCADE
);

-- Local only (personal data): rare surname pairs of the watched groups' officers.
CREATE TABLE person_keys (
    pair           TEXT PRIMARY KEY,
    group_id       INTEGER NOT NULL REFERENCES groups (group_id),
    n_foreign      INTEGER NOT NULL,
    calibrated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- record history
CREATE TABLE observations (
    obs_id       INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies (company_id),
    field        TEXT NOT NULL
                 CHECK (field IN ('name', 'address', 'city', 'capital', 'status', 'purpose')),
    value        TEXT NOT NULL,
    obs_date     TEXT NOT NULL,     -- fact date (registry) or import date (watchlist)
    source       TEXT NOT NULL CHECK (source IN ('registry', 'watchlist')),
    document_id  TEXT,
    number       INTEGER,
    created_at   TEXT NOT NULL
);
-- NULLs never collide in a unique index, hence the COALESCE: re-deriving is idempotent.
CREATE UNIQUE INDEX uq_observations ON observations (
    company_id, field, value, obs_date, source, COALESCE(document_id, ''), COALESCE(number, -1)
);
CREATE INDEX idx_observations_field ON observations (company_id, field, obs_date);

-- The value each field should have today (see record.py for the rules).
CREATE VIEW current_values AS
WITH ranked AS (
    SELECT o.*,
           ROW_NUMBER() OVER (
               PARTITION BY o.company_id, o.field
               ORDER BY (o.source = 'registry') DESC,
                        o.obs_date DESC,
                        CASE WHEN o.field <> 'status' THEN 0
                             WHEN o.value = 'extinct' THEN 4
                             WHEN o.value = 'dissolved' THEN 3
                             WHEN o.value = 'insolvency' THEN 1
                             ELSE 0 END DESC,
                        o.obs_id DESC
           ) AS rn
    FROM observations o
    WHERE NOT (o.source = 'registry' AND o.field = 'status' AND o.value = 'extinct'
               AND EXISTS (SELECT 1 FROM announcements a
                           WHERE a.company_id = o.company_id AND a.fact_date > o.obs_date))
)
SELECT company_id, field, value, obs_date, source, document_id, number
FROM ranked WHERE rn = 1;

-- What `sync --apply` wrote, with the value it overwrote (to undo it).
CREATE TABLE record_changes (
    change_id    INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies (company_id),
    field        TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT NOT NULL,
    fact_date    TEXT,
    document_id  TEXT,
    number       INTEGER,
    status       TEXT NOT NULL CHECK (status IN ('applied', 'reverted')),
    applied_at   TEXT NOT NULL,
    reverted_at  TEXT
);

-- ---------------------------------------------------------------- officers
-- Local only: holder names are personal data (the database is gitignored).
CREATE TABLE officer_events (
    event_id     INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies (company_id),
    holder       TEXT NOT NULL,                  -- as published, words never reordered
    holder_norm  TEXT NOT NULL,
    role         TEXT NOT NULL,                  -- canonical, e.g. sole_director
    role_raw     TEXT NOT NULL,                  -- as published, e.g. "Adm. Unico"
    event        TEXT NOT NULL
                 CHECK (event IN ('appointment', 'removal', 'revocation', 'reelection')),
    scope        TEXT NOT NULL,
    event_date   TEXT NOT NULL,
    is_company   INTEGER NOT NULL,               -- 1 when the holder is a legal person
    document_id  TEXT,
    number       INTEGER,
    seq          INTEGER
);
CREATE UNIQUE INDEX uq_officer_events ON officer_events (
    company_id, holder_norm, role, event, event_date,
    COALESCE(document_id, ''), COALESCE(number, -1)
);
CREATE INDEX idx_officer_events_company ON officer_events (company_id, event_date);

-- Who holds each position today: the last event of each (company, holder, role).
CREATE VIEW current_officers AS
WITH ranked AS (
    SELECT e.*,
           ROW_NUMBER() OVER (
               PARTITION BY e.company_id, e.holder_norm, e.role
               ORDER BY e.event_date DESC, e.event_id DESC
           ) AS rn
    FROM officer_events e
)
SELECT company_id, holder, role, role_raw, scope, event_date AS since, is_company
FROM ranked WHERE rn = 1 AND event IN ('appointment', 'reelection');
