from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import freeze_json, json_digest

OUTCOME_EXECUTION_BINDING_SCHEMA_VERSION = "outcome-execution-binding/v1"
OUTCOME_OBSERVATION_SCHEMA_VERSION = "outcome-observation/v1"


class OutcomeObservationStatus(StrEnum):
    OBSERVED = "observed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True, order=True)
class OutcomeArgument:
    name: str
    value: JsonScalar

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("outcome argument name must not be empty")
        frozen = freeze_json(self.value, path=f"$.arguments.{self.name}")
        if isinstance(frozen, tuple) or not (
            frozen is None or isinstance(frozen, bool | int | float | str)
        ):
            raise ValueError("outcome argument value must be a JSON scalar")
        object.__setattr__(self, "value", frozen)


@dataclass(frozen=True, slots=True, order=True)
class OutcomeTarget:
    subject: str
    predicate: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.predicate.strip():
            raise ValueError("outcome target subject and predicate must not be empty")

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True)
class OutcomeClaim:
    claim_id: str
    subject: str
    predicate: str
    value: JsonScalar
    confidence: float = 1.0

    def __post_init__(self) -> None:
        for name in ("claim_id", "subject", "predicate"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, int | float):
            raise ValueError("outcome claim confidence must be numeric")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("outcome claim confidence must be between zero and one")
        frozen = freeze_json(self.value, path=f"$.claims.{self.claim_id}.value")
        if isinstance(frozen, tuple) or not (
            frozen is None or isinstance(frozen, bool | int | float | str)
        ):
            raise ValueError("outcome claim value must be a JSON scalar")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "value", frozen)

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True, slots=True, order=True)
class OutcomeEvidencePointer:
    locator: str | None = None
    artifact_id: str | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        values = (self.locator, self.artifact_id, self.digest)
        if not any(value is not None and value.strip() for value in values):
            raise ValueError("outcome evidence requires a locator, artifact_id, or digest")
        if any(value is not None and not value.strip() for value in values):
            raise ValueError("outcome evidence fields must not be blank")
        if self.digest is not None:
            _require_sha256(self.digest, label="outcome evidence digest")


@dataclass(frozen=True, slots=True)
class OutcomeExecutionBinding:
    run_id: str
    invocation_id: str
    proposal_id: str
    proposal_digest: str
    authorization_decision_id: str
    authorized_action_digest: str
    execution_result_id: str
    execution_identity_digest: str
    execution_status: str
    affordance: str
    arguments: tuple[OutcomeArgument, ...]
    execution_adapter_id: str
    execution_adapter_contract_version: str
    completed_at: datetime
    schema_version: str = OUTCOME_EXECUTION_BINDING_SCHEMA_VERSION
    binding_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != OUTCOME_EXECUTION_BINDING_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported outcome execution binding schema {self.schema_version!r}"
            )
        for name in (
            "run_id",
            "invocation_id",
            "proposal_id",
            "authorization_decision_id",
            "execution_status",
            "affordance",
            "execution_adapter_id",
            "execution_adapter_contract_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        for name in (
            "proposal_digest",
            "authorized_action_digest",
            "execution_result_id",
            "execution_identity_digest",
        ):
            _require_sha256(getattr(self, name), label=name)
        _require_aware(self.completed_at, label="execution completion time")
        names = tuple(item.name for item in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("outcome execution argument names must be unique")
        normalized_arguments = tuple(sorted(self.arguments, key=lambda item: item.name))
        object.__setattr__(self, "arguments", normalized_arguments)
        object.__setattr__(self, "completed_at", self.completed_at.astimezone(UTC))
        object.__setattr__(self, "binding_id", json_digest(_binding_identity_payload(self)))


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    observation_id: str
    binding: OutcomeExecutionBinding
    evaluation_spec_id: str
    domain: str
    stream_id: str
    observer_id: str
    observer_contract_version: str
    status: OutcomeObservationStatus
    observed_at: datetime
    claims: tuple[OutcomeClaim, ...]
    evidence: tuple[OutcomeEvidencePointer, ...]
    schema_version: str = OUTCOME_OBSERVATION_SCHEMA_VERSION
    observation_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != OUTCOME_OBSERVATION_SCHEMA_VERSION:
            raise ValueError(f"unsupported outcome observation schema {self.schema_version!r}")
        for name in (
            "observation_id",
            "domain",
            "stream_id",
            "observer_id",
            "observer_contract_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _require_sha256(self.evaluation_spec_id, label="evaluation_spec_id")
        if not isinstance(self.status, OutcomeObservationStatus):
            raise ValueError("outcome observation status must be recognized")
        _require_aware(self.observed_at, label="outcome observation time")
        claim_ids = tuple(item.claim_id for item in self.claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("outcome observation claim ids must be unique")
        if len(self.evidence) != len(set(self.evidence)):
            raise ValueError("outcome observation evidence pointers must be unique")
        if self.status is OutcomeObservationStatus.OBSERVED:
            if not self.claims:
                raise ValueError("an observed outcome requires at least one claim")
            if not self.evidence:
                raise ValueError("an observed outcome requires explicit evidence")
        elif self.claims:
            raise ValueError("an inconclusive outcome cannot assert claims")
        if not self.evidence:
            raise ValueError("an outcome observation requires explicit evidence")
        normalized_claims = tuple(sorted(self.claims, key=lambda item: item.claim_id))
        normalized_evidence = tuple(
            sorted(
                self.evidence,
                key=lambda item: (
                    item.locator or "",
                    item.artifact_id or "",
                    item.digest or "",
                ),
            )
        )
        object.__setattr__(self, "claims", normalized_claims)
        object.__setattr__(self, "evidence", normalized_evidence)
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        object.__setattr__(
            self,
            "observation_digest",
            json_digest(_observation_identity_payload(self)),
        )


def _binding_identity_payload(binding: OutcomeExecutionBinding) -> dict[str, object]:
    return {
        "schema_version": binding.schema_version,
        "run_id": binding.run_id,
        "invocation_id": binding.invocation_id,
        "proposal_id": binding.proposal_id,
        "proposal_digest": binding.proposal_digest,
        "authorization_decision_id": binding.authorization_decision_id,
        "authorized_action_digest": binding.authorized_action_digest,
        "execution_result_id": binding.execution_result_id,
        "execution_identity_digest": binding.execution_identity_digest,
        "execution_status": binding.execution_status,
        "affordance": binding.affordance,
        "arguments": [{"name": item.name, "value": item.value} for item in binding.arguments],
        "execution_adapter_id": binding.execution_adapter_id,
        "execution_adapter_contract_version": binding.execution_adapter_contract_version,
        "completed_at": binding.completed_at.isoformat(),
    }


def _observation_identity_payload(observation: OutcomeObservation) -> dict[str, object]:
    return {
        "schema_version": observation.schema_version,
        "observation_id": observation.observation_id,
        "binding": {
            **_binding_identity_payload(observation.binding),
            "binding_id": observation.binding.binding_id,
        },
        "evaluation_spec_id": observation.evaluation_spec_id,
        "domain": observation.domain,
        "stream_id": observation.stream_id,
        "observer_id": observation.observer_id,
        "observer_contract_version": observation.observer_contract_version,
        "status": observation.status.value,
        "observed_at": observation.observed_at.isoformat(),
        "claims": [
            {
                "claim_id": item.claim_id,
                "subject": item.subject,
                "predicate": item.predicate,
                "value": item.value,
                "confidence": item.confidence,
            }
            for item in observation.claims
        ],
        "evidence": [
            {
                "locator": item.locator,
                "artifact_id": item.artifact_id,
                "digest": item.digest,
            }
            for item in observation.evidence
        ],
    }


def _require_aware(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _require_sha256(value: str, *, label: str) -> None:
    hexadecimal = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(hexadecimal) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a SHA-256 digest") from error
