from dataclasses import dataclass

from blackcell.features.retrieve_evidence.models import EvidenceKey


@dataclass(frozen=True, slots=True)
class RetrieveEvidence:
    """Retrieve task evidence with required matches taking precedence over the result target.

    ``max_results`` is the desired upper bound when the required matches fit. Required
    matches may exceed it; only optional candidates are truncated.
    """

    objective: str
    required_keys: tuple[EvidenceKey, ...] = ()
    max_results: int = 12

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("objective must not be empty")
        if self.max_results < 1:
            raise ValueError("max_results must be positive")
        required = {(key.subject, key.predicate) for key in self.required_keys}
        if len(required) != len(self.required_keys):
            raise ValueError("required_keys must not contain duplicates")
