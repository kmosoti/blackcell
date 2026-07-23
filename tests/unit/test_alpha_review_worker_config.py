from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import cast

import pytest

from blackcell.config import (
    ALPHA_REVIEW_CONFIG_FILE_ENV,
    ALPHA_REVIEW_CONFIG_SCHEMA,
    ALPHA_WORKER_CONFIG_FILE_ENV,
    ALPHA_WORKER_CONFIG_SCHEMA,
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    REPOSITORY_ROOT_ENV,
    AlphaReviewConfigError,
    ProcessConfigError,
    ProcessConfigFailureCode,
    RuntimeProcessConfig,
    load_alpha_review_config,
)
from blackcell.gateway import DataClassification, LocalityPolicy

TOKEN = "Alpha-review_config-token.0123456789-ABCDEFG"


def test_alpha_review_worker_config_loads_closed_owner_only_review_contract(
    tmp_path: Path,
) -> None:
    repository, _data_root, _isolation_root = _roots(tmp_path)
    source = tmp_path / "alpha-review.json"
    _write_config(source, _review_payload())

    config = load_alpha_review_config(
        {ALPHA_REVIEW_CONFIG_FILE_ENV: str(source)},
        repository_root=repository,
    )

    assert config is not None
    assert config.schema_version == ALPHA_REVIEW_CONFIG_SCHEMA
    assert config.source_path == source
    assert config.provider.profile_id == "alpha-review"
    assert config.provider.model_id == "gpt-review"
    assert config.provider.classification is DataClassification.PRIVATE
    assert config.provider.locality is LocalityPolicy.REMOTE_ALLOWED
    assert config.provider.max_input_tokens == 64_000
    assert config.provider.max_output_tokens == 8_192
    assert config.provider.timeout_ceiling_seconds == 180
    assert config.provider.environment_variables == ("HOME", "OPENAI_API_KEY")
    assert config.worker.worker_id == "alpha-reviewer.test"
    assert config.worker.supervisor_id == "alpha-review-supervisor.test"
    assert config.worker.lease_seconds == 300
    assert config.worker.poll_milliseconds == 125
    assert load_alpha_review_config({}, repository_root=repository) is None


def test_alpha_review_config_rejects_provider_deadline_without_lease_reserve(
    tmp_path: Path,
) -> None:
    repository, _data_root, _isolation_root = _roots(tmp_path)
    for lease_seconds in (179, 180):
        payload = _review_payload()
        cast("dict[str, object]", payload["worker"])["lease_seconds"] = lease_seconds
        source = tmp_path / f"review-lease-{lease_seconds}.json"
        _write_config(source, payload)

        with pytest.raises(AlphaReviewConfigError) as caught:
            load_alpha_review_config(
                {ALPHA_REVIEW_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
            )

        assert str(caught.value) == "invalid-alpha-review-config"

    payload = _review_payload()
    cast("dict[str, object]", payload["worker"])["lease_seconds"] = 181
    source = tmp_path / "review-lease-181.json"
    _write_config(source, payload)
    config = load_alpha_review_config(
        {ALPHA_REVIEW_CONFIG_FILE_ENV: str(source)},
        repository_root=repository,
    )
    assert config is not None
    assert config.worker.lease_seconds == 181


def test_alpha_review_worker_config_rejects_unsafe_unknown_and_shared_authority(
    tmp_path: Path,
) -> None:
    repository, data_root, isolation_root = _roots(tmp_path)
    valid = _review_payload()
    cases: list[tuple[Path | str, str, int]] = []

    unsafe_mode = tmp_path / "unsafe-mode.json"
    cases.append((unsafe_mode, json.dumps(valid), 0o644))

    unknown = copy.deepcopy(valid)
    unknown["secret_value"] = "do-not-echo"
    cases.append((tmp_path / "unknown.json", json.dumps(unknown), 0o600))

    local_only = copy.deepcopy(valid)
    cast("dict[str, object]", local_only["provider"])["locality"] = "local-only"
    cases.append((tmp_path / "local-only.json", json.dumps(local_only), 0o600))

    ambient = copy.deepcopy(valid)
    cast("dict[str, object]", ambient["provider"])["environment_variables"] = [
        "BLACKCELL_API_TOKEN"
    ]
    cases.append((tmp_path / "ambient.json", json.dumps(ambient), 0o600))

    same_identity = copy.deepcopy(valid)
    worker = cast("dict[str, object]", same_identity["worker"])
    worker["supervisor_id"] = worker["worker_id"]
    cases.append((tmp_path / "same-identity.json", json.dumps(same_identity), 0o600))

    duplicate = json.dumps(valid).replace(
        f'"schema_version": "{ALPHA_REVIEW_CONFIG_SCHEMA}"',
        f'"schema_version": "{ALPHA_REVIEW_CONFIG_SCHEMA}", '
        f'"schema_version": "{ALPHA_REVIEW_CONFIG_SCHEMA}"',
        1,
    )
    cases.append((tmp_path / "duplicate.json", duplicate, 0o600))
    cases.append((repository / "review.json", json.dumps(valid), 0o600))
    cases.append(("relative-review.json", json.dumps(valid), 0o600))

    for index, (source, content, mode) in enumerate(cases):
        if isinstance(source, Path):
            source.write_text(content, encoding="utf-8")
            source.chmod(mode)
        with pytest.raises(AlphaReviewConfigError) as caught:
            load_alpha_review_config(
                {ALPHA_REVIEW_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
            )
        assert str(caught.value) == "invalid-alpha-review-config", index
        assert "do-not-echo" not in str(caught.value)

    review_source = tmp_path / "review-valid.json"
    execution_source = tmp_path / "execution.json"
    _write_config(review_source, valid)
    execution = _execution_payload(isolation_root)
    cast("dict[str, object]", execution["provider"])["profile_id"] = "alpha-review"
    _write_config(execution_source, execution)
    environment = {
        DATA_DIR_ENV: str(data_root),
        API_TOKEN_ENV: TOKEN,
        REPOSITORY_ROOT_ENV: str(repository),
        ALPHA_WORKER_CONFIG_FILE_ENV: str(execution_source),
        ALPHA_REVIEW_CONFIG_FILE_ENV: str(review_source),
    }
    with pytest.raises(ProcessConfigError) as shared_profile:
        RuntimeProcessConfig.from_environment(environment)
    assert shared_profile.value.code is ProcessConfigFailureCode.INVALID_ALPHA_REVIEW_CONFIG

    cast("dict[str, object]", execution["provider"])["profile_id"] = "alpha-code"
    cast("dict[str, object]", execution["worker"])["worker_id"] = "alpha-reviewer.test"
    _write_config(execution_source, execution)
    with pytest.raises(ProcessConfigError) as shared_worker:
        RuntimeProcessConfig.from_environment(environment)
    assert shared_worker.value.code is ProcessConfigFailureCode.INVALID_ALPHA_REVIEW_CONFIG

    with pytest.raises(ProcessConfigError) as unsafe_process:
        RuntimeProcessConfig.from_environment(
            {
                DATA_DIR_ENV: str(data_root),
                API_TOKEN_ENV: TOKEN,
                REPOSITORY_ROOT_ENV: str(repository),
                ALPHA_REVIEW_CONFIG_FILE_ENV: str(unsafe_mode),
            }
        )
    assert unsafe_process.value.code is ProcessConfigFailureCode.INVALID_ALPHA_REVIEW_CONFIG


def test_alpha_review_config_rejects_each_typed_provider_boundary(tmp_path: Path) -> None:
    repository, _data_root, _isolation_root = _roots(tmp_path)
    valid = _review_payload()
    unsafe_executable = tmp_path / "unsafe-executable"
    unsafe_executable.write_text("#!/bin/sh\nexit 0\n")
    unsafe_executable.chmod(0o777)

    variants: list[tuple[str, object]] = []

    def add(name: str, section: str | None, key: str, value: object) -> None:
        payload = copy.deepcopy(valid)
        target = payload if section is None else cast("dict[str, object]", payload[section])
        target[key] = value
        variants.append((name, payload))

    add("schema", None, "schema_version", "blackcell.alpha-review-config/v2")
    add("provider-shape", None, "provider", None)
    add("classification", "provider", "classification", "unknown")
    add("secret", "provider", "classification", "secret")
    add("locality", "provider", "locality", "unknown")
    add("environment-shape", "provider", "environment_variables", "HOME")
    add("environment-duplicate", "provider", "environment_variables", ["HOME", "HOME"])
    add("environment-type", "provider", "environment_variables", [1])
    add("environment-forbidden", "provider", "environment_variables", ["GIT_CONFIG"])
    add("profile-id", "provider", "profile_id", "bad id")
    add("empty-model", "provider", "model_id", "")
    add("model-token", "provider", "model_id", "bad\nmodel")
    add("relative-executable", "provider", "codex_executable", "relative-codex")
    add("missing-executable", "provider", "codex_executable", str(tmp_path / "missing"))
    add("unsafe-executable", "provider", "codex_executable", str(unsafe_executable))
    add("provider-integer", "provider", "max_input_tokens", True)
    add("worker-shape", None, "worker", None)
    add("worker-id", "worker", "worker_id", "bad:worker")
    add("supervisor-id", "worker", "supervisor_id", "bad:supervisor")
    add("worker-integer", "worker", "poll_milliseconds", 0)

    for index, (name, payload) in enumerate(variants):
        source = tmp_path / f"typed-{index}-{name}.json"
        _write_config(source, payload)
        with pytest.raises(AlphaReviewConfigError) as caught:
            load_alpha_review_config(
                {ALPHA_REVIEW_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
            )
        assert str(caught.value) == "invalid-alpha-review-config", name

    malformed_inputs = (
        (b"[]", "array"),
        (b"\xff", "utf8"),
        (b"x" * (64 * 1024 + 1), "oversized"),
    )
    for content, name in malformed_inputs:
        source = tmp_path / f"malformed-{name}.json"
        source.write_bytes(content)
        source.chmod(0o600)
        with pytest.raises(AlphaReviewConfigError):
            load_alpha_review_config(
                {ALPHA_REVIEW_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
            )

    with pytest.raises(AlphaReviewConfigError):
        load_alpha_review_config(
            {ALPHA_REVIEW_CONFIG_FILE_ENV: str((tmp_path / "missing-config.json").resolve())},
            repository_root=repository,
        )

    with pytest.raises(AlphaReviewConfigError):
        load_alpha_review_config(
            {ALPHA_REVIEW_CONFIG_FILE_ENV: ""},
            repository_root=repository,
        )

    valid_source = tmp_path / "valid-for-root-type.json"
    _write_config(valid_source, valid)
    with pytest.raises(AlphaReviewConfigError):
        load_alpha_review_config(
            {ALPHA_REVIEW_CONFIG_FILE_ENV: str(valid_source)},
            repository_root=cast("Path", object()),
        )


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir(mode=0o700)
    data_root.chmod(0o700)
    isolation_root = data_root / "alpha-worktrees"
    isolation_root.mkdir(mode=0o700)
    isolation_root.chmod(0o700)
    return repository.resolve(), data_root.resolve(), isolation_root.resolve()


def _review_payload() -> dict[str, object]:
    return {
        "schema_version": ALPHA_REVIEW_CONFIG_SCHEMA,
        "provider": {
            "profile_id": "alpha-review",
            "model_id": "gpt-review",
            "codex_executable": str(_executable("true")),
            "git_executable": str(_executable("git")),
            "classification": "private",
            "locality": "remote-allowed",
            "max_input_tokens": 64_000,
            "max_output_tokens": 8_192,
            "max_cost_microusd": 0,
            "timeout_ceiling_seconds": 180,
            "environment_variables": ["OPENAI_API_KEY", "HOME"],
        },
        "worker": {
            "worker_id": "alpha-reviewer.test",
            "supervisor_id": "alpha-review-supervisor.test",
            "lease_seconds": 300,
            "poll_milliseconds": 125,
        },
    }


def _execution_payload(isolation_root: Path) -> dict[str, object]:
    executable = _executable("true")
    return {
        "schema_version": ALPHA_WORKER_CONFIG_SCHEMA,
        "provider": {
            "profile_id": "alpha-code",
            "model_id": "gpt-code",
            "codex_executable": str(executable),
            "git_executable": str(_executable("git")),
            "classification": "private",
            "locality": "remote-allowed",
            "max_input_tokens": 32_000,
            "max_output_tokens": 4_096,
            "max_cost_microusd": 0,
            "timeout_ceiling_seconds": 120,
            "environment_variables": [],
        },
        "isolation": {
            "root": str(isolation_root),
            "executables": {"true": str(executable)},
            "runtime_roots": [],
            "bubblewrap_executable": str(executable),
            "prlimit_executable": str(executable),
            "probe_executable": str(executable),
            "limits": {
                "address_space_bytes": 1_073_741_824,
                "cpu_seconds": 60,
                "processes": 128,
                "open_files": 128,
                "file_size_bytes": 16_777_216,
                "tmpfs_bytes": 67_108_864,
            },
        },
        "worker": {
            "worker_id": "alpha-executor.test",
            "stdout_limit_bytes": 65_536,
            "stderr_limit_bytes": 32_768,
            "lease_grace_seconds": 15,
            "max_retained_successful_worktrees": 2,
        },
    }


def _write_config(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _executable(name: str) -> Path:
    value = shutil.which(name)
    assert value is not None
    return Path(value).resolve(strict=True)
