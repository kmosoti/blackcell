from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / "release" / "alpha" / "acceptance-manifest.json"
README_PATH = MANIFEST_PATH.with_name("README.md")

EXPECTED_MATRIX_IDS = {
    "cancellation",
    "event-resumption",
    "legacy-replay",
    "provider-failure",
    "recovery",
    "restart",
    "review-verification",
    "success-path",
}
EXPECTED_BLOCKER_IDS = {
    "clean-published-revision",
    "full-ci-gates",
    "human-interactive-client",
    "maintained-project-live-provider",
    "package-metadata-and-compatibility",
    "platform-matrix",
}


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _assert_repo_file(raw_path: str) -> Path:
    relative_path = Path(raw_path)
    assert not relative_path.is_absolute()
    assert ".." not in relative_path.parts
    source_path = ROOT / relative_path
    assert source_path.is_file(), raw_path
    return source_path


def test_alpha_acceptance_manifest_is_closed_source_checked_and_not_tag_ready() -> None:
    manifest = _json(MANIFEST_PATH)

    assert set(manifest) == {
        "candidate",
        "environment",
        "gates",
        "known_limitations",
        "matrix",
        "publication",
        "recorded_on",
        "residual_blockers",
        "schema_version",
        "success_path",
    }
    assert manifest["schema_version"] == "blackcell-alpha-acceptance/v1"
    assert manifest["recorded_on"] == "2026-07-22"

    candidate = manifest["candidate"]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert candidate["status"] == "deterministic-candidate-partial"
    assert candidate["provider_mode"] == "recorded-deterministic"
    assert candidate["project_version"] == project["version"]
    assert candidate["alpha_tag_ready"] is False
    assert candidate["worktree_state"] == "dirty-uncommitted"

    environment = manifest["environment"]
    assert environment["host_os"] == "Linux"
    assert environment["substrate"] == "WSL2"
    assert environment["measurement_scope"] == "this-wsl2-host-only"
    assert environment["bubblewrap_version"] == "0.9.0"

    matrix = manifest["matrix"]
    assert {entry["id"] for entry in matrix} == EXPECTED_MATRIX_IDS
    assert len(matrix) == len(EXPECTED_MATRIX_IDS)

    matrix_tests: list[str] = []
    for entry in matrix:
        assert set(entry) == {"claims", "id", "source_paths", "status", "tests"}
        assert entry["status"] == "passed-deterministic"
        assert entry["claims"]
        assert len(entry["claims"]) == len(set(entry["claims"]))
        assert entry["source_paths"]
        assert len(entry["source_paths"]) == len(set(entry["source_paths"]))
        for source_path in entry["source_paths"]:
            _assert_repo_file(source_path)

        assert entry["tests"]
        assert len(entry["tests"]) == len(set(entry["tests"]))
        for test_node in entry["tests"]:
            source, separator, function = test_node.partition("::")
            assert separator == "::"
            assert "::" not in function
            test_path = _assert_repo_file(source)
            assert re.search(
                rf"^(?:async\s+)?def\s+{re.escape(function)}\s*\(",
                test_path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            matrix_tests.append(test_node)

    behavior_gate = manifest["gates"]["focused_behavior_matrix"]
    command = behavior_gate["command"]
    assert command[:4] == ["uv", "run", "python", "tools/run_pytest.py"]
    command_tests = [argument for argument in command if "::" in argument]
    assert set(command_tests) == set(matrix_tests)
    assert len(command_tests) == len(set(command_tests))
    assert all("::" in argument for argument in command if argument.startswith("tests/"))
    assert command[-2:] == ["-q", "--blackcell-require-all-pass"]
    assert "--cov" not in command
    assert behavior_gate == {
        "command": command,
        "observed_pytest_seconds": 11.72,
        "observed_wall_seconds": 12.17,
        "passed": 8,
        "skipped": 0,
        "status": "passed",
        "xfailed": 0,
    }

    gates = manifest["gates"]
    assert set(gates) == {
        "changed_path_format",
        "changed_path_ruff",
        "ci_full_pytest_coverage",
        "ci_full_type_suite",
        "focused_acceptance_bundle",
        "focused_behavior_matrix",
        "manifest_validation",
        "project_ruff",
    }
    assert gates["changed_path_format"] == {"status": "passed"}
    assert gates["changed_path_ruff"] == {"status": "passed"}
    assert gates["project_ruff"] == {"status": "passed"}
    assert gates["ci_full_pytest_coverage"] == {
        "owner": "GitHub Actions",
        "status": "not-run",
    }
    assert gates["ci_full_type_suite"] == {
        "owner": "GitHub Actions",
        "status": "not-run",
    }
    assert gates["manifest_validation"] == {
        "observed_pytest_seconds": 0.16,
        "status": "passed",
    }
    bundle_gate = gates["focused_acceptance_bundle"]
    assert bundle_gate["command"] == [
        *command[:4],
        (
            "tests/unit/test_alpha_acceptance_manifest.py::"
            "test_alpha_acceptance_manifest_is_closed_source_checked_and_not_tag_ready"
        ),
        *command[4:],
    ]
    assert bundle_gate == {
        "command": bundle_gate["command"],
        "observed_pytest_seconds": 11.55,
        "observed_wall_seconds": 11.99,
        "passed": 9,
        "skipped": 0,
        "status": "passed",
        "xfailed": 0,
    }

    blockers = manifest["residual_blockers"]
    assert {blocker["id"] for blocker in blockers} == EXPECTED_BLOCKER_IDS
    assert len(blockers) == len(EXPECTED_BLOCKER_IDS)
    for blocker in blockers:
        assert set(blocker) == {"blocks_alpha_tag", "id", "status", "unmeasured"}
        assert blocker["blocks_alpha_tag"] is True
        assert blocker["status"] == "open"
        assert blocker["unmeasured"]
        assert len(blocker["unmeasured"]) == len(set(blocker["unmeasured"]))

    assert manifest["publication"] == {
        "alpha_tag_proposed": False,
        "commit_created": False,
        "package_published": False,
        "publication_performed": False,
        "release_created": False,
    }

    success = manifest["success_path"]
    assert success["project_kind"] == "temporary-two-node-git-fixture"
    assert success["browser_resume"] == {
        "after_cursor": 4,
        "last_cursor": 17,
        "transport": "single-use-ticket-websocket",
    }
    provider = success["provider_measurement"]
    assert provider["source"] == "deterministic-recorded-fixture"
    assert provider["live_provider_measurement"] is None
    assert success["test"] in command_tests

    readme = README_PATH.read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    assert "not an alpha tag proposal" in readme
    assert "recorded provider" in readme
    assert "No package, image, tag, release, or deployment" in normalized_readme
