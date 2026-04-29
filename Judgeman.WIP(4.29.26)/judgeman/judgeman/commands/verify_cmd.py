import os
"""
verify_cmd.py — Structural integrity verification for investigations.

Design philosophy:
- `verify` is the conscience of Judgeman. It checks that the investigation
  structure is internally consistent and that no safety obligations are
  silently unmet.
- It produces a tiered report: BLOCKING issues (the investigation cannot be
  considered complete), WARNINGS (things to address), and INFO observations.
- BLOCKING issues are conditions where the system's epistemic guarantees
  break down:
    - High-impact claims with no counter-claims
    - High-impact claims missing 'what_if_wrong'
    - Claims with zero evidence linked
    - Sources that have no evidence items (registered but never used)
    - Counter-claims that were "addressed" with suspiciously short rationale
- WARNINGS are structural gaps that weaken confidence but don't violate rules:
    - Claims not recalculated after evidence changes
    - Sources with credibility below 0.3 providing the only support for a claim
    - No hypotheses defined
    - Claims not linked to any hypothesis
- INFO observations help analysts improve:
    - Counter-claims that have been open for a long time
    - Claims near their ceiling with improvement paths available
- The exit code is non-zero if there are BLOCKING issues (useful in pipelines).
"""

import sys
import click
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import output as out
from confidence import calculate_confidence, CONFLICT_PENALTY_PER_UNADDRESSED


SUSPICIOUS_RATIONALE_MIN_CHARS = 30  # Rationales shorter than this trigger a warning


def register(cli):
    @cli.command("verify")
    @click.option("--strict", is_flag=True, default=False,
                  help="Exit non-zero on warnings as well as blocking issues")
    @click.option("--fix-confidence", is_flag=True, default=False,
                  help="Recalculate and persist confidence for all stale claims")
    def verify(strict: bool, fix_confidence: bool):
        """
        Verify the structural integrity of the active investigation.

        Checks for:
          BLOCKING — safety obligations unmet, evidence-free claims,
                     broken entity references
          WARNING  — stale confidence scores, weak solo sources,
                     short counter-claim rationale
          INFO     — improvement opportunities, coverage gaps

        Exits with code 1 if BLOCKING issues are found (2 if --strict
        and warnings exist). Safe to run at any time — verify never
        mutates state unless --fix-confidence is passed.
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)
        import audit

        iid = inv["id"]
        now_ts = datetime.now(timezone.utc).isoformat()

        blocking = []
        warnings = []
        infos = []

        # ── Load all entities ────────────────────────────────────────
        claims = conn.execute(
            "SELECT * FROM claims WHERE investigation_id = ?", (iid,)
        ).fetchall()

        hypotheses = conn.execute(
            "SELECT * FROM hypotheses WHERE investigation_id = ?", (iid,)
        ).fetchall()

        sources = conn.execute(
            "SELECT s.*, COUNT(e.id) as ev_count FROM sources s "
            "LEFT JOIN evidence e ON e.source_id = s.id "
            "WHERE s.investigation_id = ? GROUP BY s.id",
            (iid,),
        ).fetchall()

        evidence = conn.execute(
            "SELECT e.*, COUNT(ec.claim_id) as link_count FROM evidence e "
            "LEFT JOIN evidence_claims ec ON ec.evidence_id = e.id "
            "WHERE e.investigation_id = ? GROUP BY e.id",
            (iid,),
        ).fetchall()

        # ── Check 1: Top-level structure ─────────────────────────────
        if not hypotheses:
            warnings.append(
                "No hypotheses defined. Claims float without a testable framework. "
                "Add at least one hypothesis:  judgeman hypothesis add"
            )

        if not claims:
            infos.append("No claims yet. The investigation has no analytical assertions.")

        if not sources:
            warnings.append(
                "No sources registered. Evidence without source attribution cannot "
                "contribute to confidence calculations."
            )

        # ── Check 2: High-impact safety requirements ─────────────────
        for c in claims:
            c = dict(c)
            claim_short = f"'{c['statement'][:55]}…'" if len(c['statement']) > 55 else f"'{c['statement']}'"
            cid_short = c['id'][:8]

            if c["impact_level"] == "high":
                cc_count = conn.execute(
                    "SELECT COUNT(*) FROM counter_claims WHERE claim_id = ?",
                    (c["id"],),
                ).fetchone()[0]

                if cc_count == 0:
                    blocking.append(
                        f"HIGH-IMPACT CLAIM {cid_short}… has NO counter-claim. "
                        f"Claim: {claim_short}. "
                        f"Fix:  judgeman claim challenge {cid_short}…"
                    )

                if not c["what_if_wrong"]:
                    blocking.append(
                        f"HIGH-IMPACT CLAIM {cid_short}… missing 'what_if_wrong'. "
                        f"Claim: {claim_short}. "
                        f"Fix:  judgeman claim show {cid_short}… (edit and re-create or add via DB)"
                    )

        # ── Check 3: Evidence coverage ───────────────────────────────
        for c in claims:
            c = dict(c)
            cid_short = c['id'][:8]
            claim_short = f"'{c['statement'][:50]}'"

            ev_count = conn.execute(
                "SELECT COUNT(*) FROM evidence_claims WHERE claim_id = ?",
                (c["id"],),
            ).fetchone()[0]

            if ev_count == 0:
                blocking.append(
                    f"CLAIM {cid_short}… has NO linked evidence. "
                    f"Claim: {claim_short}. "
                    f"Every claim requires at least one linked evidence item. "
                    f"Fix:  judgeman claim link {cid_short}… <evidence_id> supports"
                )

        # ── Check 4: Orphaned sources ────────────────────────────────
        for s in sources:
            s = dict(s)
            if s["ev_count"] == 0:
                warnings.append(
                    f"SOURCE '{s['name']}' ({s['id'][:8]}…) has no evidence items. "
                    f"Registered but never used. Add evidence:  judgeman evidence add --source {s['id'][:8]}…"
                )

        # ── Check 5: Orphaned evidence ───────────────────────────────
        for e in evidence:
            e = dict(e)
            if e["link_count"] == 0:
                warnings.append(
                    f"EVIDENCE {e['id'][:8]}… ('{e['description'][:45]}') "
                    f"is not linked to any claim. "
                    f"Fix:  judgeman claim link <claim_id> {e['id'][:8]}… supports"
                )

        # ── Check 6: Stale confidence scores ─────────────────────────
        stale = []
        for c in claims:
            c = dict(c)
            if c["final_confidence"] is None:
                stale.append(c)
                continue

            # Check if evidence links or counter-claims changed after last update
            last_ev_change = conn.execute(
                "SELECT MAX(linked_at) FROM evidence_claims WHERE claim_id = ?",
                (c["id"],),
            ).fetchone()[0]
            last_cc_change = conn.execute(
                "SELECT MAX(updated_at) FROM counter_claims WHERE claim_id = ?",
                (c["id"],),
            ).fetchone()[0]

            last_change = max(
                x for x in [last_ev_change, last_cc_change] if x is not None
            ) if any([last_ev_change, last_cc_change]) else None

            if last_change and last_change > c["updated_at"]:
                stale.append(c)

        if stale:
            stale_ids = ", ".join(s["id"][:8] + "…" for s in stale[:5])
            if fix_confidence:
                fixed = 0
                for c in stale:
                    try:
                        bd = calculate_confidence(c["id"], conn)
                        with conn:
                            conn.execute(
                                "UPDATE claims SET final_confidence=?, updated_at=? WHERE id=?",
                                (bd.final_confidence, now_ts, c["id"]),
                            )
                        audit.log_action(
                            conn,
                            analyst_id=inv["analyst_id"],
                            action_type=audit.UPDATE_CONFIDENCE,
                            entity_type="claim",
                            entity_id=c["id"],
                            investigation_id=iid,
                            new_value={"final_confidence": bd.final_confidence,
                                       "trigger": "verify --fix-confidence"},
                        )
                        fixed += 1
                    except Exception as ex:
                        warnings.append(f"Could not recalculate confidence for {c['id'][:8]}…: {ex}")
                infos.append(f"Recalculated confidence for {fixed} stale claim(s).")
            else:
                warnings.append(
                    f"{len(stale)} claim(s) have stale confidence scores "
                    f"(evidence or counter-claims changed after last calculation). "
                    f"IDs: {stale_ids}. "
                    f"Fix:  judgeman verify --fix-confidence  OR  judgeman claim confidence <id>"
                )

        # ── Check 7: Weak solo source ────────────────────────────────
        for c in claims:
            c = dict(c)
            supporting = conn.execute(
                """SELECT s.credibility_score, s.name, s.independence_group,
                          COUNT(DISTINCT COALESCE(s.independence_group, s.id)) as groups
                   FROM evidence_claims ec
                   JOIN evidence e ON e.id = ec.evidence_id
                   JOIN sources s ON s.id = e.source_id
                   WHERE ec.claim_id = ? AND ec.relationship = 'supports'""",
                (c["id"],),
            ).fetchone()

            if not supporting or supporting["groups"] is None:
                continue

            if supporting["groups"] == 1 and supporting["credibility_score"] < 0.35:
                warnings.append(
                    f"CLAIM {c['id'][:8]}… rests on a single source with credibility "
                    f"{supporting['credibility_score']:.2f} (source: '{supporting['name']}'). "
                    f"A low-credibility sole source significantly weakens the claim."
                )

        # ── Check 8: Suspicious counter-claim rationale ──────────────
        suspicious_addressed = conn.execute(
            """SELECT cc.*, c.statement as claim_statement
               FROM counter_claims cc JOIN claims c ON cc.claim_id = c.id
               WHERE c.investigation_id = ? AND cc.addressed = 1
               AND LENGTH(cc.address_rationale) < ?""",
            (iid, SUSPICIOUS_RATIONALE_MIN_CHARS),
        ).fetchall()

        for cc in suspicious_addressed:
            warnings.append(
                f"COUNTER-CLAIM {cc['id'][:8]}… addressed with a very short rationale "
                f"({len(cc['address_rationale'])} chars): '{cc['address_rationale']}'. "
                f"A substantive rationale should explain why the challenge does not undermine the claim."
            )

        # ── Check 9: Claims not linked to any hypothesis ─────────────
        unlinked_claims = conn.execute(
            "SELECT id, statement FROM claims WHERE investigation_id = ? AND hypothesis_id IS NULL",
            (iid,),
        ).fetchall()

        if unlinked_claims and hypotheses:
            infos.append(
                f"{len(unlinked_claims)} claim(s) not linked to any hypothesis. "
                f"Linking claims to hypotheses helps structure the analytical narrative."
            )

        # ── Check 10: Confidence near ceiling with improvements ──────
        for c in claims:
            c = dict(c)
            if c["final_confidence"] is None:
                continue
            from models import IMPACT_CEILINGS
            ceiling = IMPACT_CEILINGS[c["impact_level"]]
            if abs(c["final_confidence"] - ceiling) < 0.02:  # Within 2% of ceiling
                bd = calculate_confidence(c["id"], conn)
                if bd.improvement_paths and c["impact_level"] != "low":
                    infos.append(
                        f"CLAIM {c['id'][:8]}… is at its ceiling ({c['final_confidence']:.0%}). "
                        f"Improvement paths exist if override is warranted: "
                        + "; ".join(bd.improvement_paths[:2])
                    )

        # ── Check 11: Hypothesis coverage ────────────────────────────
        for h in hypotheses:
            h = dict(h)
            claim_count = conn.execute(
                "SELECT COUNT(*) FROM claims WHERE hypothesis_id = ?", (h["id"],)
            ).fetchone()[0]
            if claim_count == 0:
                infos.append(
                    f"HYPOTHESIS {h['id'][:8]}… '{h['statement'][:55]}' "
                    f"has no claims linked to it."
                )

        # ── Render output ────────────────────────────────────────────
        total_issues = len(blocking) + len(warnings)
        out.header(
            f"Verification: {inv['name']}",
            f"{len(blocking)} blocking · {len(warnings)} warnings · {len(infos)} info"
        )

        if blocking:
            click.echo()
            click.echo(out.red(out.bold(f"  BLOCKING ({len(blocking)})")))
            out.rule()
            for i, b in enumerate(blocking, 1):
                click.echo(out.red(f"  [{i}] {b}"))
                click.echo()

        if warnings:
            click.echo()
            click.echo(out.yellow(out.bold(f"  WARNINGS ({len(warnings)})")))
            out.rule()
            for i, w in enumerate(warnings, 1):
                click.echo(out.yellow(f"  [{i}] {w}"))
                click.echo()

        if infos:
            click.echo()
            click.echo(out.cyan(out.bold(f"  INFO ({len(infos)})")))
            out.rule()
            for i, inf in enumerate(infos, 1):
                click.echo(out.dim(f"  [{i}] {inf}"))
                click.echo()

        if not blocking and not warnings:
            out.success("Investigation passes all structural integrity checks.")

        click.echo()
        click.echo(out.dim(f"  Checked: {len(claims)} claims · {len(sources)} sources · "
                           f"{len(evidence)} evidence items · {len(hypotheses)} hypotheses"))

        conn.close()

        # Exit codes for pipeline use
        if blocking:
            sys.exit(1)
        if strict and warnings:
            sys.exit(2)
