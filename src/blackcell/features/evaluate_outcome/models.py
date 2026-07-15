from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json_bytes, freeze_json, json_digest

EVALUATION_SPEC_SCHEMA_VERSION = "evaluation-spec/v1"
EVALUATION_RESULT_SCHEMA_VERSION = "outcome-evaluation/v1"
OUTCOME_EVIDENCE_EVENT_TYPES = frozenset(
    {"observation.recorded", "outcome.observation-inconclusive"}
)


class EvaluationAuthorizationOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require-approval"


class EvaluationExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class EvaluationObservationStatus(StrEnum):
    OBSERVED = "observed"
    INCONCLUSIVE = "inconclusive"


class EvaluationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_EVALUATED = "not-evaluated"


@dataclass(frozen=True, slots=True, order=True)
class EvaluationCriterion:
    criterion_id: str
    subject: str
    predicate: str
    expected_value: JsonScalar = field(compare=False)
    minimum_confidence: float = field(default=0.0, compare=False)
    required: bool = field(default=True, compare=False)

    def __post_init__(self) -> None:
        for name in ("criterion_id", "subject", "predicate"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if isinstance(self.minimum_confidence, bool) or not isinstance(
            self.minimum_confidence, int | float
        ):
            raise TypeError("criterion minimum_confidence must be numeric")
        confidence = float(self.minimum_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("criterion minimum_confidence must be between zero and one")
        if not isinstance(self.required, bool):
            raise TypeError("criterion required marker must be a boolean")
        object.__setattr__(
            self,
            "expected_value",
            _json_scalar(self.expected_value, f"$.criteria.{self.criterion_id}.expected_value"),
        )
        object.__setattr__(self, "minimum_confidence", confidence)

    @property
    def target(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class EvaluationSpec:
    name: str
    objective: str
    criteria: tuple[EvaluationCriterion, ...]
    schema_version: str = EVALUATION_SPEC_SCHEMA_VERSION
    spec_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EVALUATION_SPEC_SCHEMA_VERSION:
            raise ValueError(f"unsupported EvaluationSpec schema {self.schema_version!r}")
        for name in ("name", "objective"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not self.criteria:
            raise ValueError("an EvaluationSpec requires at least one criterion")
        ordered = tuple(sorted(self.criteria))
        ids = tuple(item.criterion_id for item in ordered)
        targets = tuple(item.target for item in ordered)
        if len(ids) != len(set(ids)):
            raise ValueError("EvaluationSpec criterion ids must be unique")
        if len(targets) != len(set(targets)):
            raise ValueError("EvaluationSpec targets must be unique")
        if not any(item.required for item in ordered):
            raise ValueError("an EvaluationSpec requires at least one required criterion")
        object.__setattr__(self, "criteria", ordered)
        object.__setattr__(self, "spec_id", json_digest(_spec_identity_payload(self)))


@dataclass(frozen=True, slots=True, order=True)
class EvaluationSourceEvent:
    event_id: str
    global_position: int
    event_type: str
    stream_id: str
    correlation_id: str
    causation_id: str
    payload_hash: str

    def __post_init__(self) -> None:
        for name in ("event_id", "event_type", "stream_id", "correlation_id", "causation_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"evaluation source {name} must not be empty")
        if self.event_type not in OUTCOME_EVIDENCE_EVENT_TYPES:
            raise ValueError("evaluation source event type is not outcome evidence")
        _require_sha256(self.payload_hash, "evaluation source payload_hash")
        if isinstance(self.global_position, bool) or not isinstance(self.global_position, int):
            raise TypeError("evaluation source global_position must be an integer")
        if self.global_position < 1:
            raise ValueError("evaluation source global_position must be positive")


@dataclass(frozen=True, slots=True)
class EvaluationFact:
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float
    source_event_id: str

    def __post_init__(self) -> None:
        for name in ("claim_id", "subject", "predicate", "source_event_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, int | float):
            raise TypeError("evaluation fact confidence must be numeric")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("evaluation fact confidence must be between zero and one")
        object.__setattr__(self, "value", _json_scalar(self.value, "$.facts.value"))
        object.__setattr__(self, "confidence", confidence)

    @property
    def target(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    """Evaluation-local evidence view.

    Canonical workflows construct this through the ledger/artifact verifier in
    ``workflows.outcome_evidence``. Its derived ID binds content but does not by
    itself prove that a caller read the cited events from the ledger.
    """

    observation_id: str
    observation_digest: str
    evaluation_spec_id: str
    execution_binding_id: str
    execution_status: EvaluationExecutionStatus
    status: EvaluationObservationStatus
    observed_at: datetime
    sources: tuple[EvaluationSourceEvent, ...]
    facts: tuple[EvaluationFact, ...]
    evidence_binding_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("observation_id must not be empty")
        for name in ("observation_digest", "evaluation_spec_id", "execution_binding_id"):
            _require_sha256(getattr(self, name), name)
        if not isinstance(self.execution_status, EvaluationExecutionStatus):
            raise TypeError("evaluation observation execution_status must be recognized")
        if not isinstance(self.status, EvaluationObservationStatus):
            raise TypeError("evaluation observation status must be recognized")
        observed_at = _timestamp(self.observed_at, "observed_at")
        ordered_sources = tuple(sorted(self.sources))
        source_ids = tuple(item.event_id for item in ordered_sources)
        positions = tuple(item.global_position for item in ordered_sources)
        if not ordered_sources:
            raise ValueError("an evaluation observation requires source events")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("evaluation observation source event ids must be unique")
        if len(positions) != len(set(positions)):
            raise ValueError("evaluation observation source positions must be unique")
        expected_event_type = (
            "observation.recorded"
            if self.status is EvaluationObservationStatus.OBSERVED
            else "outcome.observation-inconclusive"
        )
        if any(item.event_type != expected_event_type for item in ordered_sources):
            raise ValueError("evaluation observation status does not match its source event type")
        if len({item.stream_id for item in ordered_sources}) != 1:
            raise ValueError("evaluation observation sources must share one domain stream")
        ordered_facts = tuple(sorted(self.facts, key=lambda item: item.claim_id))
        fact_ids = tuple(item.claim_id for item in ordered_facts)
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("evaluation observation claim ids must be unique")
        outside_sources = tuple(
            item.source_event_id for item in ordered_facts if item.source_event_id not in source_ids
        )
        if outside_sources:
            raise ValueError("evaluation facts must cite an observation source event")
        if self.status is EvaluationObservationStatus.OBSERVED and not ordered_facts:
            raise ValueError("an observed evaluation outcome requires facts")
        if self.status is EvaluationObservationStatus.INCONCLUSIVE and ordered_facts:
            raise ValueError("an inconclusive evaluation outcome cannot assert facts")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "sources", ordered_sources)
        object.__setattr__(self, "facts", ordered_facts)
        object.__setattr__(
            self,
            "evidence_binding_id",
            json_digest(_observation_evidence_payload(self)),
        )


@dataclass(frozen=True, slots=True)
class EvaluationFinding:
    criterion_id: str
    required: bool
    verdict: EvaluationVerdict
    code: str
    expected_value: JsonScalar
    actual_present: bool
    actual_value: JsonScalar
    actual_confidence: float | None = None
    observed_claim_ids: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("criterion_id", "code"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.required, bool) or not isinstance(self.actual_present, bool):
            raise TypeError("evaluation finding markers must be booleans")
        if not isinstance(self.verdict, EvaluationVerdict):
            raise TypeError("evaluation finding verdict must be recognized")
        expected = _json_scalar(self.expected_value, "$.finding.expected_value")
        actual = _json_scalar(self.actual_value, "$.finding.actual_value")
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
        if any(not item.strip() for item in (*claim_ids, *event_ids)):
            raise ValueError("evaluation finding evidence ids must not be blank")
        if len(claim_ids) != len(set(claim_ids)) or len(event_ids) != len(set(event_ids)):
            raise ValueError("evaluation finding evidence ids must be unique")
        if self.verdict in {EvaluationVerdict.PASS, EvaluationVerdict.FAIL}:
            if not self.actual_present or not claim_ids or not event_ids:
                raise ValueError("a pass or fail finding requires bound observed evidence")
            matched = scalar_values_equal(actual, expected)
            if self.verdict is EvaluationVerdict.PASS and not matched:
                raise ValueError("a pass finding requires the expected value")
            if self.verdict is EvaluationVerdict.FAIL and matched:
                raise ValueError("a fail finding requires an unexpected value")
            expected_code = (
                "expected-value-observed"
                if self.verdict is EvaluationVerdict.PASS
                else "unexpected-value-observed"
            )
            if self.code != expected_code:
                raise ValueError("pass/fail finding code does not match its verdict")
        elif self.verdict is EvaluationVerdict.INCONCLUSIVE:
            _validate_inconclusive_finding(self, claim_ids, event_ids)
        elif self.verdict is EvaluationVerdict.NOT_EVALUATED:
            if self.code not in {"authorization-denied", "authorization-requires-approval"}:
                raise ValueError("not-evaluated finding code is not recognized")
            if self.actual_present or claim_ids or event_ids:
                raise ValueError("a not-evaluated finding cannot claim outcome evidence")
        object.__setattr__(self, "expected_value", expected)
        object.__setattr__(self, "actual_value", actual)
        object.__setattr__(self, "actual_confidence", confidence)
        object.__setattr__(self, "observed_claim_ids", claim_ids)
        object.__setattr__(self, "source_event_ids", event_ids)


def _validate_inconclusive_finding(
    finding: EvaluationFinding,
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


@dataclass(frozen=True, slots=True)
class OutcomeEvaluation:
    run_id: str
    evaluation_spec_id: str
    authorization_outcome: EvaluationAuthorizationOutcome
    execution_status: EvaluationExecutionStatus | None
    execution_event_id: str | None
    execution_binding_id: str | None
    outcome_observation_id: str | None
    outcome_observation_digest: str | None
    outcome_evidence_binding_id: str | None
    initial_state_position: int
    verdict: EvaluationVerdict
    findings: tuple[EvaluationFinding, ...]
    evaluated_at: datetime
    schema_version: str = EVALUATION_RESULT_SCHEMA_VERSION
    evaluation_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EVALUATION_RESULT_SCHEMA_VERSION:
            raise ValueError(f"unsupported OutcomeEvaluation schema {self.schema_version!r}")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        _require_sha256(self.evaluation_spec_id, "evaluation_spec_id")
        if not isinstance(self.authorization_outcome, EvaluationAuthorizationOutcome):
            raise TypeError("authorization_outcome must be recognized")
        if self.execution_status is not None and not isinstance(
            self.execution_status, EvaluationExecutionStatus
        ):
            raise TypeError("execution_status must be recognized")
        if not isinstance(self.verdict, EvaluationVerdict):
            raise TypeError("evaluation verdict must be recognized")
        if isinstance(self.initial_state_position, bool) or not isinstance(
            self.initial_state_position, int
        ):
            raise TypeError("initial_state_position must be an integer")
        if self.initial_state_position < 0:
            raise ValueError("initial_state_position must be non-negative")
        evaluated_at = _timestamp(self.evaluated_at, "evaluated_at")
        findings = tuple(sorted(self.findings, key=lambda item: item.criterion_id))
        if not findings:
            raise ValueError("an OutcomeEvaluation requires findings")
        finding_ids = tuple(item.criterion_id for item in findings)
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("OutcomeEvaluation criterion findings must be unique")
        _validate_result_branch(self, findings)
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "evaluated_at", evaluated_at)
        object.__setattr__(self, "evaluation_id", json_digest(_evaluation_identity_payload(self)))


def _validate_result_branch(
    result: OutcomeEvaluation,
    findings: tuple[EvaluationFinding, ...],
) -> None:
    if result.authorization_outcome is not EvaluationAuthorizationOutcome.ALLOW:
        if any(
            value is not None
            for value in (
                result.execution_status,
                result.execution_event_id,
                result.execution_binding_id,
                result.outcome_observation_id,
                result.outcome_observation_digest,
                result.outcome_evidence_binding_id,
            )
        ):
            raise ValueError("a blocked authorization cannot carry execution or outcome evidence")
        if result.verdict is not EvaluationVerdict.NOT_EVALUATED or any(
            item.verdict is not EvaluationVerdict.NOT_EVALUATED for item in findings
        ):
            raise ValueError("a blocked authorization must be not-evaluated")
        expected_code = (
            "authorization-denied"
            if result.authorization_outcome is EvaluationAuthorizationOutcome.DENY
            else "authorization-requires-approval"
        )
        if any(item.code != expected_code for item in findings):
            raise ValueError("blocked evaluation findings do not match authorization")
        return
    if (
        result.execution_status is None
        or result.execution_event_id is None
        or result.execution_binding_id is None
    ):
        raise ValueError("an allowed evaluation requires execution identity and status")
    if not result.execution_event_id.strip():
        raise ValueError("execution_event_id must not be empty")
    _require_sha256(result.execution_binding_id, "execution_binding_id")
    if result.execution_status is EvaluationExecutionStatus.UNKNOWN:
        if (
            result.outcome_observation_id is not None
            or result.outcome_observation_digest is not None
            or result.outcome_evidence_binding_id is not None
        ):
            raise ValueError("an unknown execution cannot carry outcome observation evidence")
        if result.verdict is not EvaluationVerdict.INCONCLUSIVE or any(
            item.verdict is not EvaluationVerdict.INCONCLUSIVE for item in findings
        ):
            raise ValueError("an unknown execution must evaluate as inconclusive")
        if any(item.code != "execution-unknown" for item in findings):
            raise ValueError("unknown execution findings require the execution-unknown code")
        return
    if result.outcome_observation_id is None or not result.outcome_observation_id.strip():
        raise ValueError("a terminal execution evaluation requires an outcome observation")
    if result.outcome_observation_digest is None:
        raise ValueError("a terminal execution evaluation requires an observation digest")
    _require_sha256(result.outcome_observation_digest, "outcome_observation_digest")
    if result.outcome_evidence_binding_id is None:
        raise ValueError("a terminal execution evaluation requires bound outcome evidence")
    _require_sha256(result.outcome_evidence_binding_id, "outcome_evidence_binding_id")
    expected = _aggregate_verdict(findings)
    if result.verdict is not expected:
        raise ValueError("OutcomeEvaluation verdict does not match required findings")


def _aggregate_verdict(findings: tuple[EvaluationFinding, ...]) -> EvaluationVerdict:
    required = tuple(item for item in findings if item.required)
    if not required:
        raise ValueError("OutcomeEvaluation requires a required finding")
    if any(item.verdict is EvaluationVerdict.FAIL for item in required):
        return EvaluationVerdict.FAIL
    if any(item.verdict is EvaluationVerdict.INCONCLUSIVE for item in required):
        return EvaluationVerdict.INCONCLUSIVE
    if all(item.verdict is EvaluationVerdict.PASS for item in required):
        return EvaluationVerdict.PASS
    raise ValueError("terminal evaluation findings contain an invalid verdict")


def _spec_identity_payload(spec: EvaluationSpec) -> dict[str, object]:
    return {
        "schema_version": spec.schema_version,
        "name": spec.name,
        "objective": spec.objective,
        "criteria": [
            {
                "criterion_id": item.criterion_id,
                "subject": item.subject,
                "predicate": item.predicate,
                "expected_value": item.expected_value,
                "minimum_confidence": item.minimum_confidence,
                "required": item.required,
            }
            for item in spec.criteria
        ],
    }


def _finding_payload(finding: EvaluationFinding) -> dict[str, object]:
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


def _observation_evidence_payload(observation: EvaluationObservation) -> dict[str, object]:
    return {
        "schema_version": "evaluation-observation-binding/v1",
        "observation_id": observation.observation_id,
        "observation_digest": observation.observation_digest,
        "evaluation_spec_id": observation.evaluation_spec_id,
        "execution_binding_id": observation.execution_binding_id,
        "execution_status": observation.execution_status.value,
        "status": observation.status.value,
        "observed_at": observation.observed_at.isoformat(),
        "sources": [
            {
                "event_id": item.event_id,
                "global_position": item.global_position,
                "event_type": item.event_type,
                "stream_id": item.stream_id,
                "correlation_id": item.correlation_id,
                "causation_id": item.causation_id,
                "payload_hash": item.payload_hash,
            }
            for item in observation.sources
        ],
        "facts": [
            {
                "claim_id": item.claim_id,
                "subject": item.subject,
                "predicate": item.predicate,
                "value": item.value,
                "confidence": item.confidence,
                "source_event_id": item.source_event_id,
            }
            for item in observation.facts
        ],
    }


def _evaluation_identity_payload(evaluation: OutcomeEvaluation) -> dict[str, object]:
    return {
        "schema_version": evaluation.schema_version,
        "run_id": evaluation.run_id,
        "evaluation_spec_id": evaluation.evaluation_spec_id,
        "authorization_outcome": evaluation.authorization_outcome.value,
        "execution_status": (
            None if evaluation.execution_status is None else evaluation.execution_status.value
        ),
        "execution_event_id": evaluation.execution_event_id,
        "execution_binding_id": evaluation.execution_binding_id,
        "outcome_observation_id": evaluation.outcome_observation_id,
        "outcome_observation_digest": evaluation.outcome_observation_digest,
        "outcome_evidence_binding_id": evaluation.outcome_evidence_binding_id,
        "initial_state_position": evaluation.initial_state_position,
        "verdict": evaluation.verdict.value,
        "findings": [_finding_payload(item) for item in evaluation.findings],
        "evaluated_at": evaluation.evaluated_at.isoformat(),
    }


def scalar_values_equal(left: JsonScalar, right: JsonScalar) -> bool:
    """Compare JSON scalars without Python's bool/int equality ambiguity."""

    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _json_scalar(value: JsonScalar, path: str) -> JsonScalar:
    frozen = freeze_json(value, path=path)
    if frozen is None or isinstance(frozen, bool | int | float | str):
        return frozen
    raise TypeError(f"{path} must be a JSON scalar")


def _timestamp(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{label} must be a SHA-256 digest")
    hexadecimal = value.removeprefix("sha256:")
    if len(hexadecimal) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a SHA-256 digest") from error


__all__ = [
    "EVALUATION_RESULT_SCHEMA_VERSION",
    "EVALUATION_SPEC_SCHEMA_VERSION",
    "OUTCOME_EVIDENCE_EVENT_TYPES",
    "EvaluationAuthorizationOutcome",
    "EvaluationCriterion",
    "EvaluationExecutionStatus",
    "EvaluationFact",
    "EvaluationFinding",
    "EvaluationObservation",
    "EvaluationObservationStatus",
    "EvaluationSourceEvent",
    "EvaluationSpec",
    "EvaluationVerdict",
    "OutcomeEvaluation",
]
