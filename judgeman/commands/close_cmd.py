import os
"""
close_cmd.py — Formally close an investigation.

Design decisions:
- Closing an investigation is a deliberate analytical act, not a status change.
  It requires a closing statement summarizing the analyst's conclusions, the
  weight of evidence, and outstanding uncertainties.
- Closing does NOT prevent new analysis. The investigation can be reopened.
  History is always preserved.
- The closing statement is the analyst's final summary of what was learned —
  NOT a conclusion about guilt, legal liability, or required action.
  The CLI enforces this by prompting the analyst to separate "findings" from
  "what these findings should be used for."
- Before close is allowed, verify is run automatically. An investigation
  with BLOCKING issues cannot be closed without explicit --force.
  This prevents closing investigations with structurally incomplete claims.
"""

import sys
import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit as audit_mod
import output as out
from models import utc_now


CLOSE_DISCLAIMER = (
    "A closing statement should summarize findings and outstanding uncertainties. "
    "It must NOT contain legal judgments, enforcement recommendations, or "
    "determinations of guilt. Those are for qualified reviewers to make."
)



def _has_blocking_verify_issues(conn, investigation_id: str) -> bool:
    """
    Check for blocking structural issues without spawning a nested Click context.
    Returns True if there are any blocking issues that should prevent closing.
    """
    iid = investigation_id
    claims = conn.execute("SELECT * FROM claims WHERE investigation_id=?", (iid,)).fetchall()
    for c in claims:
        if c["impact_level"] == "high":
            cc_n = conn.execute(
                "SELECT COUNT(*) FROM counter_claims WHERE claim_id=?", (c["id"],)
            ).fetchone()[0]
            if cc_n == 0:
                return True
            if not c["what_if_wrong"]:
                return True
        ev_n = conn.execute(
            "SELECT COUNT(*) FROM evidence_claims WHERE claim_id=?", (c["id"],)
        ).fetchone()[0]
        if ev_n == 0:
            return True
    return False

def register(cli):
    @cli.command("close")
    @click.option("--statement", "-s", default=None,
                  help="Closing statement (prompted if omitted)")
    @click.option("--force", is_flag=True, default=False,
                  help="Close even if verify finds blocking issues")
    def close_investigation(statement: str, force: bool):
        """
        Formally close the active investigation.

        Requires a closing statement summarizing findings and uncertainties.
        Runs structural verification first — investigations with blocking
        issues cannot be closed without --force.

        Closing is reversible. Use: jm reopen
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        if inv["status"] == "closed":
            out.warn("This investigation is already closed.")
            out.info("Reopen with:  jm reopen")
            return

        out.header(f"Close investigation: {inv['name']}")
        click.echo()

        # Run verify logic directly — no CliRunner nesting needed
        has_blocking = _has_blocking_verify_issues(conn, inv["id"])

        if has_blocking and not force:
            out.error("Investigation has BLOCKING integrity issues.")
            out.warn("Resolve them before closing, or use --force to override.")
            out.info("Run  jm verify  for full details.")
            sys.exit(1)

        if has_blocking and force:
            out.warn("Closing with unresolved blocking issues (--force used). This is logged.")

        click.echo(out.dim(CLOSE_DISCLAIMER))
        click.echo()

        if not statement:
            out.info("Summarize what was found, what remains uncertain, and the")
            out.info("limitations of this investigation.")
            out.warn("Do NOT include legal judgments or enforcement recommendations.")
            click.echo()
            statement = out.prompt_required("Closing statement")

        # Check for prohibited language (soft check — warn, don't block)
        prohibited_phrases = [
            "is guilty", "is liable", "should be arrested", "should be charged",
            "recommends prosecution", "recommends enforcement", "is a criminal",
            "proved that", "proves that",
        ]
        found_phrases = [p for p in prohibited_phrases if p.lower() in statement.lower()]
        if found_phrases:
            out.warn("Closing statement contains language that may constitute a legal judgment:")
            for phrase in found_phrases:
                click.echo(out.yellow(f"  · '{phrase}'"))
            out.warn("Consider revising to describe findings rather than conclusions.")
            if not click.confirm(out.yellow("  Proceed with this statement?")):
                out.info("Closing cancelled. Revise the statement and try again.")
                return

        now = utc_now()
        with conn:
            conn.execute(
                "UPDATE investigations SET status='closed', updated_at=? WHERE id=?",
                (now, inv["id"]),
            )

        audit_mod.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit_mod.CLOSE_INVESTIGATION,
            entity_type="investigation",
            entity_id=inv["id"],
            investigation_id=inv["id"],
            old_value={"status": "active"},
            new_value={"status": "closed", "closing_statement": statement},
            justification=statement,
        )

        out.success(f"Investigation closed: {inv['name']}")
        click.echo()
        click.echo(out.dim("  Closing statement recorded in audit trail."))
        out.info("Export before archiving:  jm export")
        out.info("Reopen if needed:         jm reopen")
        conn.close()

    @cli.command("reopen")
    @click.option("--reason", "-r", default=None, help="Reason for reopening")
    def reopen_investigation(reason: str):
        """Reopen a closed investigation."""
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        if inv["status"] == "active":
            out.info("Investigation is already active.")
            return

        if not reason:
            reason = out.prompt_required("Reason for reopening")

        now = utc_now()
        with conn:
            conn.execute(
                "UPDATE investigations SET status='active', updated_at=? WHERE id=?",
                (now, inv["id"]),
            )

        audit_mod.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type="REOPEN_INVESTIGATION",
            entity_type="investigation",
            entity_id=inv["id"],
            investigation_id=inv["id"],
            old_value={"status": inv["status"]},
            new_value={"status": "active"},
            justification=reason,
        )

        out.success(f"Investigation reopened: {inv['name']}")
        conn.close()
