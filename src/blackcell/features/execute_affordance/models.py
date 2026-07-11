from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast

from blackcell.kernel import JsonScalar
from blackcell.kernel._json import canonical_json, freeze_json, json_digest

_EXECUTION_BINDING_SCHEMA = "execution-binding/v1"
_EXECUTION_PREPARATION_SCHEMA = "execution-preparation/v1"
_EXECUTION_RECOVERY_AUTHORIZATION_SCHEMA = "execution-recovery-authorization/v1"
_EXECUTION_RESULT_SCHEMA = "execution-result/v3"


class SideEffectClass(StrEnum):
    READ_ONLY = "read-only"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ExecutionJournalStatus(StrEnum):
    PREPARED = "prepared"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExecutionOperation(StrEnum):
    EXECUTE = "execute"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class AffordanceArgument:
    name: str
    value: JsonScalar

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("argument name must not be empty")
        frozen = freeze_json(self.value, path=f"$.arguments.{self.name}")
        if isinstance(frozen, Mapping | tuple):
            raise ValueError("affordance argument value must be a JSON scalar")
        object.__setattr__(self, "value", cast("JsonScalar", frozen))


@dataclass(frozen=True, slots=True)
class AffordanceArgumentSpec:
    name: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("argument specification name must not be empty")


@dataclass(frozen=True, slots=True)
class AffordanceDefinition:
    name: str
    adapter_id: str
    side_effect_class: SideEffectClass
    timeout_seconds: float
    arguments: tuple[AffordanceArgumentSpec, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.side_effect_class, SideEffectClass):
            raise ValueError("side_effect_class must be recognized")
        if not self.name.strip() or not self.adapter_id.strip():
            raise ValueError("affordance name and adapter id must not be empty")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, int | float
        ):
            raise ValueError("affordance timeout must be numeric")
        if self.timeout_seconds <= 0:
            raise ValueError("affordance timeout must be positive")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        names = tuple(item.name for item in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("affordance argument names must be unique")


@dataclass(frozen=True, slots=True)
class AffordanceInvocation:
    invocation_id: str
    proposal_id: str
    affordance: str
    arguments: tuple[AffordanceArgument, ...]
    idempotency_key: str
    requested_at: datetime
    action_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("invocation_id", "proposal_id", "affordance", "idempotency_key"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        names = tuple(item.name for item in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("invocation argument names must be unique")
        object.__setattr__(
            self,
            "action_digest",
            json_digest(
                {
                    "schema_version": "authorized-action/v1",
                    "proposal_id": self.proposal_id,
                    "affordance": self.affordance,
                    "arguments": [
                        {"name": item.name, "value": item.value}
                        for item in sorted(self.arguments, key=lambda item: item.name)
                    ],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ObservedEffect:
    subject: str
    predicate: str
    value: JsonScalar

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.predicate.strip():
            raise ValueError("observed effect subject and predicate must not be empty")


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    success: bool
    output_digest: str
    completed_at: datetime
    observed_effects: tuple[ObservedEffect, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.output_digest.strip():
            raise ValueError("output_digest must not be empty")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    invocation_id: str
    proposal_id: str
    authorization_decision_id: str
    affordance: str
    adapter_id: str
    idempotency_key: str
    authorized_action_digest: str
    execution_identity_digest: str
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime
    output_digest: str | None
    observed_effects: tuple[ObservedEffect, ...]
    error_code: str | None
    reconciled: bool
    schema_version: str = "execution-result/v3"
    result_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reconciled, bool):
            raise ValueError("reconciled must be a boolean")
        if self.schema_version != _EXECUTION_RESULT_SCHEMA:
            raise ValueError(f"unsupported execution result schema {self.schema_version!r}")
        if not isinstance(self.status, ExecutionStatus):
            raise ValueError("status must be a recognized execution status")
        for name in (
            "invocation_id",
            "proposal_id",
            "authorization_decision_id",
            "affordance",
            "adapter_id",
            "idempotency_key",
            "authorized_action_digest",
            "execution_identity_digest",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("started_at", "completed_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("execution completion cannot precede its start")
        if self.output_digest is not None and not self.output_digest.strip():
            raise ValueError("output_digest must not be blank")
        if self.error_code is not None and not self.error_code.strip():
            raise ValueError("error_code must not be blank")
        if self.status is ExecutionStatus.UNKNOWN:
            if self.output_digest is not None or self.observed_effects:
                raise ValueError("an unknown execution cannot claim output or observed effects")
        elif self.output_digest is None:
            raise ValueError("a terminal execution requires an output digest")
        object.__setattr__(
            self,
            "result_id",
            json_digest(execution_result_payload(self)),
        )


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    run_id: str
    invocation_id: str
    proposal_id: str
    authorization_decision_id: str
    affordance: str
    adapter_id: str
    idempotency_key: str
    authorized_action_digest: str
    adapter_contract_version: str
    invocation_digest: str
    definition_digest: str
    preparation_id: str
    schema_version: str = "execution-binding/v1"
    execution_identity_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != _EXECUTION_BINDING_SCHEMA:
            raise ValueError(f"unsupported execution binding schema {self.schema_version!r}")
        for name in (
            "run_id",
            "invocation_id",
            "proposal_id",
            "authorization_decision_id",
            "affordance",
            "adapter_id",
            "idempotency_key",
            "authorized_action_digest",
            "adapter_contract_version",
            "invocation_digest",
            "definition_digest",
            "preparation_id",
            "schema_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        object.__setattr__(
            self,
            "execution_identity_digest",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "run_id": self.run_id,
                    "invocation_id": self.invocation_id,
                    "proposal_id": self.proposal_id,
                    "authorization_decision_id": self.authorization_decision_id,
                    "affordance": self.affordance,
                    "adapter_id": self.adapter_id,
                    "idempotency_key": self.idempotency_key,
                    "authorized_action_digest": self.authorized_action_digest,
                    "adapter_contract_version": self.adapter_contract_version,
                    "invocation_digest": self.invocation_digest,
                    "definition_digest": self.definition_digest,
                    "preparation_id": self.preparation_id,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionPreparation:
    run_id: str
    invocation: AffordanceInvocation
    definition: AffordanceDefinition
    authorization_decision_id: str
    authorized_action_digest: str
    adapter_contract_version: str
    schema_version: str = _EXECUTION_PREPARATION_SCHEMA
    preparation_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != _EXECUTION_PREPARATION_SCHEMA:
            raise ValueError(f"unsupported execution preparation schema {self.schema_version!r}")
        for name in (
            "run_id",
            "authorization_decision_id",
            "authorized_action_digest",
            "adapter_contract_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.invocation.affordance != self.definition.name:
            raise ValueError("prepared invocation does not match its affordance definition")
        if self.invocation.action_digest != self.authorized_action_digest:
            raise ValueError("prepared invocation does not match the authorized action")
        object.__setattr__(
            self,
            "preparation_id",
            json_digest(execution_preparation_payload(self)),
        )

    @property
    def binding(self) -> ExecutionBinding:
        invocation_digest = json_digest(_invocation_payload(self.run_id, self.invocation))
        definition_digest = json_digest(_definition_payload(self.definition))
        return ExecutionBinding(
            run_id=self.run_id,
            invocation_id=self.invocation.invocation_id,
            proposal_id=self.invocation.proposal_id,
            authorization_decision_id=self.authorization_decision_id,
            affordance=self.invocation.affordance,
            adapter_id=self.definition.adapter_id,
            idempotency_key=self.invocation.idempotency_key,
            authorized_action_digest=self.authorized_action_digest,
            adapter_contract_version=self.adapter_contract_version,
            invocation_digest=invocation_digest,
            definition_digest=definition_digest,
            preparation_id=self.preparation_id,
        )


@dataclass(frozen=True, slots=True)
class ExecutionRecoveryAuthorization:
    execution_identity_digest: str
    expected_claim_token: str
    expected_fencing_revision: int
    authorized_by: str
    reason: str
    authorized_at: datetime
    original_worker_stopped: bool
    schema_version: str = _EXECUTION_RECOVERY_AUTHORIZATION_SCHEMA
    authorization_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != _EXECUTION_RECOVERY_AUTHORIZATION_SCHEMA:
            raise ValueError(
                f"unsupported execution recovery authorization schema {self.schema_version!r}"
            )
        for name in (
            "execution_identity_digest",
            "expected_claim_token",
            "authorized_by",
            "reason",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.expected_fencing_revision < 1:
            raise ValueError("expected fencing revision must be positive")
        if self.authorized_at.tzinfo is None or self.authorized_at.utcoffset() is None:
            raise ValueError("recovery authorization time must be timezone-aware")
        if self.original_worker_stopped is not True:
            raise ValueError("manual recovery requires confirmation that the worker stopped")
        object.__setattr__(
            self,
            "authorization_id",
            json_digest(
                {
                    "schema_version": self.schema_version,
                    "execution_identity_digest": self.execution_identity_digest,
                    "expected_claim_token": self.expected_claim_token,
                    "expected_fencing_revision": self.expected_fencing_revision,
                    "authorized_by": self.authorized_by,
                    "reason": self.reason,
                    "authorized_at": self.authorized_at.isoformat(),
                    "original_worker_stopped": self.original_worker_stopped,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionRecovery:
    preparation: ExecutionPreparation
    claim: ExecutionClaim
    authorization: ExecutionRecoveryAuthorization

    def __post_init__(self) -> None:
        if self.preparation.binding != self.claim.binding:
            raise ValueError("recovery preparation does not match its claim")
        if (
            self.authorization.execution_identity_digest
            != self.claim.binding.execution_identity_digest
        ):
            raise ValueError("recovery authorization does not match its execution")


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    journal_position: int
    binding: ExecutionBinding
    fencing_revision: int
    claim_token: str
    operation: ExecutionOperation
    acquired_at: datetime
    previous: ExecutionResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ExecutionOperation):
            raise ValueError("operation must be a recognized execution operation")
        if self.journal_position < 1 or self.fencing_revision < 1:
            raise ValueError("journal position and fencing revision must be positive")
        if not self.claim_token.strip():
            raise ValueError("claim token must not be empty")
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise ValueError("claim acquisition time must be timezone-aware")
        if self.operation is ExecutionOperation.EXECUTE and self.previous is not None:
            raise ValueError("an initial execution claim cannot have a previous result")
        if self.previous is not None:
            _validate_result_binding(self.previous, self.binding)
            if self.previous.status is not ExecutionStatus.UNKNOWN:
                raise ValueError("only an unknown result may be reconciled")


@dataclass(frozen=True, slots=True)
class ExecutionJournalEntry:
    journal_position: int
    binding: ExecutionBinding
    status: ExecutionJournalStatus
    current_result: ExecutionResult | None
    active_claim: ExecutionClaim | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExecutionJournalStatus):
            raise ValueError("status must be a recognized execution journal status")
        if self.journal_position < 1:
            raise ValueError("journal position must be positive")
        for name in ("created_at", "updated_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("journal update cannot precede creation")
        if self.status is ExecutionJournalStatus.PREPARED:
            if self.current_result is not None:
                raise ValueError("a prepared execution cannot have a result")
        else:
            if self.current_result is None:
                raise ValueError("a non-prepared execution requires a result")
            if self.status.value != self.current_result.status.value:
                raise ValueError("journal status does not match its current result")
        if self.active_claim is not None:
            if self.active_claim.binding != self.binding:
                raise ValueError("active claim belongs to a different execution binding")
            if self.active_claim.journal_position != self.journal_position:
                raise ValueError("active claim belongs to a different journal entry")


_EXECUTION_PREPARATION_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "invocation",
        "definition",
        "authorization_decision_id",
        "authorized_action_digest",
        "adapter_contract_version",
    }
)
_INVOCATION_KEYS = frozenset(
    {
        "invocation_id",
        "proposal_id",
        "affordance",
        "arguments",
        "idempotency_key",
        "requested_at",
    }
)
_DEFINITION_KEYS = frozenset(
    {"name", "adapter_id", "side_effect_class", "timeout_seconds", "arguments"}
)
_ARGUMENT_KEYS = frozenset({"name", "value"})
_ARGUMENT_SPEC_KEYS = frozenset({"name", "required"})
_EXECUTION_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "proposal_id",
        "authorization_decision_id",
        "affordance",
        "adapter_id",
        "idempotency_key",
        "authorized_action_digest",
        "execution_identity_digest",
        "status",
        "started_at",
        "completed_at",
        "output_digest",
        "observed_effects",
        "error_code",
        "reconciled",
    }
)
_OBSERVED_EFFECT_KEYS = frozenset({"subject", "predicate", "value"})


def _invocation_payload(
    run_id: str,
    invocation: AffordanceInvocation,
) -> Mapping[str, object]:
    return {
        "schema_version": "affordance-invocation/v1",
        "run_id": run_id,
        "invocation_id": invocation.invocation_id,
        "proposal_id": invocation.proposal_id,
        "affordance": invocation.affordance,
        "arguments": [{"name": item.name, "value": item.value} for item in invocation.arguments],
        "idempotency_key": invocation.idempotency_key,
        "requested_at": invocation.requested_at.isoformat(),
    }


def _definition_payload(definition: AffordanceDefinition) -> Mapping[str, object]:
    return {
        "schema_version": "affordance-definition/v1",
        "name": definition.name,
        "adapter_id": definition.adapter_id,
        "side_effect_class": definition.side_effect_class.value,
        "timeout_seconds": definition.timeout_seconds,
        "arguments": [
            {"name": item.name, "required": item.required} for item in definition.arguments
        ],
    }


def execution_preparation_payload(preparation: ExecutionPreparation) -> Mapping[str, object]:
    invocation = dict(_invocation_payload(preparation.run_id, preparation.invocation))
    invocation.pop("schema_version")
    invocation.pop("run_id")
    definition = dict(_definition_payload(preparation.definition))
    definition.pop("schema_version")
    return {
        "schema_version": preparation.schema_version,
        "run_id": preparation.run_id,
        "invocation": invocation,
        "definition": definition,
        "authorization_decision_id": preparation.authorization_decision_id,
        "authorized_action_digest": preparation.authorized_action_digest,
        "adapter_contract_version": preparation.adapter_contract_version,
    }


def serialize_execution_preparation(preparation: ExecutionPreparation) -> str:
    return canonical_json(execution_preparation_payload(preparation))


def deserialize_execution_preparation(
    value: str | bytes,
    *,
    expected_preparation_id: str | None = None,
) -> ExecutionPreparation:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ValueError("execution preparation must be valid JSON") from error
    if not isinstance(decoded, dict) or set(decoded) != _EXECUTION_PREPARATION_KEYS:
        raise ValueError("execution preparation has unexpected fields")
    payload = cast("dict[str, object]", decoded)
    schema_version = _string(payload["schema_version"], "schema_version")
    if schema_version != _EXECUTION_PREPARATION_SCHEMA:
        raise ValueError(f"unsupported execution preparation schema {schema_version!r}")
    raw_invocation = _strict_mapping(payload["invocation"], _INVOCATION_KEYS, "invocation")
    raw_definition = _strict_mapping(payload["definition"], _DEFINITION_KEYS, "definition")
    invocation_arguments = _decode_arguments(raw_invocation["arguments"])
    definition_arguments = _decode_argument_specs(raw_definition["arguments"])
    requested_at = _datetime(raw_invocation["requested_at"], "requested_at")
    timeout = raw_definition["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValueError("definition.timeout_seconds must be a number")
    try:
        side_effect_class = SideEffectClass(
            _string(raw_definition["side_effect_class"], "definition.side_effect_class")
        )
    except ValueError as error:
        raise ValueError("definition.side_effect_class is invalid") from error
    invocation = AffordanceInvocation(
        invocation_id=_string(raw_invocation["invocation_id"], "invocation.invocation_id"),
        proposal_id=_string(raw_invocation["proposal_id"], "invocation.proposal_id"),
        affordance=_string(raw_invocation["affordance"], "invocation.affordance"),
        arguments=invocation_arguments,
        idempotency_key=_string(raw_invocation["idempotency_key"], "invocation.idempotency_key"),
        requested_at=requested_at,
    )
    definition = AffordanceDefinition(
        name=_string(raw_definition["name"], "definition.name"),
        adapter_id=_string(raw_definition["adapter_id"], "definition.adapter_id"),
        side_effect_class=side_effect_class,
        timeout_seconds=float(timeout),
        arguments=definition_arguments,
    )
    preparation = ExecutionPreparation(
        run_id=_string(payload["run_id"], "run_id"),
        invocation=invocation,
        definition=definition,
        authorization_decision_id=_string(
            payload["authorization_decision_id"], "authorization_decision_id"
        ),
        authorized_action_digest=_string(
            payload["authorized_action_digest"], "authorized_action_digest"
        ),
        adapter_contract_version=_string(
            payload["adapter_contract_version"], "adapter_contract_version"
        ),
        schema_version=schema_version,
    )
    if (
        expected_preparation_id is not None
        and preparation.preparation_id != expected_preparation_id
    ):
        raise ValueError("execution preparation identity does not match its canonical content")
    return preparation


def _decode_arguments(value: object) -> tuple[AffordanceArgument, ...]:
    if not isinstance(value, list):
        raise ValueError("invocation.arguments must be an array")
    decoded: list[AffordanceArgument] = []
    for index, item in enumerate(value):
        argument = _strict_mapping(item, _ARGUMENT_KEYS, f"invocation.arguments[{index}]")
        frozen = freeze_json(argument["value"], path=f"$.invocation.arguments[{index}].value")
        if isinstance(frozen, Mapping | tuple):
            raise ValueError(f"invocation.arguments[{index}].value must be a JSON scalar")
        decoded.append(
            AffordanceArgument(
                _string(argument["name"], f"invocation.arguments[{index}].name"),
                cast("JsonScalar", frozen),
            )
        )
    return tuple(decoded)


def _decode_argument_specs(value: object) -> tuple[AffordanceArgumentSpec, ...]:
    if not isinstance(value, list):
        raise ValueError("definition.arguments must be an array")
    decoded: list[AffordanceArgumentSpec] = []
    for index, item in enumerate(value):
        argument = _strict_mapping(item, _ARGUMENT_SPEC_KEYS, f"definition.arguments[{index}]")
        required = argument["required"]
        if not isinstance(required, bool):
            raise ValueError(f"definition.arguments[{index}].required must be a boolean")
        decoded.append(
            AffordanceArgumentSpec(
                _string(argument["name"], f"definition.arguments[{index}].name"),
                required,
            )
        )
    return tuple(decoded)


def _strict_mapping(
    value: object,
    keys: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} has unexpected fields")
    return cast("dict[str, object]", value)


def _datetime(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_string(value, label))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed


def execution_result_payload(result: ExecutionResult) -> Mapping[str, object]:
    return {
        "schema_version": result.schema_version,
        "invocation_id": result.invocation_id,
        "proposal_id": result.proposal_id,
        "authorization_decision_id": result.authorization_decision_id,
        "affordance": result.affordance,
        "adapter_id": result.adapter_id,
        "idempotency_key": result.idempotency_key,
        "authorized_action_digest": result.authorized_action_digest,
        "execution_identity_digest": result.execution_identity_digest,
        "status": result.status.value,
        "started_at": result.started_at.isoformat(),
        "completed_at": result.completed_at.isoformat(),
        "output_digest": result.output_digest,
        "observed_effects": [
            {"subject": item.subject, "predicate": item.predicate, "value": item.value}
            for item in result.observed_effects
        ],
        "error_code": result.error_code,
        "reconciled": result.reconciled,
    }


def serialize_execution_result(result: ExecutionResult) -> str:
    if result.schema_version != _EXECUTION_RESULT_SCHEMA:
        raise ValueError(f"unsupported execution result schema {result.schema_version!r}")
    return canonical_json(execution_result_payload(result))


def deserialize_execution_result(
    value: str | bytes,
    *,
    expected_result_id: str | None = None,
) -> ExecutionResult:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ValueError("execution result must be valid JSON") from error
    if not isinstance(decoded, dict) or set(decoded) != _EXECUTION_RESULT_KEYS:
        raise ValueError("execution result has unexpected fields")
    payload = cast("dict[str, object]", decoded)
    schema_version = _string(payload["schema_version"], "schema_version")
    if schema_version != _EXECUTION_RESULT_SCHEMA:
        raise ValueError(f"unsupported execution result schema {schema_version!r}")
    effects_value = payload["observed_effects"]
    if not isinstance(effects_value, list):
        raise ValueError("observed_effects must be an array")
    effects: list[ObservedEffect] = []
    for index, effect_value in enumerate(effects_value):
        if not isinstance(effect_value, dict) or set(effect_value) != _OBSERVED_EFFECT_KEYS:
            raise ValueError(f"observed_effects[{index}] has unexpected fields")
        effect = cast("dict[str, object]", effect_value)
        frozen_value = freeze_json(effect["value"], path=f"$.observed_effects[{index}].value")
        if isinstance(frozen_value, Mapping | tuple):
            raise ValueError(f"observed_effects[{index}].value must be a JSON scalar")
        effects.append(
            ObservedEffect(
                _string(effect["subject"], f"observed_effects[{index}].subject"),
                _string(effect["predicate"], f"observed_effects[{index}].predicate"),
                cast("JsonScalar", frozen_value),
            )
        )
    output_digest = _optional_string(payload["output_digest"], "output_digest")
    error_code = _optional_string(payload["error_code"], "error_code")
    reconciled = payload["reconciled"]
    if not isinstance(reconciled, bool):
        raise ValueError("reconciled must be a boolean")
    try:
        status = ExecutionStatus(_string(payload["status"], "status"))
        started_at = datetime.fromisoformat(_string(payload["started_at"], "started_at"))
        completed_at = datetime.fromisoformat(_string(payload["completed_at"], "completed_at"))
    except ValueError as error:
        raise ValueError("execution result contains an invalid enum or timestamp") from error
    result = ExecutionResult(
        invocation_id=_string(payload["invocation_id"], "invocation_id"),
        proposal_id=_string(payload["proposal_id"], "proposal_id"),
        authorization_decision_id=_string(
            payload["authorization_decision_id"], "authorization_decision_id"
        ),
        affordance=_string(payload["affordance"], "affordance"),
        adapter_id=_string(payload["adapter_id"], "adapter_id"),
        idempotency_key=_string(payload["idempotency_key"], "idempotency_key"),
        authorized_action_digest=_string(
            payload["authorized_action_digest"], "authorized_action_digest"
        ),
        execution_identity_digest=_string(
            payload["execution_identity_digest"], "execution_identity_digest"
        ),
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        output_digest=output_digest,
        observed_effects=tuple(effects),
        error_code=error_code,
        reconciled=reconciled,
        schema_version=schema_version,
    )
    if expected_result_id is not None and result.result_id != expected_result_id:
        raise ValueError("execution result identity does not match its canonical content")
    return result


def _validate_result_binding(result: ExecutionResult, binding: ExecutionBinding) -> None:
    expected = {
        "invocation_id": binding.invocation_id,
        "proposal_id": binding.proposal_id,
        "authorization_decision_id": binding.authorization_decision_id,
        "affordance": binding.affordance,
        "adapter_id": binding.adapter_id,
        "idempotency_key": binding.idempotency_key,
        "authorized_action_digest": binding.authorized_action_digest,
        "execution_identity_digest": binding.execution_identity_digest,
    }
    mismatches = tuple(
        name for name, expected_value in expected.items() if getattr(result, name) != expected_value
    )
    if mismatches:
        raise ValueError(f"execution result does not match binding: {', '.join(mismatches)}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)
