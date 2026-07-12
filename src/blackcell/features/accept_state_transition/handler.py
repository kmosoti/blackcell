from __future__ import annotations

from collections import defaultdict

from blackcell.features.accept_state_transition.command import AcceptStateTransition
from blackcell.features.accept_state_transition.models import (
    AcceptedStateTransition,
    ClaimDelta,
    ConflictChange,
    EvidenceScopedConflict,
    StateTransitionIntegrityError,
    TransitionAcceptance,
    TransitionAcceptanceStatus,
    TransitionAuthorizationOutcome,
    TransitionClaim,
    TransitionEpistemicStatus,
    TransitionEvaluationVerdict,
    TransitionExecutionStatus,
)
from blackcell.kernel._json import canonical_json_bytes


class StateTransitionAcceptor:
    """Accept only independently observed, definitive, identity-consistent effects."""

    def handle(self, command: AcceptStateTransition) -> TransitionAcceptance:
        verdict = command.evaluation.verdict
        common_integrity_code = _common_integrity_error(command)
        if common_integrity_code is not None:
            raise StateTransitionIntegrityError(common_integrity_code)
        if verdict is TransitionEvaluationVerdict.NOT_EVALUATED:
            if _not_evaluated_is_corrupt(command):
                raise StateTransitionIntegrityError("integrity-mismatch")
            return _not_accepted("evaluation-not-evaluated")
        if verdict is TransitionEvaluationVerdict.INCONCLUSIVE:
            if _inconclusive_is_corrupt(command):
                raise StateTransitionIntegrityError("integrity-mismatch")
            code = (
                "execution-unknown"
                if command.execution is not None
                and command.execution.status is TransitionExecutionStatus.UNKNOWN
                else "evaluation-inconclusive"
            )
            return _not_accepted(code)

        integrity_code = _integrity_error(command)
        if integrity_code is not None:
            raise StateTransitionIntegrityError(integrity_code)

        outcome_state = command.outcome_state
        execution = command.execution
        if outcome_state is None or execution is None:  # pragma: no cover - integrity guard
            raise StateTransitionIntegrityError("integrity-mismatch")

        definitive = tuple(
            finding
            for finding in command.evaluation.findings
            if finding.verdict
            in {TransitionEvaluationVerdict.PASS, TransitionEvaluationVerdict.FAIL}
        )
        accepted_claim_ids = tuple(
            sorted({claim_id for item in definitive for claim_id in item.observed_claim_ids})
        )
        accepted_source_ids = tuple(
            sorted({event_id for item in definitive for event_id in item.source_event_ids})
        )
        outcome_by_id = {claim.claim_id: claim for claim in outcome_state.claims}
        accepted_claims = tuple(outcome_by_id[item] for item in accepted_claim_ids)
        deltas = _claim_deltas(
            initial_claims=command.initial_state.claims,
            outcome_claims=outcome_state.claims,
            accepted_claims=accepted_claims,
        )
        conflict_changes = tuple(
            change for delta in deltas if (change := _conflict_change(delta)) is not None
        )
        transition = AcceptedStateTransition(
            run_id=command.run_id,
            initial_state=command.initial_state.reference,
            outcome_state=outcome_state.reference,
            proposal=command.proposal,
            authorization=command.authorization,
            execution=execution,
            evaluation=command.evaluation,
            triggering_events=command.triggering_events,
            accepted_claim_ids=accepted_claim_ids,
            accepted_source_event_ids=accepted_source_ids,
            claim_deltas=deltas,
            conflict_changes=conflict_changes,
        )
        return TransitionAcceptance(
            status=TransitionAcceptanceStatus.ACCEPTED,
            code="definitive-outcome-evidence-accepted",
            transition=transition,
        )


def _common_integrity_error(command: AcceptStateTransition) -> str | None:
    proposal = command.proposal
    authorization = command.authorization
    execution = command.execution
    evaluation = command.evaluation
    if (
        command.run_id != evaluation.run_id
        or authorization.outcome is not evaluation.authorization_outcome
        or evaluation.initial_state_position
        != command.initial_state.reference.cutoff_global_position
        or proposal.proposal_id != authorization.proposal_id
        or proposal.proposal_digest != authorization.proposal_digest
        or proposal.action_digest != authorization.authorized_action_digest
    ):
        return "integrity-mismatch"
    if execution is None:
        if evaluation.execution_event_id is not None or evaluation.execution_binding_id is not None:
            return "integrity-mismatch"
    elif (
        command.run_id != execution.run_id
        or proposal.proposal_id != execution.proposal_id
        or proposal.proposal_digest != execution.proposal_digest
        or proposal.affordance != execution.affordance
        or proposal.arguments != execution.arguments
        or authorization.decision_id != execution.authorization_decision_id
        or authorization.authorized_action_digest != execution.authorized_action_digest
        or evaluation.execution_status is not execution.status
        or evaluation.execution_event_id != execution.execution_event_id
        or evaluation.execution_binding_id != execution.execution_binding_id
    ):
        return "integrity-mismatch"

    outcome = command.outcome_state
    if outcome is None:
        if command.triggering_events:
            return "integrity-mismatch"
        return None
    if execution is None:
        return "integrity-mismatch"
    initial_ref = command.initial_state.reference
    outcome_ref = outcome.reference
    if (
        initial_ref.scope != outcome_ref.scope
        or outcome_ref.cutoff_global_position <= initial_ref.cutoff_global_position
        or outcome_ref.last_source_stream_sequence <= initial_ref.last_source_stream_sequence
        or (
            initial_ref.effective_time_cutoff is not None
            and outcome_ref.effective_time_cutoff is not None
            and outcome_ref.effective_time_cutoff < initial_ref.effective_time_cutoff
        )
    ):
        return "integrity-mismatch"
    for event in command.triggering_events:
        if (
            event.event_type
            not in {
                "observation.recorded",
                "outcome.observation-inconclusive",
            }
            or event.stream_id != outcome_ref.stream_id
            or event.correlation_id != command.run_id
            or event.causation_id != execution.execution_event_id
            or event.global_position <= initial_ref.cutoff_global_position
            or event.global_position > outcome_ref.cutoff_global_position
            or event.stream_sequence <= initial_ref.last_source_stream_sequence
            or event.stream_sequence > outcome_ref.last_source_stream_sequence
        ):
            return "integrity-mismatch"
    if (
        outcome_ref.effective_time_cutoff is not None
        and evaluation.evaluated_at < outcome_ref.effective_time_cutoff
    ):
        return "integrity-mismatch"
    return None


def _not_evaluated_is_corrupt(command: AcceptStateTransition) -> bool:
    evaluation = command.evaluation
    return (
        command.authorization.outcome is TransitionAuthorizationOutcome.ALLOW
        or command.execution is not None
        or command.outcome_state is not None
        or bool(command.triggering_events)
        or evaluation.execution_event_id is not None
        or evaluation.execution_binding_id is not None
        or evaluation.evidence_binding_id is not None
        or evaluation.owner_observation_id is not None
        or evaluation.owner_observation_digest is not None
        or evaluation.owner_observation_artifact_digest is not None
    )


def _inconclusive_is_corrupt(command: AcceptStateTransition) -> bool:
    evaluation = command.evaluation
    execution = command.execution
    if (
        command.authorization.outcome is not TransitionAuthorizationOutcome.ALLOW
        or execution is None
    ):
        return True
    if execution.status is TransitionExecutionStatus.UNKNOWN:
        return (
            command.outcome_state is not None
            or bool(command.triggering_events)
            or evaluation.evidence_binding_id is not None
            or evaluation.owner_observation_id is not None
            or evaluation.owner_observation_digest is not None
            or evaluation.owner_observation_artifact_digest is not None
        )
    if command.outcome_state is None:
        return True
    cited_sources = {
        event_id for finding in evaluation.findings for event_id in finding.source_event_ids
    }
    return cited_sources != {item.event_id for item in command.triggering_events}


def _integrity_error(command: AcceptStateTransition) -> str | None:
    outcome = command.outcome_state
    execution = command.execution
    if outcome is None or execution is None:
        return "integrity-mismatch"
    if execution.status is TransitionExecutionStatus.UNKNOWN:
        return "execution-unknown"
    if command.authorization.outcome is not TransitionAuthorizationOutcome.ALLOW:
        return "integrity-mismatch"
    if (
        command.proposal.proposal_id != command.authorization.proposal_id
        or command.proposal.proposal_digest != command.authorization.proposal_digest
        or command.proposal.action_digest != command.authorization.authorized_action_digest
        or command.proposal.proposal_id != execution.proposal_id
        or command.proposal.proposal_digest != execution.proposal_digest
        or command.proposal.affordance != execution.affordance
        or command.proposal.arguments != execution.arguments
        or command.authorization.decision_id != execution.authorization_decision_id
        or command.authorization.authorized_action_digest != execution.authorized_action_digest
    ):
        return "integrity-mismatch"
    evaluation = command.evaluation
    if (
        evaluation.run_id != command.run_id
        or evaluation.authorization_outcome is not command.authorization.outcome
        or evaluation.execution_status is not execution.status
        or evaluation.execution_event_id != execution.execution_event_id
        or evaluation.execution_binding_id != execution.execution_binding_id
        or evaluation.initial_state_position
        != command.initial_state.reference.cutoff_global_position
    ):
        return "integrity-mismatch"

    initial_ref = command.initial_state.reference
    outcome_ref = outcome.reference
    if initial_ref.scope != outcome_ref.scope:
        return "integrity-mismatch"
    if (
        outcome_ref.cutoff_global_position <= initial_ref.cutoff_global_position
        or outcome_ref.last_source_stream_sequence <= initial_ref.last_source_stream_sequence
    ):
        return "integrity-mismatch"
    if (
        initial_ref.effective_time_cutoff is None
        or outcome_ref.effective_time_cutoff is None
        or outcome_ref.effective_time_cutoff < initial_ref.effective_time_cutoff
    ):
        return "integrity-mismatch"
    if evaluation.evaluated_at < outcome_ref.effective_time_cutoff:
        return "integrity-mismatch"

    definitive = tuple(
        finding
        for finding in evaluation.findings
        if finding.verdict in {TransitionEvaluationVerdict.PASS, TransitionEvaluationVerdict.FAIL}
    )
    if not definitive:
        return "definitive-evaluation-without-evidence"
    accepted_claim_ids = {
        claim_id for finding in definitive for claim_id in finding.observed_claim_ids
    }
    accepted_source_ids = {
        event_id for finding in definitive for event_id in finding.source_event_ids
    }
    if not accepted_claim_ids or not accepted_source_ids:
        return "definitive-evaluation-without-evidence"

    event_by_id = {item.event_id: item for item in command.triggering_events}
    if set(event_by_id) != accepted_source_ids:
        return "integrity-mismatch"
    for event in event_by_id.values():
        if (
            event.event_type != "observation.recorded"
            or event.stream_id != outcome_ref.stream_id
            or event.correlation_id != command.run_id
            or event.causation_id != execution.execution_event_id
            or event.global_position <= initial_ref.cutoff_global_position
            or event.global_position > outcome_ref.cutoff_global_position
            or event.stream_sequence <= initial_ref.last_source_stream_sequence
            or event.stream_sequence > outcome_ref.last_source_stream_sequence
        ):
            return "integrity-mismatch"

    initial_ids = {claim.claim_id for claim in command.initial_state.claims}
    if accepted_claim_ids & initial_ids:
        return "integrity-mismatch"
    outcome_by_id = {claim.claim_id: claim for claim in outcome.claims}
    if not accepted_claim_ids <= outcome_by_id.keys():
        return "integrity-mismatch"
    for finding in definitive:
        finding_claims = tuple(outcome_by_id[item] for item in finding.observed_claim_ids)
        if {item.source_event_id for item in finding_claims} != set(finding.source_event_ids):
            return "integrity-mismatch"
    for claim_id in accepted_claim_ids:
        claim = outcome_by_id[claim_id]
        event = event_by_id.get(claim.source_event_id)
        if (
            event is None
            or claim.epistemic_status is not TransitionEpistemicStatus.OBSERVED
            or claim.correlation_id != command.run_id
            or claim.domain != outcome_ref.domain
            or claim.stream_id != outcome_ref.stream_id
            or claim.global_position != event.global_position
            or claim.stream_sequence != event.stream_sequence
        ):
            return "integrity-mismatch"
    return None


def _claim_deltas(
    *,
    initial_claims: tuple[TransitionClaim, ...],
    outcome_claims: tuple[TransitionClaim, ...],
    accepted_claims: tuple[TransitionClaim, ...],
) -> tuple[ClaimDelta, ...]:
    initial_by_target: dict[tuple[str, str], list[TransitionClaim]] = defaultdict(list)
    outcome_by_target: dict[tuple[str, str], list[TransitionClaim]] = defaultdict(list)
    accepted_by_target: dict[tuple[str, str], list[TransitionClaim]] = defaultdict(list)
    for claim in initial_claims:
        initial_by_target[claim.target].append(claim)
    for claim in outcome_claims:
        outcome_by_target[claim.target].append(claim)
    for claim in accepted_claims:
        accepted_by_target[claim.target].append(claim)

    deltas: list[ClaimDelta] = []
    for target in sorted(accepted_by_target):
        before = tuple(initial_by_target.get(target, ()))
        before_ids = {item.claim_id for item in before}
        accepted = tuple(accepted_by_target[target])
        accepted_ids = {item.claim_id for item in accepted}
        # Retain initial claims still present after the action and add only accepted
        # claims.  New unrelated concurrent evidence is deliberately outside this
        # transition's evidence scope.
        after = tuple(
            item
            for item in outcome_by_target.get(target, ())
            if item.claim_id in before_ids or item.claim_id in accepted_ids
        )
        deltas.append(
            ClaimDelta(
                subject=target[0],
                predicate=target[1],
                accepted_claim_ids=tuple(item.claim_id for item in accepted),
                before=before,
                after=after,
            )
        )
    return tuple(deltas)


def _conflict_change(delta: ClaimDelta) -> ConflictChange | None:
    before = _conflict(delta.before)
    after = _conflict(delta.after)
    if before == after:
        return None
    return ConflictChange(delta.subject, delta.predicate, before, after)


def _conflict(claims: tuple[TransitionClaim, ...]) -> EvidenceScopedConflict | None:
    observed = tuple(
        sorted(
            (
                item
                for item in claims
                if item.epistemic_status is TransitionEpistemicStatus.OBSERVED
            ),
            key=lambda item: (item.subject, item.predicate, item.claim_id),
        )
    )
    if len({canonical_json_bytes(item.value) for item in observed}) < 2:
        return None
    first = observed[0]
    return EvidenceScopedConflict(
        subject=first.subject,
        predicate=first.predicate,
        source_event_ids=tuple(item.source_event_id for item in observed),
        claim_ids=tuple(item.claim_id for item in observed),
        values=tuple(item.value for item in observed),
    )


def _not_accepted(code: str) -> TransitionAcceptance:
    return TransitionAcceptance(
        status=TransitionAcceptanceStatus.NOT_ACCEPTED,
        code=code,
        transition=None,
    )


__all__ = ["StateTransitionAcceptor"]
