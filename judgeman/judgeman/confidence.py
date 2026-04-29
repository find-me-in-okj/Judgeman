"""
confidence.py — The Judgeman confidence calculation engine.

PHILOSOPHY:
    Confidence is not a prediction. It is a structured summary of the justification
    an analyst has assembled for a claim. Every component of the final score is
    named, quantified, and traceable to an analyst decision or an observed fact
    about the evidence base.

CEILING AUTO-LIFT (corrected implementation):
    The original spec requires that ceilings are permeable through demonstrated
    rigor — not merely through analyst assertion. The rule:

        If the claim satisfies ALL structural criteria for its impact level,
        the ceiling lifts automatically. No override required.
        Override is reserved for exceeding the ceiling WITHOUT meeting criteria.

    This means a well-evidenced, counter-balanced high-impact claim can reach
    0.90 purely through structured work — the analyst earns it, the system
    recognises it.

    Auto-lift criteria by impact level:

        medium (ceiling 0.85 → lifted to 1.00):
            - ≥ 2 independent source groups, each with credibility ≥ 0.70
            - ≥ 1 fully addressed counter-claim

        high (ceiling 0.75 → lifted to 0.90):
            - ≥ 3 independent source groups, each with credibility ≥ 0.70
            - ≥ 1 fully addressed counter-claim
            - what_if_wrong provided

    These criteria are checked automatically. When all are met, the breakdown
    shows "AUTO-LIFT ACTIVE" and the lifted ceiling. When partially met, the
    breakdown shows exactly which criteria remain outstanding.

    The manual override (jm claim override) is still available for cases where
    the analyst has strong grounds but cannot satisfy the structural criteria
    (e.g. only one independent source exists by nature of the domain). It
    requires justification and is permanently logged.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Optional
from models import IMPACT_CEILINGS, IMPACT_CEILING_RATIONALE


# ---------------------------------------------------------------------------
# Factor constants
# ---------------------------------------------------------------------------

CORROBORATION_BONUS_PER_GROUP  = 0.05
CORROBORATION_BONUS_MAX        = 0.15
CONFLICT_PENALTY_PER_UNADDRESSED = 0.10
CONFLICT_PENALTY_MAX           = 0.40
SOURCE_CREDIBILITY_SCALE       = 0.20

# ---------------------------------------------------------------------------
# Auto-lift criteria — the structural thresholds that earn a ceiling removal.
# These are explicit, named, and shown in every breakdown.
# ---------------------------------------------------------------------------

AUTOLIFT_CRITERIA = {
    "medium": {
        "min_independent_groups":       2,
        "min_source_credibility":       0.70,   # each group must meet this avg
        "min_addressed_counter_claims": 1,
        "requires_what_if_wrong":       False,
        "lifted_ceiling":               1.00,
        "description": (
            "Ceiling lifts to 1.00 when: ≥2 independent high-credibility source groups "
            "AND ≥1 fully addressed counter-claim."
        ),
    },
    "high": {
        "min_independent_groups":       3,
        "min_source_credibility":       0.70,
        "min_addressed_counter_claims": 1,
        "requires_what_if_wrong":       True,
        "lifted_ceiling":               0.90,
        "description": (
            "Ceiling lifts to 0.90 when: ≥3 independent high-credibility source groups "
            "AND ≥1 fully addressed counter-claim "
            "AND 'what_if_wrong' provided."
        ),
    },
    "low": None,  # No ceiling, no auto-lift needed
}


@dataclass
class AutoLiftStatus:
    """Detailed status of whether the auto-lift criteria are met."""
    impact_level: str
    applicable: bool                    # False for 'low' impact

    # Per-criterion results
    independent_groups_found:    int  = 0
    independent_groups_required: int  = 0
    high_cred_groups_found:      int  = 0   # groups meeting min_source_credibility
    addressed_counter_claims:    int  = 0
    addressed_required:          int  = 0
    what_if_wrong_provided:      bool = False
    what_if_wrong_required:      bool = False

    # Outcome
    criteria_met: bool = False
    lifted_ceiling: Optional[float] = None  # Set when criteria_met is True

    def unmet_criteria(self) -> list[str]:
        """Human-readable list of criteria still outstanding."""
        if not self.applicable or self.criteria_met:
            return []
        missing = []
        if self.high_cred_groups_found < self.independent_groups_required:
            missing.append(
                f"Need ≥{self.independent_groups_required} independent source groups "
                f"with credibility ≥0.70 (have {self.high_cred_groups_found})"
            )
        if self.addressed_counter_claims < self.addressed_required:
            missing.append(
                f"Need ≥{self.addressed_required} fully addressed counter-claim "
                f"(have {self.addressed_counter_claims})"
            )
        if self.what_if_wrong_required and not self.what_if_wrong_provided:
            missing.append("'What if I'm wrong?' section required")
        return missing

    def met_criteria(self) -> list[str]:
        """Human-readable list of criteria already satisfied."""
        if not self.applicable:
            return []
        satisfied = []
        if self.high_cred_groups_found >= self.independent_groups_required:
            satisfied.append(
                f"≥{self.independent_groups_required} independent high-credibility "
                f"source groups ({self.high_cred_groups_found} found)"
            )
        if self.addressed_counter_claims >= self.addressed_required:
            satisfied.append(
                f"≥{self.addressed_required} fully addressed counter-claim "
                f"({self.addressed_counter_claims} found)"
            )
        if self.what_if_wrong_required and self.what_if_wrong_provided:
            satisfied.append("'What if I'm wrong?' provided")
        elif not self.what_if_wrong_required:
            pass  # Not required, don't show
        return satisfied


@dataclass
class ConfidenceFactor:
    """A single named factor in the confidence breakdown."""
    name: str
    value: float
    explanation: str


@dataclass
class ConfidenceBreakdown:
    """
    The complete, transparent breakdown of a claim's confidence score.
    """
    claim_id: str
    claim_statement: str
    impact_level: str

    base_confidence: float
    factors: list[ConfidenceFactor] = field(default_factory=list)

    raw_confidence: float = 0.0
    ceiling: float = 1.0
    ceiling_applied: bool = False
    final_confidence: float = 0.0

    # Auto-lift status
    autolift: Optional[AutoLiftStatus] = None

    # Override state (manual, used when structural criteria not met)
    override_active: bool = False
    override_confidence: Optional[float] = None
    override_justification: Optional[str] = None

    # Safety requirements status
    has_counter_claim: bool = False
    has_what_if_wrong: bool = False
    high_impact_requirements_met: bool = True

    improvement_paths: list[str] = field(default_factory=list)
    reduction_risks:   list[str] = field(default_factory=list)

    def displayed_confidence(self) -> float:
        if self.override_active and self.override_confidence is not None:
            return self.override_confidence
        return self.final_confidence

    def summary_line(self) -> str:
        pct       = int(self.displayed_confidence() * 100)
        ceil_pct  = int(self.ceiling * 100)
        override  = " [OVERRIDE ACTIVE]"  if self.override_active else ""
        lift      = " [AUTO-LIFT ACTIVE]" if (self.autolift and self.autolift.criteria_met) else ""
        clip      = f" (ceiling: {ceil_pct}%)" if self.ceiling_applied else ""
        return f"{pct}%{clip}{lift}{override}"


def _check_autolift(
    claim: dict,
    supporting_sources: list[dict],
    addressed_counter_claims: int,
) -> AutoLiftStatus:
    """
    Evaluate auto-lift criteria for a claim.

    supporting_sources: list of source dicts linked to this claim via
        'supports' evidence. Each must have 'credibility_score' and
        'independence_group'.
    """
    impact = claim["impact_level"]
    criteria = AUTOLIFT_CRITERIA.get(impact)

    if criteria is None:
        return AutoLiftStatus(impact_level=impact, applicable=False)

    status = AutoLiftStatus(
        impact_level=impact,
        applicable=True,
        independent_groups_required=criteria["min_independent_groups"],
        addressed_required=criteria["min_addressed_counter_claims"],
        what_if_wrong_required=criteria["requires_what_if_wrong"],
        what_if_wrong_provided=bool(claim.get("what_if_wrong")),
        addressed_counter_claims=addressed_counter_claims,
    )

    # Count independent source groups where the group's credibility meets threshold
    # Group sources by independence_group (None = each source is its own group)
    groups: dict[str, list[float]] = {}
    for s in supporting_sources:
        grp = s.get("independence_group") or s["id"]
        groups.setdefault(grp, []).append(s["credibility_score"])

    status.independent_groups_found = len(groups)

    # A group qualifies if its average credibility meets the threshold
    threshold = criteria["min_source_credibility"]
    high_cred_groups = sum(
        1 for scores in groups.values()
        if (sum(scores) / len(scores)) >= threshold
    )
    status.high_cred_groups_found = high_cred_groups

    # Evaluate overall criteria
    groups_ok   = high_cred_groups >= criteria["min_independent_groups"]
    cc_ok       = addressed_counter_claims >= criteria["min_addressed_counter_claims"]
    wiw_ok      = (not criteria["requires_what_if_wrong"]) or bool(claim.get("what_if_wrong"))

    status.criteria_met = groups_ok and cc_ok and wiw_ok
    if status.criteria_met:
        status.lifted_ceiling = criteria["lifted_ceiling"]

    return status


def calculate_confidence(claim_id: str, conn: sqlite3.Connection) -> "ConfidenceBreakdown":
    """
    Calculate confidence for a claim. Returns a fully transparent ConfidenceBreakdown.
    Does not write to the database.
    """
    claim_row = conn.execute(
        "SELECT * FROM claims WHERE id = ?", (claim_id,)
    ).fetchone()
    if not claim_row:
        raise ValueError(f"Claim not found: {claim_id}")

    claim = dict(claim_row)
    impact_level = claim["impact_level"]
    base_ceiling  = IMPACT_CEILINGS[impact_level]

    bd = ConfidenceBreakdown(
        claim_id=claim_id,
        claim_statement=claim["statement"],
        impact_level=impact_level,
        base_confidence=claim["base_confidence"],
        ceiling=base_ceiling,
        override_active=claim.get("override_confidence") is not None,
        override_confidence=claim.get("override_confidence"),
        override_justification=claim.get("override_justification"),
        has_what_if_wrong=bool(claim.get("what_if_wrong")),
    )

    # ── Supporting sources ──────────────────────────────────────
    supporting_sources = [dict(r) for r in conn.execute(
        """SELECT DISTINCT s.id, s.credibility_score, s.independence_group, s.name
           FROM sources s
           JOIN evidence e ON e.source_id = s.id
           JOIN evidence_claims ec ON ec.evidence_id = e.id
           WHERE ec.claim_id = ? AND ec.relationship = 'supports'""",
        (claim_id,),
    ).fetchall()]

    # ── Counter-claims ──────────────────────────────────────────
    counter_claims = [dict(r) for r in conn.execute(
        "SELECT id, addressed, statement FROM counter_claims WHERE claim_id = ?",
        (claim_id,),
    ).fetchall()]
    bd.has_counter_claim = len(counter_claims) > 0
    addressed_ccs   = [cc for cc in counter_claims if cc["addressed"]]
    unaddressed_ccs = [cc for cc in counter_claims if not cc["addressed"]]

    # ── Factor 1: Source credibility ────────────────────────────
    if supporting_sources:
        avg_cred  = sum(s["credibility_score"] for s in supporting_sources) / len(supporting_sources)
        cred_delta = round((avg_cred - 0.5) * SOURCE_CREDIBILITY_SCALE, 4)
        direction  = "raises" if cred_delta >= 0 else "lowers"
        bd.factors.append(ConfidenceFactor(
            name="source_credibility",
            value=cred_delta,
            explanation=(
                f"Average credibility of {len(supporting_sources)} supporting source(s): "
                f"{avg_cred:.2f}/1.00. "
                f"(avg − 0.5) × {SOURCE_CREDIBILITY_SCALE} {direction} score by "
                f"{abs(cred_delta):.3f}."
            ),
        ))
    else:
        bd.factors.append(ConfidenceFactor(
            name="source_credibility",
            value=0.0,
            explanation="No supporting sources linked. Source credibility factor is neutral.",
        ))

    # ── Factor 2: Corroboration bonus ───────────────────────────
    seen_groups: set = set()
    independent_group_count = 0
    for s in supporting_sources:
        grp = s.get("independence_group")
        if grp is None:
            independent_group_count += 1
        elif grp not in seen_groups:
            seen_groups.add(grp)
            independent_group_count += 1

    bonus_groups         = max(0, independent_group_count - 1)
    corroboration_bonus  = round(min(bonus_groups * CORROBORATION_BONUS_PER_GROUP, CORROBORATION_BONUS_MAX), 4)

    if independent_group_count == 0:
        corr_exp = "No supporting sources. No corroboration bonus."
    elif independent_group_count == 1:
        corr_exp = (
            f"1 independent source group. Bonus begins at 2nd group. "
            f"Adding sources from different origins would increase this."
        )
    else:
        corr_exp = (
            f"{independent_group_count} independent source groups. "
            f"+{CORROBORATION_BONUS_PER_GROUP} per group beyond the first "
            f"(capped at +{CORROBORATION_BONUS_MAX}). Bonus: +{corroboration_bonus}."
        )
    bd.factors.append(ConfidenceFactor(
        name="corroboration_bonus",
        value=corroboration_bonus,
        explanation=corr_exp,
    ))

    # ── Factor 3: Conflict penalty ──────────────────────────────
    raw_penalty      = len(unaddressed_ccs) * CONFLICT_PENALTY_PER_UNADDRESSED
    conflict_penalty = round(-min(raw_penalty, CONFLICT_PENALTY_MAX), 4)

    if not counter_claims:
        cc_exp = "No counter-claims registered."
        if impact_level == "high":
            cc_exp += (
                " WARNING: High-impact claims require at least one counter-claim. "
                "Effective ceiling suppressed until one is added."
            )
    else:
        cc_exp = (
            f"{len(counter_claims)} counter-claim(s): "
            f"{len(addressed_ccs)} addressed, {len(unaddressed_ccs)} unaddressed. "
            f"Penalty: −{CONFLICT_PENALTY_PER_UNADDRESSED} per unaddressed "
            f"(capped at −{CONFLICT_PENALTY_MAX}). Applied: {conflict_penalty}."
        )
    bd.factors.append(ConfidenceFactor(
        name="conflict_penalty",
        value=conflict_penalty,
        explanation=cc_exp,
    ))

    # ── Raw confidence ──────────────────────────────────────────
    raw = claim["base_confidence"] + sum(f.value for f in bd.factors)
    bd.raw_confidence = round(max(0.0, min(1.0, raw)), 4)

    # ── Auto-lift check ─────────────────────────────────────────
    autolift = _check_autolift(claim, supporting_sources, len(addressed_ccs))
    bd.autolift = autolift

    effective_ceiling = base_ceiling

    if impact_level in ("medium", "high"):
        if autolift.criteria_met:
            # Structural criteria fully satisfied — ceiling lifts automatically
            effective_ceiling = autolift.lifted_ceiling
            bd.factors.append(ConfidenceFactor(
                name="auto_lift_active",
                value=0.0,
                explanation=(
                    f"AUTO-LIFT ACTIVE: all structural criteria satisfied. "
                    f"Ceiling raised from {base_ceiling:.0%} → {effective_ceiling:.0%}. "
                    f"Criteria met: {'; '.join(autolift.met_criteria())}."
                ),
            ))
        else:
            # Criteria not yet met — show progress and apply base ceiling
            unmet = autolift.unmet_criteria()
            met   = autolift.met_criteria()

            if impact_level == "high" and not bd.has_counter_claim:
                # Hard suppress: no counter-claim at all → extra restrictive
                effective_ceiling = min(base_ceiling, 0.60)

            bd.factors.append(ConfidenceFactor(
                name="ceiling_locked",
                value=0.0,
                explanation=(
                    f"Ceiling LOCKED at {effective_ceiling:.0%} "
                    f"({'base ceiling' if effective_ceiling == base_ceiling else 'suppressed — missing counter-claim'}). "
                    f"To auto-lift: {'; '.join(unmet)}. "
                    + (f"Already satisfied: {'; '.join(met)}." if met else "")
                ),
            ))

    bd.ceiling = effective_ceiling

    # ── Apply ceiling ───────────────────────────────────────────
    if bd.raw_confidence > effective_ceiling:
        bd.ceiling_applied = True
        bd.final_confidence = effective_ceiling
        bd.factors.append(ConfidenceFactor(
            name="ceiling_enforcement",
            value=round(effective_ceiling - bd.raw_confidence, 4),
            explanation=(
                f"{IMPACT_CEILING_RATIONALE[impact_level]} "
                f"Raw score {bd.raw_confidence:.3f} clipped to {effective_ceiling:.3f}."
            ),
        ))
    else:
        bd.final_confidence = bd.raw_confidence

    # ── High-impact safety requirements ─────────────────────────
    if impact_level == "high":
        bd.high_impact_requirements_met = bd.has_counter_claim and bd.has_what_if_wrong

    # ── Improvement paths ────────────────────────────────────────
    if autolift and autolift.applicable and not autolift.criteria_met:
        for criterion in autolift.unmet_criteria():
            bd.improvement_paths.append(
                f"[Auto-lift] {criterion}"
            )
    if independent_group_count < 4 and not (autolift and autolift.criteria_met):
        bd.improvement_paths.append(
            f"Add evidence from additional independent source groups "
            f"(currently {independent_group_count}; each new group adds +{CORROBORATION_BONUS_PER_GROUP})."
        )
    if unaddressed_ccs:
        bd.improvement_paths.append(
            f"Address {len(unaddressed_ccs)} outstanding counter-claim(s) "
            f"(each addressed removes the −{CONFLICT_PENALTY_PER_UNADDRESSED} penalty)."
        )
    if supporting_sources and any(s["credibility_score"] < 0.7 for s in supporting_sources):
        bd.improvement_paths.append(
            "Replace or supplement low-credibility sources with higher-credibility alternatives."
        )

    # ── Reduction risks ──────────────────────────────────────────
    if addressed_ccs:
        bd.reduction_risks.append(
            f"{len(addressed_ccs)} addressed counter-claim(s) could be re-opened "
            f"if rationale is found insufficient, restoring the −{CONFLICT_PENALTY_PER_UNADDRESSED} penalty."
        )
    if autolift and autolift.criteria_met:
        bd.reduction_risks.append(
            "Auto-lift is structural — removing a source, lowering credibility, or "
            "re-opening a counter-claim could collapse the lift and restore the base ceiling."
        )

    return bd


def check_ceiling_violation(
    proposed_confidence: float,
    impact_level: str,
    autolift_active: bool,
) -> tuple[bool, str]:
    """
    Check whether a proposed confidence value violates the ceiling.

    If the auto-lift is active, the lifted ceiling applies.
    If the auto-lift is not active, the base ceiling applies.

    The manual override is reserved for cases where the analyst wants to
    exceed the ceiling WITHOUT meeting the structural criteria.
    """
    if impact_level == "low":
        return False, ""

    criteria = AUTOLIFT_CRITERIA.get(impact_level)
    if autolift_active and criteria:
        effective_ceiling = criteria["lifted_ceiling"]
    else:
        effective_ceiling = IMPACT_CEILINGS[impact_level]

    if proposed_confidence > effective_ceiling:
        lift_note = ""
        if not autolift_active and criteria:
            lift_note = (
                f" Note: satisfying the auto-lift criteria would raise the ceiling to "
                f"{criteria['lifted_ceiling']:.0%}. {criteria['description']}"
            )
        return True, (
            f"Proposed confidence {proposed_confidence:.0%} exceeds the effective ceiling "
            f"{effective_ceiling:.0%} ({'auto-lifted' if autolift_active else impact_level + '-impact base'}). "
            f"{IMPACT_CEILING_RATIONALE[impact_level]}{lift_note}"
        )

    return False, ""
