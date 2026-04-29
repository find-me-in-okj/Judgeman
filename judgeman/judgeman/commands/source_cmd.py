import os
"""
source_cmd.py — Source management commands.

Design philosophy:
- A source is not just a URL. It is a characterized origin with credibility
  reasoning attached. The CLI forces this by prompting for:
    - source_type (primary/secondary/tertiary/human/technical/documentary)
    - credibility_score (analyst-assigned, 0–1)
    - credibility_rationale (why that score)
    - independence_group (optional; used by confidence engine for corroboration)

- Credibility is never inferred from the source type. A "primary" source can
  be biased. A "secondary" source can be meticulous. The analyst must reason
  about this explicitly.

- The independence_group field is explained during add, because analysts
  often don't think about source correlation until they have six sources
  from the same organization and wonder why corroboration is low.
"""

import sys
import uuid
import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit
import output as out
from models import utc_now


SOURCE_TYPES = ["primary", "secondary", "tertiary", "human", "technical", "documentary"]

SOURCE_TYPE_HINTS = {
    "primary":     "Direct observation, original document, firsthand account",
    "secondary":   "Analysis or reporting based on primary sources",
    "tertiary":    "Aggregated or compiled from secondary sources",
    "human":       "Human intelligence — informant, interview, signal intercept",
    "technical":   "Technical artifact — log file, network capture, metadata",
    "documentary": "Document — financial record, legal filing, contract",
}


def register(cli):
    @cli.group("source")
    def source_group():
        """Manage sources — origins of evidence, with credibility assessment."""
        pass

    @source_group.command("add")
    def source_add():
        """
        Register a new source for the active investigation.

        You will be prompted for:
          - Name and reference (URL, document path, person code)
          - Source type (affects how confidence engine weights independence)
          - Credibility score (0.0–1.0, analyst-assigned)
          - Credibility rationale (required — why that score?)
          - Independence group (optional — for corroboration calculation)
        """
        from db import get_connection, init_db, get_active_investigation
        init_db()
        conn = get_connection()

        from cli import require_active
        inv = require_active(conn)

        out.header("Add source", f"Investigation: {inv['name']}")
        out.info("Sources are origins of evidence, not evidence itself.")
        out.info("Credibility is analyst-assigned and must be justified.")
        click.echo()

        name = out.prompt_required("Source name (short identifier)")
        reference = out.prompt_required("Reference (URL, doc path, person code)")

        click.echo()
        click.echo(out.cyan("  Source types:"))
        for t, hint in SOURCE_TYPE_HINTS.items():
            click.echo(f"    {out.bold(t):<14} {out.dim(hint)}")

        source_type = out.prompt_choice("\nSource type", SOURCE_TYPES)

        click.echo()
        out.info("Credibility score: 0.0 = unreliable, 0.5 = neutral, 1.0 = highly reliable")
        out.info("Consider: track record, verification ability, potential bias, access level.")
        credibility_score = out.prompt_float("Credibility score (0.0–1.0)")
        credibility_rationale = out.prompt_required("Rationale for this credibility score")

        click.echo()
        out.info("Independence group (optional): sources sharing the same group string are treated")
        out.info("as correlated — they won't both trigger the corroboration bonus.")
        out.info("Example: 'ministry-of-justice' for a ministry and its press releases.")
        out.info("Leave blank if this source is independent of all others.")
        independence_group = click.prompt("Independence group", default="", show_default=False).strip() or None

        source_id = str(uuid.uuid4())
        now = utc_now()

        with conn:
            conn.execute(
                """INSERT INTO sources
                   (id, investigation_id, name, reference, source_type,
                    credibility_score, credibility_rationale, independence_group, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_id, inv["id"], name, reference, source_type,
                 credibility_score, credibility_rationale, independence_group, now),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.CREATE_SOURCE,
            entity_type="source",
            entity_id=source_id,
            investigation_id=inv["id"],
            new_value={
                "name": name, "source_type": source_type,
                "credibility_score": credibility_score,
                "independence_group": independence_group,
            },
        )

        out.success("Source registered.")
        out.entity_id("ID", source_id)
        cred_color = out.green if credibility_score >= 0.7 else out.yellow if credibility_score >= 0.4 else out.red
        out.field("Credibility", f"{credibility_score:.2f}", color=cred_color)
        if independence_group:
            out.info(f"Independence group: {independence_group}")
        out.info("Add evidence from this source:  judgeman evidence add")
        conn.close()

    @source_group.command("list")
    def source_list():
        """List all sources in the active investigation."""
        from db import get_connection, init_db, get_active_investigation
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        rows = conn.execute(
            """SELECT s.*, COUNT(e.id) as evidence_count
               FROM sources s LEFT JOIN evidence e ON e.source_id = s.id
               WHERE s.investigation_id = ? GROUP BY s.id ORDER BY s.credibility_score DESC""",
            (inv["id"],),
        ).fetchall()

        out.header("Sources", f"Investigation: {inv['name']}")
        if not rows:
            out.info("No sources yet.  Run: judgeman source add")
            return

        for r in rows:
            cred = r["credibility_score"]
            cred_color = out.green if cred >= 0.7 else out.yellow if cred >= 0.4 else out.red
            click.echo()
            click.echo(f"  {out.cyan(r['id'][:8])}…  {out.bold(r['name'])}  [{r['source_type']}]")
            click.echo(f"    {out.dim(r['reference'][:60])}")
            click.echo(f"    credibility: {cred_color(f'{cred:.2f}')}  evidence items: {r['evidence_count']}")
            if r["independence_group"]:
                out.info(f"group: {r['independence_group']}")
        conn.close()

    @source_group.command("show")
    @click.argument("source_id")
    def source_show(source_id: str):
        """Show full details for a source."""
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        row = conn.execute(
            "SELECT * FROM sources WHERE id LIKE ? AND investigation_id = ?",
            (source_id + "%", inv["id"]),
        ).fetchone()
        if not row:
            out.error(f"Source not found: {source_id}")
            sys.exit(1)

        out.header(f"Source: {row['name']}")
        out.field("ID", row["id"])
        out.field("Type", row["source_type"])
        out.field("Reference", row["reference"])
        cred = row["credibility_score"]
        cred_color = out.green if cred >= 0.7 else out.yellow if cred >= 0.4 else out.red
        out.field("Credibility", f"{cred:.2f} / 1.00", color=cred_color)
        out.multiline_field("Credibility rationale", row["credibility_rationale"])
        if row["independence_group"]:
            out.field("Independence group", row["independence_group"])
        out.field("Registered", row["created_at"][:19].replace("T", " ") + " UTC")
        conn.close()
