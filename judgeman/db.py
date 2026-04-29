"""
db.py — Database connection and schema for Judgeman.

Design decisions:
- Raw sqlite3, no ORM. The schema is the source of truth. Analysts and auditors
  should be able to read the database directly without any framework knowledge.
- UUIDs as TEXT primary keys. Avoids integer ID collisions when investigations
  are merged or exported.
- All timestamps stored as ISO-8601 UTC strings. Human-readable, portable.
- CHECK constraints enforce enum values at the DB layer, not just application layer.
- analyst_actions is fully denormalized (stores JSON blobs). This is intentional:
  the audit log must be self-contained and readable without joins, even years later.
"""

import sqlite3
import os
from pathlib import Path

def _judgeman_dir() -> Path:
    """
    Resolve the Judgeman data directory at runtime.
    Priority: JUDGEMAN_HOME env var > ~/.judgeman
    Evaluated each call so tests can override HOME/JUDGEMAN_HOME.
    """
    env_home = os.environ.get("JUDGEMAN_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".judgeman"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS investigations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'closed', 'archived')),
    analyst_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id               TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    statement        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK(status IN ('active', 'supported', 'rejected', 'inconclusive')),
    rationale        TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id                        TEXT PRIMARY KEY,
    investigation_id          TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    hypothesis_id             TEXT REFERENCES hypotheses(id) ON DELETE SET NULL,
    statement                 TEXT NOT NULL,
    base_confidence           REAL NOT NULL
                              CHECK(base_confidence >= 0.0 AND base_confidence <= 1.0),
    final_confidence          REAL
                              CHECK(final_confidence IS NULL
                                    OR (final_confidence >= 0.0 AND final_confidence <= 1.0)),
    rationale                 TEXT NOT NULL,
    what_if_wrong             TEXT,
    impact_level              TEXT NOT NULL DEFAULT 'low'
                              CHECK(impact_level IN ('low', 'medium', 'high')),
    override_confidence       REAL
                              CHECK(override_confidence IS NULL
                                    OR (override_confidence >= 0.0 AND override_confidence <= 1.0)),
    override_justification    TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id                    TEXT PRIMARY KEY,
    investigation_id      TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    reference             TEXT NOT NULL,
    source_type           TEXT NOT NULL
                          CHECK(source_type IN
                                ('primary', 'secondary', 'tertiary',
                                 'human', 'technical', 'documentary')),
    credibility_score     REAL NOT NULL
                          CHECK(credibility_score >= 0.0 AND credibility_score <= 1.0),
    credibility_rationale TEXT NOT NULL,
    independence_group    TEXT,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id               TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    source_id        TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    description      TEXT NOT NULL,
    raw_content_ref  TEXT,
    collected_at     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_claims (
    evidence_id    TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    claim_id       TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    relationship   TEXT NOT NULL
                   CHECK(relationship IN ('supports', 'undermines', 'neutral')),
    relevance_note TEXT,
    linked_at      TEXT NOT NULL,
    PRIMARY KEY (evidence_id, claim_id)
);

CREATE TABLE IF NOT EXISTS counter_claims (
    id                TEXT PRIMARY KEY,
    claim_id          TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    statement         TEXT NOT NULL,
    addressed         INTEGER NOT NULL DEFAULT 0 CHECK(addressed IN (0, 1)),
    address_rationale TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyst_actions (
    id               TEXT PRIMARY KEY,
    investigation_id TEXT REFERENCES investigations(id) ON DELETE SET NULL,
    analyst_id       TEXT NOT NULL,
    action_type      TEXT NOT NULL,
    entity_type      TEXT NOT NULL,
    entity_id        TEXT,
    old_value        TEXT,
    new_value        TEXT,
    justification    TEXT,
    timestamp        TEXT NOT NULL
);


-- Enforce what_if_wrong for high-impact claims at the DB layer.
-- Application layer also enforces this, but the trigger is the final guard.
CREATE TRIGGER IF NOT EXISTS enforce_high_impact_what_if_wrong
BEFORE INSERT ON claims
WHEN NEW.impact_level = 'high' AND (NEW.what_if_wrong IS NULL OR TRIM(NEW.what_if_wrong) = '')
BEGIN
    SELECT RAISE(ABORT, 'High-impact claims require a non-empty what_if_wrong field.');
END;

CREATE TRIGGER IF NOT EXISTS enforce_high_impact_what_if_wrong_update
BEFORE UPDATE ON claims
WHEN NEW.impact_level = 'high' AND (NEW.what_if_wrong IS NULL OR TRIM(NEW.what_if_wrong) = '')
BEGIN
    SELECT RAISE(ABORT, 'High-impact claims cannot have empty what_if_wrong field.');
END;

CREATE TABLE IF NOT EXISTS active_investigation (
    singleton        INTEGER PRIMARY KEY DEFAULT 1 CHECK(singleton = 1),
    investigation_id TEXT REFERENCES investigations(id) ON DELETE SET NULL,
    analyst_id       TEXT
);
"""


def get_db_path() -> Path:
    d = _judgeman_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "judgeman.db"


def get_connection(db_path=None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path=None) -> None:
    conn = get_connection(db_path)
    with conn:
        conn.executescript(SCHEMA)
    conn.close()


def get_active_investigation(conn: sqlite3.Connection):
    row = conn.execute(
        "SELECT investigation_id, analyst_id FROM active_investigation WHERE singleton = 1"
    ).fetchone()
    if row is None or row["investigation_id"] is None:
        return None
    inv = conn.execute(
        "SELECT * FROM investigations WHERE id = ?", (row["investigation_id"],)
    ).fetchone()
    return dict(inv) if inv else None


def set_active_investigation(conn: sqlite3.Connection, investigation_id: str, analyst_id: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO active_investigation (singleton, investigation_id, analyst_id)
            VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                investigation_id = excluded.investigation_id,
                analyst_id = excluded.analyst_id
            """,
            (investigation_id, analyst_id),
        )
