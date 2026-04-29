"""
export_cmd.py — Investigation export with tamper-evident audit chain.

Export format: a zip archive containing:
  manifest.json       — metadata, entity counts, file hashes, audit chain hash
  investigation.json  — all entities serialized as structured JSON
  report.md           — the human-readable investigation report
  audit.json          — the complete audit trail (separate for easy inspection)

Design decisions:
- The zip is not encrypted. Encryption is the operator's responsibility
  (gpg, age, etc). Judgeman handles integrity, not confidentiality.
- The manifest is always the first file inspected on import. Its hash
  of the other files means the manifest itself cannot be silently swapped.
- The audit chain hash covers ALL audit entries, including the GENERATE_EXPORT
  action logged at export time — so the chain hash in the manifest covers
  its own creation event.
- File hashes in the manifest use SHA256 of the raw file bytes. This catches
  modifications to investigation.json or report.md independently of the
  audit chain.
- The export filename encodes the investigation name and timestamp so
  analysts can manage multiple exports without confusion.
"""

import sys
import os
import json
import hashlib
import zipfile
import click
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit as audit_mod
import output as out
from models import utc_now
from chainhash import compute_audit_chain_hash

JUDGEMAN_EXPORT_VERSION = "1"


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _serialize_investigation(conn, inv_id: str) -> dict:
    """Pull all entities for an investigation into a serializable dict."""
    def rows(sql, *params):
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    return {
        "investigation": dict(conn.execute(
            "SELECT * FROM investigations WHERE id = ?", (inv_id,)
        ).fetchone()),
        "hypotheses":    rows("SELECT * FROM hypotheses WHERE investigation_id = ?", inv_id),
        "sources":       rows("SELECT * FROM sources WHERE investigation_id = ?", inv_id),
        "evidence":      rows("SELECT * FROM evidence WHERE investigation_id = ?", inv_id),
        "claims":        rows("SELECT * FROM claims WHERE investigation_id = ?", inv_id),
        "evidence_claims": rows(
            "SELECT ec.* FROM evidence_claims ec "
            "JOIN claims c ON c.id = ec.claim_id WHERE c.investigation_id = ?", inv_id
        ),
        "counter_claims": rows(
            "SELECT cc.* FROM counter_claims cc "
            "JOIN claims c ON c.id = cc.claim_id WHERE c.investigation_id = ?", inv_id
        ),
    }


def _generate_report_text(conn, inv) -> str:
    """Generate the report text for embedding in the export."""
    from commands.report_cmd import _build_report_lines
    lines = _build_report_lines(conn, inv)
    return "\n".join(lines)


def register(cli):
    @cli.command("export")
    @click.option("--output-dir", "-o", default=".", help="Directory to write the export zip")
    @click.option("--note", "-n", default=None, help="Optional export note (e.g. recipient, purpose)")
    def export_investigation(output_dir: str, note: str):
        """
        Export the active investigation as a portable, tamper-evident zip bundle.

        The bundle contains the full investigation data, audit trail, and
        a generated report. A chain hash of the audit trail is embedded in
        the manifest — recipients can verify the audit trail was not modified
        after export.

        The export does not encrypt the data. Use gpg or age to encrypt
        the zip before transmission if confidentiality is required.
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        iid = inv["id"]
        analyst_id = inv["analyst_id"]
        now = utc_now()

        out.header(f"Export: {inv['name']}", f"Analyst: {analyst_id}")

        # ── Step 1: Serialize all investigation data ─────────────────
        out.info("Serializing investigation data…")
        data = _serialize_investigation(conn, iid)

        entity_counts = {
            "hypotheses":    len(data["hypotheses"]),
            "sources":       len(data["sources"]),
            "evidence":      len(data["evidence"]),
            "claims":        len(data["claims"]),
            "evidence_claims": len(data["evidence_claims"]),
            "counter_claims": len(data["counter_claims"]),
        }

        # ── Step 2: Serialize audit trail ────────────────────────────
        out.info("Building audit trail…")
        audit_entries = [dict(r) for r in conn.execute(
            "SELECT * FROM analyst_actions WHERE investigation_id = ? ORDER BY timestamp ASC, id ASC",
            (iid,),
        ).fetchall()]

        # Log the export action BEFORE computing the chain hash,
        # so the export event itself is part of the tamper-evident chain
        export_action_id = audit_mod.log_action(
            conn,
            analyst_id=analyst_id,
            action_type="GENERATE_EXPORT",
            entity_type="investigation",
            entity_id=iid,
            investigation_id=iid,
            new_value={
                "export_note": note,
                "entity_counts": entity_counts,
            },
            justification=note,
        )

        # Re-fetch audit including the just-logged export action
        audit_entries = [dict(r) for r in conn.execute(
            "SELECT * FROM analyst_actions WHERE investigation_id = ? ORDER BY timestamp ASC, id ASC",
            (iid,),
        ).fetchall()]

        # ── Step 3: Compute chain hash ───────────────────────────────
        out.info("Computing audit chain hash…")
        chain_hash = compute_audit_chain_hash(audit_entries)

        # ── Step 4: Serialize to bytes ───────────────────────────────
        investigation_bytes = json.dumps(data, indent=2, default=str).encode("utf-8")
        audit_bytes = json.dumps(audit_entries, indent=2, default=str).encode("utf-8")

        # Generate report
        try:
            report_lines = _build_report_lines(conn, inv)
            report_text = "\n".join(report_lines)
        except Exception as e:
            report_text = f"# Report generation failed\n\n{e}"
        report_bytes = report_text.encode("utf-8")

        # ── Step 5: Build manifest ───────────────────────────────────
        manifest = {
            "judgeman_export_version": JUDGEMAN_EXPORT_VERSION,
            "exported_at": now,
            "exported_by": analyst_id,
            "export_note": note,
            "investigation_id": iid,
            "investigation_name": inv["name"],
            "investigation_status": inv["status"],
            "entity_counts": entity_counts,
            "audit_entry_count": len(audit_entries),
            "audit_chain_hash": chain_hash,
            "file_hashes": {
                "investigation.json": _sha256_bytes(investigation_bytes),
                "audit.json":         _sha256_bytes(audit_bytes),
                "report.md":          _sha256_bytes(report_bytes),
            },
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

        # ── Step 6: Write zip ────────────────────────────────────────
        safe_name = inv["name"].lower().replace(" ", "_")[:30]
        ts = now[:10].replace("-", "")
        zip_filename = f"judgeman_{safe_name}_{ts}.zip"
        zip_path = Path(output_dir) / zip_filename

        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json",      manifest_bytes.decode())
            zf.writestr("investigation.json", investigation_bytes.decode())
            zf.writestr("audit.json",         audit_bytes.decode())
            zf.writestr("report.md",          report_bytes.decode())

        # Verify the zip is readable
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            names = zf.namelist()

        size_kb = zip_path.stat().st_size // 1024

        out.success(f"Export written: {zip_path}")
        click.echo()
        out.field("Size", f"{size_kb} KB  ({zip_path.stat().st_size} bytes)")
        out.field("Files", ", ".join(names))
        out.field("Audit entries", str(len(audit_entries)))
        out.field("Chain hash", chain_hash[:32] + "…")
        click.echo()
        out.info("Recipient can verify integrity with:  jm import --verify-only <zipfile>")
        out.warn("This bundle is NOT encrypted. Use gpg/age before transmitting sensitive data.")

        conn.close()


def _build_report_lines(conn, inv) -> list[str]:
    """
    Shared report line builder used by both report_cmd and export_cmd.
    Extracted here to avoid circular imports.
    """
    from commands.report_cmd import REPORT_DISCLAIMER
    from confidence import calculate_confidence
    from models import IMPACT_CEILINGS, IMPACT_CEILING_RATIONALE
    from datetime import datetime, timezone

    iid = inv["id"]
    lines = []
    def w(s=""): lines.append(s)

    w(f"# Judgeman Investigation Report: {inv['name']}")
    w()
    w(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ")
    w(f"**Analyst:** {inv['analyst_id']}  ")
    w(f"**Investigation ID:** {inv['id']}  ")
    w(f"**Status:** {inv['status'].upper()}  ")
    w()
    w(REPORT_DISCLAIMER)
    w()

    hyps = conn.execute(
        "SELECT * FROM hypotheses WHERE investigation_id = ? ORDER BY created_at", (iid,)
    ).fetchall()

    if hyps:
        w("## Hypotheses")
        w()
        w("| # | Statement | Status |")
        w("|---|-----------|--------|")
        for i, h in enumerate(hyps, 1):
            w(f"| {i} | {h['statement']} | **{h['status'].upper()}** |")
        w()
        w("> Status reflects current weight of evidence as assessed by the analyst.")
        w("> 'SUPPORTED' or 'REJECTED' are not final conclusions.")
        w()

    claims = conn.execute(
        "SELECT * FROM claims WHERE investigation_id = ? ORDER BY impact_level DESC, created_at",
        (iid,),
    ).fetchall()

    if claims:
        w("## Claims")
        w()
        for claim in claims:
            claim = dict(claim)
            bd = calculate_confidence(claim["id"], conn)
            impact_tag = {"high": "🔴 HIGH IMPACT", "medium": "🟡 MEDIUM IMPACT",
                          "low": "⚪ LOW IMPACT"}[claim["impact_level"]]

            w(f"### {impact_tag}: {claim['statement']}")
            w()
            w(f"**Claim ID:** `{claim['id']}`  ")
            w(f"**Impact level:** {claim['impact_level'].upper()}  ")
            w()
            w("#### Confidence Assessment")
            w()
            conf_disp = bd.displayed_confidence()
            w(f"**Final confidence: {conf_disp:.0%}** (ceiling: {bd.ceiling:.0%})")
            w()

            if bd.override_active:
                w(f"> ⚠️ **ANALYST OVERRIDE ACTIVE**: confidence forced to {bd.override_confidence:.0%}")
                w(f"> Override justification: {bd.override_justification}")
                w()

            if bd.ceiling_applied:
                w(f"> ℹ️ Ceiling applied: {IMPACT_CEILING_RATIONALE[claim['impact_level']]}")
                w()

            w("| Factor | Delta | Explanation |")
            w("|--------|-------|-------------|")
            w(f"| Base confidence | {claim['base_confidence']:.3f} | {claim['rationale']} |")
            for f in bd.factors:
                sign = "+" if f.value > 0 else ""
                w(f"| {f.name.replace('_',' ')} | {sign}{f.value:.3f} | {f.explanation[:120]} |")
            w()

            if claim["impact_level"] == "high":
                w("#### High-Impact Safety Requirements")
                w()
                w(f"- Counter-claim registered: **{'✓' if bd.has_counter_claim else '✗ MISSING'}**")
                w(f"- 'What if I'm wrong?' provided: **{'✓' if bd.has_what_if_wrong else '✗ MISSING'}**")
                w()
                if claim["what_if_wrong"]:
                    w("#### What If I'm Wrong?")
                    w()
                    w(f"> {claim['what_if_wrong']}")
                    w()

            ev_rows = conn.execute(
                """SELECT e.description, ec.relationship, ec.relevance_note,
                          s.name as source_name, s.credibility_score, s.source_type
                   FROM evidence_claims ec
                   JOIN evidence e ON e.id = ec.evidence_id
                   JOIN sources s ON s.id = e.source_id
                   WHERE ec.claim_id = ? ORDER BY ec.relationship, s.credibility_score DESC""",
                (claim["id"],),
            ).fetchall()

            if ev_rows:
                w("#### Linked Evidence")
                w()
                for e in ev_rows:
                    rel_icon = {"supports": "✓", "undermines": "✗", "neutral": "~"}[e["relationship"]]
                    w(f"- **[{rel_icon} {e['relationship']}]** {e['description']}")
                    w(f"  - Source: {e['source_name']} ({e['source_type']}, credibility: {e['credibility_score']:.2f})")
                    if e["relevance_note"]:
                        w(f"  - Note: {e['relevance_note']}")
                w()

            cc_rows = conn.execute(
                "SELECT * FROM counter_claims WHERE claim_id = ? ORDER BY addressed, created_at",
                (claim["id"],),
            ).fetchall()

            if cc_rows:
                w("#### Counter-Claims")
                w()
                for cc in cc_rows:
                    status = "ADDRESSED" if cc["addressed"] else "**OPEN — -0.10 confidence penalty**"
                    w(f"- [{status}] {cc['statement']}")
                    if cc["address_rationale"]:
                        w(f"  - Analyst rationale: {cc['address_rationale']}")
                w()
            elif claim["impact_level"] == "high":
                w("> ⚠️ **No counter-claims for this high-impact claim. REQUIRED.**")
                w()

            if bd.improvement_paths:
                w("#### What Would Increase Confidence")
                w()
                for p in bd.improvement_paths:
                    w(f"- {p}")
                w()

            w("---")
            w()

    sources = conn.execute(
        "SELECT s.*, COUNT(e.id) as evidence_count FROM sources s "
        "LEFT JOIN evidence e ON e.source_id = s.id "
        "WHERE s.investigation_id = ? GROUP BY s.id ORDER BY s.credibility_score DESC",
        (iid,),
    ).fetchall()

    if sources:
        w("## Sources")
        w()
        w("| Name | Type | Credibility | Evidence items | Independence group |")
        w("|------|------|-------------|----------------|--------------------|")
        for s in sources:
            grp = s["independence_group"] or "—"
            w(f"| {s['name']} | {s['source_type']} | {s['credibility_score']:.2f} | {s['evidence_count']} | {grp} |")
        w()

    return lines
