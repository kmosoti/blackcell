from dataclasses import dataclass

from blackcell.gateway.models import DataClassification, ModelCapability


@dataclass(frozen=True, slots=True)
class GatewayProfile:
    profile_id: str
    capability: ModelCapability
    adapter_id: str
    model_id: str
    priority: int
    local: bool
    deterministic: bool
    maximum_classification: DataClassification
    max_input_tokens: int
    max_output_tokens: int
    max_cost_microusd: int
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in ("profile_id", "adapter_id", "model_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if (
            min(
                self.priority,
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_cost_microusd,
            )
            < 0
        ):
            raise ValueError("profile limits and priority must be non-negative")
