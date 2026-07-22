from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from blackcell.config import (
    ACTIVE_STORAGE_MAX_BYTES_ENV,
    API_BACKPRESSURE_ENV,
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    GRACEFUL_TIMEOUT_SECONDS_ENV,
    MUTATION_RESERVE_BYTES_ENV,
    OTEL_ENABLED_ENV,
    OTEL_ENDPOINT_ENV,
    OTEL_MAX_EXPORT_BATCH_SIZE_ENV,
    OTEL_MAX_QUEUE_SIZE_ENV,
    OTEL_SCHEDULE_DELAY_MILLISECONDS_ENV,
    OTEL_TIMEOUT_SECONDS_ENV,
    REPOSITORY_ROOT_ENV,
    REQUESTS_PER_MINUTE_ENV,
    WORKER_ID_ENV,
    WORKER_LEASE_SECONDS_ENV,
    WORKER_POLL_MILLISECONDS_ENV,
    ProcessConfigError,
    ProcessConfigFailureCode,
    RuntimeProcessConfig,
)

TOKEN = "Runtime-v1_process-token.0123456789-ABCDEFG"


def test_process_config_uses_bounded_explicit_runtime_defaults(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")

    config = RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
        },
        hostname="runtime-host",
        process_id=42,
    )

    assert config.repository_root == repository
    assert config.graceful_timeout_seconds == 30
    assert config.api_backpressure == 64
    assert config.worker_poll_milliseconds == 250
    assert config.worker_lease_seconds == 30
    assert config.worker_id == "worker:runtime-host:42"
    assert config.alpha_worker is None
    assert config.alpha_review_worker is None
    assert config.alpha_verify_worker is None
    assert not config.telemetry.enabled
    assert config.telemetry.endpoint is None
    assert config.quota.requests_per_minute == 600
    assert config.quota.active_storage_max_bytes == 10_737_418_240
    assert config.quota.mutation_reserve_bytes == 16_777_216
    assert config.quota.artifact_max_total_bytes == 10_720_641_024


def test_process_config_accepts_explicit_bounded_lifecycle_values(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")

    config = RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            GRACEFUL_TIMEOUT_SECONDS_ENV: "300",
            API_BACKPRESSURE_ENV: "1024",
            WORKER_POLL_MILLISECONDS_ENV: "60000",
            WORKER_LEASE_SECONDS_ENV: "86400",
            WORKER_ID_ENV: "worker:runtime-1",
            REQUESTS_PER_MINUTE_ENV: "100000",
            ACTIVE_STORAGE_MAX_BYTES_ENV: "1048576",
            MUTATION_RESERVE_BYTES_ENV: "4096",
        }
    )

    assert config.graceful_timeout_seconds == 300
    assert config.api_backpressure == 1024
    assert config.worker_poll_milliseconds == 60_000
    assert config.worker_lease_seconds == 86_400
    assert config.worker_id == "worker:runtime-1"
    assert config.quota.requests_per_minute == 100_000
    assert config.quota.active_storage_max_bytes == 1_048_576
    assert config.quota.mutation_reserve_bytes == 4_096


def test_process_config_accepts_bounded_explicit_otlp_http_export(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")

    config = RuntimeProcessConfig.from_environment(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            OTEL_ENABLED_ENV: "1",
            OTEL_ENDPOINT_ENV: "http://127.0.0.1:4318/v1/traces",
            OTEL_TIMEOUT_SECONDS_ENV: "3",
            OTEL_MAX_QUEUE_SIZE_ENV: "64",
            OTEL_MAX_EXPORT_BATCH_SIZE_ENV: "16",
            OTEL_SCHEDULE_DELAY_MILLISECONDS_ENV: "100",
        }
    )

    assert config.telemetry.enabled
    assert config.telemetry.endpoint == "http://127.0.0.1:4318/v1/traces"
    assert config.telemetry.timeout_seconds == 3
    assert config.telemetry.max_queue_size == 64
    assert config.telemetry.max_export_batch_size == 16
    assert config.telemetry.schedule_delay_milliseconds == 100


@pytest.mark.parametrize(
    "updates",
    (
        {OTEL_ENABLED_ENV: "yes"},
        {OTEL_ENDPOINT_ENV: "https://collector.example/v1/traces"},
        {OTEL_ENABLED_ENV: "1"},
        {
            OTEL_ENABLED_ENV: "1",
            OTEL_ENDPOINT_ENV: "http://collector.example/v1/traces",
        },
        {
            OTEL_ENABLED_ENV: "1",
            OTEL_ENDPOINT_ENV: "https://token@collector.example/v1/traces",
        },
        {
            OTEL_ENABLED_ENV: "1",
            OTEL_ENDPOINT_ENV: "https://collector.example/v1/traces?token=secret",
        },
        {
            OTEL_ENABLED_ENV: "1",
            OTEL_ENDPOINT_ENV: "https://collector.example/v1/traces",
            OTEL_MAX_QUEUE_SIZE_ENV: "8",
            OTEL_MAX_EXPORT_BATCH_SIZE_ENV: "9",
        },
        {
            OTEL_ENABLED_ENV: "1",
            OTEL_ENDPOINT_ENV: "https://collector.example/v1/traces",
            OTEL_TIMEOUT_SECONDS_ENV: "31",
        },
    ),
)
def test_process_config_rejects_ambient_or_unsafe_otel_configuration_content_free(
    tmp_path: Path,
    updates: dict[str, str],
) -> None:
    repository = _repository(tmp_path / "repository")
    environment = {
        DATA_DIR_ENV: str(tmp_path / "data"),
        API_TOKEN_ENV: TOKEN,
        REPOSITORY_ROOT_ENV: str(repository),
        **updates,
    }

    with pytest.raises(ProcessConfigError) as caught:
        RuntimeProcessConfig.from_environment(environment)

    assert caught.value.code is ProcessConfigFailureCode.INVALID_OTEL_CONFIG
    assert str(caught.value) == "invalid-otel-config"
    assert not any(value in str(caught.value) for value in updates.values())


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        (REPOSITORY_ROOT_ENV, "relative", ProcessConfigFailureCode.INVALID_REPOSITORY_ROOT),
        (
            GRACEFUL_TIMEOUT_SECONDS_ENV,
            "0",
            ProcessConfigFailureCode.INVALID_GRACEFUL_TIMEOUT,
        ),
        (API_BACKPRESSURE_ENV, "1025", ProcessConfigFailureCode.INVALID_API_BACKPRESSURE),
        (WORKER_POLL_MILLISECONDS_ENV, "9", ProcessConfigFailureCode.INVALID_WORKER_POLL),
        (WORKER_LEASE_SECONDS_ENV, "86401", ProcessConfigFailureCode.INVALID_WORKER_LEASE),
        (WORKER_ID_ENV, "worker with space", ProcessConfigFailureCode.INVALID_WORKER_ID),
        (REQUESTS_PER_MINUTE_ENV, "0", ProcessConfigFailureCode.INVALID_QUOTA_CONFIG),
        (
            ACTIVE_STORAGE_MAX_BYTES_ENV,
            "1048575",
            ProcessConfigFailureCode.INVALID_QUOTA_CONFIG,
        ),
        (
            MUTATION_RESERVE_BYTES_ENV,
            "1073741825",
            ProcessConfigFailureCode.INVALID_QUOTA_CONFIG,
        ),
    ),
)
def test_process_config_rejects_implicit_or_unbounded_values_content_free(
    tmp_path: Path,
    field: str,
    value: str,
    code: ProcessConfigFailureCode,
) -> None:
    repository = _repository(tmp_path / "repository")
    environment = {
        DATA_DIR_ENV: str(tmp_path / "data"),
        API_TOKEN_ENV: TOKEN,
        REPOSITORY_ROOT_ENV: str(repository),
        field: value,
    }

    with pytest.raises(ProcessConfigError) as caught:
        RuntimeProcessConfig.from_environment(environment)

    assert caught.value.code is code
    assert str(caught.value) == code.value
    assert value not in str(caught.value)


def test_process_config_rejects_missing_and_symlinked_repository_roots(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    linked = tmp_path / "linked-repository"
    linked.symlink_to(repository, target_is_directory=True)

    for value in (tmp_path / "missing", linked):
        with pytest.raises(ProcessConfigError) as caught:
            RuntimeProcessConfig.from_environment(
                {
                    DATA_DIR_ENV: str(tmp_path / f"data-{value.name}"),
                    API_TOKEN_ENV: TOKEN,
                    REPOSITORY_ROOT_ENV: str(value),
                }
            )
        assert caught.value.code is ProcessConfigFailureCode.INVALID_REPOSITORY_ROOT


def test_process_config_rejects_reserve_without_active_storage_headroom(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")

    with pytest.raises(ProcessConfigError) as caught:
        RuntimeProcessConfig.from_environment(
            {
                DATA_DIR_ENV: str(tmp_path / "data"),
                API_TOKEN_ENV: TOKEN,
                REPOSITORY_ROOT_ENV: str(repository),
                ACTIVE_STORAGE_MAX_BYTES_ENV: "1048576",
                MUTATION_RESERVE_BYTES_ENV: "1048576",
            }
        )

    assert caught.value.code is ProcessConfigFailureCode.INVALID_QUOTA_CONFIG


def _repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path
