"""
cli.py — Judgeman CLI entry point.

Design decisions:
- Top-level `cli` group + subcommand groups (investigation, hypothesis, etc.)
- Every command that mutates state logs to the audit trail before returning.
- The active investigation is resolved at command entry. Commands that require
  an active investigation fail loudly with a clear fix instruction.
- No command ever silently succeeds. Every action prints what happened and
  what the analyst should do next.
- Interactive prompts (for add/create commands) are preferred over flag
  arguments for fields requiring rationale — this nudges analysts to think
  rather than copy-paste flags.
"""

import sys
import os

# Ensure the package directory is on the path when running directly
sys.path.insert(0, os.path.dirname(__file__))

import click
import uuid
from datetime import datetime, timezone

import db
import audit
from compat import get_analyst_id
import output as out
from models import utc_now, IMPACT_CEILINGS, IMPACT_CEILING_RATIONALE


def get_conn():
    db.init_db()
    return db.get_connection()


def require_active(conn) -> dict:
    """Return active investigation or exit with a clear error."""
    inv = db.get_active_investigation(conn)
    if not inv:
        out.error("No active investigation.")
        out.info("Run:  judgeman init <name>  or  judgeman use <id>")
        sys.exit(1)
    return inv


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """
    Judgeman — OSINT analytical reasoning engine.

    Enforces structured epistemic discipline: hypotheses, claims with
    transparent confidence, linked evidence, source credibility,
    counter-claims, and immutable audit logs.

    The system surfaces uncertainty. The analyst owns conclusions.
    """
    pass


# ---------------------------------------------------------------------------
# investigation commands
# ---------------------------------------------------------------------------

@cli.command("init")
@click.argument("name")
@click.option("--analyst", "-a", default=None, help="Analyst identifier (default: $USER)")
@click.option("--description", "-d", default=None, help="Investigation description")
def investigation_init(name: str, analyst: str, description: str):
    """
    Create a new investigation and set it as active.

    NAME is the investigation codename or title. Keep it short and
    non-identifying in case notes are shared.
    """
    analyst_id = get_analyst_id(analyst)
    conn = get_conn()

    inv_id = str(uuid.uuid4())
    now = utc_now()

    with conn:
        conn.execute(
            "INSERT INTO investigations (id, name, description, status, analyst_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (inv_id, name, description, analyst_id, now, now),
        )

    db.set_active_investigation(conn, inv_id, analyst_id)

    audit.log_action(
        conn,
        analyst_id=analyst_id,
        action_type=audit.CREATE_INVESTIGATION,
        entity_type="investigation",
        entity_id=inv_id,
        investigation_id=inv_id,
        new_value={"name": name, "description": description, "analyst_id": analyst_id},
    )

    out.header(f"Investigation created: {name}", f"Analyst: {analyst_id}")
    out.entity_id("ID", inv_id)
    out.success("Set as active investigation.")
    out.info("Next steps:")
    click.echo("    judgeman source add          — register a source")
    click.echo("    judgeman hypothesis add       — define a hypothesis")
    conn.close()


@cli.command("status")
def investigation_status():
    """Show the active investigation and a summary of its entities."""
    conn = get_conn()
    inv = require_active(conn)

    out.header(f"Investigation: {inv['name']}", f"ID: {inv['id']}")
    out.field("Analyst", inv["analyst_id"])
    out.field("Status", inv["status"])
    out.field("Created", inv["created_at"][:19].replace("T", " ") + " UTC")

    iid = inv["id"]
    counts = {
        "hypotheses":    conn.execute("SELECT COUNT(*) FROM hypotheses WHERE investigation_id=?", (iid,)).fetchone()[0],
        "sources":       conn.execute("SELECT COUNT(*) FROM sources WHERE investigation_id=?", (iid,)).fetchone()[0],
        "evidence":      conn.execute("SELECT COUNT(*) FROM evidence WHERE investigation_id=?", (iid,)).fetchone()[0],
        "claims":        conn.execute("SELECT COUNT(*) FROM claims WHERE investigation_id=?", (iid,)).fetchone()[0],
        "counter-claims":conn.execute(
            "SELECT COUNT(*) FROM counter_claims cc JOIN claims c ON cc.claim_id=c.id WHERE c.investigation_id=?",
            (iid,)).fetchone()[0],
        "audit entries": conn.execute("SELECT COUNT(*) FROM analyst_actions WHERE investigation_id=?", (iid,)).fetchone()[0],
    }

    out.section("Entity counts")
    for k, v in counts.items():
        status_color = out.dim if v == 0 else None
        out.field(k, str(v), color=status_color)

    # Surface any high-impact claims without counter-claims
    risky = conn.execute(
        """
        SELECT c.id, c.statement, c.impact_level,
               COUNT(cc.id) as cc_count,
               c.what_if_wrong
        FROM claims c
        LEFT JOIN counter_claims cc ON cc.claim_id = c.id
        WHERE c.investigation_id = ? AND c.impact_level = 'high'
        GROUP BY c.id
        HAVING cc_count = 0 OR c.what_if_wrong IS NULL
        """,
        (iid,),
    ).fetchall()

    if risky:
        out.section("Safety warnings")
        for r in risky:
            out.warn(f"High-impact claim missing requirements: {r['statement'][:55]}…")
            if not r["cc_count"]:
                click.echo(out.red("    ✗ No counter-claim"))
            if not r["what_if_wrong"]:
                click.echo(out.red("    ✗ No 'what if I'm wrong?' section"))
            out.info(f"    Fix: judgeman claim challenge {r['id'][:8]}…")

    conn.close()


@cli.command("use")
@click.argument("investigation_id")
@click.option("--analyst", "-a", default=None)
def investigation_use(investigation_id: str, analyst: str):
    """Switch the active investigation by ID (or ID prefix)."""
    conn = get_conn()
    from resolve import resolve_id
    row = resolve_id(conn, "investigation", investigation_id)

    analyst_id = get_analyst_id(analyst) or row["analyst_id"]
    db.set_active_investigation(conn, row["id"], analyst_id)

    audit.log_action(
        conn, analyst_id=analyst_id,
        action_type=audit.SET_ACTIVE,
        entity_type="investigation",
        entity_id=row["id"],
        investigation_id=row["id"],
    )
    out.success(f"Active investigation set to: {row['name']} ({row['id'][:8]}…)")
    conn.close()


@cli.command("list")
def investigation_list():
    """List all investigations."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, status, analyst_id, created_at FROM investigations ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        out.info("No investigations found. Run: judgeman init <name>")
        return

    out.header("Investigations")
    for r in rows:
        status_color = out.green if r["status"] == "active" else out.dim
        click.echo(f"  {out.cyan(r['id'][:8])}…  {out.bold(r['name'])}  [{status_color(r['status'])}]")
        out.info(f"analyst: {r['analyst_id']}  created: {r['created_at'][:10]}")
    conn.close()


# ---------------------------------------------------------------------------
# hypothesis commands
# ---------------------------------------------------------------------------

@cli.group("hypothesis")
def hypothesis_group():
    """Manage hypotheses — testable statements under investigation."""
    pass


@hypothesis_group.command("add")
@click.option("--statement", "-s", default=None, help="Hypothesis statement (prompted if omitted)")
@click.option("--rationale", "-r", default=None, help="Why this hypothesis is worth testing")
def hypothesis_add(statement: str, rationale: str):
    """
    Add a hypothesis to the active investigation.

    A hypothesis is a falsifiable statement, not a conclusion. It should
    be specific enough that evidence can support or refute it.

    Examples:
      "Subject X was present in City Y during Event Z"
      "Organization A controls Domain B through shell company C"
    """
    conn = get_conn()
    inv = require_active(conn)

    out.header("Add hypothesis", f"Investigation: {inv['name']}")
    out.info("A hypothesis is a testable statement, not a conclusion.")
    out.info("It should be falsifiable — evidence should be able to refute it.")
    click.echo()

    statement = statement or out.prompt_required("Statement")
    rationale = rationale or click.prompt("Rationale (why test this?)", default="", show_default=False).strip() or None

    hyp_id = str(uuid.uuid4())
    now = utc_now()

    with conn:
        conn.execute(
            "INSERT INTO hypotheses (id, investigation_id, statement, status, rationale, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (hyp_id, inv["id"], statement, rationale, now, now),
        )

    audit.log_action(
        conn,
        analyst_id=inv["analyst_id"],
        action_type=audit.CREATE_HYPOTHESIS,
        entity_type="hypothesis",
        entity_id=hyp_id,
        investigation_id=inv["id"],
        new_value={"statement": statement, "rationale": rationale},
    )

    out.success("Hypothesis created.")
    out.entity_id("ID", hyp_id)
    out.info("Link claims to this hypothesis with:  judgeman claim create --hypothesis " + hyp_id[:8] + "…")
    conn.close()


@hypothesis_group.command("list")
def hypothesis_list():
    """List all hypotheses in the active investigation."""
    conn = get_conn()
    inv = require_active(conn)

    rows = conn.execute(
        "SELECT h.*, COUNT(c.id) as claim_count FROM hypotheses h "
        "LEFT JOIN claims c ON c.hypothesis_id = h.id "
        "WHERE h.investigation_id = ? GROUP BY h.id ORDER BY h.created_at",
        (inv["id"],),
    ).fetchall()

    out.header("Hypotheses", f"Investigation: {inv['name']}")
    if not rows:
        out.info("No hypotheses yet.  Run: judgeman hypothesis add")
        return

    for r in rows:
        status_color = (out.green if r["status"] == "supported"
                        else out.red if r["status"] == "rejected"
                        else out.yellow if r["status"] == "inconclusive"
                        else None)
        click.echo()
        click.echo(f"  {out.cyan(r['id'][:8])}…  [{status_color(r['status']) if status_color else r['status']}]")
        click.echo(f"    {r['statement']}")
        out.info(f"claims: {r['claim_count']}")
        if r["rationale"]:
            out.info(f"rationale: {r['rationale'][:80]}")
    conn.close()


@hypothesis_group.command("update")
@click.argument("hypothesis_id")
@click.option("--status", "-s",
              type=click.Choice(["active", "supported", "rejected", "inconclusive"]),
              required=True,
              help="New status for the hypothesis")
@click.option("--rationale", "-r", default=None, help="Rationale for this status change")
def hypothesis_update(hypothesis_id: str, status: str, rationale: str):
    """
    Update the status of a hypothesis.

    STATUS must be one of: active, supported, rejected, inconclusive.

    This does not delete the hypothesis or its claims. The audit trail
    preserves the full history of status changes.

    Important: 'supported' and 'rejected' are not conclusions — they reflect
    the current weight of evidence as assessed by the analyst. New evidence
    can always reverse a status.
    """
    conn = get_conn()
    inv = require_active(conn)
    from resolve import resolve_id
    row = resolve_id(conn, "hypothesis", hypothesis_id, inv["id"])

    if not rationale:
        out.info("Provide a rationale for this status change (required for audit trail).")
        rationale = out.prompt_required("Rationale")

    old_status = row["status"]
    now = utc_now()

    with conn:
        conn.execute(
            "UPDATE hypotheses SET status = ?, rationale = ?, updated_at = ? WHERE id = ?",
            (status, rationale, now, row["id"]),
        )

    audit.log_action(
        conn,
        analyst_id=inv["analyst_id"],
        action_type=audit.UPDATE_HYPOTHESIS,
        entity_type="hypothesis",
        entity_id=row["id"],
        investigation_id=inv["id"],
        old_value={"status": old_status},
        new_value={"status": status, "rationale": rationale},
        justification=rationale,
    )

    out.success(f"Hypothesis status: {old_status} → {status}")
    out.info(f"Rationale logged: {rationale[:80]}")

    if status in ("supported", "rejected"):
        out.warn(
            f"Status '{status}' reflects current evidence weight only. "
            "It is not a final conclusion. New evidence may change this."
        )
    conn.close()


# ---------------------------------------------------------------------------
# audit command
# ---------------------------------------------------------------------------

@cli.command("audit")
@click.argument("entity_id")
@click.option("--limit", "-n", default=20, help="Max entries to show")
def audit_trail(entity_id: str, limit: int):
    """
    Show the audit trail for an entity.

    ENTITY_ID can be an investigation, hypothesis, claim, source,
    evidence, or counter-claim ID (or prefix).
    """
    conn = get_conn()

    # Resolve prefix
    rows = audit.get_audit_trail(conn, entity_id=entity_id, limit=limit)
    if not rows:
        # Try prefix match via investigation
        inv = db.get_active_investigation(conn)
        if inv:
            rows = audit.get_audit_trail(conn, investigation_id=inv["id"], limit=limit)
        if not rows:
            out.info(f"No audit entries found for: {entity_id}")
            return

    out.header(f"Audit trail — {entity_id[:16]}…", f"Showing {len(rows)} entries (newest first)")

    for r in rows:
        ts = r["timestamp"][:19].replace("T", " ")
        click.echo()
        click.echo(f"  {out.dim(ts)}  {out.cyan(r['action_type'])}  [{out.dim(r['entity_type'])}]")
        if r["analyst_id"]:
            out.info(f"analyst: {r['analyst_id']}")
        if r["justification"]:
            click.echo(f"    {out.yellow('justification:')} {r['justification'][:100]}")
        if r["old_value"] and r["new_value"]:
            import json
            try:
                old = json.loads(r["old_value"])
                new = json.loads(r["new_value"])
                for k in set(list(old.keys()) + list(new.keys())):
                    if old.get(k) != new.get(k):
                        click.echo(out.dim(f"    {k}: {old.get(k)} → {new.get(k)}"))
            except Exception:
                pass
    conn.close()

# ---------------------------------------------------------------------------
# Register command groups from submodules
# ---------------------------------------------------------------------------

import sys
import os

# Add the judgeman package directory to path for submodule imports
sys.path.insert(0, os.path.dirname(__file__))

from commands.source_cmd import register as register_source
from commands.evidence_cmd import register as register_evidence
from commands.claim_cmd import register as register_claim
from commands.report_cmd import register as register_report
from commands.verify_cmd import register as register_verify
from commands.source_update_cmd import register as register_source_update
from commands.claim_unlink_cmd import register as register_claim_unlink
from commands.export_cmd import register as register_export
from commands.import_cmd import register as register_import
from commands.close_cmd import register as register_close
from commands.claim_edit_cmd import register as register_claim_edit

register_source(cli)
register_evidence(cli)
register_claim(cli)
register_report(cli)
register_verify(cli)
register_source_update(cli)
register_claim_unlink(cli)
register_export(cli)
register_import(cli)
register_close(cli)
register_claim_edit(cli)


if __name__ == "__main__":
    cli()
