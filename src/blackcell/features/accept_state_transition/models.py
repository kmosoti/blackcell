from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes, freeze_json, json_digest

ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION = "accepted-state-transition/v1"


class StateTransitionIntegrityError(ValueError):
    """A purported definitive transition failed an identity or evidence invariant."""


class TransitionAcceptanceStatus(StrEnum):
    ACCEPTED = "accepted"
    NOT_ACCEPTED = "not-accepted"


class TransitionEvaluationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_EVALUATED = "not-evaluated"


class TransitionExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TransitionAuthorizationOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require-approval"


class TransitionEpistemicStatus(StrEnum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, order=True)
class TransitionEventReference:
    event_id: str
    global_position: int
    stream_sequence: int
    event_type: str
    stream_id: str
    correlation_id: str
    causation_id: str
    payload_hash: str

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "event_type",
            "stream_id",
            "correlation_id",
            "causation_id",
        ):
            _non_empty(getattr(self, name), name)
        _digest(self.payload_hash, "payload_hash")
        _positive_integer(self.global_position, "global_position")
        _positive_integer(self.stream_sequence, "stream_sequence")


@dataclass(frozen=True, slots=True)
class StateSnapshotReference:
    snapshot_digest: str
    domain: str
    stream_id: str
    cutoff_global_position: int
    last_source_stream_sequence: int
    effective_time_cutoff: datetime | None

    def __post_init__(self) -> None:
        _digest(self.snapshot_digest, "snapshot_digest")
        _non_empty(self.domain, "domain")
        _non_empty(self.stream_id, "stream_id")
        _non_negative_integer(self.cutoff_global_position, "cutoff_global_position")
        _non_negative_integer(
            self.last_source_stream_sequence,
            "last_source_stream_sequence",
        )
        if self.effective_time_cutoff is not None:
            object.__setattr__(
                self,
                "effective_time_cutoff",
                _timestamp(self.effective_time_cutoff, "effective_time_cutoff"),
            )

    @property
    def scope(self) -> tuple[str, str]:
        return (self.domain, self.stream_id)


@dataclass(frozen=True, slots=True)
class TransitionClaim:
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    effective_at: datetime
    recorded_at: datetime
    source_event_id: str
    source: str
    actor: str
    correlation_id: str
    domain: str
    stream_id: str
    stream_sequence: int
    global_position: int
    correction_id: str | None = None
    supersedes_claim_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None
    epistemic_status: TransitionEpistemicStatus = TransitionEpistemicStatus.OBSERVED
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "subject",
            "predicate",
            "source_event_id",
            "source",
            "actor",
            "correlation_id",
            "domain",
            "stream_id",
        ):
            _non_empty(getattr(self, name), name)
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, int | float):
            raise TypeError("confidence must be numeric")
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be finite and between zero and one")
        _positive_integer(self.stream_sequence, "stream_sequence")
        _positive_integer(self.global_position, "global_position")
        effective_at = _timestamp(self.effective_at, "effective_at")
        recorded_at = _timestamp(self.recorded_at, "recorded_at")
        expires_at = None if self.expires_at is None else _timestamp(self.expires_at, "expires_at")
        if expires_at is not None and expires_at < effective_at:
            raise ValueError("expires_at cannot precede effective_at")
        if not isinstance(self.epistemic_status, TransitionEpistemicStatus):
            raise TypeError("epistemic_status must be recognized")
        value = _scalar(self.value, "value")
        if self.epistemic_status is TransitionEpistemicStatus.OBSERVED:
            if self.unknown_reason is not None:
                raise ValueError("observed claims cannot have an unknown reason")
        elif value is not None or confidence != 0.0 or not self.unknown_reason:
            raise ValueError("unknown claims require null, zero confidence, and a reason")
        correction_id = self.correction_id
        if correction_id is not None:
            _non_empty(correction_id, "correction_id")
        supersedes = tuple(sorted(self.supersedes_claim_ids))
        if any(not item.strip() for item in supersedes):
            raise ValueError("superseded claim ids must not be blank")
        if len(supersedes) != len(set(supersedes)):
            raise ValueError("superseded claim ids must be unique")
        if correction_id is None and supersedes:
            raise ValueError("only a correction replacement may supersede claims")
        if correction_id is not None and not supersedes:
            raise ValueError("a correction replacement must identify superseded claims")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "supersedes_claim_ids", supersedes)

    @property
    def target(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class TransitionStateView:
    """Snapshot identity plus its current claims, supplied for deterministic differencing.

    A workflow verifier is responsible for proving this view against the cited snapshot
    artifact.  Keeping the verification outside this slice preserves dependency direction.
    """

    reference: StateSnapshotReference
    claims: tuple[TransitionClaim, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reference, StateSnapshotReference):
            raise TypeError("reference must be a StateSnapshotReference")
        claims = tuple(sorted(self.claims, key=_claim_order))
        ids = tuple(item.claim_id for item in claims)
        if len(ids) != len(set(ids)):
            raise ValueError("state-view claim ids must be unique")
        for claim in claims:
            if (claim.domain, claim.stream_id) != self.reference.scope:
                raise ValueError("state-view claims must belong to the snapshot scope")
            if claim.global_position > self.reference.cutoff_global_position:
                raise ValueError("state-view claim exceeds the snapshot global cutoff")
            if claim.stream_sequence > self.reference.last_source_stream_sequence:
                raise ValueError("state-view claim exceeds the snapshot stream cutoff")
            effective_cutoff = self.reference.effective_time_cutoff
            if effective_cutoff is not None and claim.effective_at > effective_cutoff:
                raise ValueError("state-view claim exceeds the snapshot effective-time cutoff")
        object.__setattr__(self, "claims", claims)


@dataclass(frozen=True, slots=True, order=True)
class TransitionActionArgument:
    name: str
    value: JsonScalar = field(compare=False)

    def __post_init__(self) -> None:
        _non_empty(self.name, "argument name")
        object.__setattr__(self, "value", _scalar(self.value, f"arguments.{self.name}"))


@dataclass(frozen=True, slots=True)
class ProposalReference:
    proposal_id: str
    proposal_digest: str
    proposal_artifact_digest: str
    context_frame_id: str
    affordance: str
    arguments: tuple[TransitionActionArgument, ...]
    action_digest: str

    def __post_init__(self) -> None:
        for name in ("proposal_id", "affordance"):
            _non_empty(getattr(self, name), name)
        for name in (
            "proposal_digest",
            "proposal_artifact_digest",
            "context_frame_id",
            "action_digest",
        ):
            _digest(getattr(self, name), name)
        arguments = tuple(sorted(self.arguments))
        names = tuple(item.name for item in arguments)
        if len(names) != len(set(names)):
            raise ValueError("proposal argument names must be unique")
        expected = json_digest(
            {
                "schema_version": "authorized-action/v1",
                "proposal_id": self.proposal_id,
                "affordance": self.affordance,
                "arguments": [{"name": item.name, "value": item.value} for item in arguments],
            }
        )
        if self.action_digest != expected:
            raise ValueError("proposal action_digest does not match its action")
        object.__setattr__(self, "arguments", arguments)


@dataclass(frozen=True, slots=True)
class AuthorizationReference:
    decision_id: str
    decision_artifact_digest: str
    proposal_id: str
    proposal_digest: str
    constraint_evaluation_id: str
    authorized_action_digest: str
    affordance_policy_digest: str
    outcome: TransitionAuthorizationOutcome
    approval_granted: bool

    def __post_init__(self) -> None:
        _non_empty(self.proposal_id, "proposal_id")
        for name in (
            "decision_id",
            "decision_artifact_digest",
            "proposal_digest",
            "constraint_evaluation_id",
            "authorized_action_digest",
            "affordance_policy_digest",
        ):
            _digest(getattr(self, name), name)
        if not isinstance(self.outcome, TransitionAuthorizationOutcome):
            raise TypeError("authorization outcome must be recognized")
        if not isinstance(self.approval_granted, bool):
            raise TypeError("approval_granted must be a boolean")


@dataclass(frozen=True, slots=True)
class ExecutionReference:
    """Content-consistent execution reference; external existence requires workflow proof."""

    run_id: str
    execution_event_id: str
    execution_result_id: str
    execution_result_digest: str
    invocation_id: str
    proposal_id: str
    proposal_digest: str
    authorization_decision_id: str
    execution_binding_id: str
    execution_identity_digest: str
    authorized_action_digest: str
    idempotency_key: str
    affordance: str
    arguments: tuple[TransitionActionArgument, ...]
    adapter_id: str
    adapter_contract_version: str
    status: TransitionExecutionStatus
    completed_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "execution_event_id",
            "invocation_id",
            "proposal_id",
            "idempotency_key",
            "affordance",
            "adapter_id",
            "adapter_contract_version",
        ):
            _non_empty(getattr(self, name), name)
        for name in (
            "execution_result_id",
            "execution_result_digest",
            "proposal_digest",
            "authorization_decision_id",
            "execution_binding_id",
            "execution_identity_digest",
            "authorized_action_digest",
        ):
            _digest(getattr(self, name), name)
        if not isinstance(self.status, TransitionExecutionStatus):
            raise TypeError("execution status must be recognized")
        arguments = tuple(sorted(self.arguments))
        names = tuple(item.name for item in arguments)
        if len(names) != len(set(names)):
            raise ValueError("execution argument names must be unique")
        completed_at = _timestamp(self.completed_at, "completed_at")
        if self.execution_result_digest != self.execution_result_id:
            raise StateTransitionIntegrityError(
                "execution result digest must equal its canonical result id"
            )
        object.__setattr__(self, "arguments", arguments)
        object.__setattr__(self, "completed_at", completed_at)
        expected_binding = json_digest(_execution_binding_identity_payload(self))
        if self.execution_binding_id != expected_binding:
            raise StateTransitionIntegrityError(
                "execution_binding_id does not match outcome execution binding content"
            )


@dataclass(frozen=True, slots=True)
class TransitionEvaluationFinding:
    criterion_id: str
    required: bool
    verdict: TransitionEvaluationVerdict
    code: str
    expected_value: JsonScalar
    actual_present: bool
    actual_value: JsonScalar
    actual_confidence: float | None = None
    observed_claim_ids: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.criterion_id, "criterion_id")
        _non_empty(self.code, "code")
        if not isinstance(self.required, bool) or not isinstance(self.actual_present, bool):
            raise TypeError("evaluation finding markers must be booleans")
        if not isinstance(self.verdict, TransitionEvaluationVerdict):
            raise TypeError("finding verdict must be recognized")
        expected = _scalar(self.expected_value, "finding.expected_value")
        actual = _scalar(self.actual_value, "finding.actual_value")
        if not self.actual_present and actual is not None:
            raise ValueError("a finding without an actual value must use null")
        confidence = self.actual_confidence
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, int | float):
                raise TypeError("finding actual_confidence must be numeric")
            confidence = float(confidence)
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("finding actual_confidence must be between zero and one")
        if self.actual_present is (confidence is None):
            raise ValueError("finding actual presence and confidence must agree")
        claim_ids = tuple(sorted(self.observed_claim_ids))
        event_ids = tuple(sorted(self.source_event_ids))
        for label, values in (("observed claim", claim_ids), ("source event", event_ids)):
            if any(not item.strip() for item in values):
                raise ValueError(f"{label} ids must not be blank")
            if len(values) != len(set(values)):
                raise ValueError(f"{label} ids must be unique")
        if self.verdict in {TransitionEvaluationVerdict.PASS, TransitionEvaluationVerdict.FAIL}:
            if not self.actual_present or not claim_ids or not event_ids:
                raise StateTransitionIntegrityError(
                    "a definitive finding requires bound claims and source events"
                )
            matched = canonical_json_bytes(actual) == canonical_json_bytes(expected)
            if self.verdict is TransitionEvaluationVerdict.PASS and not matched:
                raise ValueError("a pass finding requires the expected value")
            if self.verdict is TransitionEvaluationVerdict.FAIL and matched:
                raise ValueError("a fail finding requires an unexpected value")
            expected_code = (
                "expected-value-observed"
                if self.verdict is TransitionEvaluationVerdict.PASS
                else "unexpected-value-observed"
            )
            if self.code != expected_code:
                raise ValueError("pass/fail finding code does not match its verdict")
        elif self.verdict is TransitionEvaluationVerdict.INCONCLUSIVE:
            _validate_inconclusive_finding(self, claim_ids, event_ids)
        elif self.verdict is TransitionEvaluationVerdict.NOT_EVALUATED:
            if self.code not in {"authorization-denied", "authorization-requires-approval"}:
                raise ValueError("not-evaluated finding code is not recognized")
            if self.actual_present or claim_ids or event_ids:
                raise ValueError("a not-evaluated finding cannot claim outcome evidence")
        object.__setattr__(self, "expected_value", expected)
        object.__setattr__(self, "actual_value", actual)
        object.__setattr__(self, "actual_confidence", confidence)
        object.__setattr__(self, "observed_claim_ids", claim_ids)
        object.__setattr__(self, "source_event_ids", event_ids)


@dataclass(frozen=True, slots=True)
class EvaluationReference:
    """Canonical evaluation identity reference; not owner-artifact or ledger proof."""

    evaluation_id: str
    evaluation_artifact_digest: str
    evaluation_spec_id: str
    evaluation_spec_digest: str
    run_id: str
    authorization_outcome: TransitionAuthorizationOutcome
    execution_status: TransitionExecutionStatus | None
    verdict: TransitionEvaluationVerdict
    execution_event_id: str | None
    execution_binding_id: str | None
    evidence_binding_id: str | None
    owner_observation_id: str | None
    owner_observation_digest: str | None
    owner_observation_artifact_digest: str | None
    initial_state_position: int
    findings: tuple[TransitionEvaluationFinding, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "evaluation_id",
            "evaluation_artifact_digest",
            "evaluation_spec_id",
            "evaluation_spec_digest",
        ):
            _digest(getattr(self, name), name)
        _non_empty(self.run_id, "evaluation run_id")
        if not isinstance(self.authorization_outcome, TransitionAuthorizationOutcome):
            raise TypeError("evaluation authorization_outcome must be recognized")
        if self.execution_status is not None and not isinstance(
            self.execution_status, TransitionExecutionStatus
        ):
            raise TypeError("evaluation execution_status must be recognized")
        if not isinstance(self.verdict, TransitionEvaluationVerdict):
            raise TypeError("evaluation verdict must be recognized")
        _non_negative_integer(self.initial_state_position, "initial_state_position")
        evaluated_at = _timestamp(self.evaluated_at, "evaluated_at")
        findings = tuple(sorted(self.findings, key=lambda item: item.criterion_id))
        ids = tuple(item.criterion_id for item in findings)
        if not findings or len(ids) != len(set(ids)):
            raise ValueError("evaluation findings must be non-empty and uniquely identified")
        _validate_evaluation_branch(self, findings)
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "evaluated_at", evaluated_at)
        expected_id = json_digest(_evaluation_identity_payload(self))
        if self.evaluation_id != expected_id:
            raise StateTransitionIntegrityError(
                "evaluation_id does not match canonical outcome evaluation content"
            )


@dataclass(frozen=True, slots=True)
class ClaimDelta:
    subject: str
    predicate: str
    accepted_claim_ids: tuple[str, ...]
    before: tuple[TransitionClaim, ...]
    after: tuple[TransitionClaim, ...]

    def __post_init__(self) -> None:
        _non_empty(self.subject, "subject")
        _non_empty(self.predicate, "predicate")
        accepted = tuple(sorted(self.accepted_claim_ids))
        before = tuple(sorted(self.before, key=_claim_order))
        after = tuple(sorted(self.after, key=_claim_order))
        if not accepted:
            raise ValueError("a claim delta requires accepted claim ids")
        if len(accepted) != len(set(accepted)):
            raise ValueError("accepted claim ids must be unique")
        if any(item.target != (self.subject, self.predicate) for item in (*before, *after)):
            raise ValueError("claim delta members must share its target")
        after_ids = {item.claim_id for item in after}
        if not set(accepted) <= after_ids:
            raise ValueError("accepted claims must appear in the after side of their delta")
        object.__setattr__(self, "accepted_claim_ids", accepted)
        object.__setattr__(self, "before", before)
        object.__setattr__(self, "after", after)

    @property
    def target(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class EvidenceScopedConflict:
    subject: str
    predicate: str
    source_event_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    values: tuple[JsonScalar, ...]

    def __post_init__(self) -> None:
        _non_empty(self.subject, "subject")
        _non_empty(self.predicate, "predicate")
        if not self.claim_ids or not (
            len(self.source_event_ids) == len(self.claim_ids) == len(self.values)
        ):
            raise ValueError("conflict provenance arrays must be non-empty and aligned")
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise ValueError("conflict claim ids must be unique")
        if any(not item.strip() for item in (*self.source_event_ids, *self.claim_ids)):
            raise ValueError("conflict provenance ids must not be blank")
        values = tuple(_scalar(item, "conflict value") for item in self.values)
        if len({canonical_json_bytes(item) for item in values}) < 2:
            raise ValueError("a conflict requires at least two distinct JSON values")
        object.__setattr__(self, "values", values)


@dataclass(frozen=True, slots=True)
class ConflictChange:
    subject: str
    predicate: str
    before: EvidenceScopedConflict | None
    after: EvidenceScopedConflict | None

    def __post_init__(self) -> None:
        _non_empty(self.subject, "subject")
        _non_empty(self.predicate, "predicate")
        if self.before is None and self.after is None:
            raise ValueError("a conflict change requires a before or after conflict")
        if any(
            item is not None and (item.subject, item.predicate) != (self.subject, self.predicate)
            for item in (self.before, self.after)
        ):
            raise ValueError("conflict change members must share its target")
        if self.before == self.after:
            raise ValueError("an unchanged conflict is not a conflict change")

    @property
    def target(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class AcceptedStateTransition:
    run_id: str
    initial_state: StateSnapshotReference
    outcome_state: StateSnapshotReference
    proposal: ProposalReference
    authorization: AuthorizationReference
    execution: ExecutionReference
    evaluation: EvaluationReference
    triggering_events: tuple[TransitionEventReference, ...]
    accepted_claim_ids: tuple[str, ...]
    accepted_source_event_ids: tuple[str, ...]
    claim_deltas: tuple[ClaimDelta, ...]
    conflict_changes: tuple[ConflictChange, ...]
    schema_version: str = ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION
    transition_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION:
            raise ValueError(f"unsupported AcceptedStateTransition schema {self.schema_version!r}")
        _non_empty(self.run_id, "run_id")
        events = tuple(sorted(self.triggering_events))
        event_ids = tuple(item.event_id for item in events)
        claims = tuple(sorted(self.accepted_claim_ids))
        sources = tuple(sorted(self.accepted_source_event_ids))
        deltas = tuple(sorted(self.claim_deltas, key=lambda item: item.target))
        conflicts = tuple(sorted(self.conflict_changes, key=lambda item: item.target))
        if not events or len(event_ids) != len(set(event_ids)):
            raise ValueError("accepted transition triggering events must be non-empty and unique")
        if not claims or len(claims) != len(set(claims)):
            raise ValueError("accepted transition claim ids must be non-empty and unique")
        if not sources or len(sources) != len(set(sources)):
            raise ValueError("accepted source event ids must be non-empty and unique")
        if set(sources) != set(event_ids):
            raise ValueError("accepted source ids must exactly match triggering events")
        delta_claims = {claim_id for item in deltas for claim_id in item.accepted_claim_ids}
        if delta_claims != set(claims):
            raise ValueError("claim deltas must exactly cover accepted claims")
        object.__setattr__(self, "triggering_events", events)
        object.__setattr__(self, "accepted_claim_ids", claims)
        object.__setattr__(self, "accepted_source_event_ids", sources)
        object.__setattr__(self, "claim_deltas", deltas)
        object.__setattr__(self, "conflict_changes", conflicts)
        _validate_transition_semantics(self)
        object.__setattr__(self, "transition_id", json_digest(_transition_identity_payload(self)))


@dataclass(frozen=True, slots=True)
class TransitionAcceptance:
    status: TransitionAcceptanceStatus
    code: str
    transition: AcceptedStateTransition | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, TransitionAcceptanceStatus):
            raise TypeError("transition acceptance status must be recognized")
        _non_empty(self.code, "code")
        if (self.status is TransitionAcceptanceStatus.ACCEPTED) is (self.transition is None):
            raise ValueError("accepted status and transition artifact must agree")


def _validate_inconclusive_finding(
    finding: TransitionEvaluationFinding,
    claim_ids: tuple[str, ...],
    event_ids: tuple[str, ...],
) -> None:
    if finding.code in {"execution-unknown", "no-fresh-outcome-evidence"}:
        if finding.actual_present or claim_ids or event_ids:
            raise ValueError("unobserved inconclusive findings cannot claim evidence")
        return
    if finding.code == "outcome-observation-inconclusive":
        if finding.actual_present or claim_ids or not event_ids:
            raise ValueError("observer-inconclusive finding requires only source events")
        return
    if finding.code == "conflicting-fresh-outcome-evidence":
        if finding.actual_present or not claim_ids or not event_ids:
            raise ValueError("conflicting finding requires bound claims and source events")
        return
    if finding.code == "outcome-confidence-below-threshold":
        if not finding.actual_present or not claim_ids or not event_ids:
            raise ValueError("low-confidence finding requires bound observed evidence")
        return
    raise ValueError("inconclusive finding code is not recognized")


def _validate_evaluation_branch(
    evaluation: EvaluationReference,
    findings: tuple[TransitionEvaluationFinding, ...],
) -> None:
    owner_fields = (
        evaluation.owner_observation_id,
        evaluation.owner_observation_digest,
        evaluation.owner_observation_artifact_digest,
        evaluation.evidence_binding_id,
    )
    if evaluation.authorization_outcome is not TransitionAuthorizationOutcome.ALLOW:
        if any(
            value is not None
            for value in (
                evaluation.execution_status,
                evaluation.execution_event_id,
                evaluation.execution_binding_id,
                *owner_fields,
            )
        ):
            raise StateTransitionIntegrityError(
                "a blocked evaluation cannot carry execution or owner evidence"
            )
        expected_code = (
            "authorization-denied"
            if evaluation.authorization_outcome is TransitionAuthorizationOutcome.DENY
            else "authorization-requires-approval"
        )
        if evaluation.verdict is not TransitionEvaluationVerdict.NOT_EVALUATED or any(
            item.verdict is not TransitionEvaluationVerdict.NOT_EVALUATED
            or item.code != expected_code
            for item in findings
        ):
            raise StateTransitionIntegrityError(
                "a blocked evaluation must be consistently not-evaluated"
            )
        return
    if (
        evaluation.execution_status is None
        or evaluation.execution_event_id is None
        or evaluation.execution_binding_id is None
    ):
        raise StateTransitionIntegrityError(
            "an allowed evaluation requires execution identity and status"
        )
    _non_empty(evaluation.execution_event_id, "execution_event_id")
    _digest(evaluation.execution_binding_id, "execution_binding_id")
    if evaluation.execution_status is TransitionExecutionStatus.UNKNOWN:
        if any(value is not None for value in owner_fields):
            raise StateTransitionIntegrityError(
                "an unknown execution cannot carry owner observation evidence"
            )
        if evaluation.verdict is not TransitionEvaluationVerdict.INCONCLUSIVE or any(
            item.verdict is not TransitionEvaluationVerdict.INCONCLUSIVE
            or item.code != "execution-unknown"
            for item in findings
        ):
            raise StateTransitionIntegrityError(
                "an unknown execution must be consistently inconclusive"
            )
        return
    if evaluation.execution_status not in {
        TransitionExecutionStatus.SUCCEEDED,
        TransitionExecutionStatus.FAILED,
    }:
        raise StateTransitionIntegrityError("terminal evaluation status is not recognized")
    if evaluation.owner_observation_id is None:
        raise StateTransitionIntegrityError("a terminal evaluation requires owner_observation_id")
    _non_empty(evaluation.owner_observation_id, "owner_observation_id")
    for name in (
        "owner_observation_digest",
        "owner_observation_artifact_digest",
        "evidence_binding_id",
    ):
        value = getattr(evaluation, name)
        if value is None:
            raise StateTransitionIntegrityError(f"a terminal evaluation requires {name}")
        _digest(value, name)
    required = tuple(item for item in findings if item.required)
    if not required:
        raise StateTransitionIntegrityError("a terminal evaluation requires a required finding")
    if _aggregate_findings(required) is not evaluation.verdict:
        raise StateTransitionIntegrityError(
            "evaluation verdict does not match its required findings"
        )


def _aggregate_findings(
    findings: tuple[TransitionEvaluationFinding, ...],
) -> TransitionEvaluationVerdict:
    if any(item.verdict is TransitionEvaluationVerdict.FAIL for item in findings):
        return TransitionEvaluationVerdict.FAIL
    if any(item.verdict is TransitionEvaluationVerdict.INCONCLUSIVE for item in findings):
        return TransitionEvaluationVerdict.INCONCLUSIVE
    if all(item.verdict is TransitionEvaluationVerdict.PASS for item in findings):
        return TransitionEvaluationVerdict.PASS
    return TransitionEvaluationVerdict.NOT_EVALUATED


def _execution_binding_identity_payload(reference: ExecutionReference) -> dict[str, object]:
    return {
        "schema_version": "outcome-execution-binding/v1",
        "run_id": reference.run_id,
        "invocation_id": reference.invocation_id,
        "proposal_id": reference.proposal_id,
        "proposal_digest": reference.proposal_digest,
        "authorization_decision_id": reference.authorization_decision_id,
        "authorized_action_digest": reference.authorized_action_digest,
        "execution_result_id": reference.execution_result_id,
        "execution_identity_digest": reference.execution_identity_digest,
        "execution_status": reference.status.value,
        "affordance": reference.affordance,
        "arguments": [{"name": item.name, "value": item.value} for item in reference.arguments],
        "execution_adapter_id": reference.adapter_id,
        "execution_adapter_contract_version": reference.adapter_contract_version,
        "completed_at": reference.completed_at.isoformat(),
    }


def _evaluation_finding_payload(finding: TransitionEvaluationFinding) -> dict[str, object]:
    return {
        "criterion_id": finding.criterion_id,
        "required": finding.required,
        "verdict": finding.verdict.value,
        "code": finding.code,
        "expected_value": finding.expected_value,
        "actual_present": finding.actual_present,
        "actual_value": finding.actual_value,
        "actual_confidence": finding.actual_confidence,
        "observed_claim_ids": list(finding.observed_claim_ids),
        "source_event_ids": list(finding.source_event_ids),
    }


def _evaluation_identity_payload(reference: EvaluationReference) -> dict[str, object]:
    return {
        "schema_version": "outcome-evaluation/v1",
        "run_id": reference.run_id,
        "evaluation_spec_id": reference.evaluation_spec_id,
        "authorization_outcome": reference.authorization_outcome.value,
        "execution_status": (
            None if reference.execution_status is None else reference.execution_status.value
        ),
        "execution_event_id": reference.execution_event_id,
        "execution_binding_id": reference.execution_binding_id,
        "outcome_observation_id": reference.owner_observation_id,
        "outcome_observation_digest": reference.owner_observation_digest,
        "outcome_evidence_binding_id": reference.evidence_binding_id,
        "initial_state_position": reference.initial_state_position,
        "verdict": reference.verdict.value,
        "findings": [_evaluation_finding_payload(item) for item in reference.findings],
        "evaluated_at": reference.evaluated_at.isoformat(),
    }


def _validate_transition_semantics(transition: AcceptedStateTransition) -> None:
    proposal = transition.proposal
    authorization = transition.authorization
    execution = transition.execution
    evaluation = transition.evaluation
    initial = transition.initial_state
    outcome = transition.outcome_state
    if evaluation.verdict not in {
        TransitionEvaluationVerdict.PASS,
        TransitionEvaluationVerdict.FAIL,
    }:
        raise StateTransitionIntegrityError(
            "an accepted transition requires a definitive evaluation"
        )
    if execution.status not in {
        TransitionExecutionStatus.SUCCEEDED,
        TransitionExecutionStatus.FAILED,
    }:
        raise StateTransitionIntegrityError(
            "an accepted transition cannot bind an unknown execution"
        )
    if authorization.outcome is not TransitionAuthorizationOutcome.ALLOW:
        raise StateTransitionIntegrityError("an accepted transition requires allowed authorization")
    if (
        transition.run_id != execution.run_id
        or transition.run_id != evaluation.run_id
        or proposal.proposal_id != authorization.proposal_id
        or proposal.proposal_digest != authorization.proposal_digest
        or proposal.action_digest != authorization.authorized_action_digest
        or proposal.proposal_id != execution.proposal_id
        or proposal.proposal_digest != execution.proposal_digest
        or proposal.affordance != execution.affordance
        or proposal.arguments != execution.arguments
        or authorization.decision_id != execution.authorization_decision_id
        or authorization.authorized_action_digest != execution.authorized_action_digest
        or authorization.outcome is not evaluation.authorization_outcome
        or execution.status is not evaluation.execution_status
        or evaluation.execution_event_id != execution.execution_event_id
        or evaluation.execution_binding_id != execution.execution_binding_id
        or evaluation.initial_state_position != initial.cutoff_global_position
    ):
        raise StateTransitionIntegrityError("accepted transition action identities do not agree")
    if initial.scope != outcome.scope:
        raise StateTransitionIntegrityError("accepted transition snapshots must share one scope")
    if (
        outcome.cutoff_global_position <= initial.cutoff_global_position
        or outcome.last_source_stream_sequence <= initial.last_source_stream_sequence
    ):
        raise StateTransitionIntegrityError("outcome snapshot must advance both state cutoffs")
    if (
        initial.effective_time_cutoff is None
        or outcome.effective_time_cutoff is None
        or outcome.effective_time_cutoff < initial.effective_time_cutoff
    ):
        raise StateTransitionIntegrityError(
            "outcome effective-time cutoff cannot precede the initial cutoff"
        )
    if evaluation.evaluated_at < outcome.effective_time_cutoff:
        raise StateTransitionIntegrityError(
            "evaluation cannot precede the outcome effective-time cutoff"
        )

    events = {item.event_id: item for item in transition.triggering_events}
    for event in events.values():
        if (
            event.event_type != "observation.recorded"
            or event.stream_id != outcome.stream_id
            or event.correlation_id != transition.run_id
            or event.causation_id != execution.execution_event_id
            or event.global_position <= initial.cutoff_global_position
            or event.global_position > outcome.cutoff_global_position
            or event.stream_sequence <= initial.last_source_stream_sequence
            or event.stream_sequence > outcome.last_source_stream_sequence
        ):
            raise StateTransitionIntegrityError(
                "accepted transition contains an invalid triggering event"
            )

    definitive = tuple(
        item
        for item in evaluation.findings
        if item.verdict in {TransitionEvaluationVerdict.PASS, TransitionEvaluationVerdict.FAIL}
    )
    finding_claim_ids = {claim_id for item in definitive for claim_id in item.observed_claim_ids}
    finding_source_ids = {event_id for item in definitive for event_id in item.source_event_ids}
    if finding_claim_ids != set(transition.accepted_claim_ids) or finding_source_ids != set(
        transition.accepted_source_event_ids
    ):
        raise StateTransitionIntegrityError(
            "accepted claims and sources must exactly match definitive findings"
        )

    before_claims = tuple(claim for delta in transition.claim_deltas for claim in delta.before)
    after_claims = tuple(claim for delta in transition.claim_deltas for claim in delta.after)
    before_ids = tuple(item.claim_id for item in before_claims)
    after_ids = tuple(item.claim_id for item in after_claims)
    if len(before_ids) != len(set(before_ids)) or len(after_ids) != len(set(after_ids)):
        raise StateTransitionIntegrityError(
            "accepted transition claim deltas cannot repeat claim ids"
        )
    before_by_id = {item.claim_id: item for item in before_claims}
    after_by_id = {item.claim_id: item for item in after_claims}
    if set(transition.accepted_claim_ids) & before_by_id.keys():
        raise StateTransitionIntegrityError(
            "accepted claims cannot already exist in the initial state"
        )
    for delta in transition.claim_deltas:
        scoped_after_ids = {item.claim_id for item in delta.after}
        permitted = {item.claim_id for item in delta.before} | set(delta.accepted_claim_ids)
        if scoped_after_ids - permitted:
            raise StateTransitionIntegrityError(
                "claim deltas cannot include unrelated concurrent evidence"
            )
    for claim in before_claims:
        _claim_within_snapshot(claim, initial, "initial")
    for claim in after_claims:
        _claim_within_snapshot(claim, outcome, "outcome")
    for finding in definitive:
        try:
            claims = tuple(after_by_id[item] for item in finding.observed_claim_ids)
        except KeyError as error:
            raise StateTransitionIntegrityError(
                "definitive finding claim is absent from transition deltas"
            ) from error
        if {item.source_event_id for item in claims} != set(finding.source_event_ids):
            raise StateTransitionIntegrityError(
                "definitive finding claim and source provenance do not agree"
            )
    for claim_id in transition.accepted_claim_ids:
        claim = after_by_id[claim_id]
        event = events.get(claim.source_event_id)
        if (
            event is None
            or claim.epistemic_status is not TransitionEpistemicStatus.OBSERVED
            or claim.correlation_id != transition.run_id
            or claim.global_position != event.global_position
            or claim.stream_sequence != event.stream_sequence
        ):
            raise StateTransitionIntegrityError(
                "accepted claim does not match its triggering event"
            )

    expected_conflicts = tuple(
        change
        for delta in transition.claim_deltas
        if (change := _expected_conflict_change(delta)) is not None
    )
    if expected_conflicts != transition.conflict_changes:
        raise StateTransitionIntegrityError(
            "conflict changes must exactly match evidence-scoped claim deltas"
        )


def _claim_within_snapshot(
    claim: TransitionClaim,
    reference: StateSnapshotReference,
    label: str,
) -> None:
    if (
        (claim.domain, claim.stream_id) != reference.scope
        or claim.global_position > reference.cutoff_global_position
        or claim.stream_sequence > reference.last_source_stream_sequence
        or (
            reference.effective_time_cutoff is not None
            and claim.effective_at > reference.effective_time_cutoff
        )
    ):
        raise StateTransitionIntegrityError(f"{label} delta claim exceeds its snapshot identity")


def _expected_conflict_change(delta: ClaimDelta) -> ConflictChange | None:
    before = _evidence_conflict(delta.before)
    after = _evidence_conflict(delta.after)
    if before == after:
        return None
    return ConflictChange(delta.subject, delta.predicate, before, after)


def _evidence_conflict(
    claims: tuple[TransitionClaim, ...],
) -> EvidenceScopedConflict | None:
    observed = tuple(
        item for item in claims if item.epistemic_status is TransitionEpistemicStatus.OBSERVED
    )
    if len({canonical_json_bytes(item.value) for item in observed}) < 2:
        return None
    return EvidenceScopedConflict(
        subject=observed[0].subject,
        predicate=observed[0].predicate,
        source_event_ids=tuple(item.source_event_id for item in observed),
        claim_ids=tuple(item.claim_id for item in observed),
        values=tuple(item.value for item in observed),
    )


def _claim_order(claim: TransitionClaim) -> tuple[str, str, str]:
    return (claim.subject, claim.predicate, claim.claim_id)


def _transition_identity_payload(transition: AcceptedStateTransition) -> dict[str, object]:
    return {
        "schema_version": transition.schema_version,
        "run_id": transition.run_id,
        "initial_state": _snapshot_payload(transition.initial_state),
        "outcome_state": _snapshot_payload(transition.outcome_state),
        "proposal": _proposal_payload(transition.proposal),
        "authorization": _authorization_payload(transition.authorization),
        "execution": _execution_payload(transition.execution),
        "evaluation": _evaluation_payload(transition.evaluation),
        "triggering_events": [_event_payload(item) for item in transition.triggering_events],
        "accepted_claim_ids": list(transition.accepted_claim_ids),
        "accepted_source_event_ids": list(transition.accepted_source_event_ids),
        "claim_deltas": [_delta_payload(item) for item in transition.claim_deltas],
        "conflict_changes": [
            _conflict_change_payload(item) for item in transition.conflict_changes
        ],
    }


def _snapshot_payload(reference: StateSnapshotReference) -> dict[str, object]:
    return {
        "snapshot_digest": reference.snapshot_digest,
        "domain": reference.domain,
        "stream_id": reference.stream_id,
        "cutoff_global_position": reference.cutoff_global_position,
        "last_source_stream_sequence": reference.last_source_stream_sequence,
        "effective_time_cutoff": (
            None
            if reference.effective_time_cutoff is None
            else reference.effective_time_cutoff.isoformat()
        ),
    }


def _proposal_payload(reference: ProposalReference) -> dict[str, object]:
    return {
        "proposal_id": reference.proposal_id,
        "proposal_digest": reference.proposal_digest,
        "proposal_artifact_digest": reference.proposal_artifact_digest,
        "context_frame_id": reference.context_frame_id,
        "affordance": reference.affordance,
        "arguments": [{"name": item.name, "value": item.value} for item in reference.arguments],
        "action_digest": reference.action_digest,
    }


def _authorization_payload(reference: AuthorizationReference) -> dict[str, object]:
    return {
        "decision_id": reference.decision_id,
        "decision_artifact_digest": reference.decision_artifact_digest,
        "proposal_id": reference.proposal_id,
        "proposal_digest": reference.proposal_digest,
        "constraint_evaluation_id": reference.constraint_evaluation_id,
        "authorized_action_digest": reference.authorized_action_digest,
        "affordance_policy_digest": reference.affordance_policy_digest,
        "outcome": reference.outcome.value,
        "approval_granted": reference.approval_granted,
    }


def _execution_payload(reference: ExecutionReference) -> dict[str, object]:
    return {
        "run_id": reference.run_id,
        "execution_event_id": reference.execution_event_id,
        "execution_result_id": reference.execution_result_id,
        "execution_result_digest": reference.execution_result_digest,
        "invocation_id": reference.invocation_id,
        "proposal_id": reference.proposal_id,
        "proposal_digest": reference.proposal_digest,
        "authorization_decision_id": reference.authorization_decision_id,
        "execution_binding_id": reference.execution_binding_id,
        "execution_identity_digest": reference.execution_identity_digest,
        "authorized_action_digest": reference.authorized_action_digest,
        "idempotency_key": reference.idempotency_key,
        "affordance": reference.affordance,
        "arguments": [{"name": item.name, "value": item.value} for item in reference.arguments],
        "adapter_id": reference.adapter_id,
        "adapter_contract_version": reference.adapter_contract_version,
        "status": reference.status.value,
        "completed_at": reference.completed_at.isoformat(),
    }


def _evaluation_payload(reference: EvaluationReference) -> dict[str, object]:
    return {
        "evaluation_id": reference.evaluation_id,
        "evaluation_artifact_digest": reference.evaluation_artifact_digest,
        "evaluation_spec_id": reference.evaluation_spec_id,
        "evaluation_spec_digest": reference.evaluation_spec_digest,
        "run_id": reference.run_id,
        "authorization_outcome": reference.authorization_outcome.value,
        "execution_status": (
            None if reference.execution_status is None else reference.execution_status.value
        ),
        "verdict": reference.verdict.value,
        "execution_event_id": reference.execution_event_id,
        "execution_binding_id": reference.execution_binding_id,
        "evidence_binding_id": reference.evidence_binding_id,
        "owner_observation_id": reference.owner_observation_id,
        "owner_observation_digest": reference.owner_observation_digest,
        "owner_observation_artifact_digest": reference.owner_observation_artifact_digest,
        "initial_state_position": reference.initial_state_position,
        "findings": [_evaluation_finding_payload(item) for item in reference.findings],
        "evaluated_at": reference.evaluated_at.isoformat(),
    }


def _event_payload(reference: TransitionEventReference) -> dict[str, object]:
    return {
        "event_id": reference.event_id,
        "global_position": reference.global_position,
        "stream_sequence": reference.stream_sequence,
        "event_type": reference.event_type,
        "stream_id": reference.stream_id,
        "correlation_id": reference.correlation_id,
        "causation_id": reference.causation_id,
        "payload_hash": reference.payload_hash,
    }


def _claim_payload(claim: TransitionClaim) -> dict[str, object]:
    return {
        "claim_id": claim.claim_id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value": claim.value,
        "confidence": claim.confidence,
        "effective_at": claim.effective_at.isoformat(),
        "recorded_at": claim.recorded_at.isoformat(),
        "source_event_id": claim.source_event_id,
        "source": claim.source,
        "actor": claim.actor,
        "correlation_id": claim.correlation_id,
        "domain": claim.domain,
        "stream_id": claim.stream_id,
        "stream_sequence": claim.stream_sequence,
        "global_position": claim.global_position,
        "correction_id": claim.correction_id,
        "supersedes_claim_ids": list(claim.supersedes_claim_ids),
        "expires_at": None if claim.expires_at is None else claim.expires_at.isoformat(),
        "epistemic_status": claim.epistemic_status.value,
        "unknown_reason": claim.unknown_reason,
    }


def _delta_payload(delta: ClaimDelta) -> dict[str, object]:
    return {
        "subject": delta.subject,
        "predicate": delta.predicate,
        "accepted_claim_ids": list(delta.accepted_claim_ids),
        "before": [_claim_payload(item) for item in delta.before],
        "after": [_claim_payload(item) for item in delta.after],
    }


def _conflict_payload(conflict: EvidenceScopedConflict) -> dict[str, object]:
    return {
        "subject": conflict.subject,
        "predicate": conflict.predicate,
        "source_event_ids": list(conflict.source_event_ids),
        "claim_ids": list(conflict.claim_ids),
        "values": list(conflict.values),
    }


def _conflict_change_payload(change: ConflictChange) -> dict[str, object]:
    return {
        "subject": change.subject,
        "predicate": change.predicate,
        "before": None if change.before is None else _conflict_payload(change.before),
        "after": None if change.after is None else _conflict_payload(change.after),
    }


def _non_empty(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _digest(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{label} must be a SHA-256 digest")
    hexadecimal = value.removeprefix("sha256:")
    if len(hexadecimal) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a SHA-256 digest") from error


def _positive_integer(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < 1:
        raise ValueError(f"{label} must be positive")


def _non_negative_integer(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < 0:
        raise ValueError(f"{label} must be non-negative")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _scalar(value: object, label: str) -> JsonScalar:
    frozen = freeze_json(value, path=f"$.{label}")
    if frozen is None or isinstance(frozen, bool | int | float | str):
        return frozen
    raise TypeError(f"{label} must be a JSON scalar")


__all__ = [
    "ACCEPTED_STATE_TRANSITION_SCHEMA_VERSION",
    "AcceptedStateTransition",
    "AuthorizationReference",
    "ClaimDelta",
    "ConflictChange",
    "EvaluationReference",
    "EvidenceScopedConflict",
    "ExecutionReference",
    "ProposalReference",
    "StateSnapshotReference",
    "StateTransitionIntegrityError",
    "TransitionAcceptance",
    "TransitionAcceptanceStatus",
    "TransitionActionArgument",
    "TransitionAuthorizationOutcome",
    "TransitionClaim",
    "TransitionEpistemicStatus",
    "TransitionEvaluationFinding",
    "TransitionEvaluationVerdict",
    "TransitionEventReference",
    "TransitionExecutionStatus",
    "TransitionStateView",
]
