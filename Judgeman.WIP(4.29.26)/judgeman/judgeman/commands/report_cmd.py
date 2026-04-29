import os
"""
report_cmd.py — Investigation report generator.

The actual report building logic lives in export_cmd._build_report_lines()
to avoid duplication. This module registers the CLI command and calls it.

Design philosophy (unchanged from Step 2):
- Reports surface reasoning, not conclusions.
- Every confidence number is accompanied by its factor breakdown.
- The disclaimer is hard-coded and unremovable.
- Overrides are visually distinct.
- Output is Markdown.
"""

import sys
import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit
import output as out

REPORT_DISCLAIMER = """---
**ANALYTICAL DISCLAIMER**

This report was produced by Judgeman, an OSINT methodology engine designed to enforce
structured analytical reasoning. It represents the structured beliefs of the named
analyst(s) at the time of generation.

This report:
- Does NOT constitute legal evidence or a legal opinion
- Does NOT make factual determinations — it surfaces confidence-weighted beliefs
- Does NOT recommend enforcement, investigation, or action against any individual
- MUST be reviewed by qualified professionals before informing any consequential decision

Confidence scores reflect the analyst's structured justification, not the truth
of any claim. A high confidence score means the analyst has assembled strong,
corroborated, coherent evidence — not that the claim is proven.

All claims in this report are subject to revision as new evidence emerges.
---""".strip()


def register(cli):
    @cli.group("report")
    def report_group():
        """Generate structured investigation reports."""
        pass

    @report_group.command("generate")
    @click.option("--output", "-o", default=None,
                  help="Output file path (default: stdout)")
    @click.option("--include-audit", is_flag=True, default=False,
                  help="Append full audit trail to report")
    def report_generate(output: str, include_audit: bool):
        """
        Generate a structured Markdown report of the active investigation.

        The report never contains conclusions, legal judgments, or
        enforcement recommendations. Every confidence score includes
        its full factor breakdown.
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        from cli import require_active
        inv = require_active(conn)

        out.info("Generating report…")

        from commands.export_cmd import _build_report_lines
        lines = _build_report_lines(conn, inv)

        if include_audit:
            entries = audit.get_audit_trail(
                conn, investigation_id=inv["id"], limit=500
            )
            lines.append("## Audit Trail")
            lines.append("")
            lines.append("| Timestamp | Analyst | Action | Entity |")
            lines.append("|-----------|---------|--------|--------|")
            for e in reversed(entries):
                ts = e["timestamp"][:19].replace("T", " ")
                lines.append(
                    f"| {ts} | {e['analyst_id']} | "
                    f"{e['action_type']} | {e['entity_type']} |"
                )
            lines.append("")

        audit.log_action(
            conn,
            analyst_id=inv["analyst_id"],
            action_type=audit.GENERATE_REPORT,
            entity_type="investigation",
            entity_id=inv["id"],
            investigation_id=inv["id"],
            new_value={"output": output or "stdout", "include_audit": include_audit},
        )

        report_text = "\n".join(lines)

        if output:
            with open(output, "w") as f:
                f.write(report_text)
            out.success(f"Report written to: {output}")
        else:
            click.echo()
            click.echo(report_text)

        conn.close()
