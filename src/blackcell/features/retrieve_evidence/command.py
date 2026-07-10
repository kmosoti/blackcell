from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvidenceKey:
    subject: str
    predicate: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.predicate.strip():
            raise ValueError("evidence keys require subject and predicate")


@dataclass(frozen=True, slots=True)
class RetrieveEvidence:
    objective: str
    required_keys: tuple[EvidenceKey, ...] = ()
    max_results: int = 12

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("objective must not be empty")
        if self.max_results < 1:
            raise ValueError("max_results must be positive")
