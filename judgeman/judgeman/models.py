"""
models.py — Dataclass representations of core Judgeman entities.

Design decisions:
- Dataclasses are used for clarity and IDE support, not for ORM purposes.
- Every model has a classmethod `from_row()` to construct from a sqlite3.Row dict.
- No business logic lives here. Models are pure data containers.
- Optional fields use None rather than sentinels, matching SQL NULL semantics.
- IDs are always strings (UUID4 format). Generation happens in the command layer,
  not here — models don't know how they're created.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Confidence ceilings — defined once, used by both confidence engine and CLI
# ---------------------------------------------------------------------------

IMPACT_CEILINGS: dict[str, float] = {
    "low":    1.00,
    "medium": 0.85,
    "high":   0.75,
}

IMPACT_CEILING_RATIONALE: dict[str, str] = {
    "low":    "No ceiling applied (exploratory/academic impact).",
    "medium": "Ceiling 0.85: reputational harm potential requires conservatism.",
    "high":   "Ceiling 0.75: legal/physical harm potential. Override requires structured justification.",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Investigation:
    id: str
    name: str
    analyst_id: str
    description: Optional[str] = None
    status: str = "active"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: dict) -> "Investigation":
        return cls(
            id=row["id"],
            name=row["name"],
            analyst_id=row["analyst_id"],
            description=row.get("description"),
            status=row.get("status", "active"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class Hypothesis:
    id: str
    investigation_id: str
    statement: str
    status: str = "active"
    rationale: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: dict) -> "Hypothesis":
        return cls(
            id=row["id"],
            investigation_id=row["investigation_id"],
            statement=row["statement"],
            status=row.get("status", "active"),
            rationale=row.get("rationale"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class Source:
    id: str
    investigation_id: str
    name: str
    reference: str
    source_type: str
    credibility_score: float
    credibility_rationale: str
    independence_group: Optional[str] = None
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: dict) -> "Source":
        return cls(
            id=row["id"],
            investigation_id=row["investigation_id"],
            name=row["name"],
            reference=row["reference"],
            source_type=row["source_type"],
            credibility_score=row["credibility_score"],
            credibility_rationale=row["credibility_rationale"],
            independence_group=row.get("independence_group"),
            created_at=row["created_at"],
        )


@dataclass
class Evidence:
    id: str
    investigation_id: str
    source_id: str
    description: str
    collected_at: str
    raw_content_ref: Optional[str] = None
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: dict) -> "Evidence":
        return cls(
            id=row["id"],
            investigation_id=row["investigation_id"],
            source_id=row["source_id"],
            description=row["description"],
            collected_at=row["collected_at"],
            raw_content_ref=row.get("raw_content_ref"),
            created_at=row["created_at"],
        )


@dataclass
class Claim:
    id: str
    investigation_id: str
    statement: str
    base_confidence: float
    rationale: str
    impact_level: str
    hypothesis_id: Optional[str] = None
    final_confidence: Optional[float] = None
    what_if_wrong: Optional[str] = None
    override_confidence: Optional[float] = None
    override_justification: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def ceiling(self) -> float:
        return IMPACT_CEILINGS[self.impact_level]

    @classmethod
    def from_row(cls, row: dict) -> "Claim":
        return cls(
            id=row["id"],
            investigation_id=row["investigation_id"],
            statement=row["statement"],
            base_confidence=row["base_confidence"],
            rationale=row["rationale"],
            impact_level=row.get("impact_level", "low"),
            hypothesis_id=row.get("hypothesis_id"),
            final_confidence=row.get("final_confidence"),
            what_if_wrong=row.get("what_if_wrong"),
            override_confidence=row.get("override_confidence"),
            override_justification=row.get("override_justification"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class EvidenceClaim:
    evidence_id: str
    claim_id: str
    relationship: str  # 'supports' | 'undermines' | 'neutral'
    relevance_note: Optional[str] = None
    linked_at: str = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: dict) -> "EvidenceClaim":
        return cls(
            evidence_id=row["evidence_id"],
            claim_id=row["claim_id"],
            relationship=row["relationship"],
            relevance_note=row.get("relevance_note"),
            linked_at=row["linked_at"],
        )


@dataclass
class CounterClaim:
    id: str
    claim_id: str
    statement: str
    addressed: bool = False
    address_rationale: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: dict) -> "CounterClaim":
        return cls(
            id=row["id"],
            claim_id=row["claim_id"],
            statement=row["statement"],
            addressed=bool(row.get("addressed", 0)),
            address_rationale=row.get("address_rationale"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class AnalystAction:
    id: str
    analyst_id: str
    action_type: str
    entity_type: str
    timestamp: str
    investigation_id: Optional[str] = None
    entity_id: Optional[str] = None
    old_value: Optional[str] = None  # JSON string
    new_value: Optional[str] = None  # JSON string
    justification: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "AnalystAction":
        return cls(
            id=row["id"],
            analyst_id=row["analyst_id"],
            action_type=row["action_type"],
            entity_type=row["entity_type"],
            timestamp=row["timestamp"],
            investigation_id=row.get("investigation_id"),
            entity_id=row.get("entity_id"),
            old_value=row.get("old_value"),
            new_value=row.get("new_value"),
            justification=row.get("justification"),
        )
