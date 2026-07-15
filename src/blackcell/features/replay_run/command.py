from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReplayRun:
    """Inspect one recorded run without applying current runtime components."""

    run_id: str

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
