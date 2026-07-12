from __future__ import annotations

from dataclasses import dataclass

from blackcell.features.evaluate_outcome.models import (
    EvaluationAuthorizationOutcome,
    EvaluationExecutionStatus,
    EvaluationObservation,
    EvaluationSpec,
)


@dataclass(frozen=True, slots=True)
class EvaluateOutcome:
    """Evaluate fresh, independently observed facts against a developer-owned spec."""

    run_id: str
    spec: EvaluationSpec
    authorization_outcome: EvaluationAuthorizationOutcome
    execution_status: EvaluationExecutionStatus | None
    execution_event_id: str | None
    execution_binding_id: str | None
    observation: EvaluationObservation | None
    initial_state_position: int

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not isinstance(self.spec, EvaluationSpec):
            raise TypeError("spec must be an EvaluationSpec")
        if not isinstance(self.authorization_outcome, EvaluationAuthorizationOutcome):
            raise TypeError("authorization_outcome must be recognized")
        if self.execution_status is not None and not isinstance(
            self.execution_status, EvaluationExecutionStatus
        ):
            raise TypeError("execution_status must be recognized")
        if isinstance(self.initial_state_position, bool) or not isinstance(
            self.initial_state_position, int
        ):
            raise TypeError("initial_state_position must be an integer")
        if self.initial_state_position < 0:
            raise ValueError("initial_state_position must be non-negative")
        if self.authorization_outcome is not EvaluationAuthorizationOutcome.ALLOW:
            if any(
                value is not None
                for value in (
                    self.execution_status,
                    self.execution_event_id,
                    self.execution_binding_id,
                    self.observation,
                )
            ):
                raise ValueError(
                    "a blocked authorization cannot carry execution or outcome evidence"
                )
            return
        if (
            self.execution_status is None
            or self.execution_event_id is None
            or self.execution_binding_id is None
        ):
            raise ValueError("an allowed evaluation requires execution identity and status")
        if not self.execution_event_id.strip():
            raise ValueError("execution_event_id must not be empty")
        _require_sha256(self.execution_binding_id, "execution_binding_id")
        if self.execution_status is EvaluationExecutionStatus.UNKNOWN:
            if self.observation is not None:
                raise ValueError("an unknown execution cannot claim an outcome observation")
            return
        if self.observation is None:
            raise ValueError("a terminal execution requires an independent outcome observation")
        if self.observation.evaluation_spec_id != self.spec.spec_id:
            raise ValueError("outcome observation belongs to a different EvaluationSpec")
        if self.observation.execution_binding_id != self.execution_binding_id:
            raise ValueError("outcome observation belongs to a different execution binding")
        if self.observation.execution_status is not self.execution_status:
            raise ValueError("outcome observation belongs to a different execution status")
        stale = tuple(
            source.event_id
            for source in self.observation.sources
            if source.global_position <= self.initial_state_position
        )
        if stale:
            raise ValueError(
                "only outcome events newer than the initial state may satisfy evaluation criteria"
            )
        unrelated = tuple(
            source.event_id
            for source in self.observation.sources
            if source.correlation_id != self.run_id
            or source.causation_id != self.execution_event_id
        )
        if unrelated:
            raise ValueError(
                "outcome evidence must be correlated to the run and caused by its execution event"
            )
        requested_targets = {criterion.target for criterion in self.spec.criteria}
        outside_targets = tuple(
            sorted(
                {
                    fact.target
                    for fact in self.observation.facts
                    if fact.target not in requested_targets
                }
            )
        )
        if outside_targets:
            raise ValueError(
                f"outcome observation contains facts outside EvaluationSpec targets: "
                f"{outside_targets}"
            )


def _require_sha256(value: str, label: str) -> None:
    hexadecimal = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(hexadecimal) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(hexadecimal, 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a SHA-256 digest") from error


__all__ = ["EvaluateOutcome"]
