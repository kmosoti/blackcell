from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class BuildContext:
    task_id: str
    objective: str
    generated_at: datetime
    max_characters: int = 12_000

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.objective.strip():
            raise ValueError("task_id and objective must not be empty")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.max_characters < 1:
            raise ValueError("max_characters must be positive")
