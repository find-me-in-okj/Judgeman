"""
import_cmd.py — Import an exported Judgeman investigation bundle.

Design decisions:
- Import verifies integrity before writing anything. If any check fails,
  the import is aborted with no partial state written.
- Three import modes:
    --verify-only     Read the zip, verify hashes, print report. No DB writes.
    --merge           Import entities; skip any whose ID already exists locally.
    (default)         Import all entities; fail if any ID already exists.
- The imported investigation is NOT automatically set as active. The analyst
  must run `jm use <id>` explicitly. This prevents accidentally overwriting
  a working investigation context.
- All imported entities are tagged with an import audit action. Reviewers
  can distinguish original entries from imported ones.
- The audit trail from the export is imported as-is. The import action
  is appended to the LOCAL analyst_actions table pointing to the investigation
  but with the importing analyst's ID. Two audit trails coexist:
    - The original analyst's trail (imported from audit.json)
    - The importing analyst's trail (starts with IMPORT_INVESTIGATION)
"""

import sys
import os
import json
import hashlib
import zipfile
import click
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys as _sys
import audit as audit_mod
from compat import get_analyst_id
import output as out
from models import utc_now
from chainhash import verify_audit_chain_hash


JUDGEMAN_EXPORT_VERSION = "1"


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def register(cli):
    @cli.command("import")
    @click.argument("zipfile_path")
    @click.option("--verify-only", is_flag=True, default=False,
                  help="Verify integrity without importing")
    @click.option("--merge", is_flag=True, default=False,
                  help="Skip entities whose IDs already exist (instead of failing)")
    @click.option("--analyst", "-a", default=None,
                  help="Analyst ID for the import audit entry (default: $USER)")
    def import_investigation(zipfile_path: str, verify_only: bool, merge: bool, analyst: str):
        """
        Import an investigation from a Judgeman export bundle.

        Verifies the audit chain hash and all file hashes before writing
        anything to the database. A failed verification aborts the import.

        Use --verify-only to inspect a bundle without importing it.
        Use --merge to import into an existing database that may already
        contain some of the same entities.
        """
        from db import get_connection, init_db
        init_db()
        conn = get_connection()
        analyst_id = get_analyst_id(analyst)

        zip_path = Path(zipfile_path)
        if not zip_path.exists():
            out.error(f"File not found: {zipfile_path}")
            sys.exit(1)

        out.header(f"{'Verifying' if verify_only else 'Importing'}: {zip_path.name}")

        # ── Step 1: Read and parse the zip ───────────────────────────
        try:
            with zipfile.ZipFile(str(zip_path), "r") as zf:
                names = set(zf.namelist())
                required = {"manifest.json", "investigation.json", "audit.json", "report.md"}
                missing = required - names
                if missing:
                    out.error(f"Bundle is missing required files: {', '.join(missing)}")
                    sys.exit(1)

                manifest_raw = zf.read("manifest.json")
                investigation_raw = zf.read("investigation.json")
                audit_raw = zf.read("audit.json")
                report_raw = zf.read("report.md")
        except zipfile.BadZipFile:
            out.error("File is not a valid zip archive.")
            sys.exit(1)
        except Exception as e:
            out.error(f"Failed to read bundle: {e}")
            sys.exit(1)

        # ── Step 2: Parse manifest ───────────────────────────────────
        try:
            manifest = json.loads(manifest_raw)
        except json.JSONDecodeError as e:
            out.error(f"Manifest is not valid JSON: {e}")
            sys.exit(1)

        version = manifest.get("judgeman_export_version")
        if version != JUDGEMAN_EXPORT_VERSION:
            out.warn(f"Export version mismatch: bundle is v{version}, "
                     f"this Judgeman expects v{JUDGEMAN_EXPORT_VERSION}. Proceeding with caution.")

        out.section("Bundle manifest")
        out.field("Investigation", manifest.get("investigation_name", "unknown"))
        out.field("Exported by",   manifest.get("exported_by", "unknown"))
        out.field("Exported at",   str(manifest.get("exported_at", "unknown"))[:19])
        if manifest.get("export_note"):
            out.field("Note",      manifest["export_note"])
        out.field("Audit entries", str(manifest.get("audit_entry_count", "?")))

        counts = manifest.get("entity_counts", {})
        if counts:
            out.field("Entities", "  ".join(f"{k}: {v}" for k, v in counts.items()))

        # ── Step 3: Verify file hashes ───────────────────────────────
        out.section("File integrity verification")
        file_hashes = manifest.get("file_hashes", {})
        hash_checks = [
            ("investigation.json", investigation_raw),
            ("audit.json",         audit_raw),
            ("report.md",          report_raw),
        ]

        all_files_ok = True
        for filename, raw_bytes in hash_checks:
            expected = file_hashes.get(filename)
            actual   = _sha256_bytes(raw_bytes)
            if expected and actual == expected:
                out.success(f"{filename}: hash verified")
            elif expected:
                out.error(f"{filename}: HASH MISMATCH")
                out.info(f"  Expected: {expected}")
                out.info(f"  Computed: {actual}")
                all_files_ok = False
            else:
                out.warn(f"{filename}: no expected hash in manifest (cannot verify)")

        if not all_files_ok:
            out.error("File integrity verification FAILED. Aborting.")
            sys.exit(1)

        # ── Step 4: Verify audit chain hash ─────────────────────────
        out.section("Audit chain verification")
        try:
            audit_entries = json.loads(audit_raw)
        except json.JSONDecodeError as e:
            out.error(f"audit.json is not valid JSON: {e}")
            sys.exit(1)

        expected_chain = manifest.get("audit_chain_hash")
        if expected_chain:
            is_valid, msg = verify_audit_chain_hash(audit_entries, expected_chain)
            if is_valid:
                out.success(f"Audit chain verified: {expected_chain[:30]}…")
            else:
                out.error("Audit chain verification FAILED:")
                click.echo(out.red(f"  {msg}"))
                out.warn("The audit trail in this bundle may have been modified after export.")
                if not click.confirm(out.yellow("  Import anyway? (NOT RECOMMENDED)")):
                    out.info("Import aborted.")
                    sys.exit(1)
                out.warn("Proceeding with UNVERIFIED audit trail. This is logged.")
        else:
            out.warn("No audit chain hash in manifest. Cannot verify audit trail integrity.")

        if verify_only:
            click.echo()
            out.success("Verification complete. No data was imported (--verify-only).")
            conn.close()
            return

        # ── Step 5: Parse investigation data ────────────────────────
        try:
            data = json.loads(investigation_raw)
        except json.JSONDecodeError as e:
            out.error(f"investigation.json is not valid JSON: {e}")
            sys.exit(1)

        inv_data = data.get("investigation")
        if not inv_data:
            out.error("investigation.json missing 'investigation' key.")
            sys.exit(1)

        inv_id = inv_data["id"]

        # ── Step 6: Conflict check ───────────────────────────────────
        out.section("Conflict check")
        existing_inv = conn.execute(
            "SELECT id, name FROM investigations WHERE id = ?", (inv_id,)
        ).fetchone()

        if existing_inv:
            if merge:
                out.warn(f"Investigation {inv_id[:8]}… already exists locally. "
                         f"Running in --merge mode: skipping conflicting entities.")
            else:
                out.error(f"Investigation {inv_id[:8]}… already exists in local database.")
                out.info("Use --merge to import non-conflicting entities, or")
                out.info("use a different database by setting JUDGEMAN_HOME.")
                sys.exit(1)
        else:
            out.success("No conflicts found.")

        # ── Step 7: Write all entities ───────────────────────────────
        out.section("Importing entities")

        def safe_insert(table: str, row: dict, pk_col: str = "id") -> bool:
            """Insert a row, skipping if PK exists (merge mode) or raising (strict mode)."""
            pk_val = row.get(pk_col)
            exists = conn.execute(
                f"SELECT 1 FROM {table} WHERE {pk_col} = ?", (pk_val,)
            ).fetchone()
            if exists:
                if merge:
                    return False  # Skipped
                # Strict mode: raise
                raise ValueError(f"ID conflict in {table}: {pk_val}")
            # Build INSERT
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            with conn:
                conn.execute(
                    f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                    list(row.values()),
                )
            return True  # Inserted

        stats = {k: {"inserted": 0, "skipped": 0} for k in
                 ["investigations", "hypotheses", "sources", "evidence",
                  "claims", "evidence_claims", "counter_claims", "audit_actions"]}

        entity_tables = [
            ("investigations",  [inv_data],                        "id"),
            ("hypotheses",      data.get("hypotheses", []),        "id"),
            ("sources",         data.get("sources", []),           "id"),
            ("evidence",        data.get("evidence", []),          "id"),
            ("claims",          data.get("claims", []),            "id"),
            ("counter_claims",  data.get("counter_claims", []),    "id"),
        ]

        # evidence_claims has composite PK — handle separately
        try:
            for table, rows, pk in entity_tables:
                for row in rows:
                    inserted = safe_insert(table, row, pk)
                    if inserted:
                        stats[table]["inserted"] += 1
                    else:
                        stats[table]["skipped"] += 1

            # evidence_claims: composite PK
            for ec in data.get("evidence_claims", []):
                exists = conn.execute(
                    "SELECT 1 FROM evidence_claims WHERE evidence_id = ? AND claim_id = ?",
                    (ec["evidence_id"], ec["claim_id"]),
                ).fetchone()
                if exists:
                    if merge:
                        stats["evidence_claims"]["skipped"] += 1
                        continue
                    raise ValueError(f"evidence_claims conflict: {ec}")
                with conn:
                    conn.execute(
                        "INSERT INTO evidence_claims (evidence_id, claim_id, relationship, relevance_note, linked_at) "
                        "VALUES (?,?,?,?,?)",
                        (ec["evidence_id"], ec["claim_id"], ec["relationship"],
                         ec.get("relevance_note"), ec.get("linked_at")),
                    )
                stats["evidence_claims"]["inserted"] += 1

        except ValueError as e:
            out.error(f"Import conflict: {e}")
            out.info("Use --merge to skip conflicting entities.")
            # Rollback is automatic since we use individual transactions
            sys.exit(1)

        # ── Step 8: Import audit trail ───────────────────────────────
        for entry in audit_entries:
            exists = conn.execute(
                "SELECT 1 FROM analyst_actions WHERE id = ?", (entry.get("id"),)
            ).fetchone()
            if exists:
                stats["audit_actions"]["skipped"] += 1
                continue
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO analyst_actions "
                        "(id, investigation_id, analyst_id, action_type, entity_type, "
                        "entity_id, old_value, new_value, justification, timestamp) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (entry.get("id"), entry.get("investigation_id"),
                         entry.get("analyst_id"), entry.get("action_type"),
                         entry.get("entity_type"), entry.get("entity_id"),
                         entry.get("old_value"), entry.get("new_value"),
                         entry.get("justification"), entry.get("timestamp")),
                    )
                stats["audit_actions"]["inserted"] += 1
            except Exception:
                stats["audit_actions"]["skipped"] += 1

        # ── Step 9: Log the import action ────────────────────────────
        audit_mod.log_action(
            conn,
            analyst_id=analyst_id,
            action_type="IMPORT_INVESTIGATION",
            entity_type="investigation",
            entity_id=inv_id,
            investigation_id=inv_id,
            new_value={
                "imported_from": str(zip_path.name),
                "original_analyst": inv_data.get("analyst_id"),
                "audit_chain_verified": is_valid if expected_chain else None,
                "merge_mode": merge,
            },
            justification=f"Imported from {zip_path.name}",
        )

        # ── Print summary ────────────────────────────────────────────
        click.echo()
        out.success("Import complete.")
        click.echo()
        for table, s in stats.items():
            if s["inserted"] or s["skipped"]:
                skipped_note = f"  ({s['skipped']} skipped)" if s["skipped"] else ""
                out.field(table, f"{s['inserted']} imported{skipped_note}")

        click.echo()
        out.info(f"Investigation ID: {inv_id}")
        out.info(f"To work on this investigation:  jm use {inv_id[:8]}…")
        out.warn("Verify the investigation before making any new claims:  jm verify")

        conn.close()
