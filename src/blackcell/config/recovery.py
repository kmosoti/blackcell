from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from blackcell.config.runtime import DATA_DIR_ENV, RuntimePaths

BACKUP_RETENTION_COUNT_ENV = "BLACKCELL_BACKUP_RETENTION_COUNT"


class RecoveryConfigFailureCode(StrEnum):
    INVALID_RECOVERY_CONFIG = "invalid-recovery-config"


class RecoveryConfigError(RuntimeError):
    def __init__(self, code: RecoveryConfigFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryConfig:
    paths: RuntimePaths
    retention_count: int

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        expected_uid: int | None = None,
    ) -> RuntimeRecoveryConfig:
        values = os.environ if environment is None else environment
        data_root = values.get(DATA_DIR_ENV)
        if not isinstance(data_root, str):
            raise RecoveryConfigError(RecoveryConfigFailureCode.INVALID_RECOVERY_CONFIG)
        retention = values.get(BACKUP_RETENTION_COUNT_ENV, "7")
        root = Path(data_root)
        if (
            not root.is_absolute()
            or ".." in root.parts
            or not root.exists()
            or root.is_symlink()
            or not isinstance(retention, str)
            or not retention.isdecimal()
            or not 1 <= int(retention) <= 365
        ):
            raise RecoveryConfigError(RecoveryConfigFailureCode.INVALID_RECOVERY_CONFIG)
        return cls(
            RuntimePaths.prepare(data_root, expected_uid=expected_uid),
            int(retention),
        )


__all__ = [
    "BACKUP_RETENTION_COUNT_ENV",
    "RecoveryConfigError",
    "RecoveryConfigFailureCode",
    "RuntimeRecoveryConfig",
]
