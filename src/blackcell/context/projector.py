from __future__ import annotations

import json
import re
from dataclasses import dataclass

from blackcell.context.models import (
    ContextFrame,
    OmissionSummary,
    SelectionReason,
)
from blackcell.domains.repository import Claim, ClaimConflict, EpistemicStatus
from blackcell.domains.repository.models import OperationalStateEstimate

_WORD = re.compile(r"[a-z0-9_.:/-]+")


class ContextBudgetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _SelectionUnit:
    key: str
    claims: tuple[Claim, ...]
    reason: str
    score: int


class DeterministicContextProjector:
    """Select claims without model calls and retain the reason for every inclusion."""

    def project(
        self,
        state: OperationalStateEstimate,
        *,
        objective: str,
        constraints: tuple[str, ...] = (),
        available_affordances: tuple[str, ...] = (),
        affordance_contracts: tuple[str, ...] = (),
        required_claim_ids: tuple[str, ...] = (),
        token_budget: int = 2_000,
        character_budget: int = 8_000,
    ) -> ContextFrame:
        if not objective.strip():
            raise ValueError("objective must be non-empty")
        if token_budget <= 0 or character_budget <= 0:
            raise ValueError("budgets must be positive")
        hard_character_limit = min(character_budget, token_budget * 4)
        required = frozenset(required_claim_ids)
        units, low_relevance_count = _candidate_units(state, objective, required)

        selected_units: list[_SelectionUnit] = []
        selected_ids: set[str] = set()
        budget_omitted = 0
        for unit in units:
            tentative_units = (*selected_units, unit)
            tentative_ids = {claim.claim_id for item in tentative_units for claim in item.claims}
            tentative_omitted = len(state.claims) - len(tentative_ids)
            rendered = _render(
                state,
                objective,
                constraints,
                available_affordances,
                affordance_contracts,
                tentative_units,
                omitted_claim_count=tentative_omitted,
            )
            if len(rendered) <= hard_character_limit:
                selected_units.append(unit)
                selected_ids.update(claim.claim_id for claim in unit.claims)
            else:
                budget_omitted += len(unit.claims)

        omitted_claim_count = len(state.claims) - len(selected_ids)
        rendered = _render(
            state,
            objective,
            constraints,
            available_affordances,
            affordance_contracts,
            selected_units,
            omitted_claim_count=omitted_claim_count,
        )
        while len(rendered) > hard_character_limit and selected_units:
            removed = selected_units.pop()
            budget_omitted += len(removed.claims)
            selected_ids.difference_update(claim.claim_id for claim in removed.claims)
            omitted_claim_count = len(state.claims) - len(selected_ids)
            rendered = _render(
                state,
                objective,
                constraints,
                available_affordances,
                affordance_contracts,
                selected_units,
                omitted_claim_count=omitted_claim_count,
            )
        if len(rendered) > hard_character_limit:
            raise ContextBudgetError("budget cannot contain the objective and frame metadata")

        selected_claims = tuple(claim for unit in selected_units for claim in unit.claims)
        selected_conflicts = _selected_conflicts(state.conflicts, selected_ids)
        selected_unknowns = tuple(
            claim for claim in selected_claims if claim.epistemic_status is EpistemicStatus.UNKNOWN
        )
        reasons = tuple(
            SelectionReason(claim.claim_id, unit.reason, unit.score)
            for unit in selected_units
            for claim in unit.claims
        )
        omitted_conflicts = len(state.conflicts) - len(selected_conflicts)
        omitted_unknowns = len(state.unknowns) - len(selected_unknowns)
        reason_counts = []
        if low_relevance_count:
            reason_counts.append(("low-relevance", low_relevance_count))
        if budget_omitted:
            reason_counts.append(("budget", budget_omitted))
        omission = OmissionSummary(
            omitted_claim_count=omitted_claim_count,
            omitted_conflict_group_count=omitted_conflicts,
            omitted_unknown_count=omitted_unknowns,
            reasons=tuple(reason_counts),
        )
        return ContextFrame(
            objective=objective,
            state_id=state.state_id,
            as_of_sequence=state.as_of_sequence,
            as_of_time=state.as_of_time,
            selected_claims=selected_claims,
            conflicts=selected_conflicts,
            unknowns=selected_unknowns,
            constraints=tuple(sorted(set(constraints))),
            available_affordances=tuple(sorted(set(available_affordances))),
            affordance_contracts=tuple(sorted(set(affordance_contracts))),
            token_budget=token_budget,
            character_budget=character_budget,
            selection_reasons=reasons,
            omission_summary=omission,
            rendered_context=rendered,
        )


def _candidate_units(
    state: OperationalStateEstimate,
    objective: str,
    required: frozenset[str],
) -> tuple[tuple[_SelectionUnit, ...], int]:
    terms = {term for term in _WORD.findall(objective.casefold()) if len(term) >= 2}
    conflicts_by_claim = {
        claim.claim_id: conflict for conflict in state.conflicts for claim in conflict.claims
    }
    consumed: set[str] = set()
    units: list[_SelectionUnit] = []

    for conflict in state.conflicts:
        claim_ids = {claim.claim_id for claim in conflict.claims}
        matches = _term_matches(conflict.claims, terms)
        if claim_ids & required:
            reason, score = "required-conflict", 1_100
        elif matches:
            reason, score = "objective-conflict", 950 + matches
        else:
            reason, score = "conflicting-evidence", 900
        units.append(
            _SelectionUnit(f"conflict:{conflict.conflict_group}", conflict.claims, reason, score)
        )
        consumed.update(claim_ids)

    unrelated: list[Claim] = []
    for claim in state.claims:
        if claim.claim_id in consumed or claim.claim_id in conflicts_by_claim:
            continue
        matches = _term_matches((claim,), terms)
        if claim.claim_id in required:
            units.append(_SelectionUnit(claim.claim_id, (claim,), "required", 1_000))
        elif claim.epistemic_status is EpistemicStatus.UNKNOWN:
            units.append(_SelectionUnit(claim.claim_id, (claim,), "unknown-evidence", 850))
        elif matches:
            units.append(_SelectionUnit(claim.claim_id, (claim,), "objective-term", 600 + matches))
        else:
            unrelated.append(claim)

    # A tiny recent-state fallback prevents an empty frame without turning projection into latest-N.
    fallback = sorted(unrelated, key=_recency_key, reverse=True)[:3]
    for claim in fallback:
        units.append(_SelectionUnit(claim.claim_id, (claim,), "recent-state-fallback", 100))
    low_relevance_count = len(unrelated) - len(fallback)
    return tuple(sorted(units, key=lambda unit: (-unit.score, unit.key))), low_relevance_count


def _render(
    state: OperationalStateEstimate,
    objective: str,
    constraints: tuple[str, ...],
    affordances: tuple[str, ...],
    affordance_contracts: tuple[str, ...],
    units: tuple[_SelectionUnit, ...] | list[_SelectionUnit],
    *,
    omitted_claim_count: int,
) -> str:
    lines = [
        f"objective: {objective.strip()}",
        f"state: {state.state_id} @ sequence {state.as_of_sequence}",
        "constraints: " + ("; ".join(sorted(set(constraints))) or "none"),
        "affordances: " + (", ".join(sorted(set(affordances))) or "none"),
        "affordance-contracts: " + ("; ".join(sorted(set(affordance_contracts))) or "none"),
        "evidence:",
    ]
    for unit in units:
        for claim in unit.claims:
            freshness = "expired" if claim.is_expired(state.as_of_time) else "current"
            value = json.dumps(claim.value, ensure_ascii=False, separators=(",", ":"))
            lines.append(
                f"- {claim.subject} {claim.predicate}={value} "
                f"[{claim.epistemic_status.value}/{claim.source_reliability.value}/{freshness}] "
                f"claim={claim.claim_id} selected={unit.reason}"
            )
    lines.append(f"omitted-claims: {omitted_claim_count}")
    return "\n".join(lines)


def _selected_conflicts(
    conflicts: tuple[ClaimConflict, ...], selected_ids: set[str]
) -> tuple[ClaimConflict, ...]:
    return tuple(
        conflict
        for conflict in conflicts
        if all(claim.claim_id in selected_ids for claim in conflict.claims)
    )


def _term_matches(claims: tuple[Claim, ...], terms: set[str]) -> int:
    text = " ".join(
        f"{claim.subject} {claim.predicate} {claim.value}".casefold() for claim in claims
    )
    tokens = set(_WORD.findall(text))
    return len(tokens & terms)


def _recency_key(claim: Claim) -> tuple[object, ...]:
    return claim.effective_at, claim.observed_at, claim.claim_id
