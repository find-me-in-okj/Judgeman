"""
audit.py — Immutable audit logging for Judgeman.

Design decisions:
- Every write to audit_log is append-only. There is no update or delete path.
- The log function is the ONLY way to write analyst_actions rows. All command
  handlers import and call it — not raw SQL. This ensures consistent structure.
- old_value and new_value are JSON-serialized dicts, not formatted strings.
  This allows programmatic querying of the audit log years later.
- justification is required for any action that overrides a system constraint
  (confidence ceilings, addressing counter-claims with trivial rationale, etc).
  It is optional for routine actions but strongly encouraged.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional


def log_action(
    conn: sqlite3.Connection,
    analyst_id: str,
    action_type: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    investigation_id: Optional[str] = None,
    old_value: Optional[dict] = None,
    new_value: Optional[dict] = None,
    justification: Optional[str] = None,
) -> str:
    """
    Write one immutable audit log entry.

    Returns the action ID.

    action_type conventions (use these constants from this module):
        CREATE_INVESTIGATION, CREATE_HYPOTHESIS, CREATE_CLAIM, CREATE_SOURCE,
        CREATE_EVIDENCE, LINK_EVIDENCE, ADD_COUNTER_CLAIM, ADDRESS_COUNTER_CLAIM,
        UPDATE_CONFIDENCE, OVERRIDE_CEILING, CLOSE_INVESTIGATION, SET_ACTIVE
    """
    action_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    with conn:
        conn.execute(
            """
            INSERT INTO analyst_actions
                (id, investigation_id, analyst_id, action_type, entity_type,
                 entity_id, old_value, new_value, justification, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                investigation_id,
                analyst_id,
                action_type,
                entity_type,
                entity_id,
                json.dumps(old_value) if old_value is not None else None,
                json.dumps(new_value) if new_value is not None else None,
                justification,
                timestamp,
            ),
        )
    return action_id


def get_audit_trail(
    conn: sqlite3.Connection,
    entity_id: Optional[str] = None,
    investigation_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """
    Retrieve audit log entries. At least one filter must be provided.
    Returns newest-first.
    """
    if not any([entity_id, investigation_id, entity_type]):
        raise ValueError("At least one filter (entity_id, investigation_id, entity_type) is required.")

    clauses = []
    params = []
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    if investigation_id:
        clauses.append("investigation_id = ?")
        params.append(investigation_id)
    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)

    where = " AND ".join(clauses)
    params.append(limit)

    rows = conn.execute(
        f"SELECT * FROM analyst_actions WHERE {where} ORDER BY timestamp DESC LIMIT ?",
        params,
    ).fetchall()

    return [dict(r) for r in rows]


# Action type constants — import these rather than using bare strings
CREATE_INVESTIGATION   = "CREATE_INVESTIGATION"
CREATE_HYPOTHESIS      = "CREATE_HYPOTHESIS"
CREATE_CLAIM           = "CREATE_CLAIM"
CREATE_SOURCE          = "CREATE_SOURCE"
CREATE_EVIDENCE        = "CREATE_EVIDENCE"
LINK_EVIDENCE          = "LINK_EVIDENCE"
ADD_COUNTER_CLAIM      = "ADD_COUNTER_CLAIM"
ADDRESS_COUNTER_CLAIM  = "ADDRESS_COUNTER_CLAIM"
UPDATE_CONFIDENCE      = "UPDATE_CONFIDENCE"
OVERRIDE_CEILING       = "OVERRIDE_CEILING"
CLOSE_INVESTIGATION    = "CLOSE_INVESTIGATION"
SET_ACTIVE             = "SET_ACTIVE"
UPDATE_HYPOTHESIS      = "UPDATE_HYPOTHESIS"
REOPEN_COUNTER_CLAIM   = "REOPEN_COUNTER_CLAIM"
GENERATE_REPORT        = "GENERATE_REPORT"
