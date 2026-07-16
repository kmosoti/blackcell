from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools import architecture_consolidation_evidence as evidence

ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / evidence.MANIFEST_PATH
DECISION_PATH = ROOT / evidence.DECISION_PATH
SBOM_PATH = ROOT / evidence.SBOM_PATH


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _initialize(repo: Path) -> None:
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "evidence@example.invalid")
    _git(repo, "config", "user.name", "Evidence Test")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _write_dependency_inputs(repo: Path, *, dependency_version: str) -> None:
    (repo / "pyproject.toml").write_text(
        """
[project]
name = "blackcell"
version = "0.2.0"
dependencies = ["dep>=1"]
""".lstrip(),
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text(
        f"""
version = 1

[[package]]
name = "blackcell"
version = "0.2.0"
source = {{ editable = "." }}
dependencies = [{{ name = "dep" }}]

[[package]]
name = "dep"
version = "{dependency_version}"
source = {{ registry = "https://pypi.org/simple" }}
""".lstrip(),
        encoding="utf-8",
    )


def _write_verification_results(path: Path, source_sha: str) -> None:
    commands = [
        {
            "argv": item["argv"],
            "environment": item.get("environment", {}),
            "exit_code": 0,
            "id": item["id"],
            "status": "pass",
            "summary": f"{item['id']} passed in the fixture",
        }
        for item in evidence.EXPECTED_VERIFICATION_COMMANDS
    ]
    path.write_text(
        json.dumps(
            {
                "commands": commands,
                "schema_version": "architecture-consolidation-verification-results/v1",
                "source_sha": source_sha,
            }
        ),
        encoding="utf-8",
    )


def _fixture_program(repo: Path, *, changed_closure: bool) -> tuple[str, str, str, Path]:
    _initialize(repo)
    source = repo / "src/blackcell/example.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """from typing import Protocol


class Reader(Protocol):
    def read(self) -> str: ...


class Resource:
    pass


def build() -> Resource:
    return Resource()
""",
        encoding="utf-8",
    )
    architecture_test = repo / evidence.ARCHITECTURE_TEST_PATH
    architecture_test.parent.mkdir(parents=True)
    architecture_test.write_text(
        "\n\n".join(
            f"def {item['test'].split('::', maxsplit=1)[1]}() -> None:\n    pass"
            for item in evidence.REQUIRED_AC07_RULES
        )
        + "\n",
        encoding="utf-8",
    )
    _write_dependency_inputs(repo, dependency_version="1.0.0")
    implementation_base_sha = _commit(repo, "implementation base")

    (repo / evidence.BLACKCELL_PLAN_PATH).write_text(
        f"architecture_consolidation:\n  implementation_base_sha: {implementation_base_sha}\n",
        encoding="utf-8",
    )
    (repo / evidence.PROGRAM_PLAN_PATH).write_text(
        f"program:\n  implementation_base_sha: {implementation_base_sha}\n",
        encoding="utf-8",
    )
    baseline_sha = _commit(repo, "AC00 baseline")

    if changed_closure:
        _write_dependency_inputs(repo, dependency_version="2.0.0")
    baseline_path = repo / evidence.BASELINE_PATH
    baseline_path.parent.mkdir(parents=True)
    baseline_path.write_text(
        json.dumps({"source": {"base_sha": baseline_sha}}),
        encoding="utf-8",
    )
    source_sha = _commit(repo, "source")
    results_path = repo / "verification-results.json"
    _write_verification_results(results_path, source_sha)
    return implementation_base_sha, baseline_sha, source_sha, results_path


def test_candidate_uses_exact_git_blobs_modes_and_canonical_bytes(tmp_path: Path) -> None:
    _initialize(tmp_path)
    regular = tmp_path / "regular.txt"
    executable = tmp_path / "executable.sh"
    unicode_path = tmp_path / "zeta-λ.txt"
    link = tmp_path / "link"
    regular.write_bytes(b"regular\n")
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o755)
    unicode_path.write_bytes(b"unicode\n")
    os.symlink("regular.txt", link)
    for relative in evidence.EXCLUDED_OUTPUTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"excluded: {relative}\n", encoding="utf-8")
    source_sha = _commit(tmp_path, "candidate")

    candidate = evidence._source_candidate(tmp_path, source_sha)
    materials = candidate["materials"]
    assert isinstance(materials, list)
    paths = [item["path"] for item in materials]
    assert paths == sorted(paths, key=lambda value: value.encode("utf-8"))
    assert set(paths) == {"executable.sh", "link", "regular.txt", "zeta-λ.txt"}
    by_path = {item["path"]: item for item in materials}
    assert by_path["regular.txt"] == {
        "mode": "100644",
        "path": "regular.txt",
        "sha256": _sha256(b"regular\n"),
        "size": 8,
    }
    assert by_path["executable.sh"]["mode"] == "100755"
    assert by_path["link"] == {
        "mode": "120000",
        "path": "link",
        "sha256": _sha256(b"regular.txt"),
        "size": 11,
    }
    canonical = {
        "materials": materials,
        "schema_version": "architecture-consolidation-materials/v1",
    }
    canonical_bytes = (
        json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    assert candidate["candidate_id"] == _sha256(canonical_bytes)
    assert candidate["canonical_document_sha256"] == candidate["candidate_id"]

    regular.write_bytes(b"working tree drift\n")
    assert evidence._source_candidate(tmp_path, source_sha) == candidate


def test_candidate_requires_an_explicit_full_commit_id() -> None:
    with pytest.raises(evidence.ConsolidationEvidenceError, match="forty-character"):
        evidence._source_candidate(ROOT, "HEAD")


def test_advisory_report_has_all_six_threshold_free_measurements() -> None:
    source_sha = _git(ROOT, "rev-parse", "HEAD")
    report = evidence._advisory_report(ROOT, source_sha)

    assert report["schema_version"] == "architecture-consolidation-advisory/v1"
    assert report["advisory_only"] is True
    measurements = report["measurements"]
    assert set(measurements) == {
        "constructor_fan_in",
        "import_breadth",
        "module_size",
        "package_co_change",
        "protocol_breadth",
        "record_similarity",
    }
    assert all(item["advisory_only"] is True for item in measurements.values())
    assert all(item["threshold"] is None for item in measurements.values())
    assert len(measurements["record_similarity"]["observations"]) == 4
    assert measurements["record_similarity"]["unavailable_records"] == []
    assert measurements["package_co_change"]["limitations"]


def test_binary_gate_inventory_includes_every_architecture_test(tmp_path: Path) -> None:
    _base_sha, _baseline_sha, source_sha, _ = _fixture_program(tmp_path, changed_closure=False)
    gate = evidence._architecture_binary_gate(evidence._source_tree(tmp_path, source_sha))
    required = {item["test"].split("::", maxsplit=1)[1] for item in evidence.REQUIRED_AC07_RULES}

    assert gate["path"] == evidence.ARCHITECTURE_TEST_PATH.as_posix()
    assert gate["test_count"] == len(gate["tests"])
    assert required <= set(gate["tests"])


def test_dependency_comparison_distinguishes_unchanged_and_changed_closure(
    tmp_path: Path,
) -> None:
    base_sha, _baseline_sha, source_sha, _ = _fixture_program(tmp_path, changed_closure=True)
    baseline_tree = evidence._source_tree(tmp_path, base_sha)
    source_tree = evidence._source_tree(tmp_path, source_sha)

    unchanged = evidence._dependency_comparison(
        base_sha,
        baseline_tree,
        base_sha,
        baseline_tree,
    )
    changed = evidence._dependency_comparison(
        base_sha,
        baseline_tree,
        source_sha,
        source_tree,
    )
    assert unchanged["changed"] is False
    assert unchanged["sbom"] == {
        "generated": False,
        "path": None,
        "reason": "locked production dependency closure is unchanged",
    }
    assert changed["changed"] is True
    assert changed["sbom"]["generated"] is True
    assert changed["sbom"]["path"] == evidence.SBOM_PATH.as_posix()


@pytest.mark.parametrize("changed_closure", [False, True])
def test_generated_manifest_and_decision_reproduce_from_source_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_closure: bool,
) -> None:
    base_sha, baseline_sha, source_sha, results_path = _fixture_program(
        tmp_path, changed_closure=changed_closure
    )
    monkeypatch.setattr(evidence, "AC00_BASELINE_SHA", baseline_sha)
    monkeypatch.setattr(evidence, "IMPLEMENTATION_BASE_SHA", base_sha)

    generated = evidence._generate(tmp_path, source_sha, results_path)
    verified = evidence._verify(tmp_path)
    manifest = json.loads((tmp_path / evidence.MANIFEST_PATH).read_text(encoding="utf-8"))
    decision = json.loads((tmp_path / evidence.DECISION_PATH).read_text(encoding="utf-8"))

    assert generated["candidate_id"] == verified["candidate_id"]
    assert manifest["program"]["source_sha"] == source_sha
    assert manifest["program"]["base_sha"] == base_sha
    assert manifest["program"]["candidate_id"] == decision["candidate_id"]
    assert manifest["source"]["materials_digest"] == decision["candidate_id"]
    assert manifest["source"]["excluded_outputs"] == [
        path.as_posix() for path in evidence.EXCLUDED_OUTPUTS
    ]
    assert decision["evidence"]["manifest"] == evidence.MANIFEST_PATH.as_posix()
    assert decision["advisory"]["thresholds"] is None
    assert manifest["historical_runtime_v1"]["verification_claim_reused"] is False
    assert manifest["historical_runtime_v1"]["changed"] is False
    binary_gate = manifest["architecture_fitness"]["binary_gate"]
    assert binary_gate["test_count"] == len(binary_gate["tests"])
    assert manifest["architecture_fitness"]["required_ac07_rules"]
    assert manifest["dependency_closure"]["changed"] is changed_closure
    assert (tmp_path / evidence.SBOM_PATH).exists() is changed_closure
    assert generated["sbom"] == (evidence.SBOM_PATH.as_posix() if changed_closure else None)


def test_current_candidate_allows_only_excluded_evidence_after_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_sha, baseline_sha, source_sha, results_path = _fixture_program(
        tmp_path, changed_closure=False
    )
    monkeypatch.setattr(evidence, "AC00_BASELINE_SHA", baseline_sha)
    monkeypatch.setattr(evidence, "IMPLEMENTATION_BASE_SHA", base_sha)
    generated = evidence._generate(tmp_path, source_sha, results_path)
    results_path.unlink()
    evidence_sha = _commit(tmp_path, "excluded evidence outputs")

    verified = evidence._verify_current(tmp_path)

    assert verified == {
        "candidate_id": generated["candidate_id"],
        "head_sha": evidence_sha,
        "material_count": generated["material_count"],
        "schema_version": "architecture-consolidation-evidence-result/v1",
        "source_sha": source_sha,
        "status": "pass",
    }

    (tmp_path / "README.md").write_text("post-source drift\n", encoding="utf-8")
    _commit(tmp_path, "non-evidence source drift")
    with pytest.raises(
        evidence.ConsolidationEvidenceError,
        match="non-evidence changes after the recorded source commit",
    ):
        evidence._verify_current(tmp_path)


def test_replay_verification_requires_source_preserving_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_sha, baseline_sha, source_sha, results_path = _fixture_program(
        tmp_path, changed_closure=False
    )
    monkeypatch.setattr(evidence, "AC00_BASELINE_SHA", baseline_sha)
    monkeypatch.setattr(evidence, "IMPLEMENTATION_BASE_SHA", base_sha)
    evidence._generate(tmp_path, source_sha, results_path)
    results_path.unlink()
    _commit(tmp_path, "evidence")
    _git(tmp_path, "checkout", "--orphan", "rewritten")
    _commit(tmp_path, "rewritten history")

    with pytest.raises(
        evidence.ConsolidationEvidenceError,
        match="recorded source commit is not an ancestor of HEAD",
    ):
        evidence._verify(tmp_path)


def test_verification_results_fail_closed_on_command_drift(tmp_path: Path) -> None:
    source_sha = "a" * 40
    path = tmp_path / "results.json"
    _write_verification_results(path, source_sha)
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    value["commands"][0]["argv"].append("--unexpected")
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(evidence.ConsolidationEvidenceError, match="argv drift"):
        evidence._verification_results(path, source_sha)


@pytest.mark.parametrize(
    "invalid_kind",
    ["duplicate", "non-object", "failing", "extra-key"],
)
def test_verification_results_reject_duplicate_malformed_or_failing_records(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    source_sha = "a" * 40
    path = tmp_path / "results.json"
    _write_verification_results(path, source_sha)
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if invalid_kind == "duplicate":
        value["commands"][-1] = value["commands"][0]
    elif invalid_kind == "non-object":
        value["commands"][-1] = "not-a-command"
    elif invalid_kind == "failing":
        value["commands"][-1]["status"] = "fail"
        value["commands"][-1]["exit_code"] = 1
    else:
        value["unexpected"] = True
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(evidence.ConsolidationEvidenceError):
        evidence._verification_results(path, source_sha)


def test_generation_rejects_source_plan_baseline_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_sha, baseline_sha, source_sha, _ = _fixture_program(tmp_path, changed_closure=False)
    monkeypatch.setattr(evidence, "AC00_BASELINE_SHA", baseline_sha)
    monkeypatch.setattr(evidence, "IMPLEMENTATION_BASE_SHA", base_sha)
    for relative, root_key in (
        (evidence.BLACKCELL_PLAN_PATH, "architecture_consolidation"),
        (evidence.PROGRAM_PLAN_PATH, "program"),
    ):
        (tmp_path / relative).write_text(
            f"{root_key}:\n  implementation_base_sha: {source_sha}\n",
            encoding="utf-8",
        )
    tampered_sha = _commit(tmp_path, "rebind dependency baseline")
    results_path = tmp_path / "tampered-results.json"
    _write_verification_results(results_path, tampered_sha)

    with pytest.raises(
        evidence.ConsolidationEvidenceError,
        match="differs from the ratified program baseline",
    ):
        evidence._generate(tmp_path, tampered_sha, results_path)


def test_generation_rejects_ac00_source_baseline_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_sha, baseline_sha, source_sha, _ = _fixture_program(tmp_path, changed_closure=False)
    monkeypatch.setattr(evidence, "AC00_BASELINE_SHA", baseline_sha)
    monkeypatch.setattr(evidence, "IMPLEMENTATION_BASE_SHA", base_sha)
    (tmp_path / evidence.BASELINE_PATH).write_text(
        json.dumps({"source": {"base_sha": source_sha}}),
        encoding="utf-8",
    )
    tampered_sha = _commit(tmp_path, "rebind AC00 source baseline")
    results_path = tmp_path / "tampered-results.json"
    _write_verification_results(results_path, tampered_sha)

    with pytest.raises(
        evidence.ConsolidationEvidenceError,
        match="differs from the ratified source baseline",
    ):
        evidence._generate(tmp_path, tampered_sha, results_path)


def test_generation_rejects_historical_runtime_v1_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_sha, baseline_sha, _source_sha, _ = _fixture_program(tmp_path, changed_closure=False)
    monkeypatch.setattr(evidence, "AC00_BASELINE_SHA", baseline_sha)
    monkeypatch.setattr(evidence, "IMPLEMENTATION_BASE_SHA", base_sha)
    historical = tmp_path / "release/runtime-v1/new-evidence.json"
    historical.parent.mkdir(parents=True)
    historical.write_text("{}\n", encoding="utf-8")
    tampered_sha = _commit(tmp_path, "change historical evidence")
    results_path = tmp_path / "tampered-results.json"
    _write_verification_results(results_path, tampered_sha)

    with pytest.raises(
        evidence.ConsolidationEvidenceError,
        match="historical runtime-v1 evidence differs",
    ):
        evidence._generate(tmp_path, tampered_sha, results_path)


def test_checked_in_final_evidence_reproduces_and_binds_its_ancestral_source() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    decision = json.loads(DECISION_PATH.read_text(encoding="utf-8"))
    source_sha = manifest["program"]["source_sha"]
    replay = evidence._verify(ROOT)
    head_sha = evidence._require_source_ancestor_of_head(ROOT, source_sha)
    candidate = evidence._source_candidate(ROOT, source_sha)

    assert head_sha == _git(ROOT, "rev-parse", "HEAD")
    assert replay["status"] == "pass"
    assert replay["candidate_id"] == candidate["candidate_id"]
    assert candidate["candidate_id"] == manifest["program"]["candidate_id"]
    assert candidate["candidate_id"] == decision["candidate_id"]
    assert candidate["materials"] == manifest["source"]["materials"]
    assert candidate["material_count"] == manifest["source"]["material_count"]
    assert candidate["excluded_outputs"] == manifest["source"]["excluded_outputs"]
    assert SBOM_PATH.exists() is manifest["dependency_closure"]["changed"]
