"""
chainhash.py — Audit chain hash computation.

The audit chain hash is a tamper-evident fingerprint of all analyst_actions
rows for an investigation. It is stored in every export manifest.

On import, the hash is recomputed from the embedded audit entries and
compared to the manifest value. Any addition, deletion, or modification
of audit entries — including changes to timestamps, justifications, or
analyst IDs — will change the hash and cause import verification to fail.

This provides a simple but meaningful guarantee: if an analyst receives
an export bundle and the chain hash verifies, they know the audit trail
they received is exactly what the exporting analyst produced.

It does NOT protect against:
  - The exporting analyst fabricating entries before export
  - The export zip itself being replaced entirely (use file signing for that)
  - Entries added AFTER export (those would simply be absent from the bundle)

The goal is to detect accidental or deliberate post-export modification of
the audit trail, which is the most common threat to analytical accountability
in a collaborative investigation workflow.
"""

import hashlib
from typing import Any


def compute_audit_chain_hash(entries: list[dict]) -> str:
    """
    Compute a deterministic SHA256 hash of a list of audit log entries.

    Entries are sorted by (timestamp, id) before hashing to guarantee
    determinism regardless of database retrieval order.

    Returns "sha256:<hex_digest>".
    """
    sorted_entries = sorted(
        entries,
        key=lambda e: (str(e.get("timestamp") or ""), str(e.get("id") or "")),
    )

    parts = []
    for e in sorted_entries:
        part = "|".join([
            str(e.get("id")          or ""),
            str(e.get("timestamp")   or ""),
            str(e.get("action_type") or ""),
            str(e.get("entity_type") or ""),
            str(e.get("entity_id")   or ""),
            str(e.get("analyst_id")  or ""),
            str(e.get("justification") or ""),
        ])
        parts.append(part)

    raw = "\n".join(parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verify_audit_chain_hash(entries: list[dict], expected_hash: str) -> tuple[bool, str]:
    """
    Verify that the entries produce the expected hash.

    Returns (is_valid: bool, message: str).
    """
    if not expected_hash.startswith("sha256:"):
        return False, "Manifest hash does not use expected sha256 format."

    actual = compute_audit_chain_hash(entries)
    if actual == expected_hash:
        return True, f"Audit chain verified. Hash: {expected_hash[:22]}…"
    else:
        return False, (
            f"Audit chain MISMATCH.\n"
            f"  Expected: {expected_hash}\n"
            f"  Computed: {actual}\n"
            "The audit trail may have been modified after export."
        )
