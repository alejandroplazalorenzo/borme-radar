-- Schema version 1: gazette days, province documents, announcements and their acts.

CREATE TABLE days (
    pub_date     TEXT PRIMARY KEY,                -- ISO date checked against the API
    status       TEXT NOT NULL CHECK (status IN ('published', 'no_gazette')),
    n_documents  INTEGER NOT NULL DEFAULT 0,      -- Section A province documents
    checked_at   TEXT NOT NULL
);

CREATE TABLE documents (
    document_id     TEXT PRIMARY KEY,             -- e.g. BORME-A-2026-183-28
    pub_date        TEXT NOT NULL,                -- ISO date of the gazette issue
    gazette_number  INTEGER NOT NULL,
    province        TEXT NOT NULL,
    url_xml         TEXT NOT NULL,
    loaded_at       TEXT NOT NULL
);
CREATE INDEX idx_documents_pub_date ON documents (pub_date);

CREATE TABLE announcements (
    document_id       TEXT NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    number            INTEGER NOT NULL,           -- announcement number in the gazette
    company_name      TEXT NOT NULL,              -- as published
    company_norm      TEXT NOT NULL,              -- normalize_name(company_name)
    registry_data     TEXT,                       -- "Datos registrales" block
    inscription_date  TEXT,                       -- ISO date taken from registry_data
    unparsed_prefix   TEXT NOT NULL DEFAULT '',   -- text before the first known heading
    raw_text          TEXT NOT NULL,
    PRIMARY KEY (document_id, number)
);
CREATE INDEX idx_announcements_norm ON announcements (company_norm);

CREATE TABLE acts (
    document_id  TEXT NOT NULL,
    number       INTEGER NOT NULL,
    seq          INTEGER NOT NULL,                -- order inside the announcement
    act_type     TEXT NOT NULL,                   -- stable English code (acts.ACT_TYPES)
    heading      TEXT NOT NULL,                   -- heading as published
    detail       TEXT NOT NULL,
    PRIMARY KEY (document_id, number, seq),
    FOREIGN KEY (document_id, number)
        REFERENCES announcements (document_id, number) ON DELETE CASCADE
);
CREATE INDEX idx_acts_type ON acts (act_type);
