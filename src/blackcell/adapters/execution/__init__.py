"""Execution adapters for Blackcell-owned affordance ports."""

from blackcell.adapters.execution.local_process import (
    LOCAL_PROCESS_ADAPTER_ID,
    LOCAL_PROCESS_V1_ACTIVATION_CONTRACT,
    ArgumentBinding,
    ArgumentKind,
    EnvironmentEntry,
    LocalProcessAdapter,
    LocalProcessAffordance,
    LocalProcessConfigurationError,
    LocalProcessRegistry,
)

__all__ = [
    "LOCAL_PROCESS_ADAPTER_ID",
    "LOCAL_PROCESS_V1_ACTIVATION_CONTRACT",
    "ArgumentBinding",
    "ArgumentKind",
    "EnvironmentEntry",
    "LocalProcessAdapter",
    "LocalProcessAffordance",
    "LocalProcessConfigurationError",
    "LocalProcessRegistry",
]
