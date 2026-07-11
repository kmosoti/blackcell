from collections.abc import Sequence
from datetime import datetime
from typing import Protocol


class ConstraintProofLike(Protocol):
    @property
    def proof_id(self) -> str: ...

    @property
    def constraint_id(self) -> str: ...

    @property
    def constraint_definition_digest(self) -> str: ...

    @property
    def outcome(self) -> object: ...

    @property
    def code(self) -> str: ...

    @property
    def evidence_event_ids(self) -> Sequence[str]: ...


class ConstraintEvaluationLike(Protocol):
    @property
    def context_frame_id(self) -> str: ...

    @property
    def evaluation_id(self) -> str: ...

    @property
    def evaluated_at(self) -> datetime: ...

    @property
    def proofs(self) -> Sequence[ConstraintProofLike]: ...
