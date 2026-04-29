import os
"""
claim_cmd.py — Claim management commands.

Design philosophy:
- A claim is the core epistemic unit of Judgeman. Every claim carries:
    - A statement (the assertion)
    - A base confidence (analyst-assigned, with rationale)
    - An impact level (determines ceiling and safety requirements)
    - Optional hypothesis link
    - Optional what_if_wrong (required for high-impact claims before ceiling lifts)

- The `create` command enforces safety requirements interactively:
    - If impact_level is 'high', the analyst is prompted for what_if_wrong
      and warned that a counter-claim will be required.
    - The system never silently accepts an incomplete high-impact claim.

- The `confidence` command runs the engine and prints the full breakdown.
  It also persists the result to the database.

- The `link` command connects evidence to claims with an explicit relationship
  (supports / undermines / neutral). This forces the analyst to characterize
  the evidence, not just accumulate it.

- The `challenge` command adds counter-claims. For high-impact claims, the
  system warns if none exist.

- The `override` command allows the analyst to force confidence above the
  ceiling. It is never blocked outright — but it requires a structured
  justification and is permanently logged. The system trusts the analyst
  but holds them accountable.
"""

import sys
import uuid
import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit
import output as out
from models import utc_now, IMPACT_CEILINGS, IMPACT_CEILING_RATIONALE


def register(cli):
    @cli.group("claim")
    def claim_group():
        """Manage claims — evidenced assertions with transparent confidence scores."""
        pass

    @claim_group.command("create")
    @click.option("--hypothesis", "-h", default=None, help="Hypothesis ID prefix to link this claim to")
    def claim_create(hypothesis: str):
        """
        Create a new claim in the active investigation.

        A claim is a specific, evidenced assertion. Unlike a hypothesis, a claim
        is concrete enough to be directly supported or refuted by evidence.

        High-impact claims (legal / physical harm potential) require:
          - A 'what if I'm wrong?' section (entered during creation)
          - At least one counter-claim (via: judgeman claim challenge <id>)
          - Confidence ceiling of 0.75 (effective 0.60 until requirements met)

        The system never says "this is true". Confidence reflects your
        justification structure, not the truth of the claim.
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        out.header("Create claim", f"Investigation: {inv['name']}")
        out.info("A claim is a specific, falsifiable assertion linked to evidence.")
        click.echo()

        # Resolve hypothesis if provided
        hyp_row = None
        if hypothesis:
            hyp_row = conn.execute(
                "SELECT * FROM hypotheses WHERE id LIKE ? AND investigation_id = ?",
                (hypothesis + "%", inv["id"]),
            ).fetchone()
            if not hyp_row:
                out.warn(f"Hypothesis not found: {hypothesis}. Claim will not be linked.")

        statement = out.prompt_required("Claim statement")

        click.echo()
        click.echo(out.cyan("  Impact levels:"))
        click.echo(f"    {out.bold('low'):<8}  {out.dim('Exploratory / academic. Ceiling: 100%')}")
        click.echo(f"    {out.bold('medium'):<8}  {out.dim('Reputational consequences. Ceiling: 85%')}")
        click.echo(f"    {out.yellow(out.bold('high')):<8}  {out.dim('Legal / physical harm potential. Ceiling: 75% (effective 60% until safety requirements met)')}")
        impact_level = out.prompt_choice("\nImpact level", ["low", "medium", "high"])

        out.info(f"Ceiling for '{impact_level}' impact: {IMPACT_CEILINGS[impact_level]:.0%}")
        out.info(f"Reason: {IMPACT_CEILING_RATIONALE[impact_level]}")
        click.echo()

        out.info("Base confidence: your starting point based on current evidence and judgment.")
        out.info("The engine will adjust this using source credibility, corroboration, and counter-claims.")
        base_confidence = out.prompt_float("Base confidence (0.0–1.0)")

        click.echo()
        rationale = out.prompt_required("Rationale for this base confidence")

        # High-impact safety requirements
        what_if_wrong = None
        if impact_level == "high":
            click.echo()
            out.warn("HIGH-IMPACT CLAIM — safety requirements apply.")
            out.warn("You must consider failure modes before proceeding.")
            click.echo(out.red("  The system will not raise confidence above 60% until:"))
            click.echo(out.red("    ✗ 'What if I'm wrong?' section is provided (now)"))
            click.echo(out.red("    ✗ At least one counter-claim is registered (via: judgeman claim challenge)"))
            click.echo()
            out.info("What if I'm wrong? Consider: alternative explanations, missing context,")
            out.info("  source bias, identification errors, temporal gaps.")
            what_if_wrong = out.prompt_required("What if I'm wrong? (alternate explanations and failure modes)")
        elif impact_level == "medium":
            click.echo()
            out.warn("Medium-impact claim: confidence ceiling is 85%.")
            click.echo()

        claim_id = str(uuid.uuid4())
        now = utc_now()

        with conn:
            conn.execute(
                """INSERT INTO claims
                   (id, investigation_id, hypothesis_id, statement, base_confidence,
                    rationale, what_if_wrong, impact_level, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (claim_id, inv["id"],
                 hyp_row["id"] if hyp_row else None,
                 statement, base_confidence, rationale,
                 what_if_wrong, impact_level, now, now),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.CREATE_CLAIM,
            entity_type="claim",
            entity_id=claim_id,
            investigation_id=inv["id"],
            new_value={
                "statement": statement,
                "base_confidence": base_confidence,
                "impact_level": impact_level,
                "hypothesis_id": hyp_row["id"] if hyp_row else None,
            },
        )

        out.success("Claim created.")
        out.entity_id("ID", claim_id)
        if hyp_row:
            out.info(f"Linked to hypothesis: {hyp_row['statement'][:60]}")
        out.info("Next steps:")
        click.echo(f"    judgeman claim link {claim_id[:8]}… <evidence_id> supports")
        if impact_level == "high":
            click.echo(out.red(f"    judgeman claim challenge {claim_id[:8]}…  ← required for high-impact"))
        click.echo(f"    judgeman claim confidence {claim_id[:8]}…")
        conn.close()

    @claim_group.command("list")
    @click.option("--hypothesis", "-h", default=None, help="Filter by hypothesis ID prefix")
    def claim_list(hypothesis: str):
        """List claims in the active investigation."""
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        query = """
            SELECT c.*,
                   COUNT(DISTINCT ec.evidence_id) as evidence_count,
                   COUNT(DISTINCT cc.id) as counter_count,
                   SUM(CASE WHEN cc.addressed = 0 THEN 1 ELSE 0 END) as unaddressed_count
            FROM claims c
            LEFT JOIN evidence_claims ec ON ec.claim_id = c.id
            LEFT JOIN counter_claims cc ON cc.claim_id = c.id
            WHERE c.investigation_id = ?
        """
        params = [inv["id"]]
        if hypothesis:
            query += " AND c.hypothesis_id LIKE ?"
            params.append(hypothesis + "%")
        query += " GROUP BY c.id ORDER BY c.created_at"

        rows = conn.execute(query, params).fetchall()
        out.header("Claims", f"Investigation: {inv['name']}")
        if not rows:
            out.info("No claims yet.  Run: judgeman claim create")
            return

        for r in rows:
            click.echo()
            impact_color = (out.red if r["impact_level"] == "high"
                            else out.yellow if r["impact_level"] == "medium"
                            else out.dim)
            conf = r["final_confidence"]
            conf_str = f"{conf:.0%}" if conf is not None else out.dim("(not calculated)")

            click.echo(f"  {out.cyan(r['id'][:8])}…  [{impact_color(r['impact_level'])}]  confidence: {conf_str}")
            click.echo(f"    {r['statement'][:70]}")
            out.info(f"evidence: {r['evidence_count']}  counter-claims: {r['counter_count']}"
                     + (out.yellow(f"  ({r['unaddressed_count']} unaddressed)") if r["unaddressed_count"] else ""))

            # Warn about high-impact incomplete claims
            if r["impact_level"] == "high":
                if not r["counter_count"]:
                    out.warn("No counter-claim registered (required for high-impact)")
                if not r["what_if_wrong"]:
                    out.warn("'What if I'm wrong?' not provided")
        conn.close()

    @claim_group.command("show")
    @click.argument("claim_id")
    def claim_show(claim_id: str):
        """Show full details for a claim including confidence breakdown."""
        from db import get_connection, init_db
        from confidence import calculate_confidence
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        row = conn.execute(
            "SELECT * FROM claims WHERE id LIKE ? AND investigation_id = ?",
            (claim_id + "%", inv["id"]),
        ).fetchone()
        if not row:
            out.error(f"Claim not found: {claim_id}")
            sys.exit(1)

        claim = dict(row)
        out.header(f"Claim: {claim['statement'][:60]}…" if len(claim["statement"]) > 60 else f"Claim: {claim['statement']}")

        impact_color = (out.red if claim["impact_level"] == "high"
                        else out.yellow if claim["impact_level"] == "medium"
                        else None)
        out.field("ID", claim["id"])
        out.field("Impact level", claim["impact_level"], color=impact_color)
        out.multiline_field("Rationale", claim["rationale"])
        if claim["what_if_wrong"]:
            out.section("What if I'm wrong?")
            click.echo(f"    {claim['what_if_wrong']}")
        elif claim["impact_level"] == "high":
            out.warn("'What if I'm wrong?' section is MISSING (required for high-impact claims)")

        # Evidence links
        ev_rows = conn.execute(
            """SELECT e.description, e.id, ec.relationship, s.name as source_name, s.credibility_score
               FROM evidence_claims ec
               JOIN evidence e ON e.id = ec.evidence_id
               JOIN sources s ON s.id = e.source_id
               WHERE ec.claim_id = ?""",
            (claim["id"],),
        ).fetchall()

        if ev_rows:
            out.section("Linked evidence")
            for e in ev_rows:
                rel_color = out.green if e["relationship"] == "supports" else out.red if e["relationship"] == "undermines" else out.dim
                cred_color = out.green if e["credibility_score"] >= 0.7 else out.yellow if e["credibility_score"] >= 0.4 else out.red
                click.echo(f"  [{rel_color(e['relationship'][:3])}] {e['description'][:60]}")
                cred_pct = f"{e['credibility_score']:.2f}"
                out.info(f"source: {e['source_name']}  cred: {cred_color(cred_pct)}")

        # Counter-claims
        cc_rows = conn.execute(
            "SELECT * FROM counter_claims WHERE claim_id = ? ORDER BY created_at",
            (claim["id"],),
        ).fetchall()

        if cc_rows:
            out.section("Counter-claims")
            for cc in cc_rows:
                status = out.green("addressed") if cc["addressed"] else out.red("OPEN")
                click.echo(f"  [{status}] {out.cyan(cc['id'][:8])}… {cc['statement'][:60]}")
                if cc["address_rationale"]:
                    out.info(f"addressed: {cc['address_rationale'][:80]}")
        elif claim["impact_level"] == "high":
            out.warn("No counter-claims registered. REQUIRED for high-impact claims.")
            out.info(f"  judgeman claim challenge {claim['id'][:8]}…")

        # Confidence breakdown
        bd = calculate_confidence(claim["id"], conn)
        out.confidence_breakdown(bd)
        conn.close()

    @claim_group.command("link")
    @click.argument("claim_id")
    @click.argument("evidence_id")
    @click.argument("relationship", type=click.Choice(["supports", "undermines", "neutral"]))
    @click.option("--note", "-n", default=None, help="Relevance note explaining the link")
    def claim_link(claim_id: str, evidence_id: str, relationship: str, note: str):
        """
        Link evidence to a claim with an explicit relationship.

        RELATIONSHIP must be one of:
          supports   — evidence increases plausibility of the claim
          undermines — evidence decreases plausibility of the claim
          neutral    — evidence is contextually relevant but directionally ambiguous

        Use 'neutral' sparingly — most evidence has a direction. If you're
        unsure, that uncertainty belongs in the relevance note, not in
        choosing 'neutral'.
        """
        from db import get_connection, init_db
        from confidence import calculate_confidence
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        claim_row = conn.execute(
            "SELECT * FROM claims WHERE id LIKE ? AND investigation_id = ?",
            (claim_id + "%", inv["id"]),
        ).fetchone()
        if not claim_row:
            out.error(f"Claim not found: {claim_id}")
            sys.exit(1)

        ev_row = conn.execute(
            "SELECT * FROM evidence WHERE id LIKE ? AND investigation_id = ?",
            (evidence_id + "%", inv["id"]),
        ).fetchone()
        if not ev_row:
            out.error(f"Evidence not found: {evidence_id}")
            sys.exit(1)

        # Check for duplicate
        existing = conn.execute(
            "SELECT * FROM evidence_claims WHERE evidence_id = ? AND claim_id = ?",
            (ev_row["id"], claim_row["id"]),
        ).fetchone()
        if existing:
            out.warn(f"Evidence already linked to this claim as '{existing['relationship']}'.")
            if not click.confirm("Update the relationship?"):
                return
            with conn:
                conn.execute(
                    "UPDATE evidence_claims SET relationship=?, relevance_note=?, linked_at=? "
                    "WHERE evidence_id=? AND claim_id=?",
                    (relationship, note, utc_now(), ev_row["id"], claim_row["id"]),
                )
        else:
            if not note:
                out.info("Relevance note: explain why this evidence " + relationship + " the claim.")
                note = click.prompt("Relevance note (optional)", default="", show_default=False).strip() or None

            with conn:
                conn.execute(
                    "INSERT INTO evidence_claims (evidence_id, claim_id, relationship, relevance_note, linked_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ev_row["id"], claim_row["id"], relationship, note, utc_now()),
                )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.LINK_EVIDENCE,
            entity_type="evidence_claim",
            entity_id=ev_row["id"],
            investigation_id=inv["id"],
            new_value={
                "claim_id": claim_row["id"],
                "evidence_id": ev_row["id"],
                "relationship": relationship,
            },
        )

        rel_color = out.green if relationship == "supports" else out.red if relationship == "undermines" else out.dim
        out.success(f"Evidence linked as: {rel_color(relationship)}")
        out.info(f"Claim: {claim_row['statement'][:55]}")
        out.info(f"Evidence: {ev_row['description'][:55]}")
        out.info(f"Recalculate:  judgeman claim confidence {claim_row['id'][:8]}…")
        conn.close()

    @claim_group.command("challenge")
    @click.argument("claim_id")
    @click.option("--statement", "-s", default=None, help="Counter-claim statement")
    def claim_challenge(claim_id: str, statement: str):
        """
        Add a counter-claim challenging an assertion.

        Counter-claims represent alternative explanations or evidence that
        challenges the claim. They are not accusations of error — they are
        the analyst's structured acknowledgment of uncertainty.

        An unaddressed counter-claim applies a -0.10 confidence penalty.
        Once addressed (with rationale), the penalty is removed.

        High-impact claims REQUIRE at least one counter-claim before
        confidence can exceed 60%.
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        claim_row = conn.execute(
            "SELECT * FROM claims WHERE id LIKE ? AND investigation_id = ?",
            (claim_id + "%", inv["id"]),
        ).fetchone()
        if not claim_row:
            out.error(f"Claim not found: {claim_id}")
            sys.exit(1)

        out.header("Add counter-claim", f"Claim: {claim_row['statement'][:55]}")
        out.info("A counter-claim is an alternative explanation that challenges this claim.")
        out.info("It applies a -0.10 confidence penalty until addressed with a rationale.")
        click.echo()

        statement = statement or out.prompt_required("Counter-claim statement")

        cc_id = str(uuid.uuid4())
        now = utc_now()

        with conn:
            conn.execute(
                "INSERT INTO counter_claims (id, claim_id, statement, addressed, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (cc_id, claim_row["id"], statement, now, now),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.ADD_COUNTER_CLAIM,
            entity_type="counter_claim",
            entity_id=cc_id,
            investigation_id=inv["id"],
            new_value={"claim_id": claim_row["id"], "statement": statement},
        )

        out.success("Counter-claim registered.")
        out.entity_id("ID", cc_id)
        out.warn("Confidence penalty: -0.10 until this counter-claim is addressed.")
        out.info(f"Address it:  judgeman claim address {cc_id[:8]}…")
        conn.close()

    @claim_group.command("address")
    @click.argument("counterclaim_id")
    def claim_address(counterclaim_id: str):
        """
        Address a counter-claim with a rationale.

        Addressing a counter-claim does not dismiss it — it means the analyst
        has actively engaged with the challenge and can explain why the claim
        stands despite it. This removes the -0.10 confidence penalty.

        The rationale is required and permanently logged. A weak rationale
        ("it's fine", "not applicable") does not strengthen the claim —
        peers reviewing the audit trail will see it.
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        cc_row = conn.execute(
            """SELECT cc.*, c.statement as claim_statement, c.investigation_id
               FROM counter_claims cc JOIN claims c ON cc.claim_id = c.id
               WHERE cc.id LIKE ?""",
            (counterclaim_id + "%",),
        ).fetchone()
        if not cc_row:
            out.error(f"Counter-claim not found: {counterclaim_id}")
            sys.exit(1)
        if cc_row["investigation_id"] != inv["id"]:
            out.error("Counter-claim does not belong to the active investigation.")
            sys.exit(1)
        if cc_row["addressed"]:
            out.warn("This counter-claim is already addressed.")
            out.info(f"Rationale: {cc_row['address_rationale']}")
            return

        out.header("Address counter-claim")
        click.echo(f"  Counter-claim: {cc_row['statement']}")
        click.echo(f"  Claim:         {cc_row['claim_statement'][:60]}")
        click.echo()
        out.info("Provide a substantive rationale explaining why this challenge does not")
        out.info("undermine the claim. This is permanently logged in the audit trail.")
        out.warn("Peers reviewing this investigation will evaluate the quality of your rationale.")
        click.echo()

        rationale = out.prompt_required("Address rationale")

        now = utc_now()
        with conn:
            conn.execute(
                "UPDATE counter_claims SET addressed=1, address_rationale=?, updated_at=? WHERE id=?",
                (rationale, now, cc_row["id"]),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.ADDRESS_COUNTER_CLAIM,
            entity_type="counter_claim",
            entity_id=cc_row["id"],
            investigation_id=inv["id"],
            old_value={"addressed": False},
            new_value={"addressed": True, "rationale": rationale},
            justification=rationale,
        )

        out.success("Counter-claim addressed. Confidence penalty removed.")
        out.info(f"Recalculate:  judgeman claim confidence {cc_row['claim_id'][:8]}…")
        conn.close()

    @claim_group.command("confidence")
    @click.argument("claim_id")
    def claim_confidence(claim_id: str):
        """
        Calculate and display the confidence breakdown for a claim.

        This runs the rule-based confidence engine and persists the result.
        The breakdown shows every contributing factor with its value and
        explanation. Nothing is hidden.
        """
        from db import get_connection, init_db
        from confidence import calculate_confidence
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        row = conn.execute(
            "SELECT * FROM claims WHERE id LIKE ? AND investigation_id = ?",
            (claim_id + "%", inv["id"]),
        ).fetchone()
        if not row:
            out.error(f"Claim not found: {claim_id}")
            sys.exit(1)

        bd = calculate_confidence(row["id"], conn)

        # Persist
        old_conf = row["final_confidence"]
        with conn:
            conn.execute(
                "UPDATE claims SET final_confidence=?, updated_at=? WHERE id=?",
                (bd.final_confidence, utc_now(), row["id"]),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.UPDATE_CONFIDENCE,
            entity_type="claim",
            entity_id=row["id"],
            investigation_id=inv["id"],
            old_value={"final_confidence": old_conf},
            new_value={"final_confidence": bd.final_confidence},
        )

        out.confidence_breakdown(bd)
        conn.close()

    @claim_group.command("override")
    @click.argument("claim_id")
    def claim_override(claim_id: str):
        """
        Request a confidence override above the impact ceiling.

        This is not blocked — the system trusts analyst judgment.
        But it is never silent. You must:
          1. Acknowledge the ceiling and its reason
          2. Provide a structured justification
          3. Accept that this override is permanently logged

        The override appears in all reports with a clear marker.
        Reviewers will see it. Use it only when you have substantive
        grounds — multiple independent high-credibility sources,
        fully addressed counter-claims, and explicit reasoning.
        """
        from db import get_connection, init_db
        from confidence import calculate_confidence, check_ceiling_violation
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        row = conn.execute(
            "SELECT * FROM claims WHERE id LIKE ? AND investigation_id = ?",
            (claim_id + "%", inv["id"]),
        ).fetchone()
        if not row:
            out.error(f"Claim not found: {claim_id}")
            sys.exit(1)

        claim = dict(row)
        ceiling = IMPACT_CEILINGS[claim["impact_level"]]
        bd = calculate_confidence(claim["id"], conn)

        out.header("Confidence override", f"Claim: {claim['statement'][:55]}")
        click.echo()
        out.warn(f"Current confidence: {bd.final_confidence:.0%}")
        out.warn(f"Ceiling ({claim['impact_level']} impact): {ceiling:.0%}")
        click.echo()
        out.info(IMPACT_CEILING_RATIONALE[claim["impact_level"]])
        click.echo()
        out.warn("Overrides are permanently logged in the audit trail.")
        out.warn("This override will be visible to all reviewers of this investigation.")
        click.echo()

        if not click.confirm(out.yellow("  Proceed with override request?")):
            out.info("Override cancelled.")
            return

        click.echo()
        out.info("For an override to be substantively justified, it should reference:")
        out.info("  - Multiple independent high-credibility sources")
        out.info("  - All counter-claims addressed with substantive rationale")
        out.info("  - Why the ceiling is insufficient in this specific case")
        click.echo()

        proposed = out.prompt_float(f"Proposed confidence (current ceiling: {ceiling:.0%})", 0.0, 1.0)
        if proposed <= bd.final_confidence:
            out.info(f"Proposed value ({proposed:.0%}) is not above current confidence. No override needed.")
            return

        justification = out.prompt_required("Structured justification for this override")

        # Check whether the proposed value exceeds ceiling
        cc_row = conn.execute(
            "SELECT COUNT(*) FROM counter_claims WHERE claim_id=?", (claim["id"],)
        ).fetchone()[0]
        is_v, reason = check_ceiling_violation(proposed, claim["impact_level"], cc_row > 0, bool(claim["what_if_wrong"]))

        if is_v:
            out.warn(f"This override exceeds safety ceiling: {reason}")
            if not click.confirm(out.red("  Override ceiling anyway? (permanently logged)")):
                out.info("Override cancelled.")
                return

        old_override = claim.get("override_confidence")
        with conn:
            conn.execute(
                "UPDATE claims SET override_confidence=?, override_justification=?, updated_at=? WHERE id=?",
                (proposed, justification, utc_now(), claim["id"]),
            )

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.OVERRIDE_CEILING,
            entity_type="claim",
            entity_id=claim["id"],
            investigation_id=inv["id"],
            old_value={"override_confidence": old_override},
            new_value={"override_confidence": proposed, "justification": justification},
            justification=justification,
        )

        out.success(f"Override recorded: {proposed:.0%}")
        out.warn("This override is now visible in all confidence breakdowns and reports.")
        conn.close()
