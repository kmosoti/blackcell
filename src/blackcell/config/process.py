from __future__ import annotations

import os
import socket
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from blackcell.config.runtime import RuntimeSecurityConfig

REPOSITORY_ROOT_ENV = "BLACKCELL_REPOSITORY_ROOT"
GRACEFUL_TIMEOUT_SECONDS_ENV = "BLACKCELL_GRACEFUL_TIMEOUT_SECONDS"
API_BACKPRESSURE_ENV = "BLACKCELL_API_BACKPRESSURE"
WORKER_POLL_MILLISECONDS_ENV = "BLACKCELL_WORKER_POLL_MILLISECONDS"
WORKER_LEASE_SECONDS_ENV = "BLACKCELL_WORKER_LEASE_SECONDS"
WORKER_ID_ENV = "BLACKCELL_WORKER_ID"


class ProcessConfigFailureCode(StrEnum):
    INVALID_REPOSITORY_ROOT = "invalid-repository-root"
    INVALID_GRACEFUL_TIMEOUT = "invalid-graceful-timeout"
    INVALID_API_BACKPRESSURE = "invalid-api-backpressure"
    INVALID_WORKER_POLL = "invalid-worker-poll"
    INVALID_WORKER_LEASE = "invalid-worker-lease"
    INVALID_WORKER_ID = "invalid-worker-id"


class ProcessConfigError(RuntimeError):
    def __init__(self, code: ProcessConfigFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class RuntimeProcessConfig:
    security: RuntimeSecurityConfig
    repository_root: Path
    graceful_timeout_seconds: int
    api_backpressure: int
    worker_poll_milliseconds: int
    worker_lease_seconds: int
    worker_id: str

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        expected_uid: int | None = None,
        process_id: int | None = None,
        hostname: str | None = None,
    ) -> RuntimeProcessConfig:
        values = os.environ if environment is None else environment
        security = RuntimeSecurityConfig.from_environment(values, expected_uid=expected_uid)
        repository_root = _repository_root(values.get(REPOSITORY_ROOT_ENV))
        graceful = _integer(
            values.get(GRACEFUL_TIMEOUT_SECONDS_ENV, "30"),
            minimum=1,
            maximum=300,
            code=ProcessConfigFailureCode.INVALID_GRACEFUL_TIMEOUT,
        )
        backpressure = _integer(
            values.get(API_BACKPRESSURE_ENV, "64"),
            minimum=1,
            maximum=1_024,
            code=ProcessConfigFailureCode.INVALID_API_BACKPRESSURE,
        )
        poll = _integer(
            values.get(WORKER_POLL_MILLISECONDS_ENV, "250"),
            minimum=10,
            maximum=60_000,
            code=ProcessConfigFailureCode.INVALID_WORKER_POLL,
        )
        lease = _integer(
            values.get(WORKER_LEASE_SECONDS_ENV, "30"),
            minimum=1,
            maximum=86_400,
            code=ProcessConfigFailureCode.INVALID_WORKER_LEASE,
        )
        default_worker_id = f"worker:{hostname or socket.gethostname()}:{process_id or os.getpid()}"
        worker_id = values.get(WORKER_ID_ENV, default_worker_id)
        if (
            not isinstance(worker_id, str)
            or not worker_id.strip()
            or len(worker_id) > 200
            or any(not 0x21 <= ord(character) <= 0x7E for character in worker_id)
        ):
            raise ProcessConfigError(ProcessConfigFailureCode.INVALID_WORKER_ID)
        return cls(
            security,
            repository_root,
            graceful,
            backpressure,
            poll,
            lease,
            worker_id,
        )


def _repository_root(value: str | None) -> Path:
    if not isinstance(value, str) or not value:
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_REPOSITORY_ROOT)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_REPOSITORY_ROOT)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_REPOSITORY_ROOT) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_REPOSITORY_ROOT)
    return path


def _integer(
    value: str,
    *,
    minimum: int,
    maximum: int,
    code: ProcessConfigFailureCode,
) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise ProcessConfigError(code)
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ProcessConfigError(code)
    return parsed


__all__ = [
    "API_BACKPRESSURE_ENV",
    "GRACEFUL_TIMEOUT_SECONDS_ENV",
    "REPOSITORY_ROOT_ENV",
    "WORKER_ID_ENV",
    "WORKER_LEASE_SECONDS_ENV",
    "WORKER_POLL_MILLISECONDS_ENV",
    "ProcessConfigError",
    "ProcessConfigFailureCode",
    "RuntimeProcessConfig",
]
