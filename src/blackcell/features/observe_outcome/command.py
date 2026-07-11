from __future__ import annotations

from dataclasses import dataclass

from blackcell.features.observe_outcome.models import OutcomeExecutionBinding, OutcomeTarget


@dataclass(frozen=True, slots=True)
class ObserveOutcome:
    """Request a fresh measurement without disclosing expected values to the observer."""

    binding: OutcomeExecutionBinding
    evaluation_spec_id: str
    domain: str
    stream_id: str
    targets: tuple[OutcomeTarget, ...]

    def __post_init__(self) -> None:
        for name in ("evaluation_spec_id", "domain", "stream_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not _is_sha256(self.evaluation_spec_id):
            raise ValueError("evaluation_spec_id must be a SHA-256 digest")
        if not self.targets:
            raise ValueError("outcome observation requires at least one target")
        keys = tuple(item.key for item in self.targets)
        if len(keys) != len(set(keys)):
            raise ValueError("outcome observation targets must be unique")
        object.__setattr__(self, "targets", tuple(sorted(self.targets)))


def _is_sha256(value: str) -> bool:
    hexadecimal = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(hexadecimal) != 64:
        return False
    try:
        int(hexadecimal, 16)
    except ValueError:
        return False
    return True
