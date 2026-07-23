from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import cast

import pytest

from blackcell.config import (
    ALPHA_WORKER_CONFIG_FILE_ENV,
    ALPHA_WORKER_CONFIG_SCHEMA,
    API_TOKEN_ENV,
    DATA_DIR_ENV,
    REPOSITORY_ROOT_ENV,
    AlphaWorkerConfigError,
    ProcessConfigError,
    ProcessConfigFailureCode,
    RuntimeProcessConfig,
    load_alpha_worker_config,
)
from blackcell.gateway import DataClassification, LocalityPolicy


def test_alpha_worker_config_loads_one_closed_owner_only_runtime_contract(
    tmp_path: Path,
) -> None:
    repository, data_root, isolation_root = _roots(tmp_path)
    source = tmp_path / "alpha-worker.json"
    payload = _payload(isolation_root)
    _write_config(source, payload)

    config = load_alpha_worker_config(
        {ALPHA_WORKER_CONFIG_FILE_ENV: str(source)},
        repository_root=repository,
        data_root=data_root,
    )

    assert config is not None
    assert config.schema_version == ALPHA_WORKER_CONFIG_SCHEMA
    assert config.source_path == source
    assert config.provider.profile_id == "alpha-code"
    assert config.provider.model_id == "gpt-alpha"
    assert config.provider.classification is DataClassification.PRIVATE
    assert config.provider.locality is LocalityPolicy.REMOTE_ALLOWED
    assert config.provider.environment_variables == ()
    assert config.provider.git_executable == _executable("git")
    assert config.isolation.root == isolation_root
    assert tuple(item.alias for item in config.isolation.executables) == ("python",)
    assert config.isolation.runtime_roots == ()
    assert config.worker.worker_id == "alpha-worker.test"
    assert config.worker.stdout_limit_bytes == 65_536
    assert config.worker.stderr_limit_bytes == 32_768
    assert config.worker.lease_grace_seconds == 15
    assert config.worker.max_retained_successful_worktrees == 2
    assert load_alpha_worker_config({}, repository_root=repository, data_root=data_root) is None


def test_alpha_worker_config_rejects_unsafe_implicit_and_unknown_input_content_free(
    tmp_path: Path,
) -> None:
    repository, data_root, isolation_root = _roots(tmp_path)
    valid = _payload(isolation_root)
    cases: list[tuple[Path | str, str | bytes, int]] = []

    unsafe_mode = tmp_path / "unsafe-mode.json"
    cases.append((unsafe_mode, json.dumps(valid), 0o644))

    unknown = copy.deepcopy(valid)
    unknown["secret_value"] = "do-not-echo"
    cases.append((tmp_path / "unknown.json", json.dumps(unknown), 0o600))

    local_only = copy.deepcopy(valid)
    local_provider = cast("dict[str, object]", local_only["provider"])
    local_provider["locality"] = "local-only"
    cases.append((tmp_path / "implicit-local.json", json.dumps(local_only), 0o600))

    ambient_secret = copy.deepcopy(valid)
    ambient_provider = cast("dict[str, object]", ambient_secret["provider"])
    ambient_provider["environment_variables"] = ["BLACKCELL_API_TOKEN"]
    cases.append((tmp_path / "ambient-secret.json", json.dumps(ambient_secret), 0o600))

    legacy_worker = copy.deepcopy(valid)
    worker = cast("dict[str, object]", legacy_worker["worker"])
    worker["worker_id"] = "worker:legacy"
    cases.append((tmp_path / "legacy-worker-id.json", json.dumps(legacy_worker), 0o600))

    invalid_retention = copy.deepcopy(valid)
    retention_worker = cast("dict[str, object]", invalid_retention["worker"])
    retention_worker["max_retained_successful_worktrees"] = -1
    cases.append((tmp_path / "invalid-retention.json", json.dumps(invalid_retention), 0o600))

    duplicate = json.dumps(valid).replace(
        '"schema_version": "blackcell.alpha-worker-config/v1"',
        '"schema_version": "blackcell.alpha-worker-config/v1", '
        '"schema_version": "blackcell.alpha-worker-config/v1"',
        1,
    )
    cases.append((tmp_path / "duplicate.json", duplicate, 0o600))

    repo_local = repository / "alpha-worker.json"
    cases.append((repo_local, json.dumps(valid), 0o600))
    cases.append(("relative-alpha-worker.json", json.dumps(valid), 0o600))

    for index, (source, content, mode) in enumerate(cases):
        if isinstance(source, Path):
            source.write_bytes(content.encode() if isinstance(content, str) else content)
            source.chmod(mode)
        with pytest.raises(AlphaWorkerConfigError) as caught:
            load_alpha_worker_config(
                {ALPHA_WORKER_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
                data_root=data_root,
            )
        assert str(caught.value) == "invalid-alpha-worker-config", index
        assert "do-not-echo" not in str(caught.value)

    with pytest.raises(ProcessConfigError) as process_error:
        RuntimeProcessConfig.from_environment(
            {
                DATA_DIR_ENV: str(data_root),
                API_TOKEN_ENV: "Alpha-config_test-token.0123456789-ABCDEFG",
                REPOSITORY_ROOT_ENV: str(repository),
                ALPHA_WORKER_CONFIG_FILE_ENV: str(unsafe_mode),
            }
        )
    assert process_error.value.code is ProcessConfigFailureCode.INVALID_ALPHA_WORKER_CONFIG
    assert str(process_error.value) == "invalid-alpha-worker-config"


def test_alpha_worker_config_rejects_each_typed_authority_boundary(tmp_path: Path) -> None:
    repository, data_root, isolation_root = _roots(tmp_path)
    valid = _payload(isolation_root)
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    repository_isolation = repository / "nested-isolation"
    repository_isolation.mkdir(mode=0o700)
    repository_isolation.chmod(0o700)
    wrong_mode_root = tmp_path / "wrong-mode-root"
    wrong_mode_root.mkdir(mode=0o755)
    wrong_mode_root.chmod(0o755)
    unsafe_executable = tmp_path / "unsafe-executable"
    unsafe_executable.write_text("#!/bin/sh\nexit 0\n")
    unsafe_executable.chmod(0o777)

    variants: list[tuple[str, object]] = []

    def add(name: str, section: str | None, key: str, value: object) -> None:
        payload = copy.deepcopy(valid)
        target = payload if section is None else cast("dict[str, object]", payload[section])
        target[key] = value
        variants.append((name, payload))

    add("schema", None, "schema_version", "blackcell.alpha-worker-config/v2")
    add("provider-shape", None, "provider", None)
    add("classification", "provider", "classification", "unknown")
    add("secret", "provider", "classification", "secret")
    add("locality", "provider", "locality", "unknown")
    add("environment-shape", "provider", "environment_variables", "HOME")
    add("environment-duplicate", "provider", "environment_variables", ["HOME", "HOME"])
    add("environment-type", "provider", "environment_variables", [1])
    add("environment-forbidden", "provider", "environment_variables", ["PYTHONPATH"])
    add("profile-id", "provider", "profile_id", "bad id")
    add("model-token", "provider", "model_id", "bad\nmodel")
    add("missing-executable", "provider", "codex_executable", str(tmp_path / "missing"))
    add("unsafe-executable", "provider", "codex_executable", str(unsafe_executable))
    add("integer", "provider", "max_input_tokens", True)
    add("repository-overlap", "isolation", "root", str(repository_isolation))
    add("owner-mode", "isolation", "root", str(wrong_mode_root))
    add("aliases-empty", "isolation", "executables", {})
    add("alias-invalid", "isolation", "executables", {"bad alias": str(_executable("true"))})
    add("runtime-shape", "isolation", "runtime_roots", str(runtime_root))
    add("runtime-duplicate", "isolation", "runtime_roots", [str(runtime_root), str(runtime_root)])
    add("runtime-protected", "isolation", "runtime_roots", [str(data_root)])
    add("worker-shape", None, "worker", None)
    add("worker-id", "worker", "worker_id", "bad:worker")
    add("worker-integer", "worker", "stdout_limit_bytes", 0)

    invalid_limits = copy.deepcopy(valid)
    cast("dict[str, object]", cast("dict[str, object]", invalid_limits["isolation"])["limits"])[
        "cpu_seconds"
    ] = 0
    variants.append(("limits", invalid_limits))

    for index, (name, payload) in enumerate(variants):
        source = tmp_path / f"typed-{index}-{name}.json"
        _write_config(source, payload)
        with pytest.raises(AlphaWorkerConfigError) as caught:
            load_alpha_worker_config(
                {ALPHA_WORKER_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
                data_root=data_root,
            )
        assert str(caught.value) == "invalid-alpha-worker-config", name

    malformed_inputs = (
        (b"[]", "array"),
        (b"\xff", "utf8"),
        (b"x" * (256 * 1024 + 1), "oversized"),
    )
    for content, name in malformed_inputs:
        source = tmp_path / f"malformed-{name}.json"
        source.write_bytes(content)
        source.chmod(0o600)
        with pytest.raises(AlphaWorkerConfigError):
            load_alpha_worker_config(
                {ALPHA_WORKER_CONFIG_FILE_ENV: str(source)},
                repository_root=repository,
                data_root=data_root,
            )

    with pytest.raises(AlphaWorkerConfigError):
        load_alpha_worker_config(
            {ALPHA_WORKER_CONFIG_FILE_ENV: str((tmp_path / "missing-config.json").resolve())},
            repository_root=repository,
            data_root=data_root,
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


def _payload(isolation_root: Path) -> dict[str, object]:
    true = _executable("true")
    return {
        "schema_version": ALPHA_WORKER_CONFIG_SCHEMA,
        "provider": {
            "profile_id": "alpha-code",
            "model_id": "gpt-alpha",
            "codex_executable": str(true),
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
            "executables": {"python": str(true)},
            "runtime_roots": [],
            "bubblewrap_executable": str(true),
            "prlimit_executable": str(true),
            "probe_executable": str(true),
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
            "worker_id": "alpha-worker.test",
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
