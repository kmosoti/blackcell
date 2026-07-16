from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tools import run_pytest


def test_pytest_gate_sets_and_restores_the_secure_umask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    umasks: list[int] = []
    pytest_args: list[list[str]] = []

    def record_umask(value: int) -> int:
        umasks.append(value)
        return 0o002

    def record_pytest(arguments: Sequence[str], *, plugins: Sequence[object]) -> int:
        pytest_args.append(list(arguments))
        assert len(plugins) == 1
        assert isinstance(plugins[0], run_pytest.RequireAllPassPlugin)
        return 0

    monkeypatch.setattr(run_pytest.os, "umask", record_umask)
    monkeypatch.setattr(run_pytest.pytest, "main", record_pytest)

    assert run_pytest.main(("-q", "tests/unit/test_pytest_gate.py")) == 0
    assert umasks == [0o022, 0o002]
    assert pytest_args == [["-q", "tests/unit/test_pytest_gate.py"]]


def test_pytest_gate_passes_exact_requested_nodes_to_required_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_ids = (
        "tests/architecture/test_dependencies.py::test_first",
        "tests/architecture/test_dependencies.py::test_second",
    )

    def record_pytest(arguments: Sequence[str], *, plugins: Sequence[object]) -> int:
        assert list(arguments) == [*node_ids, "--blackcell-require-all-pass"]
        plugin = cast("run_pytest.RequireAllPassPlugin", plugins[0])
        assert plugin._required_node_ids == frozenset(node_ids)
        return 0

    monkeypatch.setattr(run_pytest.pytest, "main", record_pytest)

    assert run_pytest.main((*node_ids, "--blackcell-require-all-pass")) == 0


def test_required_pass_plugin_rejects_skips_xfails_and_empty_collection() -> None:
    plugin = run_pytest.RequireAllPassPlugin()
    config = SimpleNamespace(getoption=lambda name: name == run_pytest.REQUIRE_ALL_PASS_OPTION)
    plugin.pytest_configure(cast("pytest.Config", config))

    plugin.pytest_collectreport(
        cast(
            "pytest.CollectReport",
            SimpleNamespace(skipped=True, nodeid="tests/architecture/test_dependencies.py"),
        )
    )
    plugin.pytest_runtest_logreport(
        cast(
            "pytest.TestReport",
            SimpleNamespace(
                skipped=False,
                nodeid="tests/architecture/test_dependencies.py::test_required",
                wasxfail="expected failure",
            ),
        )
    )

    session = SimpleNamespace(
        testscollected=0,
        exitstatus=pytest.ExitCode.OK,
        config=SimpleNamespace(
            pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
        ),
    )
    plugin.pytest_sessionfinish(cast("pytest.Session", session), int(pytest.ExitCode.OK))

    assert cast("Any", session).exitstatus == pytest.ExitCode.TESTS_FAILED


def test_required_pass_plugin_rejects_partial_deselection() -> None:
    first = "tests/architecture/test_dependencies.py::test_first"
    second = "tests/architecture/test_dependencies.py::test_second"
    plugin = run_pytest.RequireAllPassPlugin(frozenset({first, second}))
    config = SimpleNamespace(getoption=lambda name: name == run_pytest.REQUIRE_ALL_PASS_OPTION)
    plugin.pytest_configure(cast("pytest.Config", config))

    session = SimpleNamespace(
        items=[SimpleNamespace(nodeid=first)],
        testscollected=1,
        exitstatus=pytest.ExitCode.OK,
        config=SimpleNamespace(
            pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
        ),
    )
    plugin.pytest_collection_finish(cast("pytest.Session", session))
    plugin.pytest_sessionfinish(cast("pytest.Session", session), int(pytest.ExitCode.OK))

    assert cast("Any", session).exitstatus == pytest.ExitCode.TESTS_FAILED
