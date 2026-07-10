from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from blackcell.domains.repository import Claim, ClaimConflict, OperationalStateEstimate


@dataclass(frozen=True, slots=True)
class SelectionReason:
    claim_id: str
    reason: str
    score: int


@dataclass(frozen=True, slots=True)
class OmissionSummary:
    omitted_claim_count: int
    omitted_conflict_group_count: int
    omitted_unknown_count: int
    reasons: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class ContextFrame:
    objective: str
    state_id: str
    as_of_sequence: int
    as_of_time: datetime
    selected_claims: tuple[Claim, ...]
    conflicts: tuple[ClaimConflict, ...]
    unknowns: tuple[Claim, ...]
    constraints: tuple[str, ...]
    available_affordances: tuple[str, ...]
    affordance_contracts: tuple[str, ...]
    token_budget: int
    character_budget: int
    selection_reasons: tuple[SelectionReason, ...]
    omission_summary: OmissionSummary
    rendered_context: str
    schema_version: str = "context-frame/v1"
    estimated_tokens: int = field(init=False)
    frame_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("context objective must be non-empty")
        if self.token_budget <= 0 or self.character_budget <= 0:
            raise ValueError("context budgets must be positive")
        if len(self.rendered_context) > self.character_budget:
            raise ValueError("rendered context exceeds its character budget")
        tokens = estimate_tokens(self.rendered_context)
        if tokens > self.token_budget:
            raise ValueError("rendered context exceeds its estimated token budget")
        object.__setattr__(self, "estimated_tokens", tokens)
        object.__setattr__(self, "frame_id", f"context:{content_digest(_frame_payload(self))}")


@dataclass(frozen=True, slots=True)
class BaselineContext:
    renderer: str
    rendered_context: str
    included_references: tuple[str, ...]
    omitted_count: int
    token_budget: int
    character_budget: int
    estimated_tokens: int = field(init=False)
    content_id: str = field(init=False)

    def __post_init__(self) -> None:
        if len(self.rendered_context) > self.character_budget:
            raise ValueError("baseline exceeds its character budget")
        tokens = estimate_tokens(self.rendered_context)
        if tokens > self.token_budget:
            raise ValueError("baseline exceeds its estimated token budget")
        object.__setattr__(self, "estimated_tokens", tokens)
        payload = {
            "renderer": self.renderer,
            "rendered_context": self.rendered_context,
            "included_references": self.included_references,
            "omitted_count": self.omitted_count,
            "token_budget": self.token_budget,
            "character_budget": self.character_budget,
        }
        object.__setattr__(self, "content_id", f"baseline:{content_digest(payload)}")


class ContextProjectorProtocol(Protocol):
    def project(
        self,
        state: OperationalStateEstimate,
        *,
        objective: str,
        constraints: tuple[str, ...] = (),
        available_affordances: tuple[str, ...] = (),
        affordance_contracts: tuple[str, ...] = (),
        required_claim_ids: tuple[str, ...] = (),
        token_budget: int = 2_000,
        character_budget: int = 8_000,
    ) -> ContextFrame: ...


def estimate_tokens(text: str) -> int:
    """A deliberately conservative deterministic estimate, not a tokenizer claim."""

    return (len(text) + 3) // 4


def content_digest(value: object) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _frame_payload(frame: ContextFrame) -> dict[str, object]:
    return {
        "objective": frame.objective,
        "state_id": frame.state_id,
        "as_of_sequence": frame.as_of_sequence,
        "as_of_time": frame.as_of_time,
        "selected_claims": frame.selected_claims,
        "conflicts": frame.conflicts,
        "unknowns": frame.unknowns,
        "constraints": frame.constraints,
        "available_affordances": frame.available_affordances,
        "affordance_contracts": frame.affordance_contracts,
        "token_budget": frame.token_budget,
        "character_budget": frame.character_budget,
        "selection_reasons": frame.selection_reasons,
        "omission_summary": frame.omission_summary,
        "rendered_context": frame.rendered_context,
        "schema_version": frame.schema_version,
    }


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
