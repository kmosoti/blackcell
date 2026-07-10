from collections.abc import Sequence
from typing import Protocol


class ConstraintProofLike(Protocol):
    @property
    def proof_id(self) -> str: ...

    @property
    def constraint_id(self) -> str: ...

    @property
    def outcome(self) -> object: ...

    @property
    def code(self) -> str: ...


class ConstraintEvaluationLike(Protocol):
    @property
    def evaluation_id(self) -> str: ...

    @property
    def proofs(self) -> Sequence[ConstraintProofLike]: ...
