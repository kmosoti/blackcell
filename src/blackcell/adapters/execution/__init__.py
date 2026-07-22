"""Execution adapters for Blackcell-owned affordance ports."""

from blackcell.adapters.execution.bubblewrap import (
    BUBBLEWRAP_ISOLATION_POLICY_SCHEMA,
    BubblewrapAcceptanceRunner,
    BubblewrapExecutable,
    BubblewrapIsolationPolicy,
)
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
    "BUBBLEWRAP_ISOLATION_POLICY_SCHEMA",
    "LOCAL_PROCESS_ADAPTER_ID",
    "LOCAL_PROCESS_V1_ACTIVATION_CONTRACT",
    "ArgumentBinding",
    "ArgumentKind",
    "BubblewrapAcceptanceRunner",
    "BubblewrapExecutable",
    "BubblewrapIsolationPolicy",
    "EnvironmentEntry",
    "LocalProcessAdapter",
    "LocalProcessAffordance",
    "LocalProcessConfigurationError",
    "LocalProcessRegistry",
]
