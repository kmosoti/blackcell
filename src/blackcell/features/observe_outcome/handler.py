from blackcell.features.observe_outcome.command import ObserveOutcome
from blackcell.features.observe_outcome.models import OutcomeObservation
from blackcell.features.observe_outcome.ports import OutcomeObserver


class OutcomeObservationContractError(ValueError):
    """An observer returned evidence outside its exact request contract."""


class CollectOutcomeHandler:
    def __init__(self, observer: OutcomeObserver) -> None:
        if not observer.observer_id.strip():
            raise ValueError("observer_id must not be empty")
        if not observer.contract_version.strip():
            raise ValueError("observer contract_version must not be empty")
        self._observer = observer

    def handle(self, command: ObserveOutcome) -> OutcomeObservation:
        observation = self._observer.observe(command)
        if not isinstance(observation, OutcomeObservation):
            raise OutcomeObservationContractError("observer returned an unsupported result type")
        if observation.binding != command.binding:
            raise OutcomeObservationContractError(
                "outcome observation belongs to a different execution binding"
            )
        if observation.evaluation_spec_id != command.evaluation_spec_id:
            raise OutcomeObservationContractError(
                "outcome observation belongs to a different evaluation specification"
            )
        if observation.domain != command.domain or observation.stream_id != command.stream_id:
            raise OutcomeObservationContractError(
                "outcome observation belongs to a different operational-state scope"
            )
        if observation.observer_id != self._observer.observer_id:
            raise OutcomeObservationContractError("observer identity does not match its result")
        if observation.observer_contract_version != self._observer.contract_version:
            raise OutcomeObservationContractError(
                "observer contract version does not match its result"
            )
        if observation.observed_at < command.binding.completed_at:
            raise OutcomeObservationContractError(
                "outcome observation cannot precede execution completion"
            )
        allowed_targets = {item.key for item in command.targets}
        outside_targets = tuple(
            sorted({item.key for item in observation.claims if item.key not in allowed_targets})
        )
        if outside_targets:
            raise OutcomeObservationContractError(
                f"outcome observation contains claims outside requested targets: {outside_targets}"
            )
        return observation
