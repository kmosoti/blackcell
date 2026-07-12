from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from blackcell.features.authorize_action import AffordancePolicy
from blackcell.features.build_context import BuildContext
from blackcell.features.derive_signal_packet import DeriveSignalPacket
from blackcell.features.evaluate_outcome import EvaluationSpec
from blackcell.features.execute_affordance import (
    AffordanceDefinition,
    SideEffectClass,
)
from blackcell.features.ingest_observation import IngestObservation, ObservationInput, ObservedClaim
from blackcell.features.request_decision import DecisionCapability, DecisionRequirements
from blackcell.features.retrieve_evidence import RetrieveEvidence
from blackcell.features.solve_constraints import ConstraintDefinition, SolveConstraints
from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json


@dataclass(frozen=True, slots=True)
class DailyOperatorV2Request:
    """Complete immutable policy for one canonical Daily Operator v2 delivery.

    Runtime-derived identities, such as the ContextFrame and model response, do not
    belong here. Everything a caller can choose before the run starts does. The
    workflow owns the run-start causation event and derives the model affordance
    schema from the single authorization/execution affordance declared here.
    """

    run_id: str
    ingestion: IngestObservation
    initial_effective_time_cutoff: datetime
    signal: DeriveSignalPacket
    retrieval: RetrieveEvidence
    context: BuildContext
    constraints: SolveConstraints
    evaluation_spec: EvaluationSpec
    gateway_requirements: DecisionRequirements
    authorization_affordance: AffordancePolicy
    execution_affordance: AffordanceDefinition
    invocation_id: str
    idempotency_key: str
    expected_observer_id: str
    expected_observer_contract_version: str
    approval_granted: bool = False

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "invocation_id",
            "idempotency_key",
            "expected_observer_id",
            "expected_observer_contract_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.approval_granted, bool):
            raise TypeError("approval_granted must be a boolean")

        cutoff = _timestamp(self.initial_effective_time_cutoff, "initial_effective_time_cutoff")
        ingestion = _normalize_ingestion(self.ingestion)
        signal = replace(self.signal, generated_at=_utc(self.signal.generated_at))
        context = replace(self.context, generated_at=_utc(self.context.generated_at))
        constraints = _normalize_constraints(self.constraints)
        authorization = replace(
            self.authorization_affordance,
            allowed_arguments=tuple(sorted(self.authorization_affordance.allowed_arguments)),
        )
        execution = replace(
            self.execution_affordance,
            arguments=tuple(
                sorted(self.execution_affordance.arguments, key=lambda item: item.name)
            ),
        )

        object.__setattr__(self, "initial_effective_time_cutoff", cutoff)
        object.__setattr__(self, "ingestion", ingestion)
        object.__setattr__(self, "signal", signal)
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "authorization_affordance", authorization)
        object.__setattr__(self, "execution_affordance", execution)

        if ingestion.correlation_id != self.run_id:
            raise ValueError("ingestion correlation_id must match run_id")
        if ingestion.causation_id is not None:
            raise ValueError("Daily Operator owns ingestion causation")
        if any(item.effective_at > cutoff for item in ingestion.observations):
            raise ValueError("initial effective-time cutoff must include ingested observations")
        if signal.generated_at < cutoff:
            raise ValueError("signal generation cannot precede the initial effective-time cutoff")
        if context.generated_at < signal.generated_at:
            raise ValueError("context generation cannot precede signal generation")
        if self.gateway_requirements.requested_at < context.generated_at:
            raise ValueError("gateway request cannot precede context generation")
        if constraints.evaluated_at < self.gateway_requirements.requested_at:
            raise ValueError("constraint evaluation cannot precede the gateway request")

        objective = self.context.objective
        if self.retrieval.objective != objective:
            raise ValueError("retrieval and context objectives must match")
        if self.evaluation_spec.objective != objective:
            raise ValueError("EvaluationSpec and context objectives must match")
        if self.gateway_requirements.capability is not DecisionCapability.REASON:
            raise ValueError("Daily Operator requires the gateway reason capability")

        if authorization.name != execution.name:
            raise ValueError("authorization and execution affordances must match")
        execution_is_read_only = execution.side_effect_class is SideEffectClass.READ_ONLY
        if authorization.read_only != execution_is_read_only:
            raise ValueError("authorization and execution side-effect classes must agree")
        allowed_arguments = frozenset(authorization.allowed_arguments)
        execution_arguments = frozenset(item.name for item in execution.arguments)
        if allowed_arguments != execution_arguments:
            raise ValueError("authorization and execution affordance arguments must match")


def _normalize_ingestion(command: IngestObservation) -> IngestObservation:
    observations = tuple(_normalize_observation(item) for item in command.observations)
    return replace(command, observations=observations)


def _normalize_observation(observation: ObservationInput) -> ObservationInput:
    claims = tuple(_normalize_claim(item) for item in observation.claims)
    return replace(
        observation,
        effective_at=_utc(observation.effective_at),
        claims=claims,
    )


def _normalize_claim(claim: ObservedClaim) -> ObservedClaim:
    expires_at = None if claim.expires_at is None else _utc(claim.expires_at)
    return replace(claim, expires_at=expires_at)


def _normalize_constraints(command: SolveConstraints) -> SolveConstraints:
    definitions = tuple(_normalize_constraint(item) for item in command.constraints)
    return replace(
        command,
        evaluated_at=_utc(command.evaluated_at),
        constraints=definitions,
    )


def _normalize_constraint(definition: ConstraintDefinition) -> ConstraintDefinition:
    values: dict[str, JsonScalar] = {}
    for value in definition.expected_values:
        values[canonical_json({"value": value})] = value
    return replace(
        definition,
        expected_values=tuple(values[key] for key in sorted(values)),
    )


def _timestamp(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


__all__ = ["DailyOperatorV2Request"]
