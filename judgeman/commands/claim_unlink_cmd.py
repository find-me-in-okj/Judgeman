import os
"""
claim_unlink_cmd.py — Remove an evidence-claim relationship.

Design philosophy:
- Evidence links can be wrong. An analyst may link evidence to the wrong
  claim, or later determine the relationship was mislabelled.
- Unlinking is NOT silent. The removed relationship is logged in the
  audit trail with the analyst's rationale.
- Unlinking supporting evidence will reduce corroboration. The analyst
  is warned and confidence is flagged as stale.
- This command cannot be used to silently remove inconvenient evidence.
  The audit trail preserves the fact that a link existed and was removed.
"""

import sys
import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit
import output as out
from models import utc_now


def register(cli):
    # Register as a subcommand of the existing claim group
    # by finding it on the cli object
    @cli.command("claim-unlink")
    @click.argument("claim_id")
    @click.argument("evidence_id")
    @click.option("--reason", "-r", default=None,
                  help="Why this link is being removed (required for audit log)")
    def claim_unlink(claim_id: str, evidence_id: str, reason: str):
        """
        Remove an evidence-claim relationship.

        This is logged in the audit trail. Removing supporting evidence
        will mark the claim's confidence as stale.

        The evidence item itself is NOT deleted — only the relationship
        between this evidence and this claim.
        """
        from db import get_connection, init_db
        from resolve import resolve_id
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        claim = resolve_id(conn, "claim", claim_id, inv["id"])
        evidence = resolve_id(conn, "evidence", evidence_id, inv["id"])

        # Check the link exists
        link = conn.execute(
            "SELECT * FROM evidence_claims WHERE claim_id = ? AND evidence_id = ?",
            (claim["id"], evidence["id"]),
        ).fetchone()

        if not link:
            out.error("No link found between this evidence and claim.")
            out.info(f"  Claim:    {claim['statement'][:60]}")
            out.info(f"  Evidence: {evidence['description'][:60]}")
            sys.exit(1)

        link = dict(link)

        out.header("Remove evidence-claim link")
        click.echo(f"  {out.cyan('Claim:')}    {claim['statement'][:65]}")
        click.echo(f"  {out.cyan('Evidence:')} {evidence['description'][:65]}")
        click.echo(f"  {out.cyan('Relationship:')} {link['relationship']}")
        click.echo()
        out.warn("This removal will be permanently logged in the audit trail.")

        if link["relationship"] == "supports":
            out.warn("Removing supporting evidence may reduce confidence.")

        if not reason:
            reason = out.prompt_required("Reason for removing this link")

        if not click.confirm(out.yellow("  Confirm removal?")):
            out.info("Cancelled.")
            return

        with conn:
            conn.execute(
                "DELETE FROM evidence_claims WHERE claim_id = ? AND evidence_id = ?",
                (claim["id"], evidence["id"]),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type="UNLINK_EVIDENCE",
            entity_type="evidence_claim",
            entity_id=evidence["id"],
            investigation_id=inv["id"],
            old_value={
                "claim_id": claim["id"],
                "evidence_id": evidence["id"],
                "relationship": link["relationship"],
            },
            new_value=None,
            justification=reason,
        )

        out.success("Link removed.")
        out.warn(f"Claim confidence is now stale. Recalculate:  "
                 f"judgeman claim confidence {claim['id'][:8]}…")
        conn.close()
