from __future__ import annotations

from collections.abc import Sequence

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

    def record_pytest(arguments: Sequence[str]) -> int:
        pytest_args.append(list(arguments))
        return 0

    monkeypatch.setattr(run_pytest.os, "umask", record_umask)
    monkeypatch.setattr(run_pytest.pytest, "main", record_pytest)

    assert run_pytest.main(("-q", "tests/unit/test_pytest_gate.py")) == 0
    assert umasks == [0o022, 0o002]
    assert pytest_args == [["-q", "tests/unit/test_pytest_gate.py"]]
