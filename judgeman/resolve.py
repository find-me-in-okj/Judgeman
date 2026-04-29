"""
resolve.py — Shared ID prefix resolution for all commands.

Design decisions:
- Every command that accepts an entity ID also accepts a prefix of that ID.
  UUIDs are long (36 chars). Analysts type prefixes.
- Ambiguity is a hard error. The system never silently picks one when
  multiple entities match — that would be an epistemic error.
- resolve_id() is the single shared utility. It is called at the top of
  every command that looks up an entity by ID, replacing the inline
  `id LIKE ?` pattern that was scattered across commands.
- The error messages tell the analyst exactly which IDs matched so they
  can use a longer prefix immediately.
"""

import sys
import sqlite3
import output as out

# Maps entity_type string to (table_name, id_column, label_column)
ENTITY_TABLE_MAP = {
    "investigation": ("investigations",  "id", "name"),
    "hypothesis":    ("hypotheses",      "id", "statement"),
    "claim":         ("claims",          "id", "statement"),
    "source":        ("sources",         "id", "name"),
    "evidence":      ("evidence",        "id", "description"),
    "counter_claim": ("counter_claims",  "id", "statement"),
}


def resolve_id(
    conn: sqlite3.Connection,
    entity_type: str,
    prefix: str,
    investigation_id: str | None = None,
    allow_multiple: bool = False,
) -> dict | list[dict] | None:
    """
    Resolve a UUID prefix to exactly one entity row.

    Returns:
        dict — the matched row (as dict)
        None — if not found (caller decides how to handle)

    Raises SystemExit if:
        - prefix matches zero entities (with helpful error)
        - prefix matches multiple entities (with disambiguation list)
          unless allow_multiple=True, in which case returns the list

    investigation_id: if provided, adds a WHERE filter to scope the search
    to the active investigation. Prevents cross-investigation ID collisions.
    """
    if entity_type not in ENTITY_TABLE_MAP:
        out.error(f"Unknown entity type: {entity_type}")
        sys.exit(1)

    table, id_col, label_col = ENTITY_TABLE_MAP[entity_type]

    if investigation_id:
        # Prefer investigation-scoped lookup when the table has investigation_id
        try:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {id_col} LIKE ? AND investigation_id = ?",
                (prefix + "%", investigation_id),
            ).fetchall()
        except sqlite3.OperationalError:
            # Table has no investigation_id column (e.g. counter_claims)
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {id_col} LIKE ?",
                (prefix + "%",),
            ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {id_col} LIKE ?",
            (prefix + "%",),
        ).fetchall()

    if not rows:
        out.error(f"No {entity_type} found matching prefix: {prefix!r}")
        out.info(f"List all with:  judgeman {entity_type.replace('_', ' ')} list")
        sys.exit(1)

    if len(rows) > 1:
        if allow_multiple:
            return [dict(r) for r in rows]
        out.error(f"Prefix {prefix!r} is ambiguous — {len(rows)} {entity_type}s match:")
        for r in rows:
            label = str(r[label_col])[:60] if r[label_col] else ""
            out.info(f"  {r[id_col]}  {label}")
        out.info("Use a longer prefix to disambiguate.")
        sys.exit(1)

    return dict(rows[0])


def resolve_counter_claim(conn: sqlite3.Connection, prefix: str, investigation_id: str) -> dict:
    """
    Special case for counter_claims: they are scoped via their parent claim,
    which is scoped to the investigation.
    """
    rows = conn.execute(
        """SELECT cc.* FROM counter_claims cc
           JOIN claims c ON cc.claim_id = c.id
           WHERE cc.id LIKE ? AND c.investigation_id = ?""",
        (prefix + "%", investigation_id),
    ).fetchall()

    if not rows:
        out.error(f"No counter-claim found matching prefix: {prefix!r}")
        sys.exit(1)

    if len(rows) > 1:
        out.error(f"Prefix {prefix!r} is ambiguous — {len(rows)} counter-claims match:")
        for r in rows:
            out.info(f"  {r['id']}  {str(r['statement'])[:60]}")
        out.info("Use a longer prefix to disambiguate.")
        sys.exit(1)

    return dict(rows[0])
