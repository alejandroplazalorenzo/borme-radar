-- What the acts of the watched companies say over time: values of the record fields,
-- the log of what `sync` wrote, and the changes of officers with the positions still
-- open. All of it except the log is derived again by `rebuild --history-only`.

CREATE TABLE seen_values (
    seen_id     INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES watched,
    source      TEXT NOT NULL CHECK (source IN ('registry', 'watchlist')),
    doc_id      TEXT,
    num         INTEGER,
    day         TEXT NOT NULL,
    field       TEXT NOT NULL
                CHECK (field IN ('address', 'capital', 'city', 'name', 'purpose', 'status')),
    value       TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    -- Where the value was read: one announcement, or the watchlist.
    read_in     TEXT GENERATED ALWAYS AS (IFNULL(doc_id || '#' || num, source)) VIRTUAL
) STRICT;
-- A value read twice in the same place is stored once.
CREATE UNIQUE INDEX seen_values_once ON seen_values (read_in, company_id, field, day, value);

-- The stages a status can be at, earliest first (record.LIFECYCLE).
CREATE TABLE status_stages (
    status  TEXT NOT NULL PRIMARY KEY,
    stage   INTEGER NOT NULL UNIQUE
) WITHOUT ROWID, STRICT;
INSERT INTO status_stages (status, stage)
VALUES ('active', 0), ('insolvency', 1), ('dissolved', 2), ('extinct', 3);

-- Status values the company outlived: an extinction with announcements of the company
-- after it, and a dissolution inscribed on the day of such an extinction.
CREATE VIEW outlived AS
WITH ended AS (
    SELECT s.seen_id, s.company_id, s.day, s.value
    FROM seen_values s
    WHERE s.source = 'registry' AND s.field = 'status' AND s.value IN ('dissolved', 'extinct')
      AND EXISTS (SELECT 1 FROM notices n WHERE n.company_id = s.company_id AND n.fact_day > s.day)
)
SELECT seen_id FROM ended e
WHERE e.value = 'extinct'
   OR EXISTS (SELECT 1 FROM ended x
              WHERE x.company_id = e.company_id AND x.day = e.day AND x.value = 'extinct');

-- One value per company and field: the source, the day, the stage and the row decide, in
-- that order (record.py says why).
CREATE VIEW record_now AS
WITH ranked AS (
    SELECT s.company_id, s.field, s.value, s.source, s.day, s.doc_id, s.num,
           ROW_NUMBER() OVER (
               PARTITION BY s.company_id, s.field
               ORDER BY s.source = 'registry' DESC, s.day DESC, IFNULL(st.stage, -1) DESC,
                        s.seen_id DESC
           ) AS place
    FROM seen_values s
    LEFT JOIN status_stages st ON s.field = 'status' AND st.status = s.value
    WHERE s.seen_id NOT IN (SELECT seen_id FROM outlived)
)
SELECT company_id, field, value, source, day, doc_id, num FROM ranked WHERE place = 1;

-- Every value `sync --apply` wrote, with the one it replaced, so it can be undone.
CREATE TABLE sync_log (
    entry_id    INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES watched,
    field       TEXT NOT NULL,
    replaced    TEXT,
    written     TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('applied', 'reverted')),
    applied_at  TEXT NOT NULL,
    undone_at   TEXT,
    fact_day    TEXT,
    doc_id      TEXT,
    num         INTEGER
) STRICT;

-- Personal data, local only: holders exactly as published. Every change comes from one
-- act of one announcement (doc_id, num, seq).
CREATE TABLE role_changes (
    change_id     INTEGER PRIMARY KEY,
    doc_id        TEXT NOT NULL,
    num           INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    company_id    INTEGER NOT NULL REFERENCES watched,
    day           TEXT NOT NULL,
    kind          TEXT NOT NULL
                  CHECK (kind IN ('appointment', 'reelection', 'removal', 'revocation')),
    scope         TEXT NOT NULL,
    role          TEXT NOT NULL,
    label         TEXT NOT NULL,
    holder        TEXT NOT NULL,
    holder_key    TEXT NOT NULL,
    legal_person  INTEGER NOT NULL CHECK (legal_person IN (0, 1)),
    -- One announcement states a change of one holder in one role once.
    UNIQUE (doc_id, num, holder_key, role, kind)
) STRICT;
CREATE INDEX role_changes_by_day ON role_changes (company_id, day);

-- The change that opened each position still held (filled by positions.py).
CREATE TABLE open_roles (
    change_id  INTEGER NOT NULL PRIMARY KEY REFERENCES role_changes ON DELETE CASCADE
) STRICT;

CREATE VIEW officers_now AS
SELECT r.company_id, r.role, r.label, r.holder, r.legal_person, r.scope, r.day AS since
FROM role_changes r
WHERE r.change_id IN (SELECT change_id FROM open_roles);
