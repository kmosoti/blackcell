from __future__ import annotations

import ipaddress
import os
import socket
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from blackcell.config.runtime import RuntimeSecurityConfig

REPOSITORY_ROOT_ENV = "BLACKCELL_REPOSITORY_ROOT"
GRACEFUL_TIMEOUT_SECONDS_ENV = "BLACKCELL_GRACEFUL_TIMEOUT_SECONDS"
API_BACKPRESSURE_ENV = "BLACKCELL_API_BACKPRESSURE"
WORKER_POLL_MILLISECONDS_ENV = "BLACKCELL_WORKER_POLL_MILLISECONDS"
WORKER_LEASE_SECONDS_ENV = "BLACKCELL_WORKER_LEASE_SECONDS"
WORKER_ID_ENV = "BLACKCELL_WORKER_ID"
OTEL_ENABLED_ENV = "BLACKCELL_OTEL_ENABLED"
OTEL_ENDPOINT_ENV = "BLACKCELL_OTEL_ENDPOINT"
OTEL_TIMEOUT_SECONDS_ENV = "BLACKCELL_OTEL_TIMEOUT_SECONDS"
OTEL_MAX_QUEUE_SIZE_ENV = "BLACKCELL_OTEL_MAX_QUEUE_SIZE"
OTEL_MAX_EXPORT_BATCH_SIZE_ENV = "BLACKCELL_OTEL_MAX_EXPORT_BATCH_SIZE"
OTEL_SCHEDULE_DELAY_MILLISECONDS_ENV = "BLACKCELL_OTEL_SCHEDULE_DELAY_MILLISECONDS"
REQUESTS_PER_MINUTE_ENV = "BLACKCELL_REQUESTS_PER_MINUTE"
ACTIVE_STORAGE_MAX_BYTES_ENV = "BLACKCELL_ACTIVE_STORAGE_MAX_BYTES"
MUTATION_RESERVE_BYTES_ENV = "BLACKCELL_MUTATION_RESERVE_BYTES"

_OTEL_DEPENDENT_ENV = (
    OTEL_ENDPOINT_ENV,
    OTEL_TIMEOUT_SECONDS_ENV,
    OTEL_MAX_QUEUE_SIZE_ENV,
    OTEL_MAX_EXPORT_BATCH_SIZE_ENV,
    OTEL_SCHEDULE_DELAY_MILLISECONDS_ENV,
)


class ProcessConfigFailureCode(StrEnum):
    INVALID_REPOSITORY_ROOT = "invalid-repository-root"
    INVALID_GRACEFUL_TIMEOUT = "invalid-graceful-timeout"
    INVALID_API_BACKPRESSURE = "invalid-api-backpressure"
    INVALID_WORKER_POLL = "invalid-worker-poll"
    INVALID_WORKER_LEASE = "invalid-worker-lease"
    INVALID_WORKER_ID = "invalid-worker-id"
    INVALID_OTEL_CONFIG = "invalid-otel-config"
    INVALID_QUOTA_CONFIG = "invalid-quota-config"


class ProcessConfigError(RuntimeError):
    def __init__(self, code: ProcessConfigFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class RuntimeTelemetryConfig:
    enabled: bool
    endpoint: str | None
    timeout_seconds: int
    max_queue_size: int
    max_export_batch_size: int
    schedule_delay_milliseconds: int


@dataclass(frozen=True, slots=True)
class RuntimeQuotaConfig:
    requests_per_minute: int
    active_storage_max_bytes: int
    mutation_reserve_bytes: int

    @property
    def artifact_max_total_bytes(self) -> int:
        return self.active_storage_max_bytes - self.mutation_reserve_bytes


@dataclass(frozen=True, slots=True)
class RuntimeProcessConfig:
    security: RuntimeSecurityConfig
    repository_root: Path
    graceful_timeout_seconds: int
    api_backpressure: int
    worker_poll_milliseconds: int
    worker_lease_seconds: int
    worker_id: str
    telemetry: RuntimeTelemetryConfig
    quota: RuntimeQuotaConfig

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
        telemetry = _telemetry_config(values)
        quota = _quota_config(values)
        return cls(
            security,
            repository_root,
            graceful,
            backpressure,
            poll,
            lease,
            worker_id,
            telemetry,
            quota,
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


def _telemetry_config(values: Mapping[str, str]) -> RuntimeTelemetryConfig:
    enabled_value = values.get(OTEL_ENABLED_ENV, "0")
    if enabled_value not in {"0", "1"}:
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_OTEL_CONFIG)
    enabled = enabled_value == "1"
    if not enabled:
        if any(name in values for name in _OTEL_DEPENDENT_ENV):
            raise ProcessConfigError(ProcessConfigFailureCode.INVALID_OTEL_CONFIG)
        return RuntimeTelemetryConfig(False, None, 10, 2_048, 512, 5_000)
    endpoint = _telemetry_endpoint(values.get(OTEL_ENDPOINT_ENV))
    timeout = _integer(
        values.get(OTEL_TIMEOUT_SECONDS_ENV, "10"),
        minimum=1,
        maximum=30,
        code=ProcessConfigFailureCode.INVALID_OTEL_CONFIG,
    )
    queue_size = _integer(
        values.get(OTEL_MAX_QUEUE_SIZE_ENV, "2048"),
        minimum=1,
        maximum=8_192,
        code=ProcessConfigFailureCode.INVALID_OTEL_CONFIG,
    )
    batch_size = _integer(
        values.get(OTEL_MAX_EXPORT_BATCH_SIZE_ENV, "512"),
        minimum=1,
        maximum=8_192,
        code=ProcessConfigFailureCode.INVALID_OTEL_CONFIG,
    )
    if batch_size > queue_size:
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_OTEL_CONFIG)
    schedule_delay = _integer(
        values.get(OTEL_SCHEDULE_DELAY_MILLISECONDS_ENV, "5000"),
        minimum=100,
        maximum=60_000,
        code=ProcessConfigFailureCode.INVALID_OTEL_CONFIG,
    )
    return RuntimeTelemetryConfig(
        True,
        endpoint,
        timeout,
        queue_size,
        batch_size,
        schedule_delay,
    )


def _quota_config(values: Mapping[str, str]) -> RuntimeQuotaConfig:
    requests = _integer(
        values.get(REQUESTS_PER_MINUTE_ENV, "600"),
        minimum=1,
        maximum=100_000,
        code=ProcessConfigFailureCode.INVALID_QUOTA_CONFIG,
    )
    active_storage = _integer(
        values.get(ACTIVE_STORAGE_MAX_BYTES_ENV, "10737418240"),
        minimum=1_048_576,
        maximum=1_099_511_627_776,
        code=ProcessConfigFailureCode.INVALID_QUOTA_CONFIG,
    )
    reserve = _integer(
        values.get(MUTATION_RESERVE_BYTES_ENV, "16777216"),
        minimum=4_096,
        maximum=1_073_741_824,
        code=ProcessConfigFailureCode.INVALID_QUOTA_CONFIG,
    )
    if reserve >= active_storage:
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_QUOTA_CONFIG)
    return RuntimeQuotaConfig(requests, active_storage, reserve)


def _telemetry_endpoint(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2_048
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_OTEL_CONFIG)
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_OTEL_CONFIG) from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/v1/traces")
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ProcessConfigError(ProcessConfigFailureCode.INVALID_OTEL_CONFIG)
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as error:
            raise ProcessConfigError(ProcessConfigFailureCode.INVALID_OTEL_CONFIG) from error
        if not address.is_loopback:
            raise ProcessConfigError(ProcessConfigFailureCode.INVALID_OTEL_CONFIG)
    return value


__all__ = [
    "ACTIVE_STORAGE_MAX_BYTES_ENV",
    "API_BACKPRESSURE_ENV",
    "GRACEFUL_TIMEOUT_SECONDS_ENV",
    "MUTATION_RESERVE_BYTES_ENV",
    "OTEL_ENABLED_ENV",
    "OTEL_ENDPOINT_ENV",
    "OTEL_MAX_EXPORT_BATCH_SIZE_ENV",
    "OTEL_MAX_QUEUE_SIZE_ENV",
    "OTEL_SCHEDULE_DELAY_MILLISECONDS_ENV",
    "OTEL_TIMEOUT_SECONDS_ENV",
    "REPOSITORY_ROOT_ENV",
    "REQUESTS_PER_MINUTE_ENV",
    "WORKER_ID_ENV",
    "WORKER_LEASE_SECONDS_ENV",
    "WORKER_POLL_MILLISECONDS_ENV",
    "ProcessConfigError",
    "ProcessConfigFailureCode",
    "RuntimeProcessConfig",
    "RuntimeQuotaConfig",
    "RuntimeTelemetryConfig",
]
