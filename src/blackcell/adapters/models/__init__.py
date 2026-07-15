"""Model gateway adapters."""

from blackcell.adapters.models.codex_cli import (
    CODEX_CLI_ADAPTER_ID,
    CodexCliAdapterError,
    CodexCliModelAdapter,
    CodexCliOutputError,
    CodexCliTimeoutError,
)
from blackcell.adapters.models.gateway_decision import GatewayDecisionAdapter
from blackcell.adapters.models.recorded import RecordedModelAdapter

__all__ = [
    "CODEX_CLI_ADAPTER_ID",
    "CodexCliAdapterError",
    "CodexCliModelAdapter",
    "CodexCliOutputError",
    "CodexCliTimeoutError",
    "GatewayDecisionAdapter",
    "RecordedModelAdapter",
]
