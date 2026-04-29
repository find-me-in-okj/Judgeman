import os
"""
evidence_cmd.py — Evidence management commands.

Design philosophy:
- Evidence is a discrete, observable item linked to exactly one source.
  One source can produce many evidence items (e.g., a document yields
  multiple transactions, a person yields multiple statements).
- raw_content_ref stores a path or reference — never raw content. This
  prevents the investigation database from becoming the canonical store
  of sensitive data that it has no chain-of-custody for.
- collected_at is separately tracked from created_at: evidence may be
  collected before the analyst enters it into Judgeman.
"""

import sys
import uuid
import click
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit
import output as out
from models import utc_now


def register(cli):
    @cli.group("evidence")
    def evidence_group():
        """Manage evidence — discrete observations linked to sources."""
        pass

    @evidence_group.command("add")
    @click.option("--source", "-s", default=None, help="Source ID or prefix (prompted if omitted)")
    def evidence_add(source: str):
        """
        Record a new evidence item under the active investigation.

        Evidence must be linked to an existing source. If no sources
        exist yet, run: judgeman source add

        raw_content_ref should point to the original artifact (file path,
        URL, case exhibit number) — do not paste raw content here.
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        out.header("Add evidence", f"Investigation: {inv['name']}")

        # Resolve source
        sources = conn.execute(
            "SELECT id, name, credibility_score, source_type FROM sources WHERE investigation_id = ?",
            (inv["id"],),
        ).fetchall()
        if not sources:
            out.error("No sources registered. Add a source first:")
            out.info("  judgeman source add")
            sys.exit(1)

        if source:
            source_row = conn.execute(
                "SELECT * FROM sources WHERE id LIKE ? AND investigation_id = ?",
                (source + "%", inv["id"]),
            ).fetchone()
            if not source_row:
                out.error(f"Source not found: {source}")
                sys.exit(1)
        else:
            click.echo()
            click.echo(out.cyan("  Available sources:"))
            for s in sources:
                cred_color = out.green if s["credibility_score"] >= 0.7 else out.yellow if s["credibility_score"] >= 0.4 else out.red
                cred_str = f"{s['credibility_score']:.2f}"
                click.echo(f"    {out.cyan(s['id'][:8])}\u2026  {out.bold(s['name'])}  [{s['source_type']}]  cred: {cred_color(cred_str)}")

            source_prefix = out.prompt_required("\nSource ID (or prefix)")
            source_row = conn.execute(
                "SELECT * FROM sources WHERE id LIKE ? AND investigation_id = ?",
                (source_prefix + "%", inv["id"]),
            ).fetchone()
            if not source_row:
                out.error(f"Source not found: {source_prefix}")
                sys.exit(1)

        out.info(f"Source: {source_row['name']} (credibility: {source_row['credibility_score']:.2f})")
        click.echo()

        description = out.prompt_required("Description of this evidence item")

        click.echo()
        out.info("raw_content_ref: file path, URL, exhibit number, or archive reference.")
        out.info("Do not paste raw content here. Reference the original artifact only.")
        raw_content_ref = click.prompt("Reference (optional)", default="", show_default=False).strip() or None

        click.echo()
        out.info("collected_at: when was this evidence obtained? (default: now)")
        collected_at_str = click.prompt(
            "Collected at (YYYY-MM-DD or blank for now)",
            default="",
            show_default=False,
        ).strip()
        if collected_at_str:
            try:
                collected_at = datetime.fromisoformat(collected_at_str).isoformat()
            except ValueError:
                out.warn("Invalid date, using current time.")
                collected_at = utc_now()
        else:
            collected_at = utc_now()

        ev_id = str(uuid.uuid4())
        now = utc_now()

        with conn:
            conn.execute(
                """INSERT INTO evidence
                   (id, investigation_id, source_id, description, raw_content_ref, collected_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ev_id, inv["id"], source_row["id"], description, raw_content_ref, collected_at, now),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.CREATE_EVIDENCE,
            entity_type="evidence",
            entity_id=ev_id,
            investigation_id=inv["id"],
            new_value={
                "source_id": source_row["id"],
                "source_name": source_row["name"],
                "description": description,
                "raw_content_ref": raw_content_ref,
            },
        )

        out.success("Evidence recorded.")
        out.entity_id("ID", ev_id)
        out.info("Link to a claim:  judgeman claim link <claim_id> " + ev_id[:8] + "… supports")
        conn.close()

    @evidence_group.command("list")
    @click.option("--source", "-s", default=None, help="Filter by source ID prefix")
    def evidence_list(source: str):
        """List evidence items in the active investigation."""
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        query = """
            SELECT e.*, s.name as source_name, s.credibility_score,
                   COUNT(ec.claim_id) as claim_links
            FROM evidence e
            JOIN sources s ON e.source_id = s.id
            LEFT JOIN evidence_claims ec ON ec.evidence_id = e.id
            WHERE e.investigation_id = ?
        """
        params = [inv["id"]]
        if source:
            query += " AND e.source_id LIKE ?"
            params.append(source + "%")
        query += " GROUP BY e.id ORDER BY e.collected_at DESC"

        rows = conn.execute(query, params).fetchall()
        out.header("Evidence", f"Investigation: {inv['name']}")
        if not rows:
            out.info("No evidence yet.  Run: judgeman evidence add")
            return

        for r in rows:
            cred = r["credibility_score"]
            cred_color = out.green if cred >= 0.7 else out.yellow if cred >= 0.4 else out.red
            click.echo()
            click.echo(f"  {out.cyan(r['id'][:8])}…  {out.bold(r['description'][:55])}")
            click.echo(f"    source: {r['source_name']}  cred: {cred_color(f'{cred:.2f}')}"
                       f"  claim links: {r['claim_links']}")
            out.info(f"collected: {r['collected_at'][:10]}"
                     + (f"  ref: {r['raw_content_ref'][:40]}" if r["raw_content_ref"] else ""))
        conn.close()
