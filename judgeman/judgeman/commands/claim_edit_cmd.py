import os
"""
claim_edit_cmd.py — Edit mutable claim fields with full audit logging.

Design decisions:
- Only three fields are editable after creation: statement, rationale,
  and what_if_wrong. These are the analyst's interpretive fields.
- base_confidence is NOT editable via this command. To change base confidence,
  use `jm claim confidence` which runs the full engine, or re-create the claim.
  This prevents stealth confidence manipulation.
- impact_level is NOT editable. If the impact level is wrong, the analyst
  should create a new claim. Changing impact level retroactively can silently
  change what safety requirements applied historically.
- Every edit is a full diff in the audit trail: old value → new value.
- Editing the statement of a claim that has linked evidence is allowed
  but triggers a warning: the evidence was linked to the OLD statement.
  The analyst should verify the links still make sense.
"""

import sys
import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit as audit_mod
import output as out
from models import utc_now


def register(cli):
    @cli.command("claim-edit")
    @click.argument("claim_id")
    @click.option("--statement",   "-s", default=None, help="New claim statement")
    @click.option("--rationale",   "-r", default=None, help="New rationale")
    @click.option("--what-if-wrong", "-w", default=None,
                  help="New 'what if I'm wrong?' section")
    def claim_edit(claim_id: str, statement: str, rationale: str, what_if_wrong: str):
        """
        Edit the statement, rationale, or what_if_wrong of a claim.

        impact_level and base_confidence are not editable via this command.
        Every change is logged in the audit trail with old and new values.

        Editing the statement of a claim with linked evidence will trigger
        a warning — verify that existing evidence links still apply.
        """
        from db import get_connection, init_db
        from resolve import resolve_id
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        claim = resolve_id(conn, "claim", claim_id, inv["id"])

        # If no flags provided, show current values and prompt interactively
        if not any([statement, rationale, what_if_wrong]):
            out.header(f"Edit claim: {claim['statement'][:55]}")
            click.echo()
            out.field("Current statement",   claim["statement"])
            out.field("Current rationale",   claim["rationale"])
            out.field("Current what_if_wrong",
                      claim.get("what_if_wrong") or out.dim("(not set)"))
            click.echo()
            out.info("Leave blank to keep current value.")
            click.echo()

            new_statement     = click.prompt("New statement",     default=claim["statement"]).strip()
            new_rationale     = click.prompt("New rationale",     default=claim["rationale"]).strip()
            current_wiw       = claim.get("what_if_wrong") or ""
            new_what_if_wrong = click.prompt("New what_if_wrong", default=current_wiw).strip()
        else:
            new_statement     = statement     or claim["statement"]
            new_rationale     = rationale     or claim["rationale"]
            new_what_if_wrong = what_if_wrong or claim.get("what_if_wrong") or ""

        # Compute diff
        changes = {}
        if new_statement != claim["statement"]:
            changes["statement"] = (claim["statement"], new_statement)
        if new_rationale != claim["rationale"]:
            changes["rationale"] = (claim["rationale"], new_rationale)
        if new_what_if_wrong != (claim.get("what_if_wrong") or ""):
            changes["what_if_wrong"] = (claim.get("what_if_wrong"), new_what_if_wrong)

        if not changes:
            out.info("No changes made.")
            return

        # Warn if statement changed and evidence exists
        if "statement" in changes:
            ev_count = conn.execute(
                "SELECT COUNT(*) FROM evidence_claims WHERE claim_id = ?",
                (claim["id"],),
            ).fetchone()[0]
            if ev_count > 0:
                out.warn(f"This claim has {ev_count} linked evidence item(s).")
                out.warn("Changing the statement may invalidate existing evidence links.")
                out.warn("Review links after editing:  jm claim show " + claim["id"][:8] + "…")
                if not click.confirm(out.yellow("  Proceed with statement change?")):
                    out.info("Edit cancelled.")
                    return

        out.section("Changes to apply")
        for field, (old, new) in changes.items():
            click.echo(f"  {out.cyan(field)}:")
            click.echo(out.dim(f"    old: {str(old)[:80]}"))
            click.echo(f"    new: {str(new)[:80]}")

        now = utc_now()

        with conn:
            conn.execute(
                "UPDATE claims SET statement=?, rationale=?, what_if_wrong=?, updated_at=? WHERE id=?",
                (new_statement, new_rationale,
                 new_what_if_wrong or None,
                 now, claim["id"]),
            )

        audit_mod.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type="EDIT_CLAIM",
            entity_type="claim",
            entity_id=claim["id"],
            investigation_id=inv["id"],
            old_value={k: v[0] for k, v in changes.items()},
            new_value={k: v[1] for k, v in changes.items()},
            justification=f"Fields edited: {', '.join(changes.keys())}",
        )

        out.success(f"Claim updated. {len(changes)} field(s) changed.")
        out.info("Confidence score is now stale. Recalculate:  "
                 f"jm claim confidence {claim['id'][:8]}…")
        conn.close()
