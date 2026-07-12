from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from blackcell.features.evaluate_outcome.command import EvaluateOutcome
from blackcell.features.evaluate_outcome.models import (
    EvaluationAuthorizationOutcome,
    EvaluationCriterion,
    EvaluationExecutionStatus,
    EvaluationFact,
    EvaluationFinding,
    EvaluationObservationStatus,
    EvaluationVerdict,
    OutcomeEvaluation,
    scalar_values_equal,
)
from blackcell.kernel import utc_now
from blackcell.kernel._json import canonical_json_bytes


class OutcomeEvaluator:
    """Deterministic baseline evaluator for fresh independently observed facts."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock

    def handle(self, command: EvaluateOutcome) -> OutcomeEvaluation:
        if command.authorization_outcome is not EvaluationAuthorizationOutcome.ALLOW:
            code = (
                "authorization-denied"
                if command.authorization_outcome is EvaluationAuthorizationOutcome.DENY
                else "authorization-requires-approval"
            )
            findings = tuple(
                _empty_finding(
                    criterion,
                    EvaluationVerdict.NOT_EVALUATED,
                    code,
                )
                for criterion in command.spec.criteria
            )
            return self._result(
                command,
                verdict=EvaluationVerdict.NOT_EVALUATED,
                findings=findings,
            )
        if command.execution_status is EvaluationExecutionStatus.UNKNOWN:
            findings = tuple(
                _empty_finding(
                    criterion,
                    EvaluationVerdict.INCONCLUSIVE,
                    "execution-unknown",
                )
                for criterion in command.spec.criteria
            )
            return self._result(
                command,
                verdict=EvaluationVerdict.INCONCLUSIVE,
                findings=findings,
            )
        observation = command.observation
        if observation is None:  # pragma: no cover - enforced by EvaluateOutcome
            raise ValueError("terminal evaluation requires an observation")
        if observation.status is EvaluationObservationStatus.INCONCLUSIVE:
            findings = tuple(
                _empty_finding(
                    criterion,
                    EvaluationVerdict.INCONCLUSIVE,
                    "outcome-observation-inconclusive",
                    source_event_ids=tuple(item.event_id for item in observation.sources),
                )
                for criterion in command.spec.criteria
            )
            return self._result(
                command,
                verdict=EvaluationVerdict.INCONCLUSIVE,
                findings=findings,
            )
        facts_by_target: dict[tuple[str, str], list[EvaluationFact]] = {}
        for fact in observation.facts:
            facts_by_target.setdefault(fact.target, []).append(fact)
        findings = tuple(
            _evaluate_criterion(criterion, tuple(facts_by_target.get(criterion.target, ())))
            for criterion in command.spec.criteria
        )
        return self._result(
            command,
            verdict=_aggregate(findings),
            findings=findings,
        )

    def _result(
        self,
        command: EvaluateOutcome,
        *,
        verdict: EvaluationVerdict,
        findings: tuple[EvaluationFinding, ...],
    ) -> OutcomeEvaluation:
        observation = command.observation
        return OutcomeEvaluation(
            run_id=command.run_id,
            evaluation_spec_id=command.spec.spec_id,
            authorization_outcome=command.authorization_outcome,
            execution_status=command.execution_status,
            execution_event_id=command.execution_event_id,
            execution_binding_id=command.execution_binding_id,
            outcome_observation_id=(None if observation is None else observation.observation_id),
            outcome_observation_digest=(
                None if observation is None else observation.observation_digest
            ),
            outcome_evidence_binding_id=(
                None if observation is None else observation.evidence_binding_id
            ),
            initial_state_position=command.initial_state_position,
            verdict=verdict,
            findings=findings,
            evaluated_at=self._clock(),
        )


def _evaluate_criterion(
    criterion: EvaluationCriterion,
    facts: tuple[EvaluationFact, ...],
) -> EvaluationFinding:
    if not facts:
        return _empty_finding(
            criterion,
            EvaluationVerdict.INCONCLUSIVE,
            "no-fresh-outcome-evidence",
        )
    claim_ids = tuple(sorted({item.claim_id for item in facts}))
    event_ids = tuple(sorted({item.source_event_id for item in facts}))
    values: dict[bytes, EvaluationFact] = {}
    for fact in facts:
        key = canonical_json_bytes(fact.value)
        current = values.get(key)
        if current is None or fact.confidence > current.confidence:
            values[key] = fact
    if len(values) != 1:
        return _empty_finding(
            criterion,
            EvaluationVerdict.INCONCLUSIVE,
            "conflicting-fresh-outcome-evidence",
            observed_claim_ids=claim_ids,
            source_event_ids=event_ids,
        )
    actual = next(iter(values.values()))
    if actual.confidence < criterion.minimum_confidence:
        return EvaluationFinding(
            criterion_id=criterion.criterion_id,
            required=criterion.required,
            verdict=EvaluationVerdict.INCONCLUSIVE,
            code="outcome-confidence-below-threshold",
            expected_value=criterion.expected_value,
            actual_present=True,
            actual_value=actual.value,
            actual_confidence=actual.confidence,
            observed_claim_ids=claim_ids,
            source_event_ids=event_ids,
        )
    matched = scalar_values_equal(actual.value, criterion.expected_value)
    return EvaluationFinding(
        criterion_id=criterion.criterion_id,
        required=criterion.required,
        verdict=EvaluationVerdict.PASS if matched else EvaluationVerdict.FAIL,
        code="expected-value-observed" if matched else "unexpected-value-observed",
        expected_value=criterion.expected_value,
        actual_present=True,
        actual_value=actual.value,
        actual_confidence=actual.confidence,
        observed_claim_ids=claim_ids,
        source_event_ids=event_ids,
    )


def _empty_finding(
    criterion: EvaluationCriterion,
    verdict: EvaluationVerdict,
    code: str,
    *,
    observed_claim_ids: tuple[str, ...] = (),
    source_event_ids: tuple[str, ...] = (),
) -> EvaluationFinding:
    return EvaluationFinding(
        criterion_id=criterion.criterion_id,
        required=criterion.required,
        verdict=verdict,
        code=code,
        expected_value=criterion.expected_value,
        actual_present=False,
        actual_value=None,
        observed_claim_ids=observed_claim_ids,
        source_event_ids=source_event_ids,
    )


def _aggregate(findings: tuple[EvaluationFinding, ...]) -> EvaluationVerdict:
    required = tuple(item for item in findings if item.required)
    if any(item.verdict is EvaluationVerdict.FAIL for item in required):
        return EvaluationVerdict.FAIL
    if any(item.verdict is EvaluationVerdict.INCONCLUSIVE for item in required):
        return EvaluationVerdict.INCONCLUSIVE
    return EvaluationVerdict.PASS


__all__ = ["OutcomeEvaluator"]
