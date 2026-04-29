import os
"""
source_update_cmd.py — Source credibility re-assessment.

Design philosophy:
- Source credibility is not static. New information can raise or lower it:
  a formerly trusted source is exposed as biased; a previously unknown
  source is verified against independent records.
- Re-assessment requires a rationale explaining what changed and why.
- The old credibility score and rationale are preserved in the audit trail.
  The history of trust is part of the investigation record.
- After a credibility update, the analyst is warned that all claims linked
  to this source should have their confidence recalculated.
"""

import sys
import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit
import output as out
from models import utc_now


def register(cli):
    @cli.command("source-update")
    @click.argument("source_id")
    @click.option("--credibility", "-c", type=float, default=None,
                  help="New credibility score (0.0–1.0)")
    @click.option("--rationale", "-r", default=None,
                  help="Why the credibility changed")
    def source_update(source_id: str, credibility: float, rationale: str):
        """
        Re-assess the credibility of a source.

        Credibility changes require explicit rationale. The history is
        preserved in the audit trail.

        After updating, run:  judgeman verify --fix-confidence
        to propagate the change to all affected claims.
        """
        from db import get_connection, init_db
        from resolve import resolve_id
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        source = resolve_id(conn, "source", source_id, inv["id"])

        out.header(f"Re-assess source: {source['name']}")
        out.field("Current credibility",
                  f"{source['credibility_score']:.2f}",
                  color=out.green if source['credibility_score'] >= 0.7
                  else out.yellow if source['credibility_score'] >= 0.4
                  else out.red)
        out.multiline_field("Current rationale", source["credibility_rationale"])
        click.echo()

        if credibility is None:
            out.info("New credibility score: 0.0 = unreliable, 0.5 = neutral, 1.0 = highly reliable")
            credibility = out.prompt_float("New credibility score (0.0–1.0)")

        if credibility < 0.0 or credibility > 1.0:
            out.error("Credibility must be between 0.0 and 1.0.")
            sys.exit(1)

        if credibility == source["credibility_score"]:
            out.info("Score unchanged. No update recorded.")
            return

        if not rationale:
            out.info("Explain what changed — new evidence about this source, discovered bias, etc.")
            rationale = out.prompt_required("Rationale for change")

        old_score = source["credibility_score"]
        old_rationale = source["credibility_rationale"]
        now = utc_now()

        with conn:
            conn.execute(
                "UPDATE sources SET credibility_score=?, credibility_rationale=? WHERE id=?",
                (credibility, rationale, source["id"]),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type="UPDATE_SOURCE_CREDIBILITY",
            entity_type="source",
            entity_id=source["id"],
            investigation_id=inv["id"],
            old_value={"credibility_score": old_score, "credibility_rationale": old_rationale},
            new_value={"credibility_score": credibility, "credibility_rationale": rationale},
            justification=rationale,
        )

        direction = "↑" if credibility > old_score else "↓"
        color = out.green if credibility > old_score else out.red
        out.success(f"Credibility updated: {old_score:.2f} {direction} {color(f'{credibility:.2f}')}")
        out.warn("Claims linked to this source have stale confidence scores.")
        out.info("Propagate:  judgeman verify --fix-confidence")

        # Show affected claims
        affected = conn.execute(
            """SELECT DISTINCT c.id, c.statement FROM claims c
               JOIN evidence_claims ec ON ec.claim_id = c.id
               JOIN evidence e ON e.id = ec.evidence_id
               WHERE e.source_id = ? AND c.investigation_id = ?""",
            (source["id"], inv["id"]),
        ).fetchall()

        if affected:
            out.section("Affected claims")
            for a in affected:
                click.echo(f"  {out.cyan(a['id'][:8])}…  {a['statement'][:60]}")

        conn.close()
