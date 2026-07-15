from __future__ import annotations

import shutil
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from blackcell.adapters.execution.local_process import (
    LOCAL_PROCESS_ADAPTER_ID,
    LOCAL_PROCESS_V1_ACTIVATION_CONTRACT,
    ArgumentBinding,
    ArgumentKind,
    EnvironmentEntry,
    LocalProcessAffordance,
    LocalProcessConfigurationError,
    LocalProcessRegistry,
)
from blackcell.features.execute_affordance import (
    AffordanceArgumentSpec,
    AffordanceDefinition,
    SideEffectClass,
)


def test_configuration_digest_and_adapter_contract_bind_every_behavioral_field(
    tmp_path: Path,
) -> None:
    first = _configuration(tmp_path)
    changed = replace(first, stdout_limit_bytes=first.stdout_limit_bytes + 1)

    assert first.configuration_digest != changed.configuration_digest
    assert (
        LocalProcessRegistry((first,)).contract_version
        != LocalProcessRegistry((changed,)).contract_version
    )
    assert LocalProcessRegistry((first,)).contract_version.startswith(
        "local-process-registry/v1@sha256:"
    )


def test_configuration_requires_exact_read_only_declaration(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)

    assert configuration.definition.adapter_id == LOCAL_PROCESS_ADAPTER_ID
    assert configuration.executable == str(Path(sys.executable).resolve())
    assert configuration.working_directory == str(tmp_path.resolve())
    assert configuration.allowed_path_roots == (str(tmp_path.resolve()),)

    with pytest.raises(LocalProcessConfigurationError, match="adapter_id"):
        replace(
            configuration,
            definition=replace(configuration.definition, adapter_id="other"),
        )
    with pytest.raises(LocalProcessConfigurationError, match="READ_ONLY declaration"):
        replace(
            configuration,
            definition=replace(
                configuration.definition,
                side_effect_class=SideEffectClass.REVERSIBLE,
            ),
        )
    with pytest.raises(LocalProcessConfigurationError, match="exactly match"):
        replace(configuration, bindings=())
    with pytest.raises(LocalProcessConfigurationError, match="end with '--'"):
        replace(configuration, fixed_argv=("-I",))


def test_v1_requires_an_exactly_empty_environment(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)

    assert configuration.environment == ()
    with pytest.raises(LocalProcessConfigurationError, match="exactly empty"):
        replace(configuration, environment=(EnvironmentEntry("SAFE_NAME", "value"),))


@pytest.mark.parametrize(
    "name",
    (
        "PATH",
        "LD_PRELOAD",
        "PYTHONPATH",
        "HOME",
        "AWS_SECRET_ACCESS_KEY",
        "GLIBC_TUNABLES",
        "GCONV_PATH",
        "NODE_OPTIONS",
        "JAVA_TOOL_OPTIONS",
        "LUA_INIT",
        "TMPDIR",
    ),
)
def test_environment_shape_rejects_known_loader_and_runtime_injection_names(name: str) -> None:
    with pytest.raises(LocalProcessConfigurationError, match="not permitted"):
        EnvironmentEntry(name, "attacker-controlled")


def test_filesystem_configuration_rejects_relative_symlink_and_protected_paths(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(LocalProcessConfigurationError, match="absolute"):
        replace(configuration, executable="python")
    with pytest.raises(LocalProcessConfigurationError, match="symlink"):
        replace(configuration, working_directory=str(linked))
    with pytest.raises(LocalProcessConfigurationError, match="protected"):
        replace(
            configuration,
            working_directory="/etc",
            allowed_path_roots=("/etc",),
        )


def test_registry_rejects_duplicates_and_noncanonical_order(tmp_path: Path) -> None:
    alpha = _configuration(tmp_path, name="alpha")
    beta = _configuration(tmp_path, name="beta")

    assert LocalProcessRegistry((alpha, beta)).get("beta") == beta
    with pytest.raises(LocalProcessConfigurationError, match="unique and sorted"):
        LocalProcessRegistry((beta, alpha))
    with pytest.raises(LocalProcessConfigurationError, match="unique and sorted"):
        LocalProcessRegistry((alpha, alpha))
    with pytest.raises(LookupError, match="not registered"):
        LocalProcessRegistry((alpha,)).get("missing")


def test_argument_binding_types_and_bounds_are_strict() -> None:
    with pytest.raises(LocalProcessConfigurationError, match="name"):
        ArgumentBinding(" ", ArgumentKind.TEXT)
    with pytest.raises(LocalProcessConfigurationError, match="recognized"):
        ArgumentBinding("value", cast("ArgumentKind", "text"))
    with pytest.raises(LocalProcessConfigurationError, match="positive"):
        ArgumentBinding("value", ArgumentKind.TEXT, maximum_bytes=0)
    with pytest.raises(LocalProcessConfigurationError, match="--name="):
        ArgumentBinding("value", ArgumentKind.TEXT, option_prefix="--value")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        ("1INVALID", "value", "name is invalid"),
        ("SAFE", "", "must not be empty"),
        ("SAFE", "line\nbreak", "control character"),
        ("SAFE", "x" * 4097, "exceeds 4096"),
    ),
)
def test_environment_contract_rejects_noncanonical_values(
    name: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(LocalProcessConfigurationError, match=message):
        EnvironmentEntry(name, value)


def test_configuration_rejects_unbounded_or_ambiguous_runtime_settings(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    optional_definition = replace(
        configuration.definition,
        arguments=(AffordanceArgumentSpec("value", required=False),),
    )
    cases = (
        ({"schema_version": "local-process-affordance/v99"}, "unsupported"),
        ({"allowed_path_roots": ()}, "at least one"),
        ({"fixed_argv": tuple("x" for _ in range(65))}, "64 tokens"),
        ({"fixed_argv": ("--", "")}, "must not be empty"),
        ({"fixed_argv": ("--", "line\nbreak")}, "control character"),
        ({"fixed_argv": ("--", "x" * 4097)}, "exceeds 4096"),
        ({"definition": optional_definition}, "every declared"),
        ({"stdout_limit_bytes": -1}, "non-negative"),
        ({"stderr_limit_bytes": True}, "non-negative"),
        ({"termination_grace_seconds": 0}, "positive"),
        ({"drain_grace_seconds": float("nan")}, "positive"),
    )
    for changes, message in cases:
        with pytest.raises(LocalProcessConfigurationError, match=message):
            replace(configuration, **changes)


def test_configuration_rejects_environment_and_root_order_ambiguity(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    executable = Path(sys.executable).resolve()
    base = LocalProcessAffordance(
        definition=AffordanceDefinition(
            "probe",
            LOCAL_PROCESS_ADAPTER_ID,
            SideEffectClass.READ_ONLY,
            1,
        ),
        executable=str(executable),
        fixed_argv=(),
        bindings=(),
        working_directory=str(root_a),
        allowed_path_roots=(str(root_a),),
    )

    with pytest.raises(LocalProcessConfigurationError, match="unique and sorted"):
        replace(base, allowed_path_roots=(str(root_b), str(root_a)))
    with pytest.raises(LocalProcessConfigurationError, match="confined"):
        replace(base, allowed_path_roots=(str(root_b),))
    with pytest.raises(LocalProcessConfigurationError, match="exactly empty"):
        replace(
            base,
            environment=(EnvironmentEntry("FIXED", "value"),),
        )


def test_configuration_rejects_missing_nonexecutable_and_all_script_files(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    non_executable = tmp_path / "program"
    non_executable.write_text("not executable", encoding="utf-8")
    direct_script = tmp_path / "program-script"
    direct_script.write_text(f"#!{Path(sys.executable).resolve()}\npass\n", encoding="utf-8")
    direct_script.chmod(0o700)

    with pytest.raises(LocalProcessConfigurationError, match="does not resolve"):
        replace(configuration, executable=str(tmp_path / "missing"))
    with pytest.raises(LocalProcessConfigurationError, match="executable regular"):
        replace(configuration, executable=str(non_executable))
    with pytest.raises(LocalProcessConfigurationError, match="ELF binary"):
        replace(configuration, executable=str(direct_script))


def test_configuration_rejects_privileged_or_mutable_filesystem_objects(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "python-copy"
    shutil.copy2(Path(sys.executable).resolve(), executable)
    for privileged_bit in (stat.S_ISUID, stat.S_ISGID):
        executable.chmod(0o700 | privileged_bit)
        with pytest.raises(LocalProcessConfigurationError, match="setuid or setgid"):
            _configuration(tmp_path, executable=executable)

    for mutable_mode in (0o720, 0o702):
        executable.chmod(mutable_mode)
        with pytest.raises(LocalProcessConfigurationError, match="group- or world-writable"):
            _configuration(tmp_path, executable=executable)

    executable.chmod(0o700)
    mutable_cwd = tmp_path / "mutable-cwd"
    mutable_cwd.mkdir(mode=0o770)
    mutable_cwd.chmod(0o770)
    try:
        with pytest.raises(LocalProcessConfigurationError, match="group- or world-writable"):
            _configuration(mutable_cwd, executable=executable)
    finally:
        mutable_cwd.chmod(0o700)

    mutable_root = tmp_path / "mutable-root"
    safe_cwd = mutable_root / "cwd"
    mutable_root.mkdir(mode=0o700)
    safe_cwd.mkdir(mode=0o700)
    mutable_root.chmod(0o770)
    try:
        with pytest.raises(LocalProcessConfigurationError, match="group- or world-writable"):
            LocalProcessAffordance(
                definition=AffordanceDefinition(
                    "probe",
                    LOCAL_PROCESS_ADAPTER_ID,
                    SideEffectClass.READ_ONLY,
                    1,
                ),
                executable=str(executable),
                fixed_argv=("-I", "-S", "-c", "pass"),
                bindings=(),
                working_directory=str(safe_cwd),
                allowed_path_roots=(str(mutable_root),),
            )
    finally:
        mutable_root.chmod(0o700)


def test_activation_contract_explicitly_gates_untrusted_and_setsid_execution() -> None:
    contract = LOCAL_PROCESS_V1_ACTIVATION_CONTRACT.casefold()

    assert "not provide syscall" in contract
    assert "setsid()" in contract
    assert "rootless podman" in contract
    assert "cgroup" in contract and "pid namespace" in contract
    assert "unsupported" in contract


def test_registry_version_and_platform_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _configuration(tmp_path)
    with pytest.raises(LocalProcessConfigurationError, match="unsupported"):
        LocalProcessRegistry((configuration,), schema_version="local-process-registry/v99")
    with pytest.raises(LocalProcessConfigurationError, match="must not be empty"):
        LocalProcessRegistry(())

    from blackcell.adapters.execution.local_process import configuration as module

    monkeypatch.setattr(module.sys, "platform", "darwin")
    with pytest.raises(LocalProcessConfigurationError, match="POSIX Linux"):
        LocalProcessRegistry((configuration,))


def _configuration(
    tmp_path: Path,
    *,
    name: str = "probe",
    executable: Path | None = None,
) -> LocalProcessAffordance:
    definition = AffordanceDefinition(
        name,
        LOCAL_PROCESS_ADAPTER_ID,
        SideEffectClass.READ_ONLY,
        1.0,
        (AffordanceArgumentSpec("value"),),
    )
    return LocalProcessAffordance(
        definition=definition,
        executable=str((executable or Path(sys.executable)).resolve()),
        fixed_argv=("-I", "-S", "-c", "pass", "--"),
        bindings=(ArgumentBinding("value", ArgumentKind.TEXT),),
        working_directory=str(tmp_path.resolve()),
        allowed_path_roots=(str(tmp_path.resolve()),),
    )
