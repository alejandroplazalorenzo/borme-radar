-- The watched groups and companies, with the record `sync` keeps up to date, and what the
-- radar learns to recognise the companies by: registry sheets and other names.

CREATE TABLE watch_groups (
    group_id  INTEGER PRIMARY KEY,
    label     TEXT NOT NULL UNIQUE
) STRICT;

CREATE TABLE watched (
    company_id        INTEGER PRIMARY KEY,
    group_id          INTEGER NOT NULL REFERENCES watch_groups,
    name_key          TEXT NOT NULL UNIQUE,
    heads_group       INTEGER NOT NULL DEFAULT 0 CHECK (heads_group IN (0, 1)),
    incorporated      TEXT,
    activity          TEXT,
    -- The record: the fields `sync` compares with the Registro, as in record.FIELDS.
    address           TEXT,
    capital           REAL,
    city              TEXT,
    name              TEXT NOT NULL,
    purpose           TEXT,
    status            TEXT,
    -- The year the company joined its group (joining.py): the kind of fact it is read
    -- from, whether an inscribed act states the tie, and whether a person typed it (then
    -- `joined --apply` leaves it alone).
    joined_year       INTEGER,
    joined_evidence   TEXT CHECK (joined_evidence IN ('incorporation', 'officers', 'owner')),
    joined_confirmed  INTEGER NOT NULL DEFAULT 0 CHECK (joined_confirmed IN (0, 1)),
    joined_manual     INTEGER NOT NULL DEFAULT 0 CHECK (joined_manual IN (0, 1)),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
) STRICT;

-- Each registry sheet known to belong to a watched company: given in the watchlist, or
-- learned when an announcement matched by its exact name.
CREATE TABLE sheet_owner (
    company_id  INTEGER NOT NULL REFERENCES watched,
    province    TEXT NOT NULL,
    sheet       TEXT NOT NULL,
    how         TEXT NOT NULL CHECK (how IN ('listed', 'name_match')),
    doc_id      TEXT,
    learned_at  TEXT NOT NULL,
    PRIMARY KEY (province, sheet)
) WITHOUT ROWID, STRICT;

CREATE TABLE other_names (
    name_key    TEXT NOT NULL PRIMARY KEY,
    name        TEXT NOT NULL,
    company_id  INTEGER NOT NULL REFERENCES watched,
    added_by    TEXT NOT NULL CHECK (added_by IN ('watchlist', 'name_change')),
    doc_id      TEXT,
    added_at    TEXT NOT NULL
) WITHOUT ROWID, STRICT;
