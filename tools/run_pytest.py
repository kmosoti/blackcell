from __future__ import annotations

import os
import sys
from collections.abc import Sequence

import pytest

SECURE_TEST_UMASK = 0o022
REQUIRE_ALL_PASS_OPTION = "blackcell_require_all_pass"


class RequireAllPassPlugin:
    """Require an explicit pytest selection to collect and pass without soft outcomes."""

    def __init__(
        self,
        required_node_ids: frozenset[str] = frozenset(),
        *,
        selection_declared: bool = False,
    ) -> None:
        self._enabled = False
        self._required_node_ids = required_node_ids
        self._selection_declared = selection_declared
        self._invalid_outcomes: list[str] = []

    def pytest_addoption(self, parser: pytest.Parser) -> None:
        parser.addoption(
            "--blackcell-require-all-pass",
            action="store_true",
            dest=REQUIRE_ALL_PASS_OPTION,
            default=False,
            help=(
                "Require an explicitly requested pytest file or exact node selection to collect "
                "and pass without skip or xfail outcomes."
            ),
        )

    def pytest_configure(self, config: pytest.Config) -> None:
        self._enabled = bool(config.getoption(REQUIRE_ALL_PASS_OPTION))

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if self._enabled and report.skipped:
            self._invalid_outcomes.append(f"collection skipped: {report.nodeid}")

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        if not self._enabled:
            return
        if not self._selection_declared:
            self._invalid_outcomes.append("no required pytest selector declared")
            return
        if not self._required_node_ids:
            return

        collected_node_ids = {item.nodeid for item in session.items}
        for node_id in sorted(self._required_node_ids - collected_node_ids):
            self._invalid_outcomes.append(f"required node not collected: {node_id}")
        for node_id in sorted(collected_node_ids - self._required_node_ids):
            self._invalid_outcomes.append(f"unexpected node collected: {node_id}")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if not self._enabled:
            return
        if report.skipped:
            self._invalid_outcomes.append(f"test skipped: {report.nodeid}")
        elif getattr(report, "wasxfail", None) is not None:
            self._invalid_outcomes.append(f"test xfailed or xpassed: {report.nodeid}")

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        del exitstatus
        if not self._enabled:
            return
        if session.testscollected == 0:
            self._invalid_outcomes.append("no tests collected")
        if not self._invalid_outcomes:
            return

        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_sep("=", "required-pass gate rejected non-pass outcomes")
            for outcome in sorted(set(self._invalid_outcomes)):
                terminal.write_line(outcome)


def main(argv: Sequence[str] | None = None) -> int:
    """Run pytest under the repository's owner-write security boundary."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    require_all_pass = "--blackcell-require-all-pass" in arguments
    required_selectors = frozenset(
        argument
        for argument in arguments
        if require_all_pass
        and not argument.startswith("-")
        and (argument.endswith(".py") or "::" in argument)
    )
    required_node_ids = frozenset(argument for argument in required_selectors if "::" in argument)
    previous_umask = os.umask(SECURE_TEST_UMASK)
    try:
        return int(
            pytest.main(
                arguments,
                plugins=[
                    RequireAllPassPlugin(
                        required_node_ids,
                        selection_declared=bool(required_selectors),
                    )
                ],
            )
        )
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
